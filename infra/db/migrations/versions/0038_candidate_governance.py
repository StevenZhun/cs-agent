"""为运行时发现的候选样本增加可审计治理。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0038_candidate_governance"
down_revision: str | None = "0037_rag_review_expiry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "sample_candidate",
        "job_id",
        existing_type=sa.String(length=96),
        nullable=True,
        schema="quality",
    )
    op.add_column(
        "sample_candidate",
        sa.Column(
            "candidate_type",
            sa.String(length=32),
            nullable=False,
            server_default="RESPONSE_QUALITY",
            comment="意图表达、FAQ 缺口、回复质量、流程缺口或安全复核。",
        ),
        schema="quality",
    )
    op.add_column(
        "sample_candidate",
        sa.Column(
            "fingerprint",
            sa.String(length=64),
            nullable=True,
            comment="候选类型与脱敏主体哈希形成的稳定去重键。",
        ),
        schema="quality",
    )
    op.add_column(
        "sample_candidate",
        sa.Column(
            "observed_outcome",
            sa.String(length=128),
            nullable=True,
            comment="产生本候选时观察到的路径或处理结果。",
        ),
        schema="quality",
    )
    op.add_column(
        "sample_candidate",
        sa.Column(
            "conflict_status",
            sa.String(length=24),
            nullable=False,
            server_default="CLEAR",
            comment="多次观测是否出现潜在冲突。",
        ),
        schema="quality",
    )
    op.add_column(
        "sample_candidate",
        sa.Column(
            "occurrence_count",
            sa.Integer(),
            nullable=False,
            server_default="1",
            comment="去重后累计的独立证据次数。",
        ),
        schema="quality",
    )
    op.add_column(
        "sample_candidate",
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default="1",
            comment="候选证据或冲突状态变化时递增。",
        ),
        schema="quality",
    )
    op.add_column(
        "sample_candidate",
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="最近一条独立证据的发生时间。",
        ),
        schema="quality",
    )
    op.execute(
        "UPDATE quality.sample_candidate "
        "SET fingerprint = encode(sha256(candidate_id::bytea), 'hex'), "
        "last_seen_at = created_at"
    )
    op.alter_column(
        "sample_candidate",
        "fingerprint",
        existing_type=sa.String(length=64),
        nullable=False,
        schema="quality",
    )
    op.alter_column(
        "sample_candidate",
        "last_seen_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        schema="quality",
    )
    op.create_check_constraint(
        "ck_sample_candidate_candidate_type",
        "sample_candidate",
        "candidate_type IN ('INTENT_EXPRESSION', 'FAQ_GAP', 'RESPONSE_QUALITY', "
        "'PROCESS_GAP', 'SAFETY_REVIEW')",
        schema="quality",
    )
    op.create_check_constraint(
        "ck_sample_candidate_conflict_status",
        "sample_candidate",
        "conflict_status IN ('CLEAR', 'POTENTIAL_CONFLICT')",
        schema="quality",
    )
    op.create_check_constraint(
        "ck_sample_candidate_counters",
        "sample_candidate",
        "occurrence_count > 0 AND version > 0",
        schema="quality",
    )
    op.create_index(
        "uq_quality_sample_candidate_fingerprint",
        "sample_candidate",
        ["fingerprint"],
        unique=True,
        schema="quality",
    )
    for column in ("candidate_type", "conflict_status", "occurrence_count", "version"):
        op.alter_column(
            "sample_candidate",
            column,
            server_default=None,
            schema="quality",
        )

    op.create_table(
        "candidate_evidence",
        sa.Column("evidence_id", sa.String(length=96), primary_key=True, comment="候选证据编号。"),
        sa.Column(
            "candidate_id",
            sa.String(length=96),
            sa.ForeignKey("quality.sample_candidate.candidate_id", ondelete="RESTRICT"),
            nullable=False,
            comment="所属治理候选编号。",
        ),
        sa.Column(
            "source_type",
            sa.String(length=32),
            nullable=False,
            comment="质检任务、推荐反馈或确认小结。",
        ),
        sa.Column(
            "source_id",
            sa.String(length=128),
            nullable=False,
            comment="来源业务事实的稳定编号。",
        ),
        sa.Column(
            "observed_outcome",
            sa.String(length=128),
            nullable=True,
            comment="本次来源观察到的路径或处理结果。",
        ),
        sa.Column(
            "reason_codes",
            postgresql.JSONB(),
            nullable=False,
            comment="本次来源触发候选的原因码。",
        ),
        sa.Column(
            "source_refs",
            postgresql.JSONB(),
            nullable=False,
            comment="回查原业务事实所需的稳定引用。",
        ),
        sa.Column(
            "content_hashes",
            postgresql.JSONB(),
            nullable=False,
            comment="来源正文哈希；不复制正文。",
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="来源事实发生时间。",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="证据记录创建时间。",
        ),
        sa.CheckConstraint(
            "source_type IN ('QUALITY_JOB', 'RECOMMENDATION_FEEDBACK', 'CONVERSATION_WRAP_UP')",
            name="ck_candidate_evidence_source_type",
        ),
        schema="quality",
        comment="候选来源证据；只保存稳定引用、原因码和内容哈希。",
    )
    op.create_index(
        "uq_quality_candidate_evidence_source",
        "candidate_evidence",
        ["source_type", "source_id"],
        unique=True,
        schema="quality",
    )
    op.create_index(
        "ix_quality_candidate_evidence_candidate",
        "candidate_evidence",
        ["candidate_id", "occurred_at"],
        schema="quality",
    )

    op.create_table(
        "candidate_review_event",
        sa.Column("review_id", sa.String(length=96), primary_key=True, comment="审核事件编号。"),
        sa.Column(
            "candidate_id",
            sa.String(length=96),
            sa.ForeignKey("quality.sample_candidate.candidate_id", ondelete="RESTRICT"),
            nullable=False,
            comment="被审核的治理候选编号。",
        ),
        sa.Column(
            "candidate_version", sa.Integer(), nullable=False, comment="审核时看到的候选版本。"
        ),
        sa.Column(
            "decision",
            sa.String(length=16),
            nullable=False,
            comment="APPROVE=批准；REJECT=拒绝。",
        ),
        sa.Column(
            "reviewer_id",
            sa.String(length=64),
            nullable=False,
            comment="执行审核的管理员主体编号。",
        ),
        sa.Column("note", sa.Text(), nullable=False, comment="审核依据和后续处理说明。"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="审核事件创建时间。",
        ),
        sa.CheckConstraint(
            "decision IN ('APPROVE', 'REJECT')", name="ck_candidate_review_event_decision"
        ),
        schema="quality",
        comment="候选审核的不可变审计事件。",
    )
    op.create_index(
        "ix_quality_candidate_review_candidate",
        "candidate_review_event",
        ["candidate_id", "created_at"],
        schema="quality",
    )

    op.create_table(
        "candidate_release_link",
        sa.Column(
            "release_link_id", sa.String(length=96), primary_key=True, comment="发布关联编号。"
        ),
        sa.Column(
            "candidate_id",
            sa.String(length=96),
            sa.ForeignKey("quality.sample_candidate.candidate_id", ondelete="RESTRICT"),
            nullable=False,
            comment="已获人工批准的治理候选编号。",
        ),
        sa.Column(
            "candidate_version",
            sa.Integer(),
            nullable=False,
            comment="建立关联时的候选版本。",
        ),
        sa.Column(
            "target_type", sa.String(length=48), nullable=False, comment="后续发布资产类型。"
        ),
        sa.Column(
            "target_ref", sa.String(length=160), nullable=False, comment="后续发布资产稳定引用。"
        ),
        sa.Column(
            "target_version", sa.String(length=64), nullable=False, comment="后续发布资产版本。"
        ),
        sa.Column(
            "linked_by",
            sa.String(length=64),
            nullable=False,
            comment="登记发布关联的管理员主体编号。",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="发布关联登记时间。",
        ),
        schema="quality",
        comment="候选被后续版本吸收的审计关联；本表不执行内容发布。",
    )
    op.create_index(
        "uq_quality_candidate_release_target",
        "candidate_release_link",
        ["candidate_id", "target_type", "target_ref", "target_version"],
        unique=True,
        schema="quality",
    )
    op.create_index(
        "ix_quality_candidate_release_candidate",
        "candidate_release_link",
        ["candidate_id", "created_at"],
        schema="quality",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_quality_candidate_release_candidate",
        table_name="candidate_release_link",
        schema="quality",
    )
    op.drop_index(
        "uq_quality_candidate_release_target",
        table_name="candidate_release_link",
        schema="quality",
    )
    op.drop_table("candidate_release_link", schema="quality")
    op.drop_index(
        "ix_quality_candidate_review_candidate",
        table_name="candidate_review_event",
        schema="quality",
    )
    op.drop_table("candidate_review_event", schema="quality")
    op.drop_index(
        "ix_quality_candidate_evidence_candidate",
        table_name="candidate_evidence",
        schema="quality",
    )
    op.drop_index(
        "uq_quality_candidate_evidence_source",
        table_name="candidate_evidence",
        schema="quality",
    )
    op.drop_table("candidate_evidence", schema="quality")
    op.drop_index(
        "uq_quality_sample_candidate_fingerprint",
        table_name="sample_candidate",
        schema="quality",
    )
    op.drop_constraint(
        "ck_sample_candidate_counters", "sample_candidate", schema="quality", type_="check"
    )
    op.drop_constraint(
        "ck_sample_candidate_conflict_status",
        "sample_candidate",
        schema="quality",
        type_="check",
    )
    op.drop_constraint(
        "ck_sample_candidate_candidate_type",
        "sample_candidate",
        schema="quality",
        type_="check",
    )
    for column in (
        "last_seen_at",
        "version",
        "occurrence_count",
        "conflict_status",
        "observed_outcome",
        "fingerprint",
        "candidate_type",
    ):
        op.drop_column("sample_candidate", column, schema="quality")
    op.alter_column(
        "sample_candidate",
        "job_id",
        existing_type=sa.String(length=96),
        nullable=False,
        schema="quality",
    )
