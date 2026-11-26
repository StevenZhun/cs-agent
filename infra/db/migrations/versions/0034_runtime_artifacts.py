"""保存可恢复运行产物，避免全文证据和大工具结果反复进入 checkpoint。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034_runtime_artifacts"
down_revision: str | None = "0033_context_summary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_artifact",
        sa.Column("artifact_id", sa.String(96), primary_key=True, comment="运行产物唯一编号。"),
        sa.Column(
            "artifact_kind",
            sa.String(32),
            nullable=False,
            comment="RAG_RETRIEVAL、RAG_DRAFT 或 SOP_TOOL_RESULTS。",
        ),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
            nullable=False,
            comment="所属业务会话编号。",
        ),
        sa.Column(
            "thread_id",
            sa.String(64),
            sa.ForeignKey("app.conversation_thread_mapping.thread_id", ondelete="RESTRICT"),
            nullable=False,
            comment="所属 LangGraph 线程编号。",
        ),
        sa.Column(
            "turn_id",
            sa.String(64),
            sa.ForeignKey("app.turn_record.turn_id", ondelete="RESTRICT"),
            nullable=False,
            comment="生成产物的 Turn 编号。",
        ),
        sa.Column(
            "run_id",
            sa.String(64),
            sa.ForeignKey("app.graph_run_record.run_id", ondelete="RESTRICT"),
            nullable=False,
            comment="生成产物的图运行编号。",
        ),
        sa.Column("schema_version", sa.String(32), nullable=False, comment="产物契约版本。"),
        sa.Column("payload", postgresql.JSONB(), nullable=False, comment="完整状态外产物。"),
        sa.Column("content_hash", sa.String(64), nullable=False, comment="载荷 SHA-256。"),
        sa.Column("release_version", sa.String(64), comment="绑定的内容发布版本。"),
        sa.Column("index_version", sa.String(64), comment="绑定的检索索引版本。"),
        sa.Column(
            "status", sa.String(16), nullable=False, comment="ACTIVE=可回装；EXPIRED=已过期。"
        ),
        sa.Column(
            "expires_at", sa.DateTime(timezone=True), nullable=False, comment="允许回装的截止时间。"
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, comment="产物创建时间。"
        ),
        sa.CheckConstraint(
            "artifact_kind IN ('RAG_RETRIEVAL', 'RAG_DRAFT', 'SOP_TOOL_RESULTS')",
            name="artifact_kind",
        ),
        sa.CheckConstraint("status IN ('ACTIVE', 'EXPIRED')", name="status"),
        schema="app",
        comment="可恢复的 RAG 证据、草稿与大工具结果；checkpoint 只保存稳定引用。",
    )
    op.create_index(
        "ix_runtime_artifact_conversation_created",
        "runtime_artifact",
        ["conversation_id", "created_at"],
        schema="app",
    )
    op.create_index(
        "ix_runtime_artifact_expiry",
        "runtime_artifact",
        ["status", "expires_at"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index("ix_runtime_artifact_expiry", table_name="runtime_artifact", schema="app")
    op.drop_index(
        "ix_runtime_artifact_conversation_created",
        table_name="runtime_artifact",
        schema="app",
    )
    op.drop_table("runtime_artifact", schema="app")
