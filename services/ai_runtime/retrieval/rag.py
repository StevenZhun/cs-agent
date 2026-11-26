"""RAG 双路召回、RRF 融合和云端精排。

本模块只负责产生可追溯的检索结果，不生成客户答案，也不改变图的路由。
只有后续准入节点确认属于开放知识问题后，才允许调用这里的检索器。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from time import perf_counter
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx
from pymilvus import MilvusClient, MilvusException
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from services.ai_runtime.retrieval.reranking import DashScopeTextReranker
from services.ai_runtime.retrieval.semantic import SemanticSearch
from services.content.rag.publication import load_rag_release
from shared.contracts.enums import RetrievalAssessmentStatus, RetrievalRecommendedAction
from shared.contracts.rag import RagChunkMetadata, RagDocumentBundle
from shared.contracts.retrieval import (
    RetrievalAssessment,
    RetrievalDocument,
    RetrievalFilters,
    RetrievalResult,
)
from shared.semantic_embedding import DEFAULT_EMBEDDING_PROFILE, QwenEmbeddingModel

RRF_K = 60
RAG_EF_SEARCH = 64
MILVUS_RPC_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class RagRetrieverConfig:
    """可通过 Settings 调整的检索和精排参数。"""

    dense_top_k: int = 20
    bm25_top_k: int = 20
    rerank_top_n: int = 5
    min_rerank_score: float = 0.35
    min_rerank_margin: float = 0.05
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    dashscope_api_key: str | None = None
    dashscope_rerank_model: str = "qwen3-rerank"
    dashscope_timeout_seconds: float = 8


@dataclass
class _Candidate:
    """Milvus 返回的子块候选；父文档回捞前保留双路分数。"""

    chunk_id: str
    text: str
    metadata: RagChunkMetadata
    dense_score: float | None = None
    bm25_score: float | None = None
    fused_score: float = 0.0
    fused_rank: int = 0
    rerank_score: float | None = None
    parent_content: str | None = None


@dataclass(frozen=True)
class _CandidatePaths:
    """保留两路原始排序和 RRF 排序，供诊断比较检索路径。"""

    dense: tuple[_Candidate, ...]
    bm25: tuple[_Candidate, ...]
    fused: tuple[_Candidate, ...]


class RagRetriever:
    """在一个已发布 RAG 快照上执行双路检索和有界精排。"""

    def __init__(
        self,
        *,
        milvus_uri: str,
        redis,
        database_url: str | None = None,
        semantic_search: SemanticSearch | None = None,
        config: RagRetrieverConfig | None = None,
        reranker: DashScopeTextReranker | None = None,
        release_snapshot: dict[str, object] | None = None,
    ) -> None:
        """保存连接和模型依赖；不在构造函数中访问网络或加载模型。"""

        self._milvus_uri = milvus_uri
        self._redis = redis
        self._database_url = database_url
        self._semantic_search = semantic_search
        self._config = config or RagRetrieverConfig()
        self._reranker = reranker or DashScopeTextReranker(
            base_url=self._config.dashscope_base_url,
            api_key=self._config.dashscope_api_key,
            model=self._config.dashscope_rerank_model,
            timeout_seconds=self._config.dashscope_timeout_seconds,
        )
        self._release_snapshot = release_snapshot
        self._model: QwenEmbeddingModel | None = None
        self._model_lock = asyncio.Lock()
        self._client: MilvusClient | None = None
        self._client_lock = asyncio.Lock()
        self._engine: AsyncEngine | None = (
            create_async_engine(database_url, pool_pre_ping=True) if database_url else None
        )

    async def close(self) -> None:
        """释放数据库连接池和 Milvus 客户端。"""

        if self._client is not None:
            await asyncio.to_thread(self._client.close)
            self._client = None
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    async def warmup(self) -> None:
        """启动时核对活动发布指针和对应集合，避免带着失效索引提供服务。"""

        active = await self._release()
        collection = str(active.get("collection_name", ""))
        if not collection:
            raise RuntimeError("RAG active release is unavailable")
        client = await self._milvus()
        await asyncio.to_thread(
            client.describe_collection,
            collection_name=collection,
            timeout=MILVUS_RPC_TIMEOUT_SECONDS,
        )

    async def retrieve(
        self,
        query: str,
        *,
        trace_id: str,
        filters: RetrievalFilters | None = None,
        rerank: bool = True,
    ) -> RetrievalResult:
        """在当前活动快照上完成双路召回、RRF、精排和父文档回捞。"""

        result, _ = await self._retrieve_internal(
            query, trace_id=trace_id, filters=filters, rerank=rerank
        )
        return result

    async def retrieve_with_paths(
        self,
        query: str,
        *,
        trace_id: str,
        filters: RetrievalFilters | None = None,
        rerank: bool = True,
    ) -> tuple[RetrievalResult, dict[str, tuple[RetrievalDocument, ...]]]:
        """返回正式结果及各检索路径的候选，供运行诊断使用。"""

        return await self._retrieve_internal(
            query, trace_id=trace_id, filters=filters, rerank=rerank
        )

    async def _retrieve_internal(
        self,
        query: str,
        *,
        trace_id: str,
        filters: RetrievalFilters | None,
        rerank: bool,
    ) -> tuple[RetrievalResult, dict[str, tuple[RetrievalDocument, ...]]]:
        """执行一次检索，避免离线比较不同路径时重复编码和访问 Milvus。"""

        started = perf_counter()
        try:
            active = await self._release()
        except (OSError, RedisError, TypeError, ValueError):
            return (
                self._empty_result(query, trace_id, "RAG_RELEASE_UNAVAILABLE", started),
                {},
            )
        if not active:
            return (
                self._empty_result(
                    query,
                    trace_id,
                    "RAG_INDEX_UNAVAILABLE",
                    started,
                    action=RetrievalRecommendedAction.SAFE_FALLBACK,
                ),
                {},
            )
        collection = str(active["collection_name"])
        profile = str(active["embedding_profile_id"])
        try:
            vector = await self._encode(query)
            await self._milvus()
            paths = await asyncio.to_thread(
                self._retrieve_candidates,
                collection,
                vector,
                query,
                filters or RetrievalFilters(),
                profile,
                str(active["release_version"]),
            )
        except (MilvusException, OSError, RuntimeError, ValueError):
            return (
                self._empty_result(
                    query,
                    trace_id,
                    "RAG_RETRIEVAL_UNAVAILABLE",
                    started,
                    index=active,
                ),
                {},
            )
        paths = self._filter_applicable(paths)
        if not paths.fused:
            return (
                self._empty_result(
                    query, trace_id, "NO_APPLICABLE_CANDIDATE", started, index=active
                ),
                {},
            )
        candidates = list(paths.fused)
        rerank_error: str | None = None
        if rerank:
            try:
                await self._rerank(query, candidates)
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                rerank_error = type(exc).__name__
        parent_complete = await self._load_parent_content(candidates)
        documents = self._documents(candidates[: self._config.rerank_top_n])
        assessment = self._assess(
            candidates,
            rerank=rerank and rerank_error is None,
            parent_complete=parent_complete,
        )
        if rerank_error is not None:
            assessment = assessment.model_copy(
                update={"reason_codes": (*assessment.reason_codes, "RERANK_REQUEST_FAILED")}
            )
        result = RetrievalResult(
            retrieval_id=f"retrieval_{uuid5(NAMESPACE_URL, trace_id + ':' + query).hex}",
            trace_id=trace_id,
            retrieval_query=query,
            retrieval_queries=(query,),
            embedding_profile_id=profile,
            index_version=str(active["release_version"]),
            documents=documents,
            latency_ms=max(0, round((perf_counter() - started) * 1000)),
            assessment=assessment,
        )
        path_documents = {
            "DENSE": self._documents(paths.dense),
            "BM25": self._documents(paths.bm25),
            "RRF": self._documents(paths.fused),
            "RERANK": self._documents(candidates),
        }
        return result, path_documents

    async def _release(self) -> dict[str, object]:
        """默认读取活动指针；受控调用可显式注入尚未激活的候选快照。"""

        if self._release_snapshot is not None:
            return self._release_snapshot
        return await load_rag_release(self._redis)

    async def _encode(self, query: str) -> list[float]:
        """使用已加载的 Qwen 模型，并使用独立的 RAG 任务指令。"""

        instruction = DEFAULT_EMBEDDING_PROFILE.rag_query_instruction
        if self._semantic_search is not None:
            return await self._semantic_search.encode_query(query, instruction=instruction)
        async with self._model_lock:
            if self._model is None:
                self._model = await asyncio.to_thread(QwenEmbeddingModel, DEFAULT_EMBEDDING_PROFILE)
        return await asyncio.to_thread(
            lambda: self._model.encode_queries([query], instruction=instruction)[0]
        )

    async def _milvus(self) -> MilvusClient:
        """按进程复用同步 Milvus 客户端，所有调用放在线程池。"""

        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = await asyncio.to_thread(
                        MilvusClient,
                        uri=self._milvus_uri,
                        timeout=MILVUS_RPC_TIMEOUT_SECONDS,
                    )
        return self._client

    def _retrieve_candidates(
        self,
        collection: str,
        vector: list[float],
        query: str,
        filters: RetrievalFilters,
        profile_id: str,
        release_version: str,
    ) -> _CandidatePaths:
        """分别查询稠密和 BM25，再用 RRF 合并而不是直接相加异构分数。"""

        client = self._client
        if client is None:
            raise RuntimeError("Milvus client has not been initialized")
        expression = self._filter_expression(filters)
        fields = [
            "chunk_id",
            "text",
            "knowledge_id",
            "version",
            "parent_doc_id",
            "source_document_id",
            "title",
            "scene_category",
            "section_title",
            "block_kinds",
            "product_code",
            "channel",
            "region",
            "effective_at",
            "expires_at",
            "content_hash",
            "embedding_profile_id",
            "release_version",
            "page",
        ]
        dense_hits = client.search(
            collection_name=collection,
            data=[vector],
            anns_field="dense",
            filter=expression,
            limit=self._config.dense_top_k,
            search_params={"params": {"ef": RAG_EF_SEARCH}},
            output_fields=fields,
            timeout=MILVUS_RPC_TIMEOUT_SECONDS,
        )[0]
        sparse_hits = client.search(
            collection_name=collection,
            data=[query],
            anns_field="sparse",
            filter=expression,
            limit=self._config.bm25_top_k,
            search_params={"params": {"drop_ratio_search": 0.0}},
            output_fields=fields,
            timeout=MILVUS_RPC_TIMEOUT_SECONDS,
        )[0]
        grouped: dict[str, _Candidate] = {}
        dense_candidates: list[_Candidate] = []
        bm25_candidates: list[_Candidate] = []
        for rank, hit in enumerate(dense_hits, start=1):
            item = self._candidate(hit, profile_id, release_version)
            item.dense_score = float(hit.get("distance", 0.0))
            dense_candidates.append(item)
            item.fused_score += 1 / (RRF_K + rank)
            grouped[item.chunk_id] = item
        for rank, hit in enumerate(sparse_hits, start=1):
            item = self._candidate(hit, profile_id, release_version)
            item.bm25_score = float(hit.get("distance", 0.0))
            bm25_candidates.append(item)
            current = grouped.get(item.chunk_id)
            if current is None:
                item.fused_score = 1 / (RRF_K + rank)
                grouped[item.chunk_id] = item
            else:
                current.fused_score += 1 / (RRF_K + rank)
        candidates = sorted(grouped.values(), key=lambda item: item.fused_score, reverse=True)
        for rank, item in enumerate(candidates, start=1):
            item.fused_rank = rank
        return _CandidatePaths(
            dense=tuple(dense_candidates),
            bm25=tuple(bm25_candidates),
            fused=tuple(candidates),
        )

    @staticmethod
    def _filter_applicable(
        paths: _CandidatePaths,
        *,
        today: date | None = None,
    ) -> _CandidatePaths:
        """过滤当前日期之外的知识，避免过期或尚未生效内容进入精排。

        离线回放可显式指定 ``today``；在线调用不传时使用运行机器的当前日期。
        生命周期和启用状态由父文档回捞阶段再做权威校验，避免把 Milvus 中
        尚未下线的旧向量误当成可回答内容。
        """

        today = today or date.today()

        def applicable(item: _Candidate) -> bool:
            return not (
                (item.metadata.effective_at and item.metadata.effective_at > today)
                or (item.metadata.expires_at and item.metadata.expires_at < today)
            )

        fused = [item for item in paths.fused if applicable(item)]
        for rank, item in enumerate(fused, start=1):
            item.fused_rank = rank
        return _CandidatePaths(
            dense=tuple(item for item in paths.dense if applicable(item)),
            bm25=tuple(item for item in paths.bm25 if applicable(item)),
            fused=tuple(fused),
        )

    @staticmethod
    def _documents(
        candidates: list[_Candidate] | tuple[_Candidate, ...],
    ) -> tuple[RetrievalDocument, ...]:
        """把内部候选转换为检索与诊断共用的文档契约。"""

        return tuple(
            RetrievalDocument(
                document_id=item.chunk_id,
                page_content=item.parent_content or item.text,
                metadata=item.metadata,
                vector_score=item.dense_score,
                bm25_score=item.bm25_score,
                fused_rank=item.fused_rank or None,
                rerank_score=item.rerank_score,
            )
            for item in candidates
        )

    @staticmethod
    def _candidate(hit: dict[str, Any], profile_id: str, release_version: str) -> _Candidate:
        """把 Milvus 标量字段转换成有校验的 RAG 元数据。"""

        entity = hit["entity"]
        entity_profile = str(entity.get("embedding_profile_id") or profile_id)
        entity_release = str(entity.get("release_version") or release_version)
        if entity_profile != profile_id or entity_release != release_version:
            raise ValueError("Milvus 候选与活动 RAG 发布指针不一致")
        metadata = RagChunkMetadata(
            knowledge_id=str(entity["knowledge_id"]),
            title=str(entity["title"]),
            scene_category=str(entity["scene_category"]),
            product_code=str(entity.get("product_code") or "") or None,
            channel=str(entity.get("channel") or "") or None,
            region=str(entity.get("region") or "") or None,
            version=str(entity["version"]),
            source_document_id=str(entity["source_document_id"]),
            parent_doc_id=str(entity["parent_doc_id"]),
            chunk_id=str(entity["chunk_id"]),
            section_title=str(entity["section_title"]),
            page=int(entity["page"]),
            block_kinds=tuple(
                value for value in str(entity.get("block_kinds") or "").split(",") if value
            ),
            effective_at=_parse_date(entity.get("effective_at")),
            expires_at=_parse_date(entity.get("expires_at")),
            content_hash=str(entity["content_hash"]),
            embedding_profile_id=entity_profile,
            release_version=entity_release,
        )
        return _Candidate(
            chunk_id=metadata.chunk_id,
            text=str(entity["text"]),
            metadata=metadata,
        )

    @staticmethod
    def _filter_expression(filters: RetrievalFilters) -> str | None:
        """把可信范围下推到 Milvus，同时保留没有范围限制的通用知识。

        ``product_code``、``channel`` 和 ``region`` 在 PostgreSQL 中允许为空，
        空值表示该知识没有对应限制。索引为了使用 VARCHAR 标量字段把空值保存为
        空字符串，因此在线请求给出 ``WEB`` 等范围时必须同时接受空字符串；否则
        所有通用知识都会被误过滤。场景分类是每份知识必填的业务分类，不使用通配
        语义，仍然只做精确匹配。
        """

        values = []
        if filters.scene_category:
            escaped = str(filters.scene_category).replace("\\", "\\\\").replace('"', '\\"')
            values.append(f'scene_category == "{escaped}"')
        for field in ("product_code", "channel", "region"):
            value = getattr(filters, field)
            if value:
                escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
                values.append(f'({field} == "{escaped}" or {field} == "")')
        return " and ".join(values) if values else None

    async def _rerank(self, query: str, candidates: list[_Candidate]) -> None:
        """用百炼重排有限候选；没有凭据时保留候选但不宣称证据充分。"""

        items = candidates[: self._config.rerank_top_n]
        results = await self._reranker.rerank(
            query,
            [item.text for item in items],
            top_n=len(items),
        )
        for result in results:
            items[result.index].rerank_score = result.score
        items.sort(
            key=lambda item: item.rerank_score if item.rerank_score is not None else -1,
            reverse=True,
        )
        candidates[: len(items)] = items

    def _assess(
        self,
        candidates: list[_Candidate],
        *,
        rerank: bool,
        parent_complete: bool = True,
    ) -> RetrievalAssessment:
        """以精排分数、知识级竞争关系和父文档完整性生成保守评估。"""

        if not parent_complete:
            return RetrievalAssessment(
                status=RetrievalAssessmentStatus.INSUFFICIENT,
                coverage=0,
                reason_codes=("PARENT_DOCUMENT_UNAVAILABLE",),
                recommended_action=RetrievalRecommendedAction.HUMAN,
            )

        scored = [item for item in candidates if item.rerank_score is not None]
        if not rerank or not scored:
            return RetrievalAssessment(
                status=RetrievalAssessmentStatus.INSUFFICIENT,
                coverage=0,
                reason_codes=("RERANK_UNAVAILABLE",),
                recommended_action=RetrievalRecommendedAction.HUMAN,
            )
        top_candidate = scored[0]
        top = float(top_candidate.rerank_score)
        if top < self._config.min_rerank_score:
            return RetrievalAssessment(
                status=RetrievalAssessmentStatus.INSUFFICIENT,
                coverage=0,
                reason_codes=("RERANK_SCORE_BELOW_THRESHOLD",),
                recommended_action=RetrievalRecommendedAction.SAFE_FALLBACK,
            )
        # 同一知识文档的不同章节通常是互补证据，不能因为两个 chunk 分数接近
        # 就误判为内容冲突。分差门只比较最高分知识与最高分的另一知识版本。
        top_knowledge = (
            top_candidate.metadata.knowledge_id,
            top_candidate.metadata.version,
        )
        competing = next(
            (
                item
                for item in scored[1:]
                if (item.metadata.knowledge_id, item.metadata.version) != top_knowledge
            ),
            None,
        )
        margin = top - float(competing.rerank_score) if competing is not None else top
        if competing is not None and margin < self._config.min_rerank_margin:
            return RetrievalAssessment(
                # 分数接近只说明相关性竞争，不能证明两份正文互相矛盾。
                # CONFLICTING 留给后续有内容级冲突证据的评估器使用。
                status=RetrievalAssessmentStatus.INSUFFICIENT,
                coverage=0,
                reason_codes=("RERANK_KNOWLEDGE_MARGIN_TOO_SMALL",),
                # 检索分差无法自动构造可靠的业务澄清问题，先交人工核对。
                recommended_action=RetrievalRecommendedAction.HUMAN,
            )
        return RetrievalAssessment(
            status=RetrievalAssessmentStatus.SUFFICIENT,
            coverage=1,
            reason_codes=("RERANKED_CANDIDATE",),
            recommended_action=RetrievalRecommendedAction.ANSWER,
        )

    async def _load_parent_content(self, candidates: list[_Candidate]) -> bool:
        """从 PostgreSQL 权威 JSONB 按父文档编号回捞，并报告是否全部成功。"""

        if self._engine is None:
            return False
        keys = {(item.metadata.knowledge_id, item.metadata.version) for item in candidates}
        try:
            async with self._engine.connect() as connection:
                for knowledge_id, version in keys:
                    result = await connection.execute(
                        text(
                            "SELECT bundle, lifecycle_status, enabled FROM content.rag_document "
                            "WHERE knowledge_id = :knowledge_id AND version = :version"
                        ),
                        {"knowledge_id": knowledge_id, "version": version},
                    )
                    row = result.first()
                    if (
                        row is None
                        or row.lifecycle_status != "PUBLISHED"
                        or row.enabled is not True
                    ):
                        continue
                    bundle = RagDocumentBundle.model_validate(row[0])
                    parents = {
                        parent.parent_doc_id: parent.page_content for parent in bundle.parents
                    }
                    for item in candidates:
                        if (
                            item.metadata.knowledge_id == knowledge_id
                            and item.metadata.version == version
                        ):
                            item.parent_content = parents.get(item.metadata.parent_doc_id)
        except (SQLAlchemyError, TypeError, ValueError):
            return False
        return all(item.parent_content for item in candidates)

    @staticmethod
    def _empty_result(
        query: str,
        trace_id: str,
        reason: str,
        started: float,
        *,
        index: dict[str, Any] | None = None,
        action: RetrievalRecommendedAction = RetrievalRecommendedAction.SAFE_FALLBACK,
    ) -> RetrievalResult:
        """返回结构完整但不允许生成答案的空结果。"""

        version = str((index or {}).get("release_version") or "unavailable")
        profile = str((index or {}).get("embedding_profile_id") or "unavailable")
        return RetrievalResult(
            retrieval_id=f"retrieval_{uuid5(NAMESPACE_URL, trace_id + ':' + query).hex}",
            trace_id=trace_id,
            retrieval_query=query,
            retrieval_queries=(query,),
            embedding_profile_id=profile,
            index_version=version,
            documents=(),
            latency_ms=max(0, round((perf_counter() - started) * 1000)),
            assessment=RetrievalAssessment(
                status=RetrievalAssessmentStatus.INSUFFICIENT,
                coverage=0,
                reason_codes=(reason,),
                recommended_action=action,
            ),
        )


def _parse_date(value: object) -> date | None:
    """解析索引中的 ISO 日期；空值表示没有适用期限制。"""

    if value in (None, ""):
        return None
    return date.fromisoformat(str(value)[:10])
