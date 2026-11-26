"""补齐 RAG 审核审计及推荐表的数据库注释。"""

from collections.abc import Sequence

from alembic import op

revision: str = "0027_rag_review_comments"
down_revision: str | None = "0026_rag_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """把审核事实的每个字段写入 PostgreSQL 注释，便于审计核查。"""

    op.execute(
        "COMMENT ON TABLE app.recommendation_record IS "
        "'已放行推荐、所属轮次及前端读取的结构化内容。'"
    )
    recommendation_comments = {
        "recommendation_id": "推荐唯一编号。",
        "conversation_id": "推荐所属业务会话编号。",
        "turn_id": "产生推荐的客户轮次编号。",
        "status": "DRAFT=待审核；RELEASED=普通推荐；SENT=审核后已发送；REJECTED=草稿已拒绝。",
        "trace_id": "产生推荐时的追踪编号。",
        "payload": "通过契约校验的完整 RecommendationResult；旧演示记录可能为空对象。",
        "created_at": "推荐创建时间。",
    }
    review_comments = {
        "review_id": "审核记录编号。",
        "recommendation_id": "被审核的不可变推荐编号。",
        "conversation_id": "审核所属业务会话编号。",
        "turn_id": "审核所属客户轮次编号。",
        "run_id": "恢复审核动作对应的新图运行编号。",
        "decision_id": "坐席动作决定编号。",
        "actor_id": "执行审核的坐席主体编号。",
        "action": "审核动作：APPROVE=采用；EDIT=编辑后发送；REJECT=拒绝。",
        "original_draft": "生成模型原始草稿。",
        "edited_answer": "坐席编辑后的完整答复；采用或拒绝时为空。",
        "final_answer": "最终发送给客户的正文。",
        "note": "坐席处理说明。",
        "final_message_id": "最终客户可见人工消息编号。",
        "request_id": "审核入口请求编号。",
        "trace_id": "审核链路追踪编号。",
        "created_at": "审核完成时间。",
    }
    for table, comments in (
        ("app.recommendation_record", recommendation_comments),
        ("app.rag_review_record", review_comments),
    ):
        for column, comment in comments.items():
            op.execute(f"COMMENT ON COLUMN {table}.{column} IS '{comment}'")


def downgrade() -> None:
    """回退只移除新增的文字注释，不删除审核数据或表结构。"""

    # PostgreSQL 的注释是非业务数据；回退时清除本迁移补充的注释即可。
    for table, columns in (
        (
            "app.recommendation_record",
            (
                "recommendation_id",
                "conversation_id",
                "turn_id",
                "status",
                "trace_id",
                "payload",
                "created_at",
            ),
        ),
        (
            "app.rag_review_record",
            (
                "review_id",
                "recommendation_id",
                "conversation_id",
                "turn_id",
                "run_id",
                "decision_id",
                "actor_id",
                "action",
                "original_draft",
                "edited_answer",
                "final_answer",
                "note",
                "final_message_id",
                "request_id",
                "trace_id",
                "created_at",
            ),
        ),
    ):
        for column in columns:
            op.execute(f"COMMENT ON COLUMN {table}.{column} IS NULL")
