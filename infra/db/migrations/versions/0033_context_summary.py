"""保存状态外摘要记录，并记录模型请求的预估输入量。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033_context_summary"
down_revision: str | None = "0032_turn_context_cycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "context_summary_record",
        sa.Column("summary_record_id", sa.String(96), primary_key=True, comment="摘要记录编号。"),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
            nullable=False,
            comment="摘要所属业务会话。",
        ),
        sa.Column(
            "thread_id",
            sa.String(64),
            sa.ForeignKey("app.conversation_thread_mapping.thread_id", ondelete="RESTRICT"),
            nullable=False,
            comment="摘要所属 LangGraph 线程。",
        ),
        sa.Column(
            "context_cycle_id", sa.String(96), nullable=False, comment="摘要所属上下文周期。"
        ),
        sa.Column(
            "record_kind",
            sa.String(16),
            nullable=False,
            comment="SEGMENT、AGGREGATE 或 BASELINE。",
        ),
        sa.Column(
            "parent_summary_record_id",
            sa.String(96),
            sa.ForeignKey("app.context_summary_record.summary_record_id", ondelete="RESTRICT"),
            comment="聚合或基线摘要的上一版本。",
        ),
        sa.Column("summary_text", sa.Text(), nullable=False, comment="脱敏后的摘要正文。"),
        sa.Column(
            "covered_turn_ids", postgresql.JSONB(), nullable=False, comment="摘要覆盖的 Turn 编号。"
        ),
        sa.Column(
            "source_refs", postgresql.JSONB(), nullable=False, comment="有限可追溯来源引用。"
        ),
        sa.Column(
            "generation_method",
            sa.String(32),
            nullable=False,
            comment="模型或显式降级方式。",
        ),
        sa.Column("prompt_version", sa.String(64), comment="生成摘要使用的提示词版本。"),
        sa.Column("model", sa.String(128), comment="生成摘要使用的模型。"),
        sa.Column("validation_status", sa.String(32), nullable=False, comment="摘要校验状态。"),
        sa.Column("content_hash", sa.String(64), nullable=False, comment="摘要正文 SHA256。"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, comment="摘要生成时间。"
        ),
        sa.CheckConstraint(
            "record_kind IN ('SEGMENT', 'AGGREGATE', 'BASELINE')", name="record_kind"
        ),
        sa.CheckConstraint(
            "validation_status IN ('VALID', 'EXTRACTIVE_FALLBACK')",
            name="validation_status",
        ),
        schema="app",
        comment="状态外不可变摘要记录；Graph State 只保存当前摘要和有限引用。",
    )
    op.create_index(
        "ix_context_summary_conversation_created",
        "context_summary_record",
        ["conversation_id", "created_at"],
        schema="app",
    )
    op.add_column(
        "model_call_record",
        sa.Column(
            "estimated_input_tokens",
            sa.Integer(),
            comment="发送请求前按最终消息载荷估算的输入 Token 数。",
        ),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("model_call_record", "estimated_input_tokens", schema="app")
    op.drop_index(
        "ix_context_summary_conversation_created",
        table_name="context_summary_record",
        schema="app",
    )
    op.drop_table("context_summary_record", schema="app")
