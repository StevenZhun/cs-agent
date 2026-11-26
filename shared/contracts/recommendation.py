"""BFF 返回坐席工作台的 Contract v2 结构化推荐。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from shared.contracts.common import ContractModel
from shared.contracts.enums import (
    AnswerSourceType,
    FaqMatchStatus,
    FaqMissReason,
    IntentDecisionStatus,
    IntentRouteTarget,
    QualityAction,
    ResponsePath,
    RetrievalAssessmentStatus,
)
from shared.contracts.faq import FaqResolutionResult
from shared.contracts.quality import QualityDecision
from shared.contracts.retrieval import RetrievalAssessment
from shared.contracts.routing import IntentDecision
from shared.contracts.runtime import DecisionSummary
from shared.contracts.sop import SlotState, SopProgress


class AnswerSource(ContractModel):
    """推荐答案中一项不混淆语义的可追溯来源。"""

    source_type: AnswerSourceType
    source_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    title: str | None = None
    excerpt: str | None = None
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class Escalation(ContractModel):
    """是否需要升级人工或其他业务组的建议。"""

    required: bool
    reason: str | None = None
    target: str | None = None

    @model_validator(mode="after")
    def validate_required_fields(self) -> Escalation:
        """真正升级时必须说明原因和目标。"""

        if self.required and (not self.reason or not self.target):
            raise ValueError("required escalation needs reason and target")
        return self


class RecommendationResult(ContractModel):
    """坐席工作台消费的一次完整、可追溯推荐结果。"""

    request_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    recommendation_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    response_path: ResponsePath
    intent_decision: IntentDecision
    decision_summary: DecisionSummary
    answer: str = Field(min_length=1)
    slots: tuple[SlotState, ...] = ()
    sop_progress: SopProgress | None = None
    faq_resolution_summary: FaqResolutionResult | None = None
    retrieval_assessment: RetrievalAssessment | None = None
    answer_sources: tuple[AnswerSource, ...] = ()
    quality_decision: QualityDecision
    notices: tuple[str, ...] = ()
    escalation: Escalation
    review_status: Literal["DRAFT", "RELEASED", "SENT", "REJECTED", "EXPIRED"] = "RELEASED"
    review_expires_at: datetime | None = None
    rag_draft_id: str | None = Field(default=None, min_length=1, max_length=96)
    retrieval_id: str | None = Field(default=None, min_length=1, max_length=96)

    @model_validator(mode="after")
    def validate_path_payload(self) -> RecommendationResult:
        """保证每条响应路径携带自己的结果，而不是总体置信分数。"""

        if self.response_path is ResponsePath.SOP and self.sop_progress is None:
            raise ValueError("SOP recommendations require sop_progress")
        if self.response_path is ResponsePath.FAQ and (
            self.faq_resolution_summary is None
            or self.faq_resolution_summary.match_status is not FaqMatchStatus.MATCHED
        ):
            raise ValueError("FAQ recommendations require a matched FAQ result")
        if self.response_path is ResponsePath.RAG:
            direct_rag = (
                self.intent_decision.status is IntentDecisionStatus.UNKNOWN
                and self.faq_resolution_summary is None
            )
            faq_fallback = (
                self.intent_decision.status is IntentDecisionStatus.MATCHED
                and self.intent_decision.route_target is IntentRouteTarget.FAQ
                and self.faq_resolution_summary is not None
                and self.faq_resolution_summary.match_status is FaqMatchStatus.NOT_MATCHED
                and self.faq_resolution_summary.miss_reason is FaqMissReason.NO_CANDIDATE
            )
            if (
                not (direct_rag or faq_fallback)
                or self.retrieval_assessment is None
                or self.retrieval_assessment.status is not RetrievalAssessmentStatus.SUFFICIENT
                or self.rag_draft_id is None
                or self.retrieval_id is None
            ):
                raise ValueError(
                    "RAG recommendations require a valid admission origin, sufficient evidence "
                    "and draft ids"
                )
            if self.review_status == "DRAFT" and self.review_expires_at is None:
                raise ValueError("待审核 RAG 草稿必须携带审核截止时间")
            if self.review_status == "EXPIRED" and self.review_expires_at is None:
                raise ValueError("已过期 RAG 草稿必须保留原审核截止时间")
        if self.response_path is not ResponsePath.RAG and (
            self.rag_draft_id is not None or self.retrieval_id is not None
        ):
            raise ValueError("only RAG recommendations can carry draft ids")
        if self.response_path is not ResponsePath.RAG and self.review_expires_at is not None:
            raise ValueError("只有 RAG 草稿可以携带审核截止时间")
        if self.response_path in {ResponsePath.FAQ, ResponsePath.RAG} and not self.answer_sources:
            raise ValueError("FAQ and RAG recommendations require answer_sources")
        if self.quality_decision.action is QualityAction.BLOCK:
            raise ValueError("blocked content must not be returned as RecommendationResult.answer")
        return self
