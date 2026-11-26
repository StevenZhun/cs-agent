"""建立可审核发布的 SOP 配置和不可变快照，并附中文表字段注释。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0018_sop_configuration"
down_revision = "0017_model_call_record"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增量建表，不删除现有消息、规则或 FAQ。"""
    op.create_table(
        "sop_definition",
        sa.Column("sop_id", sa.String(64), primary_key=True, comment="稳定的业务流程编号。"),
        sa.Column(
            "version",
            sa.String(32),
            primary_key=True,
            comment="不可变流程版本；更新发布需创建新版本。",
        ),
        sa.Column(
            "intent_id",
            sa.String(64),
            nullable=False,
            comment="绑定的全局动作意图；必须指向 SCENE。",
        ),
        sa.Column("intent_version", sa.String(32), nullable=False, comment="绑定意图定义的版本。"),
        sa.Column(
            "config",
            JSONB,
            nullable=False,
            comment=(
                "经 SopConfig 校验的配置：字段名/标签/类型/选项、"
                "条件必填、命令、影响提示和固定回复。"
            ),
        ),
        sa.Column(
            "lifecycle_status",
            sa.String(16),
            nullable=False,
            comment="DRAFT=可编辑草稿；APPROVED=已核验批准；PUBLISHED=已发布；RETIRED=已下线。",
        ),
        sa.Column(
            "source_ref",
            sa.String(255),
            nullable=False,
            comment="配置来源和发布归属。",
        ),
        sa.Column(
            "approved_by",
            sa.String(64),
            comment="批准配置的操作主体；该记录不替代部署环境的业务审核。",
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), comment="批准时间，未批准时为空。"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, comment="配置版本创建时间。"
        ),
        sa.ForeignKeyConstraint(
            ["intent_id", "intent_version"],
            ["content.intent_definition.intent_id", "content.intent_definition.version"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('DRAFT', 'APPROVED', 'PUBLISHED', 'RETIRED')",
            name="lifecycle_status",
        ),
        schema="content",
        comment="SOP 动作绑定、字段、条件分支与文案的权威配置版本。",
    )
    op.create_table(
        "sop_release",
        sa.Column(
            "release_version",
            sa.String(64),
            primary_key=True,
            comment="整批 SOP 发布版本，不能同名覆盖不同内容。",
        ),
        sa.Column(
            "payload",
            JSONB,
            nullable=False,
            comment="发布快照，包括 definitions、content_hash、审核者和发布时刻。",
        ),
        sa.Column(
            "published_at", sa.DateTime(timezone=True), nullable=False, comment="发布快照保存时间。"
        ),
        schema="content",
        comment="SOP 不可变发布快照与校验值；活动版本由 Redis 指针选择。",
    )


def downgrade() -> None:
    """撤销本次新增表；正常升级不执行此函数。"""
    op.drop_table("sop_release", schema="content")
    op.drop_table("sop_definition", schema="content")
