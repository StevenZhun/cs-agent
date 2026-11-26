"""建立 Trace Span、异步质检、Shadow 对照和样本候选表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0036_observe_quality"
down_revision: str | None = "0035_retention_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Trace 留在 app；异步质量事实写入独立 quality schema。"""

    op.create_table(
        "trace_span",
        sa.Column("span_id", sa.String(96), primary_key=True, comment="阶段记录编号。"),
        sa.Column("trace_id", sa.String(128), nullable=False, comment="端到端追踪编号。"),
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
            comment="所属客户轮次编号。",
        ),
        sa.Column(
            "run_id",
            sa.String(64),
            sa.ForeignKey("app.graph_run_record.run_id", ondelete="RESTRICT"),
            nullable=False,
            comment="所属图运行编号。",
        ),
        sa.Column("parent_span_id", sa.String(96), comment="父阶段编号。"),
        sa.Column("component", sa.String(32), nullable=False, comment="处理模块。"),
        sa.Column("operation", sa.String(64), nullable=False, comment="处理操作。"),
        sa.Column("status", sa.String(16), nullable=False, comment="完成或失败。"),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, comment="阶段开始时间。"
        ),
        sa.Column(
            "finished_at", sa.DateTime(timezone=True), nullable=False, comment="阶段结束时间。"
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=False, comment="耗时毫秒数。"),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="不含正文的有限诊断标签。",
        ),
        sa.Column("error_code", sa.String(64), comment="失败原因码。"),
        sa.CheckConstraint("status IN ('COMPLETED', 'FAILED')", name="ck_trace_span_status"),
        sa.CheckConstraint(
            "(status = 'FAILED') = (error_code IS NOT NULL)",
            name="ck_trace_span_failed_error_pair",
        ),
        schema="app",
        comment="端到端 Trace 的节点级耗时与有限状态标签；不得保存消息正文。",
    )
    op.create_index(
        "ix_trace_span_trace_started", "trace_span", ["trace_id", "started_at"], schema="app"
    )
    op.create_index(
        "ix_trace_span_run_started", "trace_span", ["run_id", "started_at"], schema="app"
    )

    op.create_table(
        "async_quality_job",
        sa.Column("job_id", sa.String(96), primary_key=True, comment="质检任务编号。"),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
            nullable=False,
            comment="所属业务会话编号。",
        ),
        sa.Column(
            "turn_id",
            sa.String(64),
            sa.ForeignKey("app.turn_record.turn_id", ondelete="RESTRICT"),
            nullable=False,
            comment="所属客户轮次编号。",
        ),
        sa.Column(
            "run_id",
            sa.String(64),
            sa.ForeignKey("app.graph_run_record.run_id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
            comment="被复核的图运行编号。",
        ),
        sa.Column("trace_id", sa.String(128), nullable=False, comment="端到端追踪编号。"),
        sa.Column("status", sa.String(16), nullable=False, comment="任务执行状态。"),
        sa.Column("attempts", sa.Integer(), nullable=False, comment="已经领取的次数。"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, comment="最大尝试次数。"),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), nullable=False, comment="允许领取时间。"
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), comment="当前领取时间。"),
        sa.Column("locked_by", sa.String(64), comment="当前工作者标识。"),
        sa.Column("last_error_code", sa.String(64), comment="最近失败原因码。"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, comment="任务创建时间。"
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), comment="最终结束时间。"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')",
            name="ck_async_quality_job_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts > 0", name="ck_async_quality_job_attempts"
        ),
        schema="quality",
        comment="回复发送后的异步质检任务；只保存应用记录引用，不复制消息正文。",
    )
    op.create_index(
        "ix_async_quality_job_poll",
        "async_quality_job",
        ["status", "available_at", "created_at"],
        schema="quality",
    )
    op.create_index(
        "ix_async_quality_job_trace", "async_quality_job", ["trace_id"], schema="quality"
    )

    op.create_table(
        "async_quality_result",
        sa.Column("result_id", sa.String(96), primary_key=True, comment="质检结果编号。"),
        sa.Column(
            "job_id",
            sa.String(96),
            sa.ForeignKey("quality.async_quality_job.job_id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
            comment="所属质检任务编号。",
        ),
        sa.Column("trace_id", sa.String(128), nullable=False, comment="端到端追踪编号。"),
        sa.Column("action", sa.String(16), nullable=False, comment="active 规则复核动作。"),
        sa.Column(
            "findings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="结构化发现；不包含正文。",
        ),
        sa.Column("evaluator", sa.String(64), nullable=False, comment="active 评估器。"),
        sa.Column("evaluator_version", sa.String(64), nullable=False, comment="评估器版本。"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, comment="结果形成时间。"
        ),
        sa.CheckConstraint(
            "action IN ('PASS', 'BLOCK', 'REWRITE', 'CLARIFY', 'HANDOFF')",
            name="ck_async_quality_result_action",
        ),
        schema="quality",
        comment="异步 active 质量复核结果；不得反向改写已发送回复。",
    )
    op.create_index(
        "ix_async_quality_result_trace",
        "async_quality_result",
        ["trace_id", "created_at"],
        schema="quality",
    )

    op.create_table(
        "shadow_comparison",
        sa.Column("comparison_id", sa.String(96), primary_key=True, comment="对照记录编号。"),
        sa.Column(
            "job_id",
            sa.String(96),
            sa.ForeignKey("quality.async_quality_job.job_id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
            comment="所属质检任务编号。",
        ),
        sa.Column("trace_id", sa.String(128), nullable=False, comment="端到端追踪编号。"),
        sa.Column("active_evaluator", sa.String(64), nullable=False, comment="active 评估器。"),
        sa.Column("shadow_evaluator", sa.String(64), nullable=False, comment="shadow 评估器。"),
        sa.Column("active_action", sa.String(16), nullable=False, comment="active 动作。"),
        sa.Column("shadow_action", sa.String(16), comment="shadow 建议动作。"),
        sa.Column("status", sa.String(24), nullable=False, comment="一致、分歧或失败。"),
        sa.Column(
            "shadow_signals", postgresql.JSONB(astext_type=sa.Text()), comment="shadow 原子信号。"
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=False, comment="shadow 耗时。"),
        sa.Column("error_code", sa.String(64), comment="shadow 失败原因码。"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, comment="对照形成时间。"
        ),
        sa.CheckConstraint(
            "status IN ('AGREED', 'DISAGREED', 'SHADOW_FAILED')",
            name="ck_shadow_comparison_status",
        ),
        sa.CheckConstraint(
            "(status = 'SHADOW_FAILED') = (error_code IS NOT NULL)",
            name="ck_shadow_comparison_failure_pair",
        ),
        schema="quality",
        comment="active 与 shadow 评估器的异步对照记录。",
    )
    op.create_index(
        "ix_shadow_comparison_trace",
        "shadow_comparison",
        ["trace_id", "created_at"],
        schema="quality",
    )

    op.create_table(
        "sample_candidate",
        sa.Column("candidate_id", sa.String(96), primary_key=True, comment="样本候选编号。"),
        sa.Column(
            "job_id",
            sa.String(96),
            sa.ForeignKey("quality.async_quality_job.job_id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
            comment="来源质检任务编号。",
        ),
        sa.Column("trace_id", sa.String(128), nullable=False, comment="端到端追踪编号。"),
        sa.Column("status", sa.String(24), nullable=False, comment="人工审核状态。"),
        sa.Column(
            "reason_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="形成候选的原因码。",
        ),
        sa.Column(
            "source_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="指向原事实的稳定引用。",
        ),
        sa.Column(
            "content_hashes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="用于去重的原文哈希。",
        ),
        sa.Column("reviewer_id", sa.String(64), comment="人工审核者编号。"),
        sa.Column("review_note", sa.Text(), comment="人工审核说明。"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, comment="候选创建时间。"
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), comment="人工审核时间。"),
        sa.CheckConstraint(
            "status IN ('PENDING_REVIEW', 'APPROVED', 'REJECTED')",
            name="ck_sample_candidate_status",
        ),
        schema="quality",
        comment="质检发现的待审核样本；不会自动进入规则、Prompt 或训练数据。",
    )
    op.create_index(
        "ix_quality_sample_candidate_status",
        "sample_candidate",
        ["status", "created_at"],
        schema="quality",
    )


def downgrade() -> None:
    """先删除依赖任务的质量事实，再删除 Trace Span。"""

    op.drop_index(
        "ix_quality_sample_candidate_status",
        table_name="sample_candidate",
        schema="quality",
    )
    op.drop_table("sample_candidate", schema="quality")
    op.drop_index("ix_shadow_comparison_trace", table_name="shadow_comparison", schema="quality")
    op.drop_table("shadow_comparison", schema="quality")
    op.drop_index(
        "ix_async_quality_result_trace",
        table_name="async_quality_result",
        schema="quality",
    )
    op.drop_table("async_quality_result", schema="quality")
    op.drop_index("ix_async_quality_job_trace", table_name="async_quality_job", schema="quality")
    op.drop_index("ix_async_quality_job_poll", table_name="async_quality_job", schema="quality")
    op.drop_table("async_quality_job", schema="quality")
    op.drop_index("ix_trace_span_run_started", table_name="trace_span", schema="app")
    op.drop_index("ix_trace_span_trace_started", table_name="trace_span", schema="app")
    op.drop_table("trace_span", schema="app")
