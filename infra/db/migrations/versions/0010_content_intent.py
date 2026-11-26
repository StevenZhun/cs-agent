"""建立 content schema 中动作意图和规则的权威版本表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0010_content_intent"
down_revision: str | None = "0009_conversation_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """先建意图版本，再建引用该版本的高精度规则。"""

    op.create_table(
        "intent_definition",
        sa.Column("intent_id", sa.String(64), primary_key=True, comment="动作意图编号。"),
        sa.Column("version", sa.String(32), primary_key=True, comment="意图版本号。"),
        sa.Column("name", sa.String(128), nullable=False, comment="供管理和展示的意图名称。"),
        sa.Column("description", sa.Text(), nullable=False, comment="动作意图的业务定义。"),
        sa.Column(
            "route_target",
            sa.String(16),
            nullable=False,
            comment="SCENE=场景 SOP；FAQ=固定问答分组。",
        ),
        sa.Column(
            "request_kind",
            sa.String(32),
            nullable=False,
            comment="KNOWLEDGE=知识咨询；ACTION=办理动作；CONTROL=会话控制；SOCIAL=寒暄；"
            "EXTERNAL_CAPABILITY=外部能力；UNSURE=无法确定。",
        ),
        sa.Column(
            "lifecycle_status",
            sa.String(16),
            nullable=False,
            comment="DRAFT=草稿；IN_REVIEW=审核中；APPROVED=已批准；"
            "PUBLISHED=已发布；RETIRED=已下线。",
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, comment="当前版本是否允许命中。"),
        sa.Column(
            "source_ref",
            sa.String(255),
            nullable=False,
            comment="业务定义来源和发布归属。",
        ),
        sa.Column(
            "release_version",
            sa.String(64),
            comment="发布后所属的不可变内容发布版本；未发布时为空。",
        ),
        sa.Column("approved_by", sa.String(64), comment="批准该版本的主体编号。"),
        sa.Column("approved_at", sa.DateTime(timezone=True), comment="该版本批准时间。"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, comment="版本创建时间。"
        ),
        sa.CheckConstraint("route_target IN ('SCENE', 'FAQ')", name="route_target"),
        sa.CheckConstraint(
            "request_kind IN ('KNOWLEDGE', 'ACTION', 'CONTROL', 'SOCIAL', "
            "'EXTERNAL_CAPABILITY', 'UNSURE')",
            name="request_kind",
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'RETIRED')",
            name="lifecycle_status",
        ),
        schema="content",
        comment="动作级意图权威版本；历史版本不可原地覆盖。",
    )
    op.create_table(
        "intent_rule",
        sa.Column("rule_id", sa.String(64), primary_key=True, comment="规则编号。"),
        sa.Column("version", sa.String(32), primary_key=True, comment="规则版本号。"),
        sa.Column(
            "rule_kind",
            sa.String(16),
            nullable=False,
            comment="INTENT=动作意图；CONTROL=会话控制；CAPABILITY=未接入能力边界。",
        ),
        sa.Column("intent_id", sa.String(64), comment="INTENT 规则指向的动作意图。"),
        sa.Column("intent_version", sa.String(32), comment="INTENT 规则指向的意图版本。"),
        sa.Column("pattern", sa.Text(), nullable=False, comment="待精确匹配的规范化短语。"),
        sa.Column(
            "negative_patterns",
            JSONB(),
            nullable=False,
            comment="命中后用于排除误判的否定短语数组。",
        ),
        sa.Column("priority", sa.Integer(), nullable=False, comment="规则优先级；越大越先判定。"),
        sa.Column(
            "product_codes",
            JSONB(),
            nullable=False,
            comment="允许的产品代码数组；空数组表示不限制产品。",
        ),
        sa.Column(
            "channels",
            JSONB(),
            nullable=False,
            comment="允许的渠道代码数组；空数组表示不限制渠道。",
        ),
        sa.Column(
            "regions",
            JSONB(),
            nullable=False,
            comment="允许的政策地区数组；空数组表示不限制地区。",
        ),
        sa.Column(
            "required_capability",
            sa.String(64),
            comment="CAPABILITY 规则标出的当前未接入能力，如 WEATHER。",
        ),
        sa.Column(
            "lifecycle_status",
            sa.String(16),
            nullable=False,
            comment="DRAFT=草稿；IN_REVIEW=审核中；APPROVED=已批准；"
            "PUBLISHED=已发布；RETIRED=已下线。",
        ),
        sa.Column("source_ref", sa.String(255), nullable=False, comment="规则来源和审核依据。"),
        sa.Column(
            "release_version",
            sa.String(64),
            comment="发布后所属的不可变内容发布版本；未发布时为空。",
        ),
        sa.Column("approved_by", sa.String(64), comment="批准该规则的主体编号。"),
        sa.Column("approved_at", sa.DateTime(timezone=True), comment="该规则批准时间。"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, comment="规则版本创建时间。"
        ),
        sa.ForeignKeyConstraint(
            ["intent_id", "intent_version"],
            ["content.intent_definition.intent_id", "content.intent_definition.version"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "rule_kind IN ('INTENT', 'CONTROL', 'CAPABILITY')",
            name="rule_kind",
        ),
        sa.CheckConstraint("priority >= 0", name="priority_non_negative"),
        sa.CheckConstraint(
            "lifecycle_status IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'RETIRED')",
            name="lifecycle_status",
        ),
        sa.CheckConstraint(
            "(rule_kind = 'INTENT' AND intent_id IS NOT NULL AND intent_version IS NOT NULL) "
            "OR (rule_kind <> 'INTENT' AND intent_id IS NULL AND intent_version IS NULL)",
            name="intent_binding",
        ),
        schema="content",
        comment="高精度关键词、控制命令和能力边界规则的权威版本。",
    )


def downgrade() -> None:
    """仅供未写入正式内容的本地演练库回退；删表会丢失规则版本。"""

    op.drop_table("intent_rule", schema="content")
    op.drop_table("intent_definition", schema="content")
