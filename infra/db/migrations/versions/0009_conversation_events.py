"""为会话消息增加与业务写入同事务提交的可补发事件日志。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0009_conversation_events"
down_revision: str | None = "0008_app_engagement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建全局递增序号的事件表和按会话补发的索引。"""

    op.create_table(
        "conversation_event",
        sa.Column(
            "sequence",
            sa.BigInteger(),
            sa.Identity(),
            primary_key=True,
            comment="数据库分配的全局递增序号；同会话可有空号。",
        ),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
            nullable=False,
            comment="事件所属业务会话编号。",
        ),
        sa.Column("turn_id", sa.String(64), comment="相关客户轮次编号；会话级事件可为空。"),
        sa.Column(
            "event_type",
            sa.String(32),
            nullable=False,
            comment="MESSAGE_CREATED=新消息；CONVERSATION_UPDATED=会话状态改变。",
        ),
        sa.Column(
            "payload",
            JSONB(),
            nullable=False,
            comment="最小事件内容；客户端仍应按编号读取权威状态。",
        ),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False, comment="事件写入时间。"
        ),
        schema="app",
        comment="会话事件补发日志；消息正文仍以 chat_history 为准。",
    )
    op.create_index(
        "ix_conversation_event_replay",
        "conversation_event",
        ["conversation_id", "sequence"],
        schema="app",
    )


def downgrade() -> None:
    """仅供空演示库回退；正式库删除会丢失补发事件。"""

    op.drop_index("ix_conversation_event_replay", table_name="conversation_event", schema="app")
    op.drop_table("conversation_event", schema="app")
