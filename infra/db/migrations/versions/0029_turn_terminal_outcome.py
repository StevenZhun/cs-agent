"""保存客户最终收到的响应路径和原因。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_turn_terminal_outcome"
down_revision: str | None = "0028_rag_review_align"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增可查询终态，不回填无法从旧记录可靠还原的历史轮次。"""

    op.add_column(
        "turn_record",
        sa.Column(
            "terminal_response_path",
            sa.String(32),
            comment=(
                "客户最终收到的响应路径：FAQ=问答；SOP=流程；RAG=检索草稿；HUMAN=人工；"
                "CLARIFICATION=澄清；SAFE_FALLBACK=安全兜底。与路由阶段的建议路径分开保存。"
            ),
        ),
        schema="app",
    )
    op.add_column(
        "turn_record",
        sa.Column(
            "terminal_reason_code",
            sa.String(64),
            comment="最终响应原因码；用于解释路由建议与客户实收为何不同。",
        ),
        schema="app",
    )
    op.create_check_constraint(
        "terminal_response_path",
        "turn_record",
        "terminal_response_path IS NULL OR terminal_response_path IN "
        "('FAQ', 'SOP', 'RAG', 'HUMAN', 'CLARIFICATION', 'SAFE_FALLBACK')",
        schema="app",
    )
    op.create_check_constraint(
        "terminal_outcome_pair",
        "turn_record",
        "(terminal_response_path IS NULL) = (terminal_reason_code IS NULL)",
        schema="app",
    )


def downgrade() -> None:
    """删除终态查询列；消息和路由快照仍保留。"""

    op.drop_constraint(
        "ck_turn_record_terminal_outcome_pair",
        "turn_record",
        schema="app",
        type_="check",
    )
    op.drop_constraint(
        "ck_turn_record_terminal_response_path",
        "turn_record",
        schema="app",
        type_="check",
    )
    op.drop_column("turn_record", "terminal_reason_code", schema="app")
    op.drop_column("turn_record", "terminal_response_path", schema="app")
