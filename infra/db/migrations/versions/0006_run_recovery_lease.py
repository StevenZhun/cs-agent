"""为现有图运行增加超时接管字段，不删除已有消息或 Run。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_run_recovery_lease"
down_revision: str | None = "0005_postgres_comments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """补齐旧行默认值，再为恢复扫描建立索引和中文数据库注释。"""

    op.add_column(
        "graph_run_record",
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        schema="app",
    )
    op.alter_column("graph_run_record", "lease_expires_at", server_default=None, schema="app")
    op.add_column(
        "graph_run_record",
        sa.Column("lease_version", sa.Integer(), nullable=False, server_default="0"),
        schema="app",
    )
    op.alter_column("graph_run_record", "lease_version", server_default=None, schema="app")
    op.add_column(
        "graph_run_record",
        sa.Column("lease_owner", sa.String(length=32), nullable=False, server_default="ONLINE"),
        schema="app",
    )
    op.alter_column("graph_run_record", "lease_owner", server_default=None, schema="app")
    op.create_index(
        "ix_graph_run_record_recovery",
        "graph_run_record",
        ["status", "lease_expires_at"],
        schema="app",
    )
    op.execute(
        "COMMENT ON COLUMN app.graph_run_record.lease_expires_at IS "
        "'本次运行允许原工作者处理到的截止时间；过期后恢复者才可尝试接管。'"
    )
    op.execute(
        "COMMENT ON COLUMN app.graph_run_record.lease_version IS "
        "'接管版本号；每次恢复者接管加一，旧版本不得再写入助手回复。'"
    )
    op.execute(
        "COMMENT ON COLUMN app.graph_run_record.lease_owner IS "
        "'当前处理来源：ONLINE=在线请求；RECOVERY=后台恢复者。'"
    )


def downgrade() -> None:
    """仅回退本次新增的索引与字段。"""

    op.drop_index("ix_graph_run_record_recovery", table_name="graph_run_record", schema="app")
    op.drop_column("graph_run_record", "lease_owner", schema="app")
    op.drop_column("graph_run_record", "lease_version", schema="app")
    op.drop_column("graph_run_record", "lease_expires_at", schema="app")
