"""Quality schema 的异步任务、结果、Shadow 对照和样本候选模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from services.bff.infrastructure.models import Base


class AsyncQualityJobModel(Base):
    """回复落库后创建的异步质检任务。"""

    __tablename__ = "async_quality_job"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')",
            name="status",
        ),
        CheckConstraint("attempts >= 0 AND max_attempts > 0", name="attempts"),
        Index("ix_async_quality_job_poll", "status", "available_at", "created_at"),
        Index("ix_async_quality_job_trace", "trace_id"),
        {
            "schema": "quality",
            "comment": "回复发送后的异步质检任务；只保存应用记录引用，不复制消息正文。",
        },
    )

    job_id: Mapped[str] = mapped_column(String(96), primary_key=True, comment="质检任务编号。")
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属业务会话编号。",
    )
    turn_id: Mapped[str] = mapped_column(
        ForeignKey("app.turn_record.turn_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属客户轮次编号。",
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("app.graph_run_record.run_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        comment="被复核的图运行编号；同一 Run 只创建一个任务。",
    )
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, comment="端到端追踪编号。")
    status: Mapped[str] = mapped_column(String(16), nullable=False, comment="任务执行状态。")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, comment="已经领取的次数。")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, comment="最大尝试次数。")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="任务允许再次领取的时间。"
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="当前工作者领取时间。"
    )
    locked_by: Mapped[str | None] = mapped_column(String(64), comment="当前工作者标识。")
    last_error_code: Mapped[str | None] = mapped_column(String(64), comment="最近一次失败原因码。")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="任务创建时间。"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="任务最终完成或失败时间。"
    )


class AsyncQualityResultModel(Base):
    """active 规则复核产生的不可变质量结论。"""

    __tablename__ = "async_quality_result"
    __table_args__ = (
        CheckConstraint(
            "action IN ('PASS', 'BLOCK', 'REWRITE', 'CLARIFY', 'HANDOFF')",
            name="action",
        ),
        Index("ix_async_quality_result_trace", "trace_id", "created_at"),
        {
            "schema": "quality",
            "comment": "异步 active 质量复核结果；不得反向改写客户已收到的回复。",
        },
    )

    result_id: Mapped[str] = mapped_column(String(96), primary_key=True, comment="质检结果编号。")
    job_id: Mapped[str] = mapped_column(
        ForeignKey("quality.async_quality_job.job_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        comment="所属质检任务编号。",
    )
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, comment="端到端追踪编号。")
    action: Mapped[str] = mapped_column(String(16), nullable=False, comment="active 规则复核动作。")
    findings: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, comment="结构化质量发现，不包含消息正文。"
    )
    evaluator: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="active 评估器名称。"
    )
    evaluator_version: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="active 规则或模型版本。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="结果形成时间。"
    )


class ShadowComparisonModel(Base):
    """active 与 shadow 结果的旁路比较。"""

    __tablename__ = "shadow_comparison"
    __table_args__ = (
        CheckConstraint(
            "status IN ('AGREED', 'DISAGREED', 'SHADOW_FAILED')",
            name="status",
        ),
        CheckConstraint(
            "(status = 'SHADOW_FAILED') = (error_code IS NOT NULL)",
            name="failure_pair",
        ),
        Index("ix_shadow_comparison_trace", "trace_id", "created_at"),
        {"schema": "quality", "comment": "active 与 shadow 评估器的异步对照记录。"},
    )

    comparison_id: Mapped[str] = mapped_column(
        String(96), primary_key=True, comment="对照记录编号。"
    )
    job_id: Mapped[str] = mapped_column(
        ForeignKey("quality.async_quality_job.job_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        comment="所属质检任务编号。",
    )
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, comment="端到端追踪编号。")
    active_evaluator: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="影响 active 质检结论的评估器。"
    )
    shadow_evaluator: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="只做旁路评估的候选评估器。"
    )
    active_action: Mapped[str] = mapped_column(String(16), nullable=False, comment="active 动作。")
    shadow_action: Mapped[str | None] = mapped_column(String(16), comment="shadow 建议动作。")
    status: Mapped[str] = mapped_column(String(24), nullable=False, comment="一致、分歧或失败。")
    shadow_signals: Mapped[dict[str, object] | None] = mapped_column(
        JSONB, comment="shadow 的结构化原子信号。"
    )
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, comment="shadow 耗时。")
    error_code: Mapped[str | None] = mapped_column(String(64), comment="shadow 失败原因码。")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="对照形成时间。"
    )


class QualitySampleCandidateModel(Base):
    """质量问题或分歧形成的待审核样本引用。"""

    __tablename__ = "sample_candidate"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING_REVIEW', 'APPROVED', 'REJECTED')",
            name="status",
        ),
        CheckConstraint(
            "candidate_type IN ('INTENT_EXPRESSION', 'FAQ_GAP', 'RESPONSE_QUALITY', "
            "'PROCESS_GAP', 'SAFETY_REVIEW')",
            name="candidate_type",
        ),
        CheckConstraint(
            "conflict_status IN ('CLEAR', 'POTENTIAL_CONFLICT')",
            name="conflict_status",
        ),
        CheckConstraint("occurrence_count > 0 AND version > 0", name="counters"),
        Index("uq_quality_sample_candidate_fingerprint", "fingerprint", unique=True),
        Index("ix_quality_sample_candidate_status", "status", "created_at"),
        {
            "schema": "quality",
            "comment": "质检发现的待人工审核样本；不会自动进入规则、Prompt 或训练数据。",
        },
    )

    candidate_id: Mapped[str] = mapped_column(
        String(96), primary_key=True, comment="样本候选编号。"
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("quality.async_quality_job.job_id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
        comment="首次来源质检任务编号；反馈或小结候选可以为空。",
    )
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, comment="端到端追踪编号。")
    candidate_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="意图表达、FAQ 缺口、回复质量、流程缺口或安全复核。"
    )
    fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="候选类型与脱敏主体哈希形成的稳定去重键。"
    )
    observed_outcome: Mapped[str | None] = mapped_column(
        String(128), comment="产生本候选时观察到的路径或处理结果。"
    )
    conflict_status: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="多次观测是否出现潜在冲突。"
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="人工审核状态；默认 PENDING_REVIEW。"
    )
    reason_codes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, comment="触发候选发现的原因码。"
    )
    source_refs: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, comment="指向 Turn、Run、消息或推荐的稳定引用。"
    )
    content_hashes: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, comment="原始内容哈希，用于去重而不复制正文。"
    )
    occurrence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="去重后累计的独立证据次数。"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="候选证据或冲突状态变化时递增。"
    )
    reviewer_id: Mapped[str | None] = mapped_column(String(64), comment="人工审核者编号。")
    review_note: Mapped[str | None] = mapped_column(Text, comment="人工审核说明。")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="候选创建时间。"
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="人工审核时间。"
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="最近一条独立证据的发生时间。"
    )


class QualityCandidateEvidenceModel(Base):
    """候选的一次来源观测，正文仍由原业务表持有。"""

    __tablename__ = "candidate_evidence"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('QUALITY_JOB', 'RECOMMENDATION_FEEDBACK', 'CONVERSATION_WRAP_UP')",
            name="source_type",
        ),
        Index(
            "uq_quality_candidate_evidence_source",
            "source_type",
            "source_id",
            unique=True,
        ),
        Index("ix_quality_candidate_evidence_candidate", "candidate_id", "occurred_at"),
        {
            "schema": "quality",
            "comment": "候选来源证据；只保存稳定引用、原因码和内容哈希。",
        },
    )

    evidence_id: Mapped[str] = mapped_column(String(96), primary_key=True, comment="候选证据编号。")
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("quality.sample_candidate.candidate_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属治理候选编号。",
    )
    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="质检任务、推荐反馈或确认小结。"
    )
    source_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="来源业务事实的稳定编号。"
    )
    observed_outcome: Mapped[str | None] = mapped_column(
        String(128), comment="本次来源观察到的路径或处理结果。"
    )
    reason_codes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, comment="本次来源触发候选的原因码。"
    )
    source_refs: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, comment="回查原业务事实所需的稳定引用。"
    )
    content_hashes: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, comment="来源正文哈希；不复制正文。"
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="来源事实发生时间。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="证据记录创建时间。"
    )


class QualityCandidateReviewEventModel(Base):
    """人工审核候选的追加写审计事件。"""

    __tablename__ = "candidate_review_event"
    __table_args__ = (
        CheckConstraint("decision IN ('APPROVE', 'REJECT')", name="decision"),
        Index("ix_quality_candidate_review_candidate", "candidate_id", "created_at"),
        {"schema": "quality", "comment": "候选审核的不可变审计事件。"},
    )

    review_id: Mapped[str] = mapped_column(String(96), primary_key=True, comment="审核事件编号。")
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("quality.sample_candidate.candidate_id", ondelete="RESTRICT"),
        nullable=False,
        comment="被审核的治理候选编号。",
    )
    candidate_version: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="审核时看到的候选版本。"
    )
    decision: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="APPROVE=批准；REJECT=拒绝。"
    )
    reviewer_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="执行审核的管理员主体编号。"
    )
    note: Mapped[str] = mapped_column(Text, nullable=False, comment="审核依据和后续处理说明。")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="审核事件创建时间。"
    )


class QualityCandidateReleaseLinkModel(Base):
    """已批准候选与人工发布资产之间的追加写关联。"""

    __tablename__ = "candidate_release_link"
    __table_args__ = (
        Index(
            "uq_quality_candidate_release_target",
            "candidate_id",
            "target_type",
            "target_ref",
            "target_version",
            unique=True,
        ),
        Index("ix_quality_candidate_release_candidate", "candidate_id", "created_at"),
        {
            "schema": "quality",
            "comment": "候选被后续版本吸收的审计关联；本表不执行内容发布。",
        },
    )

    release_link_id: Mapped[str] = mapped_column(
        String(96), primary_key=True, comment="发布关联编号。"
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("quality.sample_candidate.candidate_id", ondelete="RESTRICT"),
        nullable=False,
        comment="已获人工批准的治理候选编号。",
    )
    candidate_version: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="建立关联时的候选版本。"
    )
    target_type: Mapped[str] = mapped_column(
        String(48), nullable=False, comment="后续发布资产类型。"
    )
    target_ref: Mapped[str] = mapped_column(
        String(160), nullable=False, comment="后续发布资产稳定引用。"
    )
    target_version: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="后续发布资产版本。"
    )
    linked_by: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="登记发布关联的管理员主体编号。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="发布关联登记时间。"
    )
