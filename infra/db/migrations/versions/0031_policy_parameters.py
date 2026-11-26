"""新增项目构造口径参数的权威版本表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0031_policy_parameters"
down_revision: str | None = "0030_policy_answers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "policy_parameter",
        sa.Column(
            "parameter_key",
            sa.String(64),
            primary_key=True,
            comment="稳定口径键；正文只引用此键，不复制裸数字。",
        ),
        sa.Column(
            "version",
            sa.String(32),
            primary_key=True,
            comment="不可变口径版本，采用 parameter-v* 形式。",
        ),
        sa.Column(
            "value_text", sa.String(160), nullable=False, comment="面向客户可读的项目构造取值。"
        ),
        sa.Column(
            "value_numeric",
            sa.Numeric(18, 6),
            comment="用于一致性校验的规范数值；TEXT 类型为空。",
        ),
        sa.Column(
            "unit",
            sa.String(16),
            nullable=False,
            comment="DAY/WORKDAY/AMOUNT/RATIO/COUNT/TEXT。",
        ),
        sa.Column(
            "scope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="产品、渠道和地区适用范围；空数组表示不限制。",
        ),
        sa.Column("effective_at", sa.Date(), nullable=False, comment="口径生效日期。"),
        sa.Column("retired_at", sa.Date(), comment="口径失效日期；为空表示未设置。"),
        sa.Column(
            "source_kind",
            sa.String(32),
            nullable=False,
            comment="固定为 PROJECT_CONSTRUCTED，避免冒充真实制度。",
        ),
        sa.Column(
            "business_reference",
            sa.String(255),
            comment="可选的外部业务字段引用，用于跨系统一致性校验。",
        ),
        sa.Column(
            "lifecycle_status",
            sa.String(16),
            nullable=False,
            comment=(
                "DRAFT=草稿；IN_REVIEW=审核中；APPROVED=已批准；"
                "PUBLISHED=已发布；RETIRED=已下线。"
            ),
        ),
        sa.Column("source_ref", sa.String(255), nullable=False, comment="作者数据来源和审核依据。"),
        sa.Column("approved_by", sa.String(64), comment="批准该口径的主体。"),
        sa.Column("approved_at", sa.DateTime(timezone=True), comment="口径批准时间。"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="口径版本首次创建时间。",
        ),
        sa.CheckConstraint(
            "unit IN ('DAY', 'WORKDAY', 'AMOUNT', 'RATIO', 'COUNT', 'TEXT')",
            name=op.f("ck_policy_parameter_unit"),
        ),
        sa.CheckConstraint(
            "source_kind = 'PROJECT_CONSTRUCTED'",
            name=op.f("ck_policy_parameter_source_kind"),
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'RETIRED')",
            name=op.f("ck_policy_parameter_lifecycle_status"),
        ),
        sa.CheckConstraint(
            "retired_at IS NULL OR effective_at <= retired_at",
            name=op.f("ck_policy_parameter_validity"),
        ),
        sa.CheckConstraint(
            "(unit = 'TEXT' AND value_numeric IS NULL) OR "
            "(unit <> 'TEXT' AND value_numeric IS NOT NULL)",
            name=op.f("ck_policy_parameter_numeric_value"),
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('DRAFT', 'IN_REVIEW') OR "
            "(approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name=op.f("ck_policy_parameter_approval"),
        ),
        schema="content",
        comment="项目构造口径的权威版本；供知识与 FAQ 引用，不是客户个人事实。",
    )


def downgrade() -> None:
    op.drop_table("policy_parameter", schema="content")
