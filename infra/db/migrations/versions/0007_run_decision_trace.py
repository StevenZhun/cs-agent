"""在应用 Run 上保存结构化 Query 决策追踪。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_run_decision_trace"
down_revision: str | None = "0006_run_recovery_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增可空 JSONB；历史 Run 没有对应决策时不伪造追踪数据。"""

    op.add_column(
        "graph_run_record",
        sa.Column("decision_trace", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema="app",
    )
    op.execute(
        "COMMENT ON COLUMN app.graph_run_record.decision_trace IS "
        "'本次图运行的结构化 Query 决策追踪；空值表示旧 Run 或尚未生成决策。'"
    )


def downgrade() -> None:
    """回退新字段会丢失其追踪记录，正式环境需先备份。"""

    op.drop_column("graph_run_record", "decision_trace", schema="app")
