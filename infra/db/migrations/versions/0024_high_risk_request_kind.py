"""允许已发布策略意图标记高风险请求。"""

from alembic import op

revision = "0024_high_risk_request_kind"
down_revision = "0023_rag_content"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """扩展请求形态约束，不修改已有意图版本或内容数据。"""

    op.drop_constraint(
        "request_kind", table_name="intent_definition", schema="content", type_="check"
    )
    op.create_check_constraint(
        "request_kind",
        "intent_definition",
        "request_kind IN ('KNOWLEDGE', 'ACTION', 'CONTROL', 'SOCIAL', "
        "'EXTERNAL_CAPABILITY', 'HIGH_RISK', 'UNSURE')",
        schema="content",
    )


def downgrade() -> None:
    """回退前先保证没有 HIGH_RISK 意图，否则数据库约束无法安全收窄。"""

    op.drop_constraint(
        "request_kind", table_name="intent_definition", schema="content", type_="check"
    )
    op.create_check_constraint(
        "request_kind",
        "intent_definition",
        "request_kind IN ('KNOWLEDGE', 'ACTION', 'CONTROL', 'SOCIAL', "
        "'EXTERNAL_CAPABILITY', 'UNSURE')",
        schema="content",
    )
