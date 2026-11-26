"""为业务会话增加可选的自定义标题。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_conversation_title"
down_revision: str | None = "0013_faq_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加自定义标题；旧会话保持空值并从首条客户消息生成默认标题。"""

    op.add_column(
        "conversation",
        sa.Column(
            "title",
            sa.String(30),
            nullable=True,
            comment="用户主动设置的会话标题；为空时，页面根据首条客户消息生成默认标题。",
        ),
        schema="app",
    )


def downgrade() -> None:
    """删除自定义标题；回退后页面只能使用自动生成的标题。"""

    op.drop_column("conversation", "title", schema="app")
