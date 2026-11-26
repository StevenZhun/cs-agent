"""给已存在的意图规则表增加域内知识范围规则。"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012_content_domain_rule"
down_revision: str | None = "0011_content_faq_release"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """调整枚举约束与注释；不删表、不覆盖历史规则。"""

    op.drop_constraint("ck_intent_rule_rule_kind", "intent_rule", schema="content")
    op.create_check_constraint(
        "ck_intent_rule_rule_kind",
        "intent_rule",
        "rule_kind IN ('INTENT', 'CONTROL', 'CAPABILITY', 'DOMAIN')",
        schema="content",
    )
    op.execute(
        "COMMENT ON COLUMN content.intent_rule.rule_kind IS "
        "'INTENT=动作意图；CONTROL=会话控制；CAPABILITY=未接能力；DOMAIN=域内知识范围。'"
    )


def downgrade() -> None:
    """仅当不存在 DOMAIN 规则时才可回退约束。"""

    op.drop_constraint("ck_intent_rule_rule_kind", "intent_rule", schema="content")
    op.create_check_constraint(
        "ck_intent_rule_rule_kind",
        "intent_rule",
        "rule_kind IN ('INTENT', 'CONTROL', 'CAPABILITY')",
        schema="content",
    )
    op.execute(
        "COMMENT ON COLUMN content.intent_rule.rule_kind IS "
        "'INTENT=动作意图；CONTROL=会话控制；CAPABILITY=未接入能力边界。'"
    )
