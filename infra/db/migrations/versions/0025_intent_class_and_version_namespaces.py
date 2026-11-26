"""Distinguish action intents from system strategy intents."""

import sqlalchemy as sa
from alembic import op

revision = "0025_intent_class_versions"
down_revision = "0024_high_risk_request_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "intent_definition",
        sa.Column(
            "intent_class",
            sa.String(16),
            nullable=False,
            server_default="ACTION",
            comment="ACTION=业务咨询或办理动作；STRATEGY=系统策略、安全或交互意图。",
        ),
        schema="content",
    )
    op.create_check_constraint(
        "intent_class",
        "intent_definition",
        "intent_class IN ('ACTION', 'STRATEGY')",
        schema="content",
    )
    op.create_check_constraint(
        "strategy_route_target",
        "intent_definition",
        "intent_class <> 'STRATEGY' OR route_target = 'FAQ'",
        schema="content",
    )
    op.create_check_constraint(
        "high_risk_intent_class",
        "intent_definition",
        "request_kind <> 'HIGH_RISK' OR intent_class = 'STRATEGY'",
        schema="content",
    )
    op.execute(
        "COMMENT ON TABLE content.intent_definition IS "
        "'全局意图权威版本；历史版本不可原地覆盖。'"
    )
    op.execute(
        "COMMENT ON TABLE content.intent_similarity IS "
        "'全局意图的相似检索语料；PostgreSQL 是权威源，Milvus 只保存向量副本。'"
    )


def downgrade() -> None:
    op.drop_constraint(
        "high_risk_intent_class",
        table_name="intent_definition",
        schema="content",
        type_="check",
    )
    op.drop_constraint(
        "strategy_route_target",
        table_name="intent_definition",
        schema="content",
        type_="check",
    )
    op.drop_constraint(
        "intent_class", table_name="intent_definition", schema="content", type_="check"
    )
    op.drop_column("intent_definition", "intent_class", schema="content")
