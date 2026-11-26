"""新增高风险策略答案及其不可变发布快照。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030_policy_answers"
down_revision: str | None = "0029_turn_terminal_outcome"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "policy_answer",
        sa.Column("intent_id", sa.String(64), primary_key=True, comment="高风险策略意图编号。"),
        sa.Column("version", sa.String(64), primary_key=True, comment="不可变策略答案版本。"),
        sa.Column("intent_version", sa.String(32), nullable=False, comment="绑定意图版本。"),
        sa.Column("answer_text", sa.Text(), nullable=False, comment="客户可见固定答复。"),
        sa.Column(
            "answer_content_hash", sa.String(64), nullable=False, comment="答复正文 SHA-256。"
        ),
        sa.Column("lifecycle_status", sa.String(16), nullable=False, comment="内容生命周期。"),
        sa.Column("source_ref", sa.String(255), nullable=False, comment="内容来源。"),
        sa.Column("release_version", sa.String(64), comment="所属发布版本。"),
        sa.Column("approved_by", sa.String(64), comment="批准主体。"),
        sa.Column("approved_at", sa.DateTime(timezone=True), comment="批准时间。"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间。"),
        sa.ForeignKeyConstraint(
            ["intent_id", "intent_version"],
            ["content.intent_definition.intent_id", "content.intent_definition.version"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'RETIRED')",
            name=op.f("ck_policy_answer_lifecycle_status"),
        ),
        schema="content",
        comment="高风险策略意图的固定答复；不经过普通 FAQ 与向量检索。",
    )
    op.create_table(
        "policy_answer_release",
        sa.Column("release_version", sa.String(64), primary_key=True, comment="不可变发布版本。"),
        sa.Column("source_ref", sa.String(255), nullable=False, comment="内容来源。"),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment="发布快照。"
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False, comment="发布时间。"),
        schema="content",
        comment="策略答案不可变发布快照；活动版本由 Redis 指针选择。",
    )


def downgrade() -> None:
    op.drop_table("policy_answer_release", schema="content")
    op.drop_table("policy_answer", schema="content")
