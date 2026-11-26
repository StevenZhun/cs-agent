"""记录分类模型调用的状态、用量和耗时。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_model_call_record"
down_revision: str | None = "0016_semantic_content"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """在应用库保存每次模型调用，便于解释响应耗时和上下文用量。"""

    op.create_table(
        "model_call_record",
        sa.Column("call_id", sa.String(96), primary_key=True, comment="模型调用编号。"),
        sa.Column(
            "run_id",
            sa.String(64),
            sa.ForeignKey("app.graph_run_record.run_id", ondelete="RESTRICT"),
            nullable=False,
            comment="所属图运行编号。",
        ),
        sa.Column(
            "turn_id",
            sa.String(64),
            sa.ForeignKey("app.turn_record.turn_id", ondelete="RESTRICT"),
            nullable=False,
            comment="所属会话轮次编号。",
        ),
        sa.Column("trace_id", sa.String(128), nullable=False, comment="所属链路追踪编号。"),
        sa.Column("purpose", sa.String(64), nullable=False, comment="调用用途，如 MODEL_ROUTING。"),
        sa.Column("provider", sa.String(64), nullable=False, comment="模型服务提供方。"),
        sa.Column("model", sa.String(128), nullable=False, comment="实际调用的模型名称。"),
        sa.Column("provider_request_id", sa.String(128), comment="供应商返回的请求编号。"),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            comment="调用状态：COMPLETED=完成；FAILED=失败；TIMEOUT=超时。",
        ),
        sa.Column("input_tokens", sa.Integer(), comment="供应商统计的输入 Token 数。"),
        sa.Column("output_tokens", sa.Integer(), comment="供应商统计的输出 Token 数。"),
        sa.Column("total_tokens", sa.Integer(), comment="供应商统计的输入加输出 Token 数。"),
        sa.Column(
            "context_window_tokens",
            sa.Integer(),
            comment="本次模型配置允许使用的上下文窗口上限。",
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=False, comment="本次调用耗时，单位毫秒。"),
        sa.Column("error_code", sa.String(64), comment="内部失败原因码；不直接展示给客户。"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, comment="调用开始记录时间。"
        ),
        sa.CheckConstraint(
            "status IN ('COMPLETED', 'FAILED', 'TIMEOUT')", name="status"
        ),
        schema="app",
        comment="一次真实模型调用的用量、耗时和结束信息；一个 Run 可有多条。",
    )
    op.create_index(
        "ix_model_call_record_run_created",
        "model_call_record",
        ["run_id", "created_at"],
        schema="app",
    )
    op.create_index(
        "ix_model_call_record_turn_created",
        "model_call_record",
        ["turn_id", "created_at"],
        schema="app",
    )


def downgrade() -> None:
    """删除模型调用记录及其查询索引。"""

    op.drop_index("ix_model_call_record_turn_created", table_name="model_call_record", schema="app")
    op.drop_index("ix_model_call_record_run_created", table_name="model_call_record", schema="app")
    op.drop_table("model_call_record", schema="app")
