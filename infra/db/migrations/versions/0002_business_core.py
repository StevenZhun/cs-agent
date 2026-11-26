"""Create generic customer profiles and service records."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002_business_core"
down_revision: str | None = "0001_app_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customer",
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("contact_last4", sa.String(4), nullable=False),
        sa.Column("service_level", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("tags", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('ACTIVE', 'INACTIVE')", name="status"),
        sa.PrimaryKeyConstraint("customer_id", name="pk_customer"),
        schema="business",
        comment="脱敏客户资料；登录身份和客户资料通过应用绑定表关联。",
    )
    op.create_table(
        "service_record",
        sa.Column("record_id", sa.String(64), nullable=False),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reference_code", sa.String(64)),
        sa.Column("details", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('OPEN', 'PENDING', 'RESOLVED', 'CLOSED')", name="status"),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["business.customer.customer_id"],
            name="fk_service_record_customer",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("record_id", name="pk_service_record"),
        schema="business",
        comment="可由行业适配器映射的通用服务记录。",
    )
    op.create_index(
        "ix_service_record_customer_status",
        "service_record",
        ["customer_id", "status"],
        schema="business",
    )
    op.create_foreign_key(
        "fk_subject_customer_binding_customer",
        "subject_customer_binding",
        "customer",
        ["customer_id"],
        ["customer_id"],
        source_schema="app",
        referent_schema="business",
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_subject_customer_binding_customer",
        "subject_customer_binding",
        schema="app",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_service_record_customer_status", table_name="service_record", schema="business"
    )
    op.drop_table("service_record", schema="business")
    op.drop_table("customer", schema="business")
