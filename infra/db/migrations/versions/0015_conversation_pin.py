"""增加按登录主体隔离的会话置顶。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_conversation_pin"
down_revision: str | None = "0014_conversation_title"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立个人置顶关系，避免客户与坐席互相改变列表顺序。"""

    op.create_table(
        "conversation_pin",
        sa.Column(
            "subject_id",
            sa.String(64),
            sa.ForeignKey("app.subject.subject_id", ondelete="CASCADE"),
            primary_key=True,
            comment="执行置顶操作的应用主体 ID。",
        ),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("app.conversation.conversation_id", ondelete="CASCADE"),
            primary_key=True,
            comment="被当前主体置顶的业务会话 ID。",
        ),
        sa.Column(
            "pinned_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="最近一次置顶时间；用于排列多条置顶会话。",
        ),
        schema="app",
        comment="登录主体的个人会话置顶；不同主体互不影响。",
    )
    op.create_index(
        "ix_conversation_pin_subject_time",
        "conversation_pin",
        ["subject_id", "pinned_at"],
        schema="app",
    )


def downgrade() -> None:
    """删除个人置顶关系；不会删除业务会话。"""

    op.drop_index(
        "ix_conversation_pin_subject_time",
        table_name="conversation_pin",
        schema="app",
    )
    op.drop_table("conversation_pin", schema="app")
