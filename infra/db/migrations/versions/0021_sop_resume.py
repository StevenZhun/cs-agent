"""Persist an agent-authorized command used to resume a suspended SOP."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0021_sop_resume"
down_revision = "0020_sop_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "graph_run_record",
        sa.Column(
            "resume_payload",
            JSONB,
            comment="坐席授权后的恢复动作、操作者、更正字段和核验声明。",
        ),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("graph_run_record", "resume_payload", schema="app")
