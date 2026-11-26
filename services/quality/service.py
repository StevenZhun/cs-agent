"""异步 active 复核、Shadow 对照和待审核样本发现。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import httpx
from sqlalchemy.exc import SQLAlchemyError

from services.quality.governance import candidate_fingerprint, text_hash
from services.quality.repository import QualityRepository, QualityReviewContext
from services.quality.reviewer import ShadowQualityReviewer, quality_snapshot
from shared.contracts.enums import (
    QualityAction,
    SampleCandidateConflictStatus,
    SampleCandidateStatus,
    SampleCandidateType,
    ShadowComparisonStatus,
)
from shared.contracts.quality import (
    AsyncQualityResult,
    DeepQualitySignals,
    QualitySampleCandidate,
    ShadowComparison,
)
from shared.guardrails import GUARDRAIL_POLICY_VERSION, evaluate_output_content


class AsyncQualityProcessor:
    """只分析已经落库的 Run；任何结果都不能反向改写当前回复。"""

    def __init__(
        self,
        repository: QualityRepository,
        reviewer: ShadowQualityReviewer | None,
        *,
        worker_id: str,
    ) -> None:
        self._repository = repository
        self._reviewer = reviewer
        self._worker_id = worker_id

    async def process_one(self) -> bool:
        context = await self._repository.claim(self._worker_id)
        if context is None:
            return False
        try:
            result = self._active_result(context)
            comparison = await self._shadow_comparison(context, result)
            candidate = self._sample_candidate(context, result, comparison)
            await self._repository.complete(
                result=result,
                comparison=comparison,
                candidate=candidate,
            )
        except (httpx.HTTPError, SQLAlchemyError, KeyError, TypeError, ValueError) as error:
            await self._repository.fail(context.job, type(error).__name__.upper())
        return True

    @staticmethod
    def _active_result(context: QualityReviewContext) -> AsyncQualityResult:
        decision = evaluate_output_content(
            context.assistant_content,
            decision_id=f"async:{context.job.job_id}",
        )
        return AsyncQualityResult(
            result_id=f"qr_{uuid5(NAMESPACE_URL, context.job.job_id + ':active').hex}",
            job_id=context.job.job_id,
            trace_id=context.job.trace_id,
            action=decision.action,
            findings=decision.findings,
            evaluator="DETERMINISTIC_OUTPUT_GUARDRAIL",
            evaluator_version=GUARDRAIL_POLICY_VERSION,
            created_at=datetime.now(UTC),
        )

    async def _shadow_comparison(
        self,
        context: QualityReviewContext,
        result: AsyncQualityResult,
    ) -> ShadowComparison | None:
        if self._reviewer is None:
            return None
        comparison_id = f"qc_{uuid5(NAMESPACE_URL, context.job.job_id + ':shadow').hex}"
        try:
            signals, latency_ms = await self._reviewer.review(
                quality_snapshot(
                    customer_content=context.customer_content,
                    assistant_content=context.assistant_content,
                    response_path=context.response_path,
                    reason_code=context.reason_code,
                )
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return ShadowComparison(
                comparison_id=comparison_id,
                job_id=context.job.job_id,
                trace_id=context.job.trace_id,
                active_evaluator=result.evaluator,
                shadow_evaluator=self._reviewer.name,
                active_action=result.action,
                status=ShadowComparisonStatus.SHADOW_FAILED,
                latency_ms=0,
                error_code="SHADOW_REVIEW_FAILED",
                created_at=datetime.now(UTC),
            )
        shadow_action = self._action_from_signals(signals)
        return ShadowComparison(
            comparison_id=comparison_id,
            job_id=context.job.job_id,
            trace_id=context.job.trace_id,
            active_evaluator=result.evaluator,
            shadow_evaluator=self._reviewer.name,
            active_action=result.action,
            shadow_action=shadow_action,
            status=(
                ShadowComparisonStatus.AGREED
                if shadow_action is result.action
                else ShadowComparisonStatus.DISAGREED
            ),
            shadow_signals=signals,
            latency_ms=latency_ms,
            created_at=datetime.now(UTC),
        )

    @staticmethod
    def _action_from_signals(signals: DeepQualitySignals) -> QualityAction:
        if signals.unsafe_commitment:
            return QualityAction.BLOCK
        if signals.needs_human_review:
            return QualityAction.HANDOFF
        if signals.answer_relevant is False or signals.evidence_supported is False:
            return QualityAction.REWRITE
        return QualityAction.PASS

    @staticmethod
    def _sample_candidate(
        context: QualityReviewContext,
        result: AsyncQualityResult,
        comparison: ShadowComparison | None,
    ) -> QualitySampleCandidate | None:
        reason_codes = list(code for finding in result.findings for code in (finding.code,))
        if comparison is not None:
            if comparison.status is ShadowComparisonStatus.DISAGREED:
                reason_codes.append("ACTIVE_SHADOW_DISAGREEMENT")
            if comparison.shadow_signals:
                reason_codes.extend(comparison.shadow_signals.reason_codes)
        if context.response_path in {"CLARIFICATION", "HUMAN", "SAFE_FALLBACK"}:
            reason_codes.append(f"TERMINAL_{context.response_path}")
        unique_codes = tuple(dict.fromkeys(reason_codes))
        if not unique_codes:
            return None
        hashes = {
            "customer": text_hash(context.customer_content),
            "assistant": text_hash(context.assistant_content),
        }
        candidate_type = AsyncQualityProcessor._candidate_type(context, unique_codes)
        fingerprint = candidate_fingerprint(candidate_type, hashes["customer"])
        created_at = datetime.now(UTC)
        return QualitySampleCandidate(
            candidate_id=f"qsc_{fingerprint[:32]}",
            job_id=context.job.job_id,
            trace_id=context.job.trace_id,
            candidate_type=candidate_type,
            fingerprint=fingerprint,
            observed_outcome=(
                f"{context.response_path}:{context.reason_code}"
                if context.response_path and context.reason_code
                else context.response_path
            ),
            conflict_status=SampleCandidateConflictStatus.CLEAR,
            status=SampleCandidateStatus.PENDING_REVIEW,
            reason_codes=unique_codes,
            source_refs=context.source_refs,
            content_hashes=hashes,
            created_at=created_at,
            last_seen_at=created_at,
        )

    @staticmethod
    def _candidate_type(
        context: QualityReviewContext,
        reason_codes: tuple[str, ...],
    ) -> SampleCandidateType:
        """按已落库的终点和质量信号分类，不猜测应发布的最终内容。"""

        if context.reason_code in {"INTENT_AMBIGUOUS", "QUERY_AMBIGUOUS"}:
            return SampleCandidateType.INTENT_EXPRESSION
        if context.reason_code in {"NO_GROUNDED_ANSWER", "RAG_EVIDENCE_INSUFFICIENT"}:
            return SampleCandidateType.FAQ_GAP
        if context.response_path == "SAFE_FALLBACK":
            return SampleCandidateType.SAFETY_REVIEW
        if "ACTIVE_SHADOW_DISAGREEMENT" in reason_codes:
            return SampleCandidateType.RESPONSE_QUALITY
        return SampleCandidateType.RESPONSE_QUALITY
