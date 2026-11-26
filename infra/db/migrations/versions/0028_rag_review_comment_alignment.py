"""使 RAG 审核字段注释与 ORM 契约保持一致。"""

from collections.abc import Sequence

from alembic import op

revision: str = "0028_rag_review_align"
down_revision: str | None = "0027_rag_review_comments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """更新审核字段说明，不修改审核数据和约束。"""

    comments = {
        "action": "审核动作：APPROVE=采用；EDIT=编辑后发送；REJECT=拒绝。",
        "original_draft": "生成模型原始草稿。",
        "edited_answer": "坐席编辑后的完整答复；采用或拒绝时为空。",
        "final_answer": "最终发送给客户的正文。",
        "note": "坐席处理说明。",
        "final_message_id": "最终客户可见人工消息编号。",
        "request_id": "审核入口请求编号。",
        "trace_id": "审核链路追踪编号。",
    }
    for column, comment in comments.items():
        op.execute(f"COMMENT ON COLUMN app.rag_review_record.{column} IS '{comment}'")

def downgrade() -> None:
    """回到 0027 中较宽的字段说明，不回滚表结构。"""

    comments = {
        "action": "审核动作：APPROVE=采用草稿；EDIT=编辑后发送；REJECT=拒绝草稿。",
        "original_draft": "人工审核前保存的不可变 RAG 草稿。",
        "edited_answer": "坐席编辑后的答复；仅 EDIT 动作有值。",
        "final_answer": "最终写入客户消息的答复正文。",
        "note": "坐席审核说明；EDIT 和 REJECT 必填。",
        "final_message_id": "最终客户可见消息编号。",
        "request_id": "发起审核动作的请求编号。",
        "trace_id": "审核恢复运行的追踪编号。",
    }
    for column, comment in comments.items():
        op.execute(f"COMMENT ON COLUMN app.rag_review_record.{column} IS '{comment}'")
