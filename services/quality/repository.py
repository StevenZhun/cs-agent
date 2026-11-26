"""异步质检任务领取、事实读取和结果持久化。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.bff.infrastructure.models import (
    ChatHistoryMessageModel,
    GraphRunRecordModel,
    RecommendationRecordModel,
    TurnRecordModel,
)
from services.quality.governance import CandidateObservation, observe_candidate
from services.quality.models import (
    AsyncQualityJobModel,
    AsyncQualityResultModel,
    QualitySampleCandidateModel,
    ShadowComparisonModel,
)
from shared.contracts.enums import (
    AsyncQualityJobStatus,
    SampleCandidateSourceType,
)
from shared.contracts.quality import (
    AsyncQualityJob,
    AsyncQualityResult,
    QualitySampleCandidate,
    ShadowComparison,
)


@dataclass(frozen=True)
class QualityReviewContext:
    """工作进程临时读取的原始上下文；不会整体复制进 quality schema。"""

    job: AsyncQualityJob
    customer_content: str
    assistant_content: str
    response_path: str | None
    reason_code: str | None
    source_refs: tuple[str, ...]


class QualityRepository:
    """在短事务中领取任务，模型等待期间不持有数据库锁。"""

    _stale_lease_after = timedelta(minutes=5)

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def claim(self, worker_id: str) -> QualityReviewContext | None:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            await self._recover_stale_jobs(session, now)
            job = await session.scalar(
                select(AsyncQualityJobModel)
                .where(
                    AsyncQualityJobModel.status == AsyncQualityJobStatus.PENDING.value,
                    AsyncQualityJobModel.available_at <= now,
                )
                .order_by(AsyncQualityJobModel.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            job.status = AsyncQualityJobStatus.RUNNING.value
            job.attempts += 1
            job.locked_at = now
            job.locked_by = worker_id
            claimed_job = AsyncQualityJob(
                job_id=job.job_id,
                conversation_id=job.conversation_id,
                turn_id=job.turn_id,
                run_id=job.run_id,
                trace_id=job.trace_id,
                status=AsyncQualityJobStatus.RUNNING,
                attempts=job.attempts,
                created_at=job.created_at,
            )
        try:
            async with self._session_factory() as session:
                return await self._load_context(session, claimed_job)
        except (KeyError, TypeError, ValueError):
            await self.fail(claimed_job, "SOURCE_CONTEXT_UNAVAILABLE")
            raise

    async def _recover_stale_jobs(self, session: AsyncSession, now: datetime) -> None:
        """回收已失去工作进程的任务，避免任务永久停在 RUNNING。"""

        stale_before = now - self._stale_lease_after
        await session.execute(
            update(AsyncQualityJobModel)
            .where(
                AsyncQualityJobModel.status == AsyncQualityJobStatus.RUNNING.value,
                AsyncQualityJobModel.locked_at < stale_before,
                AsyncQualityJobModel.attempts < AsyncQualityJobModel.max_attempts,
            )
            .values(
                status=AsyncQualityJobStatus.PENDING.value,
                available_at=now,
                locked_at=None,
                locked_by=None,
                last_error_code="WORKER_LEASE_EXPIRED",
            )
        )
        await session.execute(
            update(AsyncQualityJobModel)
            .where(
                AsyncQualityJobModel.status == AsyncQualityJobStatus.RUNNING.value,
                AsyncQualityJobModel.locked_at < stale_before,
                AsyncQualityJobModel.attempts >= AsyncQualityJobModel.max_attempts,
            )
            .values(
                status=AsyncQualityJobStatus.FAILED.value,
                finished_at=now,
                locked_at=None,
                locked_by=None,
                last_error_code="WORKER_LEASE_EXPIRED",
            )
        )

    async def _load_context(
        self,
        session: AsyncSession,
        job: AsyncQualityJob,
    ) -> QualityReviewContext:
        turn = await session.get(TurnRecordModel, job.turn_id)
        run = await session.get(GraphRunRecordModel, job.run_id)
        if turn is None or run is None:
            raise ValueError("quality job source run is missing")
        customer = await session.get(ChatHistoryMessageModel, turn.user_message_id)
        assistant = await session.scalar(
            select(ChatHistoryMessageModel)
            .where(
                ChatHistoryMessageModel.turn_id == job.turn_id,
                ChatHistoryMessageModel.role.in_(["ASSISTANT", "AGENT"]),
            )
            .order_by(ChatHistoryMessageModel.created_at.desc())
            .limit(1)
        )
        recommendation = await session.scalar(
            select(RecommendationRecordModel)
            .where(RecommendationRecordModel.turn_id == job.turn_id)
            .order_by(RecommendationRecordModel.created_at.desc())
            .limit(1)
        )
        if customer is None or assistant is None:
            raise ValueError("quality job source messages are missing")
        answer = assistant.content
        if recommendation is not None and recommendation.status == "DRAFT":
            draft_answer = recommendation.payload.get("answer")
            if isinstance(draft_answer, str) and draft_answer.strip():
                answer = draft_answer
        return QualityReviewContext(
            job=job,
            customer_content=customer.content,
            assistant_content=answer,
            response_path=turn.terminal_response_path,
            reason_code=turn.terminal_reason_code,
            source_refs=(
                f"message:{customer.message_id}",
                f"message:{assistant.message_id}",
                f"turn:{turn.turn_id}",
                f"run:{run.run_id}",
            ),
        )

    async def complete(
        self,
        *,
        result: AsyncQualityResult,
        comparison: ShadowComparison | None,
        candidate: QualitySampleCandidate | None,
    ) -> None:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            job = await session.scalar(
                select(AsyncQualityJobModel)
                .where(AsyncQualityJobModel.job_id == result.job_id)
                .with_for_update()
            )
            if job is None or job.status != AsyncQualityJobStatus.RUNNING.value:
                raise ValueError("quality job is no longer running")
            session.add(
                AsyncQualityResultModel(
                    result_id=result.result_id,
                    job_id=result.job_id,
                    trace_id=result.trace_id,
                    action=result.action.value,
                    findings=[item.model_dump(mode="json") for item in result.findings],
                    evaluator=result.evaluator,
                    evaluator_version=result.evaluator_version,
                    created_at=result.created_at,
                )
            )
            if comparison is not None:
                session.add(
                    ShadowComparisonModel(
                        comparison_id=comparison.comparison_id,
                        job_id=comparison.job_id,
                        trace_id=comparison.trace_id,
                        active_evaluator=comparison.active_evaluator,
                        shadow_evaluator=comparison.shadow_evaluator,
                        active_action=comparison.active_action.value,
                        shadow_action=(
                            comparison.shadow_action.value if comparison.shadow_action else None
                        ),
                        status=comparison.status.value,
                        shadow_signals=(
                            comparison.shadow_signals.model_dump(mode="json")
                            if comparison.shadow_signals
                            else None
                        ),
                        latency_ms=comparison.latency_ms,
                        error_code=comparison.error_code,
                        created_at=comparison.created_at,
                    )
                )
            if candidate is not None:
                await observe_candidate(
                    session,
                    CandidateObservation(
                        candidate_type=candidate.candidate_type,
                        subject_hash=candidate.content_hashes["customer"],
                        source_type=SampleCandidateSourceType.QUALITY_JOB,
                        source_id=candidate.job_id or candidate.candidate_id,
                        trace_id=candidate.trace_id,
                        reason_codes=candidate.reason_codes,
                        source_refs=candidate.source_refs,
                        content_hashes=candidate.content_hashes,
                        observed_outcome=candidate.observed_outcome,
                        occurred_at=candidate.created_at,
                        job_id=candidate.job_id,
                    ),
                )
            job.status = AsyncQualityJobStatus.COMPLETED.value
            job.finished_at = now
            job.locked_at = None
            job.locked_by = None

    async def fail(self, job: AsyncQualityJob, error_code: str) -> None:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            model = await session.scalar(
                select(AsyncQualityJobModel)
                .where(AsyncQualityJobModel.job_id == job.job_id)
                .with_for_update()
            )
            if model is None or model.status != AsyncQualityJobStatus.RUNNING.value:
                return
            model.last_error_code = error_code[:64]
            model.locked_at = None
            model.locked_by = None
            if model.attempts >= model.max_attempts:
                model.status = AsyncQualityJobStatus.FAILED.value
                model.finished_at = now
            else:
                model.status = AsyncQualityJobStatus.PENDING.value
                model.available_at = now + timedelta(seconds=min(60, 2**model.attempts))

    async def counts(self, since: datetime) -> dict[str, int]:
        """返回运营面板所需计数，不读取消息正文。"""

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(AsyncQualityJobModel.status, func.count())
                    .where(AsyncQualityJobModel.created_at >= since)
                    .group_by(AsyncQualityJobModel.status)
                )
            ).all()
            comparisons = await session.scalar(
                select(func.count(ShadowComparisonModel.comparison_id)).where(
                    ShadowComparisonModel.created_at >= since
                )
            )
            disagreements = await session.scalar(
                select(func.count(ShadowComparisonModel.comparison_id)).where(
                    ShadowComparisonModel.created_at >= since,
                    ShadowComparisonModel.status == "DISAGREED",
                )
            )
            candidates = await session.scalar(
                select(func.count(QualitySampleCandidateModel.candidate_id)).where(
                    QualitySampleCandidateModel.created_at >= since,
                    QualitySampleCandidateModel.status == "PENDING_REVIEW",
                )
            )
        counts = {status: count for status, count in rows}
        return {
            **counts,
            "shadow_comparisons": int(comparisons or 0),
            "shadow_disagreements": int(disagreements or 0),
            "pending_candidates": int(candidates or 0),
        }
