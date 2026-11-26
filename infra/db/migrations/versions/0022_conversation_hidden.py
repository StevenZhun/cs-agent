"""允许客户隐藏已结束咨询，保留全部历史和业务审计。"""

import sqlalchemy as sa
from alembic import op

revision = "0022_conversation_hidden"
down_revision = "0021_sop_resume"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建按主体保存的隐藏记录，每张表和各字段均有中文注释。"""
    op.create_table(
        "conversation_hidden",
        sa.Column(
            "subject_id",
            sa.String(64),
            sa.ForeignKey("app.subject.subject_id", ondelete="CASCADE"),
            primary_key=True,
            comment="执行软删除的登录主体；不影响其他主体的列表。",
        ),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("app.conversation.conversation_id", ondelete="CASCADE"),
            primary_key=True,
            comment="被隐藏的已结束咨询编号；恢复时只移除本隐藏记录。",
        ),
        sa.Column(
            "hidden_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="客户确认软删除的时间；与最后一条消息时间无关。",
        ),
        schema="app",
        comment="当前主体隐藏的已结束会话；不物理删除消息、checkpoint 或业务操作。",
    )


def downgrade() -> None:
    """移除隐藏记录，会使被隐藏咨询重新出现在列表，不删除原会话。"""
    op.drop_table("conversation_hidden", schema="app")
