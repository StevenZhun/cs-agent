"""保存 RAG 人工审核、最终消息和可恢复图快照。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0026_rag_review"
down_revision = "0025_intent_class_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """扩展推荐生命周期，并新增一次性 RAG 审核审计。"""

    # 旧库可能来自迁移或导出的 bootstrap，约束名不同；两种都兼容。
    op.execute(
        "ALTER TABLE app.recommendation_record "
        "DROP CONSTRAINT IF EXISTS ck_recommendation_record_ck_recommendation_record_status"
    )
    op.execute(
        "ALTER TABLE app.recommendation_record "
        "DROP CONSTRAINT IF EXISTS ck_recommendation_record_status"
    )
    op.create_check_constraint(
        "status",
        "recommendation_record",
        "status IN ('DRAFT', 'RELEASED', 'SENT', 'REJECTED')",
        schema="app",
    )
    op.alter_column(
        "recommendation_record",
        "status",
        schema="app",
        existing_type=sa.String(16),
        comment="DRAFT=待审核；RELEASED=普通推荐；SENT=审核后已发送；REJECTED=草稿已拒绝。",
    )
    op.add_column(
        "graph_run_record",
        sa.Column(
            "rag_snapshot",
            JSONB,
            comment="RAG 检索、不可变草稿和人工审核终态；客户可见答复仍以消息表为准。",
        ),
        schema="app",
    )
    op.create_table(
        "rag_review_record",
        sa.Column("review_id", sa.String(64), primary_key=True, comment="审核记录编号。"),
        sa.Column(
            "recommendation_id",
            sa.String(64),
            sa.ForeignKey("app.recommendation_record.recommendation_id", ondelete="RESTRICT"),
            nullable=False,
            comment="被审核的不可变推荐编号。",
        ),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "turn_id",
            sa.String(64),
            sa.ForeignKey("app.turn_record.turn_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(64),
            sa.ForeignKey("app.graph_run_record.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("decision_id", sa.String(64), nullable=False),
        sa.Column(
            "actor_id",
            sa.String(64),
            sa.ForeignKey("app.subject.subject_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("original_draft", sa.Text, nullable=False),
        sa.Column("edited_answer", sa.Text),
        sa.Column("final_answer", sa.Text, nullable=False),
        sa.Column("note", sa.Text, nullable=False),
        sa.Column(
            "final_message_id",
            sa.String(64),
            sa.ForeignKey("app.chat_history.message_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action IN ('APPROVE', 'EDIT', 'REJECT')", name="action"),
        sa.UniqueConstraint("recommendation_id", name="recommendation"),
        sa.UniqueConstraint("decision_id", name="decision"),
        sa.UniqueConstraint("final_message_id", name="final_message"),
        schema="app",
        comment="RAG 草稿人工审核审计；每份推荐只允许形成一个终局动作。",
    )


def downgrade() -> None:
    """仅在不存在已处理草稿时收窄结构，避免回退时静默删除审计数据。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM app.recommendation_record
                WHERE status IN ('SENT', 'REJECTED')
            ) THEN
                RAISE EXCEPTION 'cannot downgrade while reviewed RAG recommendations exist';
            END IF;
        END $$
        """
    )
    op.drop_table("rag_review_record", schema="app")
    op.drop_column("graph_run_record", "rag_snapshot", schema="app")
    op.execute(
        "ALTER TABLE app.recommendation_record "
        "DROP CONSTRAINT IF EXISTS ck_recommendation_record_status"
    )
    op.create_check_constraint(
        "status",
        "recommendation_record",
        "status IN ('DRAFT', 'RELEASED')",
        schema="app",
    )
    op.alter_column(
        "recommendation_record",
        "status",
        schema="app",
        existing_type=sa.String(16),
        comment="DRAFT=未放行；RELEASED=已放行供坐席查看。",
    )
