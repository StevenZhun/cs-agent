"""为 Turn 增加模型上下文累计周期。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_turn_context_cycle"
down_revision: str | None = "0031_policy_parameters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "turn_record",
        sa.Column(
            "context_cycle_id",
            sa.String(96),
            comment="本轮完成时所属的模型上下文累计周期；上下文压缩开启新周期。",
        ),
        schema="app",
    )
    # 旧 checkpoint 没有周期字段；将同一线程的历史 Turn 归入初始周期，
    # 使迁移前等待恢复的 Turn 与 Runtime 的兼容回退值保持一致。
    op.execute(
        "UPDATE app.turn_record "
        "SET context_cycle_id = 'ctx:' || thread_id || ':' || '0' "
        "WHERE context_cycle_id IS NULL"
    )
    op.create_index(
        "ix_turn_record_conversation_cycle_completed",
        "turn_record",
        ["conversation_id", "context_cycle_id", "completed_at"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_turn_record_conversation_cycle_completed",
        table_name="turn_record",
        schema="app",
    )
    op.drop_column("turn_record", "context_cycle_id", schema="app")
