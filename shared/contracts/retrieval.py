"""自研 RAG 的检索请求、证据和充分性评估契约。"""

from __future__ import annotations

from pydantic import Field, model_validator

from shared.contracts.common import ContractModel
from shared.contracts.enums import (
    RetrievalAssessmentStatus,
    RetrievalRecommendedAction,
)
from shared.contracts.knowledge import KnowledgeMetadata
from shared.contracts.rag import RagChunkMetadata


class RetrievalFilters(ContractModel):
    """向量召回前应用到 RAG 索引的业务元数据过滤条件。"""

    scene_category: str | None = None
    product_code: str | None = None
    channel: str | None = None
    region: str | None = None
    enabled: bool = True


class RetrievalRequest(ContractModel):
    """AI Runtime 调用自研 RAG 时提交的实际检索请求。"""

    trace_id: str = Field(min_length=1)
    retrieval_query: str = Field(min_length=1)
    retrieval_queries: tuple[str, ...] = ()
    embedding_profile_id: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=50)
    filters: RetrievalFilters = Field(default_factory=RetrievalFilters)


class RetrievalDocument(ContractModel):
    """一次召回返回的 RAG 正文、元数据和分阶段得分。"""

    document_id: str = Field(min_length=1)
    page_content: str = Field(min_length=1)
    metadata: KnowledgeMetadata | RagChunkMetadata
    vector_score: float | None = None
    bm25_score: float | None = None
    fused_rank: int | None = Field(default=None, ge=1)
    rerank_score: float | None = None


class RetrievalAssessment(ContractModel):
    """检索证据是否足以支持回答的独立判断。"""

    status: RetrievalAssessmentStatus
    coverage: float = Field(ge=0, le=1)
    reason_codes: tuple[str, ...] = ()
    recommended_action: RetrievalRecommendedAction

    @model_validator(mode="after")
    def validate_recommended_action(self) -> RetrievalAssessment:
        """只有证据充分时才允许生成有依据的回答。"""

        if self.status is RetrievalAssessmentStatus.SUFFICIENT:
            if self.recommended_action is not RetrievalRecommendedAction.ANSWER:
                raise ValueError("SUFFICIENT evidence must recommend ANSWER")
        elif self.recommended_action is RetrievalRecommendedAction.ANSWER:
            raise ValueError("insufficient, conflicting or stale evidence cannot recommend ANSWER")
        return self


class RetrievalResult(ContractModel):
    """带索引版本、证据评估和耗时信息的统一检索响应。"""

    retrieval_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    retrieval_query: str = Field(min_length=1)
    retrieval_queries: tuple[str, ...] = ()
    embedding_profile_id: str = Field(min_length=1)
    index_version: str = Field(min_length=1)
    documents: tuple[RetrievalDocument, ...]
    latency_ms: int = Field(ge=0)
    assessment: RetrievalAssessment
