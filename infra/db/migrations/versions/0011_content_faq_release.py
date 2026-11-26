"""补 FAQ 权威表、发布记录和规则匹配模式。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0011_content_faq_release"
down_revision: str | None = "0010_content_intent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LIFECYCLE = "DRAFT=草稿；IN_REVIEW=审核中；APPROVED=已批准；PUBLISHED=已发布；RETIRED=已下线。"


def upgrade() -> None:
    """增量建表并给已有规则增加明确的匹配与控制语义。"""

    op.add_column(
        "intent_rule",
        sa.Column(
            "match_mode",
            sa.String(16),
            nullable=False,
            server_default="EXACT",
            comment="EXACT=整句匹配；CONTAINS=包含短语。",
        ),
        schema="content",
    )
    op.alter_column("intent_rule", "match_mode", server_default=None, schema="content")
    op.add_column(
        "intent_rule",
        sa.Column(
            "control_action",
            sa.String(32),
            comment="CONTROL 规则对应的命令：CANCEL=取消；HUMAN=转人工。",
        ),
        schema="content",
    )
    op.create_check_constraint(
        "ck_intent_rule_match_mode",
        "intent_rule",
        "match_mode IN ('EXACT', 'CONTAINS')",
        schema="content",
    )
    op.add_column(
        "graph_run_record",
        sa.Column(
            "routing_trace",
            JSONB(),
            comment="本次运行的确定性路由追踪；包含规则版本、命中状态与原因。",
        ),
        schema="app",
    )
    op.create_check_constraint(
        "ck_intent_rule_control_action",
        "intent_rule",
        "(rule_kind = 'CONTROL' AND control_action IN ('CANCEL', 'HUMAN')) "
        "OR (rule_kind <> 'CONTROL' AND control_action IS NULL)",
        schema="content",
    )

    op.create_table(
        "faq_group",
        sa.Column("faq_group_id", sa.String(64), primary_key=True, comment="FAQ 分组编号。"),
        sa.Column("version", sa.String(32), primary_key=True, comment="分组版本号。"),
        sa.Column("intent_id", sa.String(64), nullable=False, comment="所属动作意图编号。"),
        sa.Column("intent_version", sa.String(32), nullable=False, comment="所属意图版本。"),
        sa.Column("name", sa.String(128), nullable=False, comment="分组展示名称。"),
        sa.Column("lifecycle_status", sa.String(16), nullable=False, comment=LIFECYCLE),
        sa.Column("source_ref", sa.String(255), nullable=False, comment="问题分组的来源。"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间。"),
        sa.ForeignKeyConstraint(
            ["intent_id", "intent_version"],
            ["content.intent_definition.intent_id", "content.intent_definition.version"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'RETIRED')",
            name="lifecycle_status",
        ),
        schema="content",
        comment="FAQ 动作意图下的权威问题分组版本。",
    )
    op.create_table(
        "faq_item",
        sa.Column("faq_item_id", sa.String(64), primary_key=True, comment="FAQ 条目编号。"),
        sa.Column("version", sa.String(32), primary_key=True, comment="条目版本号。"),
        sa.Column("faq_group_id", sa.String(64), nullable=False, comment="所属 FAQ 分组。"),
        sa.Column("group_version", sa.String(32), nullable=False, comment="所属分组版本。"),
        sa.Column("title", sa.String(128), nullable=False, comment="条目标题。"),
        sa.Column("lifecycle_status", sa.String(16), nullable=False, comment=LIFECYCLE),
        sa.Column("source_ref", sa.String(255), nullable=False, comment="问答内容来源。"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间。"),
        sa.ForeignKeyConstraint(
            ["faq_group_id", "group_version"],
            ["content.faq_group.faq_group_id", "content.faq_group.version"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'RETIRED')",
            name="lifecycle_status",
        ),
        schema="content",
        comment="FAQ 问答条目的权威版本。",
    )
    op.create_table(
        "faq_question",
        sa.Column("variant_id", sa.String(64), primary_key=True, comment="问法变体编号。"),
        sa.Column("faq_item_id", sa.String(64), nullable=False, comment="所属 FAQ 条目。"),
        sa.Column("item_version", sa.String(32), nullable=False, comment="所属条目版本。"),
        sa.Column("question", sa.Text(), nullable=False, comment="用户可读的问题原文。"),
        sa.Column(
            "normalized_question", sa.Text(), nullable=False, comment="用于精确键的规范化问题。"
        ),
        sa.Column(
            "exact_match_enabled",
            sa.Boolean(),
            nullable=False,
            comment="是否允许投影到 Redis 精确索引。",
        ),
        sa.Column("locale", sa.String(16), nullable=False, comment="问法语言，如 zh-CN。"),
        sa.ForeignKeyConstraint(
            ["faq_item_id", "item_version"],
            ["content.faq_item.faq_item_id", "content.faq_item.version"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("faq_item_id", "item_version", "normalized_question", "locale"),
        schema="content",
        comment="FAQ 条目下的精确或语义候选问法。",
    )
    op.create_table(
        "faq_answer",
        sa.Column("answer_id", sa.String(64), primary_key=True, comment="答案版本编号。"),
        sa.Column("faq_item_id", sa.String(64), nullable=False, comment="所属 FAQ 条目。"),
        sa.Column("item_version", sa.String(32), nullable=False, comment="所属条目版本。"),
        sa.Column("answer", sa.Text(), nullable=False, comment="经审核的固定答案正文。"),
        sa.Column(
            "product_codes", JSONB(), nullable=False, comment="适用产品代码；空数组表示不限。"
        ),
        sa.Column("channels", JSONB(), nullable=False, comment="适用渠道；空数组表示不限。"),
        sa.Column("regions", JSONB(), nullable=False, comment="适用地区；空数组表示不限。"),
        sa.Column(
            "effective_from", sa.DateTime(timezone=True), comment="答案生效时间；空值表示无下界。"
        ),
        sa.Column(
            "effective_to", sa.DateTime(timezone=True), comment="答案失效时间；空值表示无上界。"
        ),
        sa.Column("source_ref", sa.String(255), nullable=False, comment="固定答案来源或审核依据。"),
        sa.ForeignKeyConstraint(
            ["faq_item_id", "item_version"],
            ["content.faq_item.faq_item_id", "content.faq_item.version"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from",
            name="valid_time",
        ),
        schema="content",
        comment="FAQ 固定答案的来源、适用范围和生效时间。",
    )
    op.create_table(
        "intent_release",
        sa.Column(
            "release_version", sa.String(64), primary_key=True, comment="不可变的发布版本号。"
        ),
        sa.Column(
            "manifest", JSONB(), nullable=False, comment="通过冲突检查的发布清单和资产引用。"
        ),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="审核发布入库时间；不代表当前仍是活动版本。",
        ),
        schema="content",
        comment="意图目录和规则的发布清单；在线活动版本由 Redis 指针决定。",
    )


def downgrade() -> None:
    """仅用于本地未发布数据的回退；正式版本不可直接删表。"""

    op.drop_table("intent_release", schema="content")
    op.drop_column("graph_run_record", "routing_trace", schema="app")
    op.drop_table("faq_answer", schema="content")
    op.drop_table("faq_question", schema="content")
    op.drop_table("faq_item", schema="content")
    op.drop_table("faq_group", schema="content")
    op.drop_constraint("ck_intent_rule_control_action", "intent_rule", schema="content")
    op.drop_constraint("ck_intent_rule_match_mode", "intent_rule", schema="content")
    op.drop_column("intent_rule", "control_action", schema="content")
    op.drop_column("intent_rule", "match_mode", schema="content")
