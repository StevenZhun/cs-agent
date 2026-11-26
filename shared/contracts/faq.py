"""FAQ 内容定义、候选与条目解析结果契约。"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from shared.contracts.common import ContractModel
from shared.contracts.enums import (
    AnswerEligibility,
    ConfidenceBand,
    FaqMatchStatus,
    FaqMissReason,
)


class FaqGroup(ContractModel):
    """与一个 FAQ 全局意图绑定的条目分组。"""

    faq_group_id: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


class FaqQuestionVariant(ContractModel):
    """经审核并投影到精确或语义索引的问题变体。"""

    variant_id: str = Field(min_length=1)
    faq_item_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    exact_match_enabled: bool = False
    locale: str = "zh-CN"


class FaqAnswerVariant(ContractModel):
    """具有适用范围和有效期的 FAQ 固定答案版本。"""

    answer_id: str = Field(min_length=1)
    faq_item_id: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    product_codes: tuple[str, ...] = ()
    channels: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    effective_from: datetime | None = None
    effective_to: datetime | None = None

    @model_validator(mode="after")
    def validate_validity_window(self) -> FaqAnswerVariant:
        """拒绝倒置的固定答案有效期。"""

        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_from > self.effective_to
        ):
            raise ValueError("effective_from must not be later than effective_to")
        return self


class FaqItem(ContractModel):
    """FAQ 权威内容库中的一个具体问答条目。"""

    faq_item_id: str = Field(min_length=1)
    faq_group_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    question_variants: tuple[FaqQuestionVariant, ...]
    answer_variants: tuple[FaqAnswerVariant, ...]
    version: str = Field(min_length=1)
    published: bool = False


class FaqCandidate(ContractModel):
    """按 faq_item_id 聚合后的一个候选条目。"""

    faq_item_id: str = Field(min_length=1)
    winning_variant_id: str = Field(min_length=1)
    score: float = Field(ge=0, le=1)


class FaqExactHint(ContractModel):
    """标准问题精确命中后留给 FAQ Resolver 的条目线索。"""

    intent_id: str = Field(min_length=1)
    faq_group_id: str = Field(min_length=1)
    faq_item_id: str = Field(min_length=1)
    faq_version: str = Field(min_length=1)
    winning_variant_id: str = Field(min_length=1)
    release_version: str = Field(min_length=1)
    locale: str = Field(min_length=1)
    query_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class FaqResolutionResult(ContractModel):
    """FAQ 模块对具体条目的可解释解析结果。"""

    intent_id: str = Field(min_length=1)
    faq_item_id: str | None = None
    faq_title: str | None = None
    winning_variant_id: str | None = None
    top1_score: float | None = Field(default=None, ge=0, le=1)
    top2_item_score: float | None = Field(default=None, ge=0, le=1)
    score_margin: float | None = Field(default=None, ge=0, le=1)
    confidence_band: ConfidenceBand
    match_status: FaqMatchStatus
    miss_reason: FaqMissReason | None = None
    answer_eligibility: AnswerEligibility
    answer_id: str | None = None
    answer: str | None = None
    answer_source_ref: str | None = None
    answer_content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    faq_version: str = Field(min_length=1)
    index_version: str = Field(min_length=1)
    candidates: tuple[FaqCandidate, ...] = ()
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_resolution(self) -> FaqResolutionResult:
        """约束高置信直答、未命中原因和不同条目分数关系。"""

        if self.match_status is FaqMatchStatus.MATCHED:
            if (
                not self.faq_item_id
                or not self.faq_title
                or not self.winning_variant_id
                or self.top1_score is None
            ):
                raise ValueError("MATCHED FAQ requires item, variant and top1_score")
            if not all(
                (
                    self.answer_id,
                    self.answer,
                    self.answer_source_ref,
                    self.answer_content_hash,
                )
            ):
                raise ValueError("MATCHED FAQ requires one traceable answer")
            if self.miss_reason is not None:
                raise ValueError("MATCHED FAQ must not carry miss_reason")
        else:
            if self.miss_reason is None:
                raise ValueError("non-matched FAQ requires miss_reason")
            if any(
                (
                    self.answer_id,
                    self.answer,
                    self.answer_source_ref,
                    self.answer_content_hash,
                    self.faq_title,
                )
            ):
                raise ValueError("non-matched FAQ must not carry an answer")

        if self.confidence_band is ConfidenceBand.HIGH:
            if self.match_status is not FaqMatchStatus.MATCHED:
                raise ValueError("HIGH confidence FAQ must be MATCHED")
            if self.answer_eligibility is not AnswerEligibility.ELIGIBLE:
                raise ValueError("HIGH confidence FAQ must have an eligible answer")

        if self.top1_score is not None and self.top2_item_score is not None:
            if self.top1_score < self.top2_item_score:
                raise ValueError("top1_score must be at least top2_item_score")
            expected_margin = self.top1_score - self.top2_item_score
            if self.score_margin is None or abs(self.score_margin - expected_margin) > 1e-9:
                raise ValueError("score_margin must equal top1_score - top2_item_score")

        candidate_ids = [candidate.faq_item_id for candidate in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("FAQ candidates must be aggregated by faq_item_id")
        return self
