"""本地 HTTP 服务共用的环境配置。"""

from __future__ import annotations

from urllib.parse import urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.contracts.enums import DecisionProfile
from shared.web.service_app import TcpDependency


class ServiceSettings(BaseSettings):
    """从根目录 `.env` 读取服务地址、运行参数和内部凭据。

    所有字段都必须由运行环境明确提供。这样本机进程、Alembic 和 Docker
    容器共用同一套配置，不会因为代码默认值不同而连接到错误的服务。
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str
    service_version: str
    database_url: str
    redis_url: str
    milvus_uri: str
    semantic_enabled: bool
    sop_llm_enabled: bool
    semantic_index_version: str
    semantic_intent_min_score: float = Field(ge=0, le=1)
    semantic_intent_margin: float = Field(ge=0, le=1)
    semantic_faq_min_score: float = Field(ge=0, le=1)
    semantic_faq_margin: float = Field(ge=0, le=1)
    faq_rerank_min_score: float = Field(ge=0, le=1)
    faq_rerank_min_margin: float = Field(ge=0, le=1)
    # 配置按通用语言模型能力命名；具体供应商由 provider、base_url 和 model 决定。
    llm_provider: str = Field(min_length=1)
    llm_base_url: str
    llm_api_key: SecretStr
    llm_model: str
    llm_timeout_seconds: float = Field(ge=1, le=60)
    llm_routing_timeout_seconds: float = Field(ge=1, le=60)
    llm_routing_fast_retry_timeout_seconds: float = Field(ge=1, le=30)
    llm_context_window_tokens: int = Field(ge=1)
    context_effective_window_tokens: int = Field(ge=4096)
    context_compression_trigger_ratio: float = Field(gt=0, lt=1)
    context_dynamic_input_reserve_tokens: int = Field(ge=0)
    context_output_reserve_tokens: int = Field(ge=1)
    context_recent_turn_budget_tokens: int = Field(ge=1)
    context_summary_budget_tokens: int = Field(ge=1)
    context_rebaseline_after_compressions: int = Field(ge=2, le=32)
    context_summary_source_ref_limit: int = Field(ge=8, le=128)
    runtime_artifact_retention_days: int = Field(ge=1, le=365)
    runtime_artifact_cache_size: int = Field(ge=16, le=2048)
    tool_artifact_externalize_bytes: int = Field(ge=1024, le=1048576)
    customer_data_url: str
    business_tool_timeout_seconds: float = Field(ge=1, le=30)
    business_timezone: str
    ai_runtime_url: str
    ai_runtime_internal_token: SecretStr
    ai_runtime_execute_timeout_seconds: float = Field(ge=10, le=300)
    ai_runtime_inspect_timeout_seconds: float = Field(ge=1, le=30)
    active_decision_profile: DecisionProfile
    decision_timeout_ms: int = Field(ge=1, le=10000)
    typesafe_api_key: SecretStr | None = None
    typesafe_base_url: str = "https://api.typesafe.ai"
    typesafe_model: str = "jev-1.14.0"
    typesafe_timeout_seconds: float = Field(default=0.8, gt=0, le=30)
    # 该开关只控制在线准入和检索；关闭时仍可独立构建或重建索引。
    rag_enabled: bool = False
    # 草稿生成独立于检索开关；关闭时不允许客户进入等待人工审核的状态。
    rag_generation_enabled: bool = False
    rag_dense_top_k: int = Field(default=20, ge=1, le=50)
    rag_bm25_top_k: int = Field(default=20, ge=1, le=50)
    rag_rerank_top_n: int = Field(default=5, ge=1, le=20)
    rag_min_rerank_score: float = Field(default=0.35, ge=0, le=1)
    rag_min_rerank_margin: float = Field(default=0.05, ge=0, le=1)
    rag_review_ttl_minutes: int = Field(ge=5, le=1440)
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    dashscope_api_key: SecretStr | None = None
    dashscope_rerank_model: str = "qwen3-rerank"
    dashscope_timeout_seconds: float = Field(default=8, ge=1, le=60)
    quality_async_enabled: bool
    quality_shadow_enabled: bool
    quality_poll_interval_seconds: float = Field(ge=0.1, le=60)
    quality_max_attempts: int = Field(ge=1, le=10)

    @model_validator(mode="after")
    def validate_context_budgets(self) -> ServiceSettings:
        """保证一次压缩后的摘要、近期 Turn 和预留空间能回到触发线以内。"""

        if self.context_effective_window_tokens > self.llm_context_window_tokens:
            raise ValueError("context effective window cannot exceed the model context window")
        threshold = int(
            self.context_effective_window_tokens * self.context_compression_trigger_ratio
        )
        compressed_request = (
            self.context_dynamic_input_reserve_tokens
            + self.context_output_reserve_tokens
            + self.context_recent_turn_budget_tokens
            + self.context_summary_budget_tokens
        )
        if compressed_request > threshold:
            raise ValueError("context budgets must fit below the compression threshold")
        return self

    def postgres_dependency(self) -> TcpDependency:
        """把 PostgreSQL 地址转换成健康检查需要的主机和端口。"""

        return self._dependency("postgresql", self.database_url, 5432)

    def redis_dependency(self) -> TcpDependency:
        """把 Redis 地址转换成健康检查需要的主机和端口。"""

        return self._dependency("redis", self.redis_url, 6379)

    def milvus_dependency(self) -> TcpDependency:
        """把 Milvus 地址转换成健康检查需要的主机和端口。"""

        return self._dependency("milvus", self.milvus_uri, 19530)

    @staticmethod
    def _dependency(name: str, value: str, default_port: int) -> TcpDependency:
        """解析一个服务地址；地址缺少主机名时立即报错。"""

        parsed = urlparse(value)
        if parsed.hostname is None:
            raise ValueError(f"{name} endpoint must contain a hostname")
        return TcpDependency(name=name, host=parsed.hostname, port=parsed.port or default_port)
