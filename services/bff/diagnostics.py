"""授权读取 Trace 明细和运营汇总；诊断接口不返回客户正文。"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.bff.errors import AppError
from services.bff.infrastructure.models import (
    GraphRunRecordModel,
    ModelCallRecordModel,
    TraceSpanModel,
    TurnRecordModel,
)
from services.bff.security import AuthPrincipal, ObjectAuthorizationPort
from services.quality.models import (
    AsyncQualityJobModel,
    QualitySampleCandidateModel,
    ShadowComparisonModel,
)
from shared.contracts.observability import (
    ToolCallTrace,
    TraceDetail,
    TraceOperationsSummary,
    TraceSpan,
)
from shared.contracts.quality import QualityOperationsSummary
from shared.contracts.runtime import DecisionTrace, ModelCallRecord


def _percentile(values: list[int], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = max(0, math.ceil(len(ordered) * percentile) - 1)
    return float(ordered[position])


class DiagnosticsService:
    """以应用授权边界读取诊断事实，避免凭 trace_id 越权查看其他客户。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        object_authorizer: ObjectAuthorizationPort,
    ) -> None:
        self._session_factory = session_factory
        self._object_authorizer = object_authorizer

    async def trace_detail(self, principal: AuthPrincipal, trace_id: str) -> TraceDetail:
        async with self._session_factory() as session:
            turn = await session.scalar(
                select(TurnRecordModel).where(TurnRecordModel.trace_id == trace_id)
            )
            if turn is None or turn.active_run_id is None:
                raise AppError(code="TRACE_NOT_FOUND", message="追踪记录不存在", status_code=404)
            if not await self._object_authorizer.can_access_conversation(
                principal.subject_id, turn.conversation_id
            ):
                raise AppError(
                    code="OBJECT_FORBIDDEN",
                    message="无权查看该追踪记录",
                    status_code=403,
                )
            run = await session.get(GraphRunRecordModel, turn.active_run_id)
            if run is None:
                raise AppError(code="TRACE_NOT_FOUND", message="追踪记录不存在", status_code=404)
            span_models = tuple(
                (
                    await session.scalars(
                        select(TraceSpanModel)
                        .where(TraceSpanModel.trace_id == trace_id)
                        .order_by(TraceSpanModel.started_at, TraceSpanModel.span_id)
                    )
                ).all()
            )
            call_models = tuple(
                (
                    await session.scalars(
                        select(ModelCallRecordModel)
                        .where(ModelCallRecordModel.trace_id == trace_id)
                        .order_by(ModelCallRecordModel.created_at)
                    )
                ).all()
            )
        traces = tuple(
            DecisionTrace.model_validate(value)
            for value in (run.decision_trace, run.routing_trace)
            if value
        )
        return TraceDetail(
            trace_id=trace_id,
            conversation_id=turn.conversation_id,
            turn_id=turn.turn_id,
            run_id=run.run_id,
            terminal_response_path=turn.terminal_response_path,
            terminal_reason_code=turn.terminal_reason_code,
            spans=tuple(
                TraceSpan.model_validate(
                    {
                        column.name: getattr(item, column.name)
                        for column in TraceSpanModel.__table__.columns
                    }
                )
                for item in span_models
            ),
            decision_traces=traces,
            model_calls=tuple(
                ModelCallRecord.model_validate(
                    {
                        column.name: getattr(item, column.name)
                        for column in ModelCallRecordModel.__table__.columns
                    }
                )
                for item in call_models
            ),
            tool_calls=tuple(
                ToolCallTrace(
                    tool_call_id=str(item["tool_call_id"]),
                    tool_name=str(item["tool_name"]),
                    status=str(item["status"]),
                    latency_ms=int(item.get("latency_ms") or 0),
                    error_code=(str(item["error_code"]) if item.get("error_code") else None),
                )
                for item in (run.business_tool_results or [])
            ),
        )

    async def trace_summary(self, window_hours: int) -> TraceOperationsSummary:
        since = datetime.now(UTC) - timedelta(hours=window_hours)
        async with self._session_factory() as session:
            spans = tuple(
                (
                    await session.scalars(
                        select(TraceSpanModel).where(TraceSpanModel.started_at >= since)
                    )
                ).all()
            )
        counts: dict[str, int] = {}
        for span in spans:
            counts[span.component] = counts.get(span.component, 0) + 1
        latencies = [span.latency_ms for span in spans]
        return TraceOperationsSummary(
            window_hours=window_hours,
            operation_count=len(spans),
            failed_count=sum(span.status == "FAILED" for span in spans),
            p50_latency_ms=_percentile(latencies, 0.50),
            p95_latency_ms=_percentile(latencies, 0.95),
            by_component=counts,
        )

    async def quality_summary(self, window_hours: int) -> QualityOperationsSummary:
        since = datetime.now(UTC) - timedelta(hours=window_hours)
        async with self._session_factory() as session:
            job_rows = (
                await session.execute(
                    select(AsyncQualityJobModel.status, func.count())
                    .where(AsyncQualityJobModel.created_at >= since)
                    .group_by(AsyncQualityJobModel.status)
                )
            ).all()
            shadow_count = await session.scalar(
                select(func.count(ShadowComparisonModel.comparison_id)).where(
                    ShadowComparisonModel.created_at >= since
                )
            )
            disagreement_count = await session.scalar(
                select(func.count(ShadowComparisonModel.comparison_id)).where(
                    ShadowComparisonModel.created_at >= since,
                    ShadowComparisonModel.status == "DISAGREED",
                )
            )
            candidate_count = await session.scalar(
                select(func.count(QualitySampleCandidateModel.candidate_id)).where(
                    QualitySampleCandidateModel.created_at >= since,
                    QualitySampleCandidateModel.status == "PENDING_REVIEW",
                )
            )
            candidate_reason_rows = tuple(
                (
                    await session.scalars(
                        select(QualitySampleCandidateModel.reason_codes).where(
                            QualitySampleCandidateModel.created_at >= since
                        )
                    )
                ).all()
            )
        jobs = {status: int(count) for status, count in job_rows}
        reason_code_counts: dict[str, int] = {}
        for reason_codes in candidate_reason_rows:
            for reason_code in reason_codes:
                reason_code_counts[reason_code] = reason_code_counts.get(reason_code, 0) + 1
        return QualityOperationsSummary(
            window_hours=window_hours,
            pending_jobs=jobs.get("PENDING", 0),
            running_jobs=jobs.get("RUNNING", 0),
            completed_jobs=jobs.get("COMPLETED", 0),
            failed_jobs=jobs.get("FAILED", 0),
            shadow_comparisons=int(shadow_count or 0),
            shadow_disagreements=int(disagreement_count or 0),
            pending_candidates=int(candidate_count or 0),
            reason_code_counts=reason_code_counts,
        )
