"""Create application-owned Turn, Run, and append-only message records."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_app_history"
down_revision: str | None = "0003_bff_conversation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "turn_record",
        sa.Column("turn_id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("user_message_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("routing_decision", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("active_run_id", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'INTERRUPTED', 'COMPLETED', 'FAILED')",
            name="ck_turn_record_status",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["app.conversation.conversation_id"],
            name="fk_turn_record_conversation_id_conversation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["app.conversation_thread_mapping.thread_id"],
            name="fk_turn_record_thread_id_conversation_thread_mapping",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("turn_id", name="pk_turn_record"),
        sa.UniqueConstraint("user_message_id", name="uq_turn_record_user_message_id"),
        schema="app",
    )
    op.create_index(
        "ix_turn_record_conversation_started",
        "turn_record",
        ["conversation_id", "started_at"],
        schema="app",
    )
    op.create_table(
        "graph_run_record",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("turn_id", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("decision_profile", sa.String(length=16), nullable=False),
        sa.Column("decision_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("resumed_from_run_id", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'INTERRUPTED', 'COMPLETED', 'FAILED')",
            name="ck_graph_run_record_status",
        ),
        sa.CheckConstraint(
            "decision_profile IN ('BASELINE', 'JEV')",
            name="ck_graph_run_record_decision_profile",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["app.turn_record.turn_id"],
            name="fk_graph_run_record_turn_id_turn_record",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["app.conversation_thread_mapping.thread_id"],
            name="fk_graph_run_record_thread_id_conversation_thread_mapping",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", name="pk_graph_run_record"),
        schema="app",
    )
    op.create_index(
        "ix_graph_run_record_turn_started",
        "graph_run_record",
        ["turn_id", "started_at"],
        schema="app",
    )
    op.create_table(
        "chat_history",
        sa.Column("message_id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("turn_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_call_id", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('CUSTOMER', 'AGENT', 'ASSISTANT', 'SYSTEM', 'TOOL')",
            name="ck_chat_history_role",
        ),
        sa.CheckConstraint("length(btrim(content)) > 0", name="ck_chat_history_non_blank_content"),
        sa.CheckConstraint(
            "role <> 'TOOL' OR tool_call_id IS NOT NULL", name="ck_chat_history_tool_call_id"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["app.conversation.conversation_id"],
            name="fk_chat_history_conversation_id_conversation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["app.conversation_thread_mapping.thread_id"],
            name="fk_chat_history_thread_id_conversation_thread_mapping",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["app.turn_record.turn_id"],
            name="fk_chat_history_turn_id_turn_record",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("message_id", name="pk_chat_history"),
        schema="app",
    )
    op.create_index(
        "ix_chat_history_conversation_order",
        "chat_history",
        ["conversation_id", "created_at", "message_id"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index("ix_chat_history_conversation_order", table_name="chat_history", schema="app")
    op.drop_table("chat_history", schema="app")
    op.drop_index("ix_graph_run_record_turn_started", table_name="graph_run_record", schema="app")
    op.drop_table("graph_run_record", schema="app")
    op.drop_index("ix_turn_record_conversation_started", table_name="turn_record", schema="app")
    op.drop_table("turn_record", schema="app")
