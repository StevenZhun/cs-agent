"""把运行信号收敛为可去重、可审核、可追溯的治理候选。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.bff.errors import AppError
from services.quality.models import (
    QualityCandidateEvidenceModel,
    QualityCandidateReleaseLinkModel,
    QualityCandidateReviewEventModel,
    QualitySampleCandidateModel,
)
from shared.contracts.enums import (
    SampleCandidateConflictStatus,
    SampleCandidateReviewDecision,
    SampleCandidateSourceType,
    SampleCandidateStatus,
    SampleCandidateType,
)
from shared.contracts.quality import (
    QualityCandidateDetail,
    QualityCandidateEvidence,
    QualityCandidateReleaseLink,
    QualityCandidateReviewEvent,
    QualitySampleCandidate,
    RegisterCandidateReleaseRequest,
    ReviewQualityCandidateRequest,
)


@dataclass(frozen=True)
class CandidateObservation:
    """一次不含正文的候选观测；原始事实继续由来源业务表持有。"""

    candidate_type: SampleCandidateType
    subject_hash: str
    source_type: SampleCandidateSourceType
    source_id: str
    trace_id: str
    reason_codes: tuple[str, ...]
    source_refs: tuple[str, ...]
    content_hashes: dict[str, str]
    observed_outcome: str | None
    occurred_at: datetime
    job_id: str | None = None


def stable_hash(payload: object) -> str:
    """对结构化信号生成稳定哈希，既可去重又不复制客户正文。"""

    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def text_hash(content: str) -> str:
    """统一正文哈希口径，使不同来源能围绕同一条内容聚合。"""

    return hashlib.sha256(content.encode()).hexdigest()


def candidate_fingerprint(candidate_type: SampleCandidateType, subject_hash: str) -> str:
    """同类候选围绕同一脱敏主体聚合，不受某次任务编号影响。"""

    return stable_hash({"candidate_type": candidate_type.value, "subject_hash": subject_hash})


def _candidate_contract(row: QualitySampleCandidateModel) -> QualitySampleCandidate:
    return QualitySampleCandidate(
        candidate_id=row.candidate_id,
        job_id=row.job_id,
        trace_id=row.trace_id,
        candidate_type=SampleCandidateType(row.candidate_type),
        fingerprint=row.fingerprint,
        observed_outcome=row.observed_outcome,
        conflict_status=SampleCandidateConflictStatus(row.conflict_status),
        status=SampleCandidateStatus(row.status),
        reason_codes=tuple(row.reason_codes),
        source_refs=tuple(row.source_refs),
        content_hashes=dict(row.content_hashes),
        occurrence_count=row.occurrence_count,
        version=row.version,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
    )


def _evidence_contract(row: QualityCandidateEvidenceModel) -> QualityCandidateEvidence:
    return QualityCandidateEvidence(
        evidence_id=row.evidence_id,
        candidate_id=row.candidate_id,
        source_type=SampleCandidateSourceType(row.source_type),
        source_id=row.source_id,
        observed_outcome=row.observed_outcome,
        reason_codes=tuple(row.reason_codes),
        source_refs=tuple(row.source_refs),
        content_hashes=dict(row.content_hashes),
        occurred_at=row.occurred_at,
        created_at=row.created_at,
    )


def _review_contract(row: QualityCandidateReviewEventModel) -> QualityCandidateReviewEvent:
    return QualityCandidateReviewEvent(
        review_id=row.review_id,
        candidate_id=row.candidate_id,
        candidate_version=row.candidate_version,
        decision=SampleCandidateReviewDecision(row.decision),
        reviewer_id=row.reviewer_id,
        note=row.note,
        created_at=row.created_at,
    )


def _release_contract(row: QualityCandidateReleaseLinkModel) -> QualityCandidateReleaseLink:
    return QualityCandidateReleaseLink(
        release_link_id=row.release_link_id,
        candidate_id=row.candidate_id,
        candidate_version=row.candidate_version,
        target_type=row.target_type,
        target_ref=row.target_ref,
        target_version=row.target_version,
        linked_by=row.linked_by,
        created_at=row.created_at,
    )


async def observe_candidate(
    session: AsyncSession,
    observation: CandidateObservation,
) -> QualitySampleCandidate:
    """原子地去重来源证据，并在结果互相矛盾时重新交给人工复核。"""

    now = datetime.now(UTC)
    fingerprint = candidate_fingerprint(observation.candidate_type, observation.subject_hash)
    candidate_id = f"qsc_{fingerprint[:32]}"
    inserted_candidate = await session.scalar(
        insert(QualitySampleCandidateModel)
        .values(
            candidate_id=candidate_id,
            job_id=observation.job_id,
            trace_id=observation.trace_id,
            candidate_type=observation.candidate_type.value,
            fingerprint=fingerprint,
            observed_outcome=observation.observed_outcome,
            conflict_status=SampleCandidateConflictStatus.CLEAR.value,
            status=SampleCandidateStatus.PENDING_REVIEW.value,
            reason_codes=list(dict.fromkeys(observation.reason_codes)),
            source_refs=list(dict.fromkeys(observation.source_refs)),
            content_hashes=dict(observation.content_hashes),
            occurrence_count=1,
            version=1,
            created_at=now,
            last_seen_at=observation.occurred_at,
        )
        .on_conflict_do_nothing(index_elements=["fingerprint"])
        .returning(QualitySampleCandidateModel.candidate_id)
    )
    row = await session.scalar(
        select(QualitySampleCandidateModel)
        .where(QualitySampleCandidateModel.fingerprint == fingerprint)
        .with_for_update()
    )
    if row is None:
        raise RuntimeError("candidate upsert did not return a row")

    evidence_key = f"{observation.source_type.value}:{observation.source_id}"
    evidence_id = f"qce_{uuid5(NAMESPACE_URL, evidence_key).hex}"
    inserted_evidence = await session.scalar(
        insert(QualityCandidateEvidenceModel)
        .values(
            evidence_id=evidence_id,
            candidate_id=row.candidate_id,
            source_type=observation.source_type.value,
            source_id=observation.source_id,
            observed_outcome=observation.observed_outcome,
            reason_codes=list(dict.fromkeys(observation.reason_codes)),
            source_refs=list(dict.fromkeys(observation.source_refs)),
            content_hashes=dict(observation.content_hashes),
            occurred_at=observation.occurred_at,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=["source_type", "source_id"])
        .returning(QualityCandidateEvidenceModel.evidence_id)
    )
    if inserted_candidate is None and inserted_evidence is not None:
        conflicts = bool(
            row.observed_outcome
            and observation.observed_outcome
            and row.observed_outcome != observation.observed_outcome
        )
        row.reason_codes = list(dict.fromkeys([*row.reason_codes, *observation.reason_codes]))
        row.occurrence_count += 1
        row.version += 1
        row.last_seen_at = max(row.last_seen_at, observation.occurred_at)
        if conflicts:
            row.conflict_status = SampleCandidateConflictStatus.POTENTIAL_CONFLICT.value
            row.status = SampleCandidateStatus.PENDING_REVIEW.value
            row.reviewer_id = None
            row.review_note = None
            row.reviewed_at = None
    return _candidate_contract(row)


class CandidateGovernanceService:
    """提供候选查询、人工审核和发布关联，不承担规则或内容发布。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def list_candidates(
        self,
        *,
        status: SampleCandidateStatus | None,
        limit: int,
    ) -> tuple[QualitySampleCandidate, ...]:
        async with self._sessions() as session:
            statement = select(QualitySampleCandidateModel)
            if status is not None:
                statement = statement.where(QualitySampleCandidateModel.status == status.value)
            rows = (
                await session.scalars(
                    statement.order_by(
                        QualitySampleCandidateModel.last_seen_at.desc(),
                        QualitySampleCandidateModel.candidate_id,
                    ).limit(limit)
                )
            ).all()
        return tuple(_candidate_contract(row) for row in rows)

    async def detail(self, candidate_id: str) -> QualityCandidateDetail:
        async with self._sessions() as session:
            candidate = await session.get(QualitySampleCandidateModel, candidate_id)
            if candidate is None:
                raise AppError(
                    code="QUALITY_CANDIDATE_NOT_FOUND",
                    message="治理候选不存在",
                    status_code=404,
                )
            evidence = (
                await session.scalars(
                    select(QualityCandidateEvidenceModel)
                    .where(QualityCandidateEvidenceModel.candidate_id == candidate_id)
                    .order_by(QualityCandidateEvidenceModel.occurred_at)
                )
            ).all()
            reviews = (
                await session.scalars(
                    select(QualityCandidateReviewEventModel)
                    .where(QualityCandidateReviewEventModel.candidate_id == candidate_id)
                    .order_by(QualityCandidateReviewEventModel.created_at)
                )
            ).all()
            release_links = (
                await session.scalars(
                    select(QualityCandidateReleaseLinkModel)
                    .where(QualityCandidateReleaseLinkModel.candidate_id == candidate_id)
                    .order_by(QualityCandidateReleaseLinkModel.created_at)
                )
            ).all()
        return QualityCandidateDetail(
            candidate=_candidate_contract(candidate),
            evidence=tuple(_evidence_contract(row) for row in evidence),
            reviews=tuple(_review_contract(row) for row in reviews),
            release_links=tuple(_release_contract(row) for row in release_links),
        )

    async def review(
        self,
        candidate_id: str,
        request: ReviewQualityCandidateRequest,
        reviewer_id: str,
    ) -> QualityCandidateDetail:
        async with self._sessions() as session, session.begin():
            candidate = await session.scalar(
                select(QualitySampleCandidateModel)
                .where(QualitySampleCandidateModel.candidate_id == candidate_id)
                .with_for_update()
            )
            if candidate is None:
                raise AppError(
                    code="QUALITY_CANDIDATE_NOT_FOUND",
                    message="治理候选不存在",
                    status_code=404,
                )
            if candidate.version != request.expected_version:
                raise AppError(
                    code="QUALITY_CANDIDATE_VERSION_CONFLICT",
                    message="候选已出现新证据，请刷新后重新审核",
                    status_code=409,
                )
            if candidate.status != SampleCandidateStatus.PENDING_REVIEW.value:
                raise AppError(
                    code="QUALITY_CANDIDATE_ALREADY_REVIEWED",
                    message="治理候选已经审核",
                    status_code=409,
                )
            now = datetime.now(UTC)
            reviewed_version = candidate.version
            candidate.status = (
                SampleCandidateStatus.APPROVED.value
                if request.decision is SampleCandidateReviewDecision.APPROVE
                else SampleCandidateStatus.REJECTED.value
            )
            candidate.reviewer_id = reviewer_id
            candidate.review_note = request.note
            candidate.reviewed_at = now
            candidate.version += 1
            review_key = self._review_key(candidate_id, reviewed_version, request)
            session.add(
                QualityCandidateReviewEventModel(
                    review_id=f"qcr_{uuid5(NAMESPACE_URL, review_key).hex}",
                    candidate_id=candidate_id,
                    candidate_version=reviewed_version,
                    decision=request.decision.value,
                    reviewer_id=reviewer_id,
                    note=request.note,
                    created_at=now,
                )
            )
        return await self.detail(candidate_id)

    async def register_release(
        self,
        candidate_id: str,
        request: RegisterCandidateReleaseRequest,
        linked_by: str,
    ) -> QualityCandidateReleaseLink:
        async with self._sessions() as session, session.begin():
            candidate = await session.scalar(
                select(QualitySampleCandidateModel)
                .where(QualitySampleCandidateModel.candidate_id == candidate_id)
                .with_for_update()
            )
            if candidate is None:
                raise AppError(
                    code="QUALITY_CANDIDATE_NOT_FOUND",
                    message="治理候选不存在",
                    status_code=404,
                )
            if candidate.version != request.expected_version:
                raise AppError(
                    code="QUALITY_CANDIDATE_VERSION_CONFLICT",
                    message="候选版本已变化，请刷新后重试",
                    status_code=409,
                )
            if candidate.status != SampleCandidateStatus.APPROVED.value:
                raise AppError(
                    code="QUALITY_CANDIDATE_NOT_APPROVED",
                    message="只有人工审核通过的候选才能关联发布版本",
                    status_code=409,
                )
            existing = await session.scalar(
                select(QualityCandidateReleaseLinkModel).where(
                    QualityCandidateReleaseLinkModel.candidate_id == candidate_id,
                    QualityCandidateReleaseLinkModel.target_type == request.target_type,
                    QualityCandidateReleaseLinkModel.target_ref == request.target_ref,
                    QualityCandidateReleaseLinkModel.target_version == request.target_version,
                )
            )
            if existing is not None:
                return _release_contract(existing)
            now = datetime.now(UTC)
            release_key = self._release_key(candidate_id, request)
            row = QualityCandidateReleaseLinkModel(
                release_link_id=f"qcl_{uuid5(NAMESPACE_URL, release_key).hex}",
                candidate_id=candidate_id,
                candidate_version=candidate.version,
                target_type=request.target_type,
                target_ref=request.target_ref,
                target_version=request.target_version,
                linked_by=linked_by,
                created_at=now,
            )
            session.add(row)
            await session.flush()
            return _release_contract(row)

    @staticmethod
    def _review_key(
        candidate_id: str,
        candidate_version: int,
        request: ReviewQualityCandidateRequest,
    ) -> str:
        return f"{candidate_id}:{candidate_version}:{request.decision.value}"

    @staticmethod
    def _release_key(candidate_id: str, request: RegisterCandidateReleaseRequest) -> str:
        return f"{candidate_id}:{request.target_type}:{request.target_ref}:{request.target_version}"
