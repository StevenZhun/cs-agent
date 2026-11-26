"""为 RAG 人工审核增加独立截止时间和过期状态。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_rag_review_expiry"
down_revision: str | None = "0036_observe_quality"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """草稿审核时钟独立于运行产物保留期，普通推荐继续保持空值。"""

    op.add_column(
        "recommendation_record",
        sa.Column(
            "review_expires_at",
            sa.DateTime(timezone=True),
            comment="RAG 草稿允许坐席审核的截止时间；普通推荐为空。",
        ),
        schema="app",
    )
    op.execute(
        "UPDATE app.recommendation_record "
        "SET review_expires_at = created_at + INTERVAL '30 minutes', "
        "payload = jsonb_set(payload, '{review_expires_at}', "
        "to_jsonb(created_at + INTERVAL '30 minutes'), true) "
        "WHERE status = 'DRAFT' AND payload <> '{}'::jsonb"
    )
    op.drop_constraint("status", "recommendation_record", schema="app", type_="check")
    op.create_check_constraint(
        "status",
        "recommendation_record",
        "status IN ('DRAFT', 'RELEASED', 'SENT', 'REJECTED', 'EXPIRED')",
        schema="app",
    )
    op.alter_column(
        "recommendation_record",
        "status",
        schema="app",
        existing_type=sa.String(16),
        comment=(
            "DRAFT=待审核；RELEASED=普通推荐；SENT=审核后已发送；"
            "REJECTED=草稿已拒绝；EXPIRED=审核窗口已过期。"
        ),
    )
    op.create_table(
        "rag_regeneration_record",
        sa.Column(
            "regeneration_id",
            sa.String(64),
            primary_key=True,
            comment="重新生成记录编号。",
        ),
        sa.Column(
            "previous_recommendation_id",
            sa.String(64),
            sa.ForeignKey("app.recommendation_record.recommendation_id", ondelete="RESTRICT"),
            nullable=False,
            comment="已过期的原推荐编号。",
        ),
        sa.Column(
            "next_recommendation_id",
            sa.String(64),
            sa.ForeignKey("app.recommendation_record.recommendation_id", ondelete="RESTRICT"),
            nullable=False,
            comment="重新检索后形成的新推荐编号。",
        ),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
            nullable=False,
            comment="重新生成所属业务会话编号。",
        ),
        sa.Column(
            "turn_id",
            sa.String(64),
            sa.ForeignKey("app.turn_record.turn_id", ondelete="RESTRICT"),
            nullable=False,
            comment="原审核草稿所属客户轮次编号。",
        ),
        sa.Column(
            "run_id",
            sa.String(64),
            sa.ForeignKey("app.graph_run_record.run_id", ondelete="RESTRICT"),
            nullable=False,
            comment="重新检索生成草稿的图运行编号。",
        ),
        sa.Column(
            "decision_id",
            sa.String(64),
            nullable=False,
            comment="坐席重新生成决策编号。",
        ),
        sa.Column(
            "actor_id",
            sa.String(64),
            sa.ForeignKey("app.subject.subject_id", ondelete="RESTRICT"),
            nullable=False,
            comment="发起重新生成的坐席主体编号。",
        ),
        sa.Column(
            "previous_draft_id",
            sa.String(96),
            nullable=False,
            comment="已过期的原 RAG 草稿编号。",
        ),
        sa.Column(
            "next_draft_id",
            sa.String(96),
            nullable=False,
            comment="重新检索生成的新 RAG 草稿编号。",
        ),
        sa.Column("note", sa.Text(), nullable=False, comment="坐席发起重新生成时填写的原因。"),
        sa.Column(
            "request_id",
            sa.String(128),
            nullable=False,
            comment="重新生成入口请求编号。",
        ),
        sa.Column(
            "trace_id",
            sa.String(128),
            nullable=False,
            comment="重新生成链路追踪编号。",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="重新生成审计记录创建时间。",
        ),
        sa.UniqueConstraint("decision_id", name="uq_rag_regeneration_record_decision"),
        sa.UniqueConstraint(
            "next_recommendation_id",
            name="uq_rag_regeneration_record_next_recommendation",
        ),
        schema="app",
        comment="坐席对过期草稿发起重新检索的审计；不产生客户可见消息。",
    )


def downgrade() -> None:
    """回退前将过期草稿恢复为待审核，避免旧约束无法建立。"""

    op.drop_table("rag_regeneration_record", schema="app")
    op.execute("UPDATE app.recommendation_record SET status = 'DRAFT' WHERE status = 'EXPIRED'")
    op.drop_constraint("status", "recommendation_record", schema="app", type_="check")
    op.create_check_constraint(
        "status",
        "recommendation_record",
        "status IN ('DRAFT', 'RELEASED', 'SENT', 'REJECTED')",
        schema="app",
    )
    op.drop_column("recommendation_record", "review_expires_at", schema="app")
