"""Create BFF identity, authorization and idempotency tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_app_identity"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subject",
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("subject_type", sa.String(length=16), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="ck_subject_status"),
        sa.CheckConstraint("subject_type IN ('CUSTOMER', 'STAFF')", name="ck_subject_subject_type"),
        sa.PrimaryKeyConstraint("subject_id", name="pk_subject"),
        sa.UniqueConstraint("username", name="uq_subject_username"),
        schema="app",
    )
    op.create_table(
        "role",
        sa.Column("role_code", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("role_code", name="pk_role"),
        schema="app",
    )
    op.create_table(
        "idempotency_record",
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=96), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('PROCESSING', 'COMPLETED', 'FAILED')",
            name="ck_idempotency_record_status",
        ),
        sa.PrimaryKeyConstraint(
            "subject_id", "operation", "idempotency_key", name="pk_idempotency_record"
        ),
        schema="app",
    )
    op.create_table(
        "subject_customer_binding",
        sa.Column("binding_id", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("relationship", sa.String(length=32), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "relationship IN ('SELF', 'ASSIGNED_AGENT', 'SUPERVISOR')",
            name="ck_subject_customer_binding_relationship",
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["app.subject.subject_id"],
            name="fk_binding_subject",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("binding_id", name="pk_subject_customer_binding"),
        sa.UniqueConstraint(
            "subject_id",
            "customer_id",
            "relationship",
            name="uq_subject_customer_relationship",
        ),
        schema="app",
    )
    op.create_index(
        "ix_subject_customer_active",
        "subject_customer_binding",
        ["subject_id", "customer_id", "active"],
        schema="app",
    )
    op.create_table(
        "subject_role",
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("role_code", sa.String(length=32), nullable=False),
        sa.Column(
            "granted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["role_code"], ["app.role.role_code"], name="fk_subject_role_role", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["app.subject.subject_id"],
            name="fk_subject_role_subject",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("subject_id", "role_code", name="pk_subject_role"),
        schema="app",
    )

    role_table = sa.table(
        "role",
        sa.column("role_code", sa.String()),
        sa.column("display_name", sa.String()),
        schema="app",
    )
    op.bulk_insert(
        role_table,
        [
            {"role_code": "CUSTOMER", "display_name": "客户"},
            {"role_code": "AGENT", "display_name": "坐席"},
            {"role_code": "ADMIN", "display_name": "管理员"},
        ],
    )


def downgrade() -> None:
    op.drop_table("subject_role", schema="app")
    op.drop_index("ix_subject_customer_active", table_name="subject_customer_binding", schema="app")
    op.drop_table("subject_customer_binding", schema="app")
    op.drop_table("idempotency_record", schema="app")
    op.drop_table("role", schema="app")
    op.drop_table("subject", schema="app")
