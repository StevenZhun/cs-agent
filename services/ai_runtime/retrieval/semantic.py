"""在线语义检索：把用户 Query 送入已有的 Embedding 模型和 Milvus 索引。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import Lock
from urllib.parse import urlparse

from pymilvus import MilvusClient

from services.content.semantic_index import HNSW_EF_SEARCH, collection_names
from shared.contracts.health import DependencyState
from shared.semantic_embedding import DEFAULT_EMBEDDING_PROFILE, QwenEmbeddingModel
from shared.web.service_app import TcpDependency, probe_tcp

MILVUS_RPC_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class SemanticThresholds:
    """由运行环境统一提供的语义检索安全门槛。"""

    intent_min_score: float
    intent_margin: float
    faq_min_score: float
    faq_margin: float


class SemanticSearch:
    """复用一个模型和 Milvus 客户端，完成当前 Run 的两级语义检索。"""

    def __init__(
        self,
        *,
        milvus_uri: str,
        index_version: str,
        thresholds: SemanticThresholds,
    ) -> None:
        """保存连接配置；服务启动时由 warmup() 预先加载模型。"""

        self._milvus_uri = milvus_uri
        self._index_version = index_version
        self._names = collection_names(index_version)
        self._thresholds = thresholds
        self._model: QwenEmbeddingModel | None = None
        self._client: MilvusClient | None = None
        # 模型加载只允许一个协程执行，避免多个首批请求重复加载大模型权重。
        self._model_load_lock = asyncio.Lock()
        # MilvusClient 是同步对象，在线程池中创建时需要保护一次性初始化。
        self._client_create_lock = Lock()

    async def warmup(self) -> None:
        """启动时加载模型并核对两类在线索引，缺一项就拒绝对外服务。"""

        # 先做便宜的远端依赖检查。Milvus 已不可用时无需再占用数秒加载模型，
        # 也避免 reload 循环反复读取同一份大权重后才报告真正故障。
        await self.probe_ready()
        await self._ensure_model_loaded()

    async def probe_ready(self) -> None:
        """确认意图与 FAQ Collection 都可描述，而不是只检查 Milvus 端口。"""

        parsed = urlparse(self._milvus_uri)
        if parsed.hostname is None:
            raise RuntimeError("Milvus URI must contain a hostname")
        dependency = TcpDependency(
            name="milvus",
            host=parsed.hostname,
            port=parsed.port or 19530,
            timeout_seconds=0.5,
        )
        if (await probe_tcp(dependency)).state is not DependencyState.UP:
            raise RuntimeError("Milvus is unavailable during semantic warmup")
        await asyncio.wait_for(
            asyncio.to_thread(self._describe_collections),
            timeout=MILVUS_RPC_TIMEOUT_SECONDS * 3,
        )

    async def close(self) -> None:
        """释放 Milvus 连接；模型由 Python 进程退出时回收。"""

        if self._client is not None:
            await asyncio.to_thread(self._client.close)
            self._client = None

    async def search_intents(self, query: str) -> list[dict[str, object]]:
        """用一次 Query 向量检索意图语料，并按 intent_id 取最高分。"""

        vector = await self.encode_query(query)
        return await asyncio.to_thread(self._search_intents, vector)

    async def search_intents_vector(self, vector: list[float]) -> list[dict[str, object]]:
        """使用已生成的 Query 向量检索全局意图。"""

        return await asyncio.to_thread(self._search_intents, vector)

    async def search_faq(self, query: str, intent_id: str) -> list[dict[str, object]]:
        """复用本轮 Query 向量，在已确定的 FAQ 意图范围内检索条目。"""

        vector = await self.encode_query(query)
        return await asyncio.to_thread(self._search_faq, vector, intent_id)

    async def search_faq_vector(
        self, vector: list[float], intent_id: str
    ) -> list[dict[str, object]]:
        """使用已经生成的 Query 向量检索 FAQ，避免同一轮重复编码。"""

        return await asyncio.to_thread(self._search_faq, vector, intent_id)

    async def encode_query(self, query: str, *, instruction: str | None = None) -> list[float]:
        """编码一次 Query；不同检索任务必须显式使用各自的任务指令。"""

        await self._ensure_model_loaded()
        return await asyncio.to_thread(
            self._encode_sync,
            query,
            instruction or DEFAULT_EMBEDDING_PROFILE.shared_query_instruction,
        )

    @property
    def thresholds(self) -> SemanticThresholds:
        """返回当前运行时采用的门槛，便于写入决策追踪。"""

        return self._thresholds

    @property
    def index_version(self) -> str:
        """返回本实例实际读取的不可变语义索引版本。"""

        return self._index_version

    def _encode_sync(self, query: str, instruction: str) -> list[float]:
        """在线程池中编码 Query，避免同步模型调用卡住事件循环。"""

        if self._model is None:
            raise RuntimeError("Embedding model has not been warmed up")
        return self._model.encode_queries([query], instruction=instruction)[0]

    async def _ensure_model_loaded(self) -> None:
        """只允许一个协程负责加载模型，其他请求等待同一次加载完成。"""

        if self._model is not None:
            return
        async with self._model_load_lock:
            if self._model is None:
                await asyncio.to_thread(self._load_model)

    def _load_model(self) -> None:
        """在线程池中完成同步模型初始化，模型文件从本地缓存读取。"""

        self._model = QwenEmbeddingModel(DEFAULT_EMBEDDING_PROFILE)

    def _client_or_create(self) -> MilvusClient:
        """按进程复用 Milvus 客户端，不为每次查询重新建立连接。"""

        if self._client is None:
            with self._client_create_lock:
                if self._client is None:
                    self._client = MilvusClient(
                        uri=self._milvus_uri,
                        timeout=MILVUS_RPC_TIMEOUT_SECONDS,
                    )
        return self._client

    def _describe_collections(self) -> None:
        """用在线检索实际依赖的两个集合完成协议级健康检查。"""

        client = self._client_or_create()
        client.describe_collection(
            collection_name=self._names.intent,
            timeout=MILVUS_RPC_TIMEOUT_SECONDS,
        )
        client.describe_collection(
            collection_name=self._names.faq,
            timeout=MILVUS_RPC_TIMEOUT_SECONDS,
        )

    def _search_intents(self, vector: list[float]) -> list[dict[str, object]]:
        """检索意图语料，并保留最高分、命中语料和来源编号。"""

        hits = self._client_or_create().search(
            collection_name=self._names.intent,
            data=[vector],
            limit=15,
            search_params={"params": {"ef": HNSW_EF_SEARCH}},
            output_fields=["intent_id", "text", "record_id"],
            timeout=MILVUS_RPC_TIMEOUT_SECONDS,
        )[0]
        grouped: dict[str, dict[str, object]] = {}
        for hit in hits:
            entity = hit["entity"]
            intent_id = str(entity["intent_id"])
            score = float(hit["distance"])
            current = grouped.get(intent_id)
            if current is None or score > float(current["score"]):
                grouped[intent_id] = {
                    "intent_id": intent_id,
                    "score": score,
                    "source_ref": str(entity.get("record_id", "")),
                    "matched_text": str(entity.get("text", "")),
                }
        return sorted(grouped.values(), key=lambda item: float(item["score"]), reverse=True)

    def _search_faq(self, vector: list[float], intent_id: str) -> list[dict[str, object]]:
        """只在已命中的全局意图内检索 FAQ，并按 faq_item_id 聚合。"""

        safe_intent_id = intent_id.replace('"', '\\"')
        hits = self._client_or_create().search(
            collection_name=self._names.faq,
            data=[vector],
            filter=f'intent_id == "{safe_intent_id}"',
            limit=20,
            search_params={"params": {"ef": HNSW_EF_SEARCH}},
            output_fields=[
                "faq_item_id",
                "item_version",
                "faq_group_id",
                "intent_id",
                "question",
                "record_id",
            ],
            timeout=MILVUS_RPC_TIMEOUT_SECONDS,
        )[0]
        grouped: dict[str, dict[str, object]] = {}
        for hit in hits:
            entity = hit["entity"]
            item_id = str(entity["faq_item_id"])
            score = float(hit["distance"])
            current = grouped.get(item_id)
            if current is None or score > float(current["score"]):
                grouped[item_id] = {
                    "faq_item_id": item_id,
                    "faq_version": str(entity["item_version"]),
                    "faq_group_id": str(entity["faq_group_id"]),
                    "intent_id": str(entity["intent_id"]),
                    "score": score,
                    "winning_variant_id": str(entity.get("record_id", "")),
                    "matched_question": str(entity.get("question", "")),
                }
        return sorted(grouped.values(), key=lambda item: float(item["score"]), reverse=True)
