"""推荐交互、反馈及会话结束小结都由应用库持久化。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0008_app_engagement"
down_revision: str | None = "0007_run_decision_trace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建五张应用表，并在建表时提交中文表/列注释。"""

    op.create_table(
        "recommendation_record",
        sa.Column("recommendation_id", sa.String(64), primary_key=True, comment="推荐唯一编号。"),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
            nullable=False,
            comment="推荐所属业务会话编号。",
        ),
        sa.Column(
            "turn_id",
            sa.String(64),
            sa.ForeignKey("app.turn_record.turn_id", ondelete="RESTRICT"),
            nullable=False,
            comment="产生推荐的客户轮次编号。",
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            comment="DRAFT=未放行；RELEASED=已放行供坐席查看。",
        ),
        sa.Column("trace_id", sa.String(128), nullable=False, comment="产生推荐时的追踪编号。"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, comment="推荐创建时间。"
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'RELEASED')", name="ck_recommendation_record_status"
        ),
        sa.UniqueConstraint("recommendation_id", "turn_id", name="uq_recommendation_turn"),
        schema="app",
        comment="推荐身份与所属轮次；真实推荐由后续阶段生成。",
    )
    op.create_table(
        "recommendation_interaction_event",
        sa.Column("interaction_id", sa.String(64), primary_key=True, comment="交互事件唯一编号。"),
        sa.Column("request_id", sa.String(128), nullable=False, comment="本次 HTTP 请求编号。"),
        sa.Column(
            "recommendation_id",
            sa.String(64),
            sa.ForeignKey("app.recommendation_record.recommendation_id", ondelete="RESTRICT"),
            nullable=False,
            comment="已放行推荐编号。",
        ),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
            nullable=False,
            comment="推荐所属业务会话编号。",
        ),
        sa.Column(
            "turn_id",
            sa.String(64),
            sa.ForeignKey("app.turn_record.turn_id", ondelete="RESTRICT"),
            nullable=False,
            comment="推荐所属客户轮次编号。",
        ),
        sa.Column(
            "actor_id",
            sa.String(64),
            sa.ForeignKey("app.subject.subject_id", ondelete="RESTRICT"),
            nullable=False,
            comment="执行交互的坐席主体编号。",
        ),
        sa.Column(
            "action",
            sa.String(16),
            nullable=False,
            comment="VIEWED=查看；COPIED=复制；INSERTED=插入；DISMISSED=忽略；ESCALATED=升级处理；均不代表已采纳。",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, comment="交互发生时间。"
        ),
        schema="app",
        comment="推荐的查看、复制等客观交互事件。",
    )
    op.create_table(
        "recommendation_feedback",
        sa.Column("feedback_id", sa.String(64), primary_key=True, comment="反馈唯一编号。"),
        sa.Column("request_id", sa.String(128), nullable=False, comment="本次 HTTP 请求编号。"),
        sa.Column(
            "recommendation_id",
            sa.String(64),
            sa.ForeignKey("app.recommendation_record.recommendation_id", ondelete="RESTRICT"),
            nullable=False,
            comment="已放行推荐编号。",
        ),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
            nullable=False,
            comment="推荐所属业务会话编号。",
        ),
        sa.Column(
            "turn_id",
            sa.String(64),
            sa.ForeignKey("app.turn_record.turn_id", ondelete="RESTRICT"),
            nullable=False,
            comment="推荐所属客户轮次编号。",
        ),
        sa.Column("trace_id", sa.String(128), nullable=False, comment="原推荐的追踪编号。"),
        sa.Column(
            "actor_id",
            sa.String(64),
            sa.ForeignKey("app.subject.subject_id", ondelete="RESTRICT"),
            nullable=False,
            comment="提交反馈的坐席主体编号。",
        ),
        sa.Column(
            "adoption_status",
            sa.String(24),
            nullable=False,
            comment="ADOPTED=直接采用；ADOPTED_AFTER_EDIT=修改后采用；NOT_ADOPTED=未采用；NOT_RATED=未评价。",
        ),
        sa.Column(
            "helpfulness",
            sa.String(24),
            nullable=False,
            comment="HELPFUL=有帮助；PARTIALLY_HELPFUL=部分有帮助；NOT_HELPFUL=无帮助；NOT_RATED=未评价。",
        ),
        sa.Column("result_message_id", sa.String(64), comment="修改后采用所关联的最终消息编号。"),
        sa.Column("edit_summary", sa.Text(), comment="修改后采用时的差异摘要，不复制敏感全文。"),
        sa.Column("reason_codes", JSONB(), nullable=False, comment="坐席选择的原因代码数组。"),
        sa.Column("comment_text", sa.Text(), comment="坐席补充评价。"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, comment="反馈提交时间。"
        ),
        schema="app",
        comment="坐席对推荐的采用、帮助程度和修改说明。",
    )
    op.create_table(
        "conversation_wrap_up",
        sa.Column("wrap_up_id", sa.String(64), primary_key=True, comment="结束小结唯一编号。"),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
            unique=True,
            nullable=False,
            comment="一条业务会话对应一份当前小结。",
        ),
        sa.Column("version", sa.Integer(), nullable=False, comment="从 1 起递增的小结版本号。"),
        sa.Column(
            "status", sa.String(16), nullable=False, comment="DRAFT=待坐席确认；CONFIRMED=已确认。"
        ),
        sa.Column("topic_summary", sa.Text(), nullable=False, comment="坐席可修改的咨询主题摘要。"),
        sa.Column(
            "intent_ids", JSONB(), nullable=False, comment="已确认的动作意图编号数组；未知时为空。"
        ),
        sa.Column(
            "resolved_items", JSONB(), nullable=False, comment="已解决事项数组；未确认时不得臆测。"
        ),
        sa.Column("unresolved_items", JSONB(), nullable=False, comment="尚未解决事项数组。"),
        sa.Column(
            "actions_taken",
            JSONB(),
            nullable=False,
            comment="实际已执行动作数组；不把模型建议当实际动作。",
        ),
        sa.Column("pending_follow_ups", JSONB(), nullable=False, comment="后续待跟进事项数组。"),
        sa.Column("risk_flags", JSONB(), nullable=False, comment="风险标记数组。"),
        sa.Column(
            "source_message_ids", JSONB(), nullable=False, comment="支撑小结的原始消息编号数组。"
        ),
        sa.Column(
            "source_turn_ids", JSONB(), nullable=False, comment="支撑小结的客户轮次编号数组。"
        ),
        sa.Column(
            "generated_at", sa.DateTime(timezone=True), nullable=False, comment="草稿生成时间。"
        ),
        sa.Column(
            "generated_by",
            sa.String(64),
            nullable=False,
            comment="生成方式及版本，例如 RULES_V1 或 LEGACY_IMPORT。",
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, comment="当前版本更新时间。"
        ),
        sa.Column("last_edited_by", sa.String(64), comment="最后编辑的坐席主体编号。"),
        sa.Column("confirmed_by", sa.String(64), comment="确认小结的坐席主体编号。"),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), comment="坐席确认时间。"),
        schema="app",
        comment="坐席可编辑确认的结构化会话结束小结当前版。",
    )
    op.create_table(
        "conversation_wrap_up_audit",
        sa.Column("audit_id", sa.String(64), primary_key=True, comment="审计记录唯一编号。"),
        sa.Column(
            "wrap_up_id",
            sa.String(64),
            sa.ForeignKey("app.conversation_wrap_up.wrap_up_id", ondelete="RESTRICT"),
            nullable=False,
            comment="所属结束小结编号。",
        ),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
            nullable=False,
            comment="所属业务会话编号。",
        ),
        sa.Column("version", sa.Integer(), nullable=False, comment="对应的小结版本号。"),
        sa.Column(
            "action",
            sa.String(16),
            nullable=False,
            comment="CREATE=生成草稿；EDIT=坐席编辑；CONFIRM=坐席确认。",
        ),
        sa.Column("actor_id", sa.String(64), nullable=False, comment="生成程序或操作坐席的标识。"),
        sa.Column("snapshot", JSONB(), nullable=False, comment="本版本的小结内容快照。"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, comment="审计记录创建时间。"
        ),
        schema="app",
        comment="会话结束小结的逐版本审计快照。",
    )


def downgrade() -> None:
    """仅供空演示库回退；正式库删除会丢失交互与小结审计。"""

    for table in (
        "conversation_wrap_up_audit",
        "conversation_wrap_up",
        "recommendation_feedback",
        "recommendation_interaction_event",
        "recommendation_record",
    ):
        op.drop_table(table, schema="app")
