"""保存面向坐席的流程进度，不改动已存在的客户历史消息。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0020_sop_snapshot"
down_revision = "0019_business_tools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """添加可为空的进度快照；旧会话通过受保护的 Runtime 查询读取。"""
    op.add_column(
        "graph_run_record",
        sa.Column(
            "sop_snapshot",
            JSONB,
            comment="与助手消息一起保存的 SOP 进度与暂停原因，供坐席查看；不是人工审批记录。",
        ),
        schema="app",
    )


def downgrade() -> None:
    """回退只移除此新增列，不删除聊天记录。"""
    op.drop_column("graph_run_record", "sop_snapshot", schema="app")
