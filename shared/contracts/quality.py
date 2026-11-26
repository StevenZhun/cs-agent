"""同步质量放行和单项质量发现契约。"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import Field, model_validator

from shared.contracts.common import ContractModel
from shared.contracts.enums import (
    AsyncQualityJobStatus,
    QualityAction,
    QualitySeverity,
    SampleCandidateConflictStatus,
    SampleCandidateReviewDecision,
    SampleCandidateSourceType,
    SampleCandidateStatus,
    SampleCandidateType,
    ShadowComparisonStatus,
)


class QualityFinding(ContractModel):
    """规则或模型产生的一项可审计质量发现。"""

    finding_id: str = Field(min_length=1)
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    severity: QualitySeverity
    message: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()


class QualityDecision(ContractModel):
    """回复展示前由确定性 Quality Policy 形成的同步决定。"""

    decision_id: str = Field(min_length=1)
    action: QualityAction
    findings: tuple[QualityFinding, ...] = ()
    policy_version: str = Field(min_length=1)
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_blocking_action(self) -> QualityDecision:
        """非 PASS 决策必须给出可审计依据。"""

        if self.action is not QualityAction.PASS and not self.findings:
            raise ValueError("non-PASS quality decisions require findings")
        if self.action is QualityAction.PASS and any(
            finding.severity in {QualitySeverity.ERROR, QualitySeverity.CRITICAL}
            for finding in self.findings
        ):
            raise ValueError("PASS cannot contain ERROR or CRITICAL findings")
        return self


class AsyncQualityJob(ContractModel):
    """已发送回复对应的一次异步质检任务；正文仍由应用消息表拥有。"""

    job_id: str = Field(min_length=1, max_length=96)
    conversation_id: str = Field(min_length=1, max_length=64)
    turn_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    trace_id: str = Field(min_length=1, max_length=128)
    status: AsyncQualityJobStatus
    attempts: int = Field(ge=0)
    created_at: datetime


class DeepQualitySignals(ContractModel):
    """异步模型只返回原子质量信号，不生成或改写客户答案。"""

    answer_relevant: bool | None = None
    evidence_supported: bool | None = None
    unsafe_commitment: bool = False
    needs_human_review: bool = False
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_reason_codes(self) -> DeepQualitySignals:
        if any(re.fullmatch(r"[A-Z][A-Z0-9_]*", code) is None for code in self.reason_codes):
            raise ValueError("quality reason codes must be identifier-like")
        return self


class AsyncQualityResult(ContractModel):
    """异步规则复核的事实记录；只能用于运营和后续人工复盘。"""

    result_id: str = Field(min_length=1, max_length=96)
    job_id: str = Field(min_length=1, max_length=96)
    trace_id: str = Field(min_length=1, max_length=128)
    action: QualityAction
    findings: tuple[QualityFinding, ...] = ()
    evaluator: str = Field(min_length=1, max_length=64)
    evaluator_version: str = Field(min_length=1, max_length=64)
    created_at: datetime


class ShadowComparison(ContractModel):
    """active 规则复核与异步 shadow Provider 的一次不可变对照。"""

    comparison_id: str = Field(min_length=1, max_length=96)
    job_id: str = Field(min_length=1, max_length=96)
    trace_id: str = Field(min_length=1, max_length=128)
    active_evaluator: str = Field(min_length=1, max_length=64)
    shadow_evaluator: str = Field(min_length=1, max_length=64)
    active_action: QualityAction
    shadow_action: QualityAction | None = None
    status: ShadowComparisonStatus
    shadow_signals: DeepQualitySignals | None = None
    latency_ms: int = Field(ge=0)
    error_code: str | None = Field(default=None, max_length=64)
    created_at: datetime

    @model_validator(mode="after")
    def validate_shadow_outcome(self) -> ShadowComparison:
        if self.status is ShadowComparisonStatus.SHADOW_FAILED:
            if self.error_code is None or self.shadow_action is not None:
                raise ValueError("failed shadow comparison requires only an error code")
        elif self.shadow_action is None or self.error_code is not None:
            raise ValueError("successful shadow comparison requires a shadow action")
        return self


class QualitySampleCandidate(ContractModel):
    """质检发现的待审核样本引用，不复制客户原文也不自动进入训练集。"""

    candidate_id: str = Field(min_length=1, max_length=96)
    job_id: str | None = Field(default=None, min_length=1, max_length=96)
    trace_id: str = Field(min_length=1, max_length=128)
    candidate_type: SampleCandidateType
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_outcome: str | None = Field(default=None, max_length=128)
    conflict_status: SampleCandidateConflictStatus = SampleCandidateConflictStatus.CLEAR
    status: SampleCandidateStatus
    reason_codes: tuple[str, ...]
    source_refs: tuple[str, ...]
    content_hashes: dict[str, str] = Field(default_factory=dict)
    occurrence_count: int = Field(default=1, ge=1)
    version: int = Field(default=1, ge=1)
    created_at: datetime
    last_seen_at: datetime


class QualityCandidateEvidence(ContractModel):
    """候选的一次来源观测，只保存引用、原因码和内容哈希。"""

    evidence_id: str = Field(min_length=1, max_length=96)
    candidate_id: str = Field(min_length=1, max_length=96)
    source_type: SampleCandidateSourceType
    source_id: str = Field(min_length=1, max_length=128)
    observed_outcome: str | None = Field(default=None, max_length=128)
    reason_codes: tuple[str, ...]
    source_refs: tuple[str, ...]
    content_hashes: dict[str, str] = Field(default_factory=dict)
    occurred_at: datetime
    created_at: datetime


class QualityCandidateReviewEvent(ContractModel):
    """人工审核候选的不可变审计事件。"""

    review_id: str = Field(min_length=1, max_length=96)
    candidate_id: str = Field(min_length=1, max_length=96)
    candidate_version: int = Field(ge=1)
    decision: SampleCandidateReviewDecision
    reviewer_id: str = Field(min_length=1, max_length=64)
    note: str = Field(min_length=1, max_length=2000)
    created_at: datetime


class QualityCandidateReleaseLink(ContractModel):
    """已批准候选与后续人工发布资产之间的审计关联。"""

    release_link_id: str = Field(min_length=1, max_length=96)
    candidate_id: str = Field(min_length=1, max_length=96)
    candidate_version: int = Field(ge=1)
    target_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$", max_length=48)
    target_ref: str = Field(min_length=1, max_length=160)
    target_version: str = Field(min_length=1, max_length=64)
    linked_by: str = Field(min_length=1, max_length=64)
    created_at: datetime


class QualityCandidateDetail(ContractModel):
    """候选及其来源、审核和发布关联的完整审计视图。"""

    candidate: QualitySampleCandidate
    evidence: tuple[QualityCandidateEvidence, ...] = ()
    reviews: tuple[QualityCandidateReviewEvent, ...] = ()
    release_links: tuple[QualityCandidateReleaseLink, ...] = ()


class ReviewQualityCandidateRequest(ContractModel):
    """使用乐观版本审核候选，避免覆盖新的冲突证据。"""

    expected_version: int = Field(ge=1)
    decision: SampleCandidateReviewDecision
    note: str = Field(min_length=1, max_length=2000)


class RegisterCandidateReleaseRequest(ContractModel):
    """登记候选被哪个人工发布资产吸收，不负责执行发布。"""

    expected_version: int = Field(ge=1)
    target_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$", max_length=48)
    target_ref: str = Field(min_length=1, max_length=160)
    target_version: str = Field(min_length=1, max_length=64)


class QualityOperationsSummary(ContractModel):
    """异步质检的运营汇总，不包含任何客户正文。"""

    window_hours: int = Field(ge=1, le=24 * 31)
    pending_jobs: int = Field(ge=0)
    running_jobs: int = Field(ge=0)
    completed_jobs: int = Field(ge=0)
    failed_jobs: int = Field(ge=0)
    shadow_comparisons: int = Field(ge=0)
    shadow_disagreements: int = Field(ge=0)
    pending_candidates: int = Field(ge=0)
    reason_code_counts: dict[str, int] = Field(default_factory=dict)
