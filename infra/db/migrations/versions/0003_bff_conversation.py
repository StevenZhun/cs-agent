"""Create BFF conversation ownership and LangGraph thread mapping tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_bff_conversation"
down_revision: str | None = "0002_business_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation",
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("created_by_subject_id", sa.String(length=64), nullable=False),
        sa.Column("assigned_agent_subject_id", sa.String(length=64), nullable=True),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'WRAP_UP', 'FINISHED')", name="ck_conversation_status"
        ),
        sa.ForeignKeyConstraint(
            ["assigned_agent_subject_id"],
            ["app.subject.subject_id"],
            name="fk_conversation_assigned_agent_subject_id_subject",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_subject_id"],
            ["app.subject.subject_id"],
            name="fk_conversation_created_by_subject_id_subject",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["business.customer.customer_id"],
            name="fk_conversation_customer_id_customer",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("conversation_id", name="pk_conversation"),
        schema="app",
    )
    op.create_index(
        "ix_conversation_customer_status",
        "conversation",
        ["customer_id", "status"],
        schema="app",
    )
    op.create_table(
        "conversation_thread_mapping",
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["app.conversation.conversation_id"],
            name="fk_conversation_thread_mapping_conversation_id_conversation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("conversation_id", name="pk_conversation_thread_mapping"),
        sa.UniqueConstraint("thread_id", name="uq_conversation_thread_mapping_thread_id"),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("conversation_thread_mapping", schema="app")
    op.drop_index("ix_conversation_customer_status", table_name="conversation", schema="app")
    op.drop_table("conversation", schema="app")
