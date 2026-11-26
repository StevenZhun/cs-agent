"""Persist read-only SOP tool results with each graph run."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0019_business_tools"
down_revision = "0018_sop_configuration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "graph_run_record",
        sa.Column(
            "business_tool_results",
            JSONB,
            comment="本轮 SOP 使用的只读工具结果、来源、状态和耗时。",
        ),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("graph_run_record", "business_tool_results", schema="app")
