"""LangGraph 官方 PostgreSQL 保存器及其 `langgraph` schema 边界。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph.state import CompiledStateGraph
from psycopg import AsyncConnection
from redis.asyncio import Redis
from sqlalchemy.engine import make_url

from services.ai_runtime.decision.jev import (
    JevIntentDecisionAdapter,
    create_jev_intent_adapter,
)
from services.ai_runtime.generation.grounded import GroundedAnswerGenerator
from services.ai_runtime.graph import build_graph
from services.ai_runtime.intent.classifier import LlmIntentClassifier
from services.ai_runtime.persistence.artifacts import RuntimeArtifactRepository
from services.ai_runtime.persistence.context import ContextSummaryRepository
from services.ai_runtime.persistence.trace import TraceSpanRepository
from services.ai_runtime.query.history import HistoryRecallCoordinator
from services.ai_runtime.query.message_state import ContextBudgetPolicy
from services.ai_runtime.query.rewrite import LlmQueryRewriter
from services.ai_runtime.query.summary import ContextSummarizer
from services.ai_runtime.retrieval.rag import RagRetriever, RagRetrieverConfig
from services.ai_runtime.retrieval.reranking import DashScopeTextReranker
from services.ai_runtime.retrieval.semantic import SemanticSearch, SemanticThresholds
from services.ai_runtime.sop.business import SopBusinessClient
from shared.web.settings import ServiceSettings

# Framework-owned tables from the locked langgraph-checkpoint-postgres version.
# When the dependency changes its schema, review the new columns before running setup.
CHECKPOINT_COMMENTS: dict[str, tuple[str, dict[str, str]]] = {
    "checkpoint_migrations": (
        "LangGraph checkpointer 的内部迁移版本；由框架 setup() 管理。",
        {"v": "框架 checkpoint 表结构迁移序号；不得手工修改。"},
    ),
    "checkpoints": (
        "LangGraph 线程的状态快照；不是完整原始消息历史。",
        {
            "thread_id": "恢复状态使用的 LangGraph 线程 ID。",
            "checkpoint_ns": "checkpoint 命名空间；空字符串为默认命名空间。",
            "checkpoint_id": "单个状态快照的唯一 ID。",
            "parent_checkpoint_id": "上一个状态快照 ID；起始快照可为空。",
            "type": "框架内部 checkpoint 序列化类型标记。",
            "checkpoint": "框架维护的状态快照元信息 JSON。",
            "metadata": "框架维护的执行来源、步骤等元数据 JSON。",
        },
    ),
    "checkpoint_blobs": (
        "LangGraph 各状态通道的序列化值；由 checkpointer 管理。",
        {
            "thread_id": "状态值所属 LangGraph 线程 ID。",
            "checkpoint_ns": "状态值所属 checkpoint 命名空间。",
            "channel": "Graph State 的通道名称。",
            "version": "该通道值的框架版本标识。",
            "type": "通道值的序列化类型标记。",
            "blob": "通道值的序列化字节；框架可用空值表示特殊状态。",
        },
    ),
    "checkpoint_writes": (
        "LangGraph 节点执行中的待提交写入，用于失败或中断后的恢复。",
        {
            "thread_id": "待写入所属 LangGraph 线程 ID。",
            "checkpoint_ns": "待写入所属 checkpoint 命名空间。",
            "checkpoint_id": "该写入关联的状态快照 ID。",
            "task_id": "产生写入的图任务 ID。",
            "idx": "同一图任务内写入的序号。",
            "channel": "写入目标状态通道名称。",
            "type": "待写入值的序列化类型标记。",
            "blob": "待写入值的序列化字节。",
            "task_path": "图任务路径；用于区分嵌套执行路径。",
        },
    ),
}


@dataclass(frozen=True)
class GraphRuntime:
    graph: CompiledStateGraph
    checkpointer: AsyncPostgresSaver
    semantic_search: SemanticSearch | None = None
    classifier: LlmIntentClassifier | None = None
    rag_retriever: RagRetriever | None = None
    artifact_repository: RuntimeArtifactRepository | None = None
    trace_repository: TraceSpanRepository | None = None
    jev_intent_adapter: JevIntentDecisionAdapter | None = None


def psycopg_url(database_url: str) -> str:
    """把应用使用的 SQLAlchemy 地址转换成 psycopg 连接地址。"""

    url = make_url(database_url)
    if url.drivername != "postgresql+psycopg":
        raise ValueError("AI Runtime requires a postgresql+psycopg DATABASE_URL")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


async def annotate_checkpoint_tables(connection: AsyncConnection) -> None:
    """先核对框架表结构，再给 checkpoint 表和字段补充中文注释。"""

    cursor = await connection.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'langgraph' ORDER BY table_name, ordinal_position"
    )
    actual: dict[str, set[str]] = defaultdict(set)
    for row in await cursor.fetchall():
        actual[row["table_name"]].add(row["column_name"])
    expected = {table: set(columns) for table, (_, columns) in CHECKPOINT_COMMENTS.items()}
    if actual != expected:
        raise RuntimeError("LangGraph checkpoint schema changed; review its column comments")

    for table, (table_comment, columns) in CHECKPOINT_COMMENTS.items():
        await connection.execute(f"COMMENT ON TABLE langgraph.{table} IS '{table_comment}'")
        for column, comment in columns.items():
            escaped = comment.replace("'", "''")
            await connection.execute(f"COMMENT ON COLUMN langgraph.{table}.{column} IS '{escaped}'")


@asynccontextmanager
async def open_graph_runtime(
    settings: ServiceSettings,
) -> AsyncIterator[GraphRuntime]:
    """使用统一 Settings 初始化图、检查点、Redis、Milvus 和分类模型。"""

    serializer = JsonPlusSerializer(allowed_msgpack_modules=[])
    redis = Redis.from_url(settings.redis_url)
    # Runtime 的内容目录、SOP 和 RAG 都依赖 Redis；启动时不可静默降级。
    await redis.ping()
    semantic_search = (
        SemanticSearch(
            milvus_uri=settings.milvus_uri,
            index_version=settings.semantic_index_version,
            thresholds=SemanticThresholds(
                intent_min_score=settings.semantic_intent_min_score,
                intent_margin=settings.semantic_intent_margin,
                faq_min_score=settings.semantic_faq_min_score,
                faq_margin=settings.semantic_faq_margin,
            ),
        )
        if settings.semantic_enabled
        else None
    )
    classifier = LlmIntentClassifier(
        provider=settings.llm_provider,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key.get_secret_value(),
        model=settings.llm_model,
        timeout_seconds=settings.llm_routing_timeout_seconds,
        fast_retry_timeout_seconds=settings.llm_routing_fast_retry_timeout_seconds,
        context_window_tokens=settings.llm_context_window_tokens,
    )
    jev_intent_adapter = create_jev_intent_adapter(
        api_key=(
            settings.typesafe_api_key.get_secret_value()
            if settings.typesafe_api_key is not None
            else None
        ),
        base_url=settings.typesafe_base_url,
        model=settings.typesafe_model,
        timeout_seconds=settings.typesafe_timeout_seconds,
    )
    query_rewriter = LlmQueryRewriter(
        provider=settings.llm_provider,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key.get_secret_value(),
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        context_window_tokens=settings.llm_context_window_tokens,
    )
    context_summarizer = ContextSummarizer(
        provider=settings.llm_provider,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key.get_secret_value(),
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        context_window_tokens=settings.llm_context_window_tokens,
    )
    history_recall = HistoryRecallCoordinator(
        provider=settings.llm_provider,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key.get_secret_value(),
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        context_window_tokens=settings.llm_context_window_tokens,
        database_dsn=psycopg_url(settings.database_url),
    )
    context_budget_policy = ContextBudgetPolicy(
        effective_window_tokens=min(
            settings.context_effective_window_tokens,
            settings.llm_context_window_tokens,
        ),
        compression_trigger_ratio=settings.context_compression_trigger_ratio,
        dynamic_input_reserve_tokens=settings.context_dynamic_input_reserve_tokens,
        output_reserve_tokens=settings.context_output_reserve_tokens,
        recent_turn_budget_tokens=settings.context_recent_turn_budget_tokens,
        summary_budget_tokens=settings.context_summary_budget_tokens,
        rebaseline_after_compressions=settings.context_rebaseline_after_compressions,
        summary_source_ref_limit=settings.context_summary_source_ref_limit,
    )
    context_summary_repository = ContextSummaryRepository(psycopg_url(settings.database_url))
    artifact_repository = RuntimeArtifactRepository(
        psycopg_url(settings.database_url),
        retention_days=settings.runtime_artifact_retention_days,
        cache_size=settings.runtime_artifact_cache_size,
    )
    trace_repository = TraceSpanRepository(psycopg_url(settings.database_url))
    dashscope_api_key = (
        settings.dashscope_api_key.get_secret_value() if settings.dashscope_api_key else None
    )
    text_reranker = DashScopeTextReranker(
        base_url=settings.dashscope_base_url,
        api_key=dashscope_api_key,
        model=settings.dashscope_rerank_model,
        timeout_seconds=settings.dashscope_timeout_seconds,
    )
    rag_retriever = (
        RagRetriever(
            milvus_uri=settings.milvus_uri,
            redis=redis,
            database_url=settings.database_url,
            semantic_search=semantic_search,
            config=RagRetrieverConfig(
                dense_top_k=settings.rag_dense_top_k,
                bm25_top_k=settings.rag_bm25_top_k,
                rerank_top_n=settings.rag_rerank_top_n,
                min_rerank_score=settings.rag_min_rerank_score,
                min_rerank_margin=settings.rag_min_rerank_margin,
                dashscope_base_url=settings.dashscope_base_url,
                dashscope_api_key=dashscope_api_key,
                dashscope_rerank_model=settings.dashscope_rerank_model,
                dashscope_timeout_seconds=settings.dashscope_timeout_seconds,
            ),
            reranker=text_reranker,
        )
        if settings.rag_enabled
        else None
    )
    grounded_generator = (
        GroundedAnswerGenerator(
            provider=settings.llm_provider,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key.get_secret_value(),
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
            context_window_tokens=settings.llm_context_window_tokens,
        )
        if settings.rag_enabled and settings.rag_generation_enabled
        else None
    )
    async with AsyncPostgresSaver.from_conn_string(
        psycopg_url(settings.database_url), serde=serializer
    ) as saver:
        await saver.conn.execute("SET search_path TO langgraph")
        await saver.setup()
        await annotate_checkpoint_tables(saver.conn)
        try:
            if semantic_search is not None:
                # 语义能力启用时，健康检查通过前就完成模型初始化。
                await semantic_search.warmup()
            if rag_retriever is not None:
                await rag_retriever.warmup()
            yield GraphRuntime(
                graph=build_graph(
                    human_review_enabled=True,
                    checkpointer=saver,
                    active_profile=settings.active_decision_profile,
                    decision_timeout_ms=settings.decision_timeout_ms,
                    redis=redis,
                    semantic_search=semantic_search,
                    classifier=classifier,
                    jev_intent_adapter=jev_intent_adapter,
                    query_rewriter=query_rewriter,
                    history_recall=history_recall,
                    context_summarizer=context_summarizer,
                    context_budget_policy=context_budget_policy,
                    context_summary_repository=context_summary_repository,
                    artifact_repository=artifact_repository,
                    tool_artifact_externalize_bytes=(settings.tool_artifact_externalize_bytes),
                    llm_settings=settings,
                    business_client=SopBusinessClient(
                        settings.customer_data_url,
                        settings.ai_runtime_internal_token.get_secret_value(),
                        settings.business_tool_timeout_seconds,
                    ),
                    business_timezone=settings.business_timezone,
                    rag_retriever=rag_retriever,
                    rag_enabled=settings.rag_enabled,
                    grounded_generator=grounded_generator,
                    faq_reranker=text_reranker,
                    faq_rerank_min_score=settings.faq_rerank_min_score,
                    faq_rerank_min_margin=settings.faq_rerank_min_margin,
                ),
                checkpointer=saver,
                semantic_search=semantic_search,
                classifier=classifier,
                rag_retriever=rag_retriever,
                artifact_repository=artifact_repository,
                trace_repository=trace_repository,
                jev_intent_adapter=jev_intent_adapter,
            )
        finally:
            if jev_intent_adapter is not None:
                await jev_intent_adapter.aclose()
            if rag_retriever is not None:
                await rag_retriever.close()
            if semantic_search is not None:
                await semantic_search.close()
            if redis is not None:
                await redis.aclose()
