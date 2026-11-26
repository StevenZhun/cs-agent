"""保存 checkpoint 清理清单、审批和执行结果。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035_retention_requests"
down_revision: str | None = "0034_runtime_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "retention_request",
        sa.Column("request_id", sa.String(96), primary_key=True, comment="保留期处置请求编号。"),
        sa.Column(
            "resource_kind",
            sa.String(32),
            nullable=False,
            comment="处置资源类型；当前仅支持 LANGGRAPH_THREAD。",
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            comment="PLANNED、APPROVED、EXECUTED 或 REJECTED。",
        ),
        sa.Column("manifest", postgresql.JSONB(), nullable=False, comment="待处置线程的精确清单。"),
        sa.Column("manifest_hash", sa.String(64), nullable=False, comment="清单正文 SHA-256。"),
        sa.Column("reason", sa.Text(), nullable=False, comment="申请处置的业务或运维原因。"),
        sa.Column("requested_by", sa.String(128), nullable=False, comment="计划发起人标识。"),
        sa.Column("approved_by", sa.String(128), comment="审批人标识。"),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), nullable=False, comment="计划创建时间。"
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), comment="审批通过时间。"),
        sa.Column("executed_at", sa.DateTime(timezone=True), comment="官方保存器完成删除的时间。"),
        sa.CheckConstraint("resource_kind = 'LANGGRAPH_THREAD'", name="retention_resource_kind"),
        sa.CheckConstraint(
            "status IN ('PLANNED', 'APPROVED', 'EXECUTED', 'REJECTED')",
            name="retention_status",
        ),
        schema="app",
        comment="精确 checkpoint 线程清单的计划、审批和执行记录；不删除聊天历史。",
    )
    op.create_index(
        "ix_retention_request_status_requested",
        "retention_request",
        ["status", "requested_at"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_retention_request_status_requested",
        table_name="retention_request",
        schema="app",
    )
    op.drop_table("retention_request", schema="app")
