"""SQLAlchemy models owned by the PostgreSQL app schema."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.persistence import PostgresBase
from shared.postgres_comments import apply_model_comments

Base = PostgresBase


class SubjectModel(Base):
    """Local application identity; customer business data stays out of this table."""

    __tablename__ = "subject"
    __table_args__ = (
        CheckConstraint("subject_type IN ('CUSTOMER', 'STAFF')", name="subject_type"),
        CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="status"),
        {"schema": "app"},
    )

    subject_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    username: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class RoleModel(Base):
    """Small stable RBAC role catalog."""

    __tablename__ = "role"
    __table_args__ = {"schema": "app"}

    role_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)


class SubjectRoleModel(Base):
    """Many-to-many assignment between local subjects and roles."""

    __tablename__ = "subject_role"
    __table_args__ = {"schema": "app"}

    subject_id: Mapped[str] = mapped_column(
        ForeignKey("app.subject.subject_id", ondelete="CASCADE"), primary_key=True
    )
    role_code: Mapped[str] = mapped_column(
        ForeignKey("app.role.role_code", ondelete="RESTRICT"), primary_key=True
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SubjectCustomerBindingModel(Base):
    """Application-owned authorization link to a business customer identifier."""

    __tablename__ = "subject_customer_binding"
    __table_args__ = (
        CheckConstraint(
            "relationship IN ('SELF', 'ASSIGNED_AGENT', 'SUPERVISOR')",
            name="relationship",
        ),
        UniqueConstraint(
            "subject_id",
            "customer_id",
            "relationship",
            name="uq_subject_customer_relationship",
        ),
        Index("ix_subject_customer_active", "subject_id", "customer_id", "active"),
        {"schema": "app"},
    )

    binding_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[str] = mapped_column(
        ForeignKey("app.subject.subject_id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[str] = mapped_column(
        ForeignKey("business.customer.customer_id", ondelete="RESTRICT"), nullable=False
    )
    relationship: Mapped[str] = mapped_column(String(32), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdempotencyRecordModel(Base):
    """BFF-owned result record for retry-safe mutation endpoints."""

    __tablename__ = "idempotency_record"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PROCESSING', 'COMPLETED', 'FAILED')",
            name="status",
        ),
        {"schema": "app"},
    )

    subject_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation: Mapped[str] = mapped_column(String(96), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    response_status: Mapped[int | None]
    response_body: Mapped[dict[str, object] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ConversationModel(Base):
    """Application-owned business conversation visible to the frontend."""

    __tablename__ = "conversation"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'WRAP_UP', 'FINISHED')", name="status"),
        Index("ix_conversation_customer_status", "customer_id", "status"),
        {"schema": "app"},
    )

    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str | None] = mapped_column(
        String(30),
        comment="用户主动设置的会话标题；为空时，页面根据首条客户消息生成默认标题。",
    )
    customer_id: Mapped[str] = mapped_column(
        ForeignKey("business.customer.customer_id", ondelete="RESTRICT"), nullable=False
    )
    created_by_subject_id: Mapped[str] = mapped_column(
        ForeignKey("app.subject.subject_id", ondelete="RESTRICT"), nullable=False
    )
    assigned_agent_subject_id: Mapped[str | None] = mapped_column(
        ForeignKey("app.subject.subject_id", ondelete="SET NULL")
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ConversationHiddenModel(Base):
    """客户从自己的列表软删除已结束咨询；历史消息及坐席审计仍保留。"""

    __tablename__ = "conversation_hidden"
    __table_args__ = {
        "schema": "app",
        "comment": "当前主体隐藏的已结束会话；不物理删除消息、checkpoint 或业务操作。",
    }
    subject_id: Mapped[str] = mapped_column(
        ForeignKey("app.subject.subject_id", ondelete="CASCADE"),
        primary_key=True,
        comment="执行软删除的登录主体；不影响其他主体的列表。",
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation.conversation_id", ondelete="CASCADE"),
        primary_key=True,
        comment="被隐藏的已结束咨询编号；恢复时只移除本隐藏记录。",
    )
    hidden_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="客户确认软删除的时间；与最后一条消息时间无关。",
    )


class ConversationPinModel(Base):
    """某个登录主体置顶的一条业务会话，不影响其他主体的列表。"""

    __tablename__ = "conversation_pin"
    __table_args__ = (
        Index("ix_conversation_pin_subject_time", "subject_id", "pinned_at"),
        {"schema": "app", "comment": "登录主体的个人会话置顶；不同主体互不影响。"},
    )

    subject_id: Mapped[str] = mapped_column(
        ForeignKey("app.subject.subject_id", ondelete="CASCADE"),
        primary_key=True,
        comment="执行置顶操作的应用主体 ID。",
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation.conversation_id", ondelete="CASCADE"),
        primary_key=True,
        comment="被当前主体置顶的业务会话 ID。",
    )
    pinned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="最近一次置顶时间；用于排列多条置顶会话。",
    )


class ConversationThreadMappingModel(Base):
    """Stable business conversation to LangGraph thread mapping."""

    __tablename__ = "conversation_thread_mapping"
    __table_args__ = ({"schema": "app"},)

    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation.conversation_id", ondelete="CASCADE"), primary_key=True
    )
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TurnRecordModel(Base):
    """Application-owned lifecycle record for one incoming customer turn."""

    __tablename__ = "turn_record"
    __table_args__ = (
        CheckConstraint(
            "status IN ('RUNNING', 'INTERRUPTED', 'COMPLETED', 'FAILED')", name="status"
        ),
        CheckConstraint(
            "terminal_response_path IS NULL OR terminal_response_path IN "
            "('FAQ', 'SOP', 'RAG', 'HUMAN', 'CLARIFICATION', 'SAFE_FALLBACK')",
            name="terminal_response_path",
        ),
        CheckConstraint(
            "(terminal_response_path IS NULL) = (terminal_reason_code IS NULL)",
            name="terminal_outcome_pair",
        ),
        Index("ix_turn_record_conversation_started", "conversation_id", "started_at"),
        Index(
            "ix_turn_record_conversation_cycle_completed",
            "conversation_id",
            "context_cycle_id",
            "completed_at",
        ),
        {"schema": "app"},
    )

    turn_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"), nullable=False
    )
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation_thread_mapping.thread_id", ondelete="RESTRICT"),
        nullable=False,
    )
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_message_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    routing_decision: Mapped[dict[str, object] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )
    terminal_response_path: Mapped[str | None] = mapped_column(
        String(32),
        comment=(
            "客户最终收到的响应路径：FAQ=问答；SOP=流程；RAG=检索草稿；HUMAN=人工；"
            "CLARIFICATION=澄清；SAFE_FALLBACK=安全兜底。与路由阶段的建议路径分开保存。"
        ),
    )
    terminal_reason_code: Mapped[str | None] = mapped_column(
        String(64), comment="最终响应原因码；用于解释路由建议与客户实收为何不同。"
    )
    context_cycle_id: Mapped[str | None] = mapped_column(
        String(96),
        comment="本轮完成时所属的模型上下文累计周期；上下文压缩开启新周期。",
    )
    active_run_id: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GraphRunRecordModel(Base):
    """一个 Turn 中的一次图运行；租约字段控制超时接管和旧请求回写。"""

    __tablename__ = "graph_run_record"
    __table_args__ = (
        CheckConstraint(
            "status IN ('RUNNING', 'INTERRUPTED', 'COMPLETED', 'FAILED')", name="status"
        ),
        CheckConstraint("decision_profile IN ('BASELINE', 'JEV')", name="decision_profile"),
        Index("ix_graph_run_record_turn_started", "turn_id", "started_at"),
        Index("ix_graph_run_record_recovery", "status", "lease_expires_at"),
        {"schema": "app"},
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sop_snapshot: Mapped[dict[str, object] | None] = mapped_column(
        JSONB, comment="与助手消息一起保存的 SOP 进度与暂停原因，供坐席查看；不是人工审批记录。"
    )
    rag_snapshot: Mapped[dict[str, object] | None] = mapped_column(
        JSONB,
        comment="RAG 检索、不可变草稿和人工审核终态；客户可见答复仍以消息表为准。",
    )
    resume_payload: Mapped[dict[str, object] | None] = mapped_column(
        JSONB,
        comment="经坐席授权后保存的恢复动作：处理编号、操作者、动作、更正字段、核验声明与请求时间；后台恢复沿用原动作。",
    )
    business_tool_results: Mapped[list[dict[str, object]] | None] = mapped_column(
        JSONB,
        comment=(
            "本轮 SOP 使用的只读工具结果：调用编号、来源轮次、输入、状态、事实与耗时；"
            "可能引用已保存的原轮结果。"
        ),
    )
    turn_id: Mapped[str] = mapped_column(
        ForeignKey("app.turn_record.turn_id", ondelete="RESTRICT"), nullable=False
    )
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation_thread_mapping.thread_id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    decision_profile: Mapped[str] = mapped_column(String(16), nullable=False)
    decision_ids: Mapped[list[str]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    decision_trace: Mapped[dict[str, object] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        comment="本次图运行的结构化 Query 决策追踪；空值表示旧 Run 或尚未生成决策。",
    )
    routing_trace: Mapped[dict[str, object] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        comment="本次运行的确定性路由追踪；包含规则版本、命中状态与原因。",
    )
    resumed_from_run_id: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="本次运行允许原工作者处理到的截止时间；过期后恢复者才可尝试接管。",
    )
    lease_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="接管版本号；每次恢复者接管加一，旧版本不得再写入助手回复。",
    )
    lease_owner: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="ONLINE",
        comment="当前处理来源：ONLINE=在线请求；RECOVERY=后台恢复者。",
    )


class ModelCallRecordModel(Base):
    """记录分类或 SOP 消息理解的真实调用用量；完整对话仍由消息表保存。"""

    __tablename__ = "model_call_record"
    __table_args__ = (
        CheckConstraint("status IN ('COMPLETED', 'FAILED', 'TIMEOUT')", name="status"),
        Index("ix_model_call_record_run_created", "run_id", "created_at"),
        Index("ix_model_call_record_turn_created", "turn_id", "created_at"),
        {
            "schema": "app",
            "comment": "一次真实模型调用的用量、耗时和结束信息；一个 Run 可有多条。",
        },
    )

    call_id: Mapped[str] = mapped_column(String(96), primary_key=True, comment="模型调用编号。")
    run_id: Mapped[str] = mapped_column(
        ForeignKey("app.graph_run_record.run_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属图运行编号。",
    )
    turn_id: Mapped[str] = mapped_column(
        ForeignKey("app.turn_record.turn_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属会话轮次编号。",
    )
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, comment="所属链路追踪编号。")
    purpose: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="调用用途，如 MODEL_ROUTING。"
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, comment="模型服务提供方。")
    model: Mapped[str] = mapped_column(String(128), nullable=False, comment="实际调用的模型名称。")
    provider_request_id: Mapped[str | None] = mapped_column(
        String(128), comment="供应商返回的请求编号。"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="调用状态：COMPLETED=完成；FAILED=失败；TIMEOUT=超时。",
    )
    estimated_input_tokens: Mapped[int | None] = mapped_column(
        Integer,
        comment="发送前按最终模型请求载荷估算的输入 Token 数。",
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer, comment="供应商统计的输入 Token 数。")
    output_tokens: Mapped[int | None] = mapped_column(
        Integer, comment="供应商统计的输出 Token 数。"
    )
    total_tokens: Mapped[int | None] = mapped_column(
        Integer, comment="供应商统计的输入加输出 Token 数。"
    )
    context_window_tokens: Mapped[int | None] = mapped_column(
        Integer, comment="本次模型配置允许使用的上下文窗口上限。"
    )
    latency_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="本次调用耗时，单位毫秒。"
    )
    error_code: Mapped[str | None] = mapped_column(
        String(64), comment="内部失败原因码；不直接展示给客户。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="调用开始记录时间。"
    )


class TraceSpanModel(Base):
    """一个 Run 内单个图节点或运行边界的结构化耗时记录。"""

    __tablename__ = "trace_span"
    __table_args__ = (
        CheckConstraint("status IN ('COMPLETED', 'FAILED')", name="status"),
        CheckConstraint(
            "(status = 'FAILED') = (error_code IS NOT NULL)",
            name="failed_error_pair",
        ),
        Index("ix_trace_span_trace_started", "trace_id", "started_at"),
        Index("ix_trace_span_run_started", "run_id", "started_at"),
        {
            "schema": "app",
            "comment": "端到端 Trace 的节点级耗时与有限状态标签；不得保存消息正文。",
        },
    )

    span_id: Mapped[str] = mapped_column(String(96), primary_key=True, comment="阶段记录编号。")
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, comment="端到端追踪编号。")
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属业务会话编号。",
    )
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation_thread_mapping.thread_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属 LangGraph 线程编号。",
    )
    turn_id: Mapped[str] = mapped_column(
        ForeignKey("app.turn_record.turn_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属客户轮次编号。",
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("app.graph_run_record.run_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属图运行编号。",
    )
    parent_span_id: Mapped[str | None] = mapped_column(
        String(96), comment="父阶段编号；首版图节点通常为空。"
    )
    component: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="CONTEXT、ROUTING、FAQ、SOP、RAG、QUALITY 等模块。"
    )
    operation: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="稳定的图节点或运行边界名称。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="COMPLETED=完成；FAILED=失败。"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="阶段开始时间。"
    )
    finished_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="阶段结束时间。"
    )
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, comment="阶段耗时，单位毫秒。")
    attributes: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, comment="不含正文的路径、状态、候选数量等有限诊断标签。"
    )
    error_code: Mapped[str | None] = mapped_column(String(64), comment="失败原因码；成功阶段为空。")


class ContextSummaryRecordModel(Base):
    """状态外不可变摘要；checkpoint 只保留当前聚合摘要和稳定引用。"""

    __tablename__ = "context_summary_record"
    __table_args__ = (
        CheckConstraint(
            "record_kind IN ('SEGMENT', 'AGGREGATE', 'BASELINE')",
            name="record_kind",
        ),
        CheckConstraint(
            "validation_status IN ('VALID', 'EXTRACTIVE_FALLBACK')",
            name="validation_status",
        ),
        Index("ix_context_summary_conversation_created", "conversation_id", "created_at"),
        {
            "schema": "app",
            "comment": "状态外不可变摘要记录；Graph State 只保存当前摘要和有限引用。",
        },
    )

    summary_record_id: Mapped[str] = mapped_column(
        String(96), primary_key=True, comment="不可变摘要记录编号。"
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属业务会话编号。",
    )
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation_thread_mapping.thread_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属 LangGraph 线程编号。",
    )
    context_cycle_id: Mapped[str] = mapped_column(
        String(96), nullable=False, comment="生成本摘要的上下文累计周期编号。"
    )
    record_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="摘要层级：SEGMENT、AGGREGATE 或 BASELINE。"
    )
    parent_summary_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("app.context_summary_record.summary_record_id", ondelete="RESTRICT"),
        comment="生成本记录时采用的直接上层摘要编号；没有上层时为空。",
    )
    summary_text: Mapped[str] = mapped_column(Text, nullable=False, comment="脱敏后的摘要正文。")
    covered_turn_ids: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        comment="本摘要直接或间接覆盖的 Turn 编号列表。",
    )
    source_refs: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        comment="可回查原始消息或上层摘要的有限来源引用。",
    )
    generation_method: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="生成方式，如 MODEL 或 EXTRACTIVE_FALLBACK。"
    )
    prompt_version: Mapped[str | None] = mapped_column(
        String(64), comment="模型摘要使用的提示词版本；确定性降级时可为空。"
    )
    model: Mapped[str | None] = mapped_column(
        String(128), comment="生成摘要的模型名称；未调用模型时为空。"
    )
    validation_status: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="校验结果：VALID 或 EXTRACTIVE_FALLBACK。"
    )
    content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="摘要正文的 SHA-256 哈希。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="摘要记录创建时间，带时区。"
    )


class RuntimeArtifactModel(Base):
    """RAG 全文证据、草稿和大工具结果的状态外副本。"""

    __tablename__ = "runtime_artifact"
    __table_args__ = (
        CheckConstraint(
            "artifact_kind IN ('RAG_RETRIEVAL', 'RAG_DRAFT', 'SOP_TOOL_RESULTS')",
            name="artifact_kind",
        ),
        CheckConstraint("status IN ('ACTIVE', 'EXPIRED')", name="status"),
        Index("ix_runtime_artifact_conversation_created", "conversation_id", "created_at"),
        Index("ix_runtime_artifact_expiry", "status", "expires_at"),
        {
            "schema": "app",
            "comment": "可恢复的 RAG 证据、草稿与大工具结果；checkpoint 只保存稳定引用。",
        },
    )

    artifact_id: Mapped[str] = mapped_column(
        String(96), primary_key=True, comment="运行产物唯一编号。"
    )
    artifact_kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="产物类型：RAG_RETRIEVAL、RAG_DRAFT 或 SOP_TOOL_RESULTS。",
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属业务会话编号，用于回装授权校验。",
    )
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation_thread_mapping.thread_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属 LangGraph 线程编号，用于回装授权校验。",
    )
    turn_id: Mapped[str] = mapped_column(
        ForeignKey("app.turn_record.turn_id", ondelete="RESTRICT"),
        nullable=False,
        comment="生成该产物的 Turn 编号。",
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("app.graph_run_record.run_id", ondelete="RESTRICT"),
        nullable=False,
        comment="生成该产物的图运行编号。",
    )
    schema_version: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="产物载荷契约版本。"
    )
    payload: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, comment="完整状态外运行产物。"
    )
    content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="规范化载荷的 SHA-256 哈希。"
    )
    release_version: Mapped[str | None] = mapped_column(
        String(64), comment="产物绑定的内容发布版本；不适用时为空。"
    )
    index_version: Mapped[str | None] = mapped_column(
        String(64), comment="产物绑定的检索索引版本；不适用时为空。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="产物状态：ACTIVE=可回装；EXPIRED=已过期。"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="允许恢复使用的截止时间。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="产物创建时间，带时区。"
    )


class RetentionRequestModel(Base):
    """精确 checkpoint 线程清单的计划、审批和执行审计。"""

    __tablename__ = "retention_request"
    __table_args__ = (
        CheckConstraint("resource_kind = 'LANGGRAPH_THREAD'", name="retention_resource_kind"),
        CheckConstraint(
            "status IN ('PLANNED', 'APPROVED', 'EXECUTED', 'REJECTED')",
            name="retention_status",
        ),
        Index("ix_retention_request_status_requested", "status", "requested_at"),
        {
            "schema": "app",
            "comment": "精确 checkpoint 线程清单的计划、审批和执行记录；不删除聊天历史。",
        },
    )

    request_id: Mapped[str] = mapped_column(
        String(96), primary_key=True, comment="保留期处置请求编号。"
    )
    resource_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="处置资源类型；当前仅支持 LANGGRAPH_THREAD。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="请求状态：PLANNED、APPROVED、EXECUTED 或 REJECTED。"
    )
    manifest: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        comment="待处置精确线程清单及计划时的校验信息。",
    )
    manifest_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="规范化处置清单的 SHA-256 哈希。"
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, comment="申请处置的业务或运维原因。")
    requested_by: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="计划发起人标识。"
    )
    approved_by: Mapped[str | None] = mapped_column(String(128), comment="审批人标识。")
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="计划创建时间，带时区。"
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="审批通过时间；未批准时为空。"
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="官方保存器完成删除的时间；未执行时为空。"
    )


class ChatHistoryMessageModel(Base):
    """Append-only raw message history; never a substitute for a graph checkpoint."""

    __tablename__ = "chat_history"
    __table_args__ = (
        CheckConstraint(
            "role IN ('CUSTOMER', 'AGENT', 'ASSISTANT', 'SYSTEM', 'TOOL')", name="role"
        ),
        CheckConstraint("length(btrim(content)) > 0", name="non_blank_content"),
        CheckConstraint("role <> 'TOOL' OR tool_call_id IS NOT NULL", name="tool_call_id"),
        Index("ix_chat_history_conversation_order", "conversation_id", "created_at", "message_id"),
        {"schema": "app"},
    )

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"), nullable=False
    )
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation_thread_mapping.thread_id", ondelete="RESTRICT"),
        nullable=False,
    )
    turn_id: Mapped[str] = mapped_column(
        ForeignKey("app.turn_record.turn_id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_call_id: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RecommendationRecordModel(Base):
    """已生成并通过同步检查的推荐及其完整结构化内容。"""

    __tablename__ = "recommendation_record"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT', 'RELEASED', 'SENT', 'REJECTED', 'EXPIRED')", name="status"
        ),
        UniqueConstraint("recommendation_id", "turn_id", name="uq_recommendation_turn"),
        {"schema": "app", "comment": "已放行推荐、所属轮次及前端读取的结构化内容。"},
    )

    recommendation_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="推荐唯一编号。"
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
        nullable=False,
        comment="推荐所属业务会话编号。",
    )
    turn_id: Mapped[str] = mapped_column(
        ForeignKey("app.turn_record.turn_id", ondelete="RESTRICT"),
        nullable=False,
        comment="产生推荐的客户轮次编号。",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment=(
            "DRAFT=待审核；RELEASED=普通推荐；SENT=审核后已发送；"
            "REJECTED=草稿已拒绝；EXPIRED=审核窗口已过期。"
        ),
    )
    trace_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="产生推荐时的追踪编号。"
    )
    payload: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="通过契约校验的完整 RecommendationResult；旧演示记录可能为空对象。",
    )
    review_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="RAG 草稿允许坐席审核的截止时间；普通推荐为空。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="推荐创建时间。"
    )


class RagReviewRecordModel(Base):
    """一份 RAG 草稿唯一的人工审核事实；原草稿、编辑稿和客户终稿分开保存。"""

    __tablename__ = "rag_review_record"
    __table_args__ = (
        CheckConstraint("action IN ('APPROVE', 'EDIT', 'REJECT')", name="action"),
        UniqueConstraint("recommendation_id", name="recommendation"),
        UniqueConstraint("decision_id", name="decision"),
        UniqueConstraint("final_message_id", name="final_message"),
        {"schema": "app", "comment": "RAG 草稿人工审核审计；每份推荐只允许形成一个终局动作。"},
    )

    review_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="审核记录编号。")
    recommendation_id: Mapped[str] = mapped_column(
        ForeignKey("app.recommendation_record.recommendation_id", ondelete="RESTRICT"),
        nullable=False,
        comment="被审核的不可变推荐编号。",
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
        nullable=False,
        comment="审核所属业务会话编号。",
    )
    turn_id: Mapped[str] = mapped_column(
        ForeignKey("app.turn_record.turn_id", ondelete="RESTRICT"),
        nullable=False,
        comment="审核所属客户轮次编号。",
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("app.graph_run_record.run_id", ondelete="RESTRICT"),
        nullable=False,
        comment="恢复审核动作对应的新图运行编号。",
    )
    decision_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="坐席动作决定编号。"
    )
    actor_id: Mapped[str] = mapped_column(
        ForeignKey("app.subject.subject_id", ondelete="RESTRICT"),
        nullable=False,
        comment="执行审核的坐席主体编号。",
    )
    action: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="审核动作：APPROVE=采用；EDIT=编辑后发送；REJECT=拒绝。"
    )
    original_draft: Mapped[str] = mapped_column(Text, nullable=False, comment="生成模型原始草稿。")
    edited_answer: Mapped[str | None] = mapped_column(
        Text, comment="坐席编辑后的完整答复；采用或拒绝时为空。"
    )
    final_answer: Mapped[str] = mapped_column(
        Text, nullable=False, comment="最终发送给客户的正文。"
    )
    note: Mapped[str] = mapped_column(Text, nullable=False, default="", comment="坐席处理说明。")
    final_message_id: Mapped[str] = mapped_column(
        ForeignKey("app.chat_history.message_id", ondelete="RESTRICT"),
        nullable=False,
        comment="最终客户可见人工消息编号。",
    )
    request_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="审核入口请求编号。"
    )
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, comment="审核链路追踪编号。")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="审核完成时间。"
    )


class RagRegenerationRecordModel(Base):
    """过期 RAG 草稿重新检索生成的不可变审计记录。"""

    __tablename__ = "rag_regeneration_record"
    __table_args__ = (
        UniqueConstraint("decision_id", name="uq_rag_regeneration_record_decision"),
        UniqueConstraint(
            "next_recommendation_id",
            name="uq_rag_regeneration_record_next_recommendation",
        ),
        {
            "schema": "app",
            "comment": "坐席对过期草稿发起重新检索的审计；不产生客户可见消息。",
        },
    )

    regeneration_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="重新生成记录编号。"
    )
    previous_recommendation_id: Mapped[str] = mapped_column(
        ForeignKey("app.recommendation_record.recommendation_id", ondelete="RESTRICT"),
        nullable=False,
        comment="已过期的原推荐编号。",
    )
    next_recommendation_id: Mapped[str] = mapped_column(
        ForeignKey("app.recommendation_record.recommendation_id", ondelete="RESTRICT"),
        nullable=False,
        comment="重新检索后形成的新推荐编号。",
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
        nullable=False,
        comment="重新生成所属业务会话编号。",
    )
    turn_id: Mapped[str] = mapped_column(
        ForeignKey("app.turn_record.turn_id", ondelete="RESTRICT"),
        nullable=False,
        comment="原审核草稿所属客户轮次编号。",
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("app.graph_run_record.run_id", ondelete="RESTRICT"),
        nullable=False,
        comment="重新检索生成草稿的图运行编号。",
    )
    decision_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="坐席重新生成决策编号。"
    )
    actor_id: Mapped[str] = mapped_column(
        ForeignKey("app.subject.subject_id", ondelete="RESTRICT"),
        nullable=False,
        comment="发起重新生成的坐席主体编号。",
    )
    previous_draft_id: Mapped[str] = mapped_column(
        String(96), nullable=False, comment="已过期的原 RAG 草稿编号。"
    )
    next_draft_id: Mapped[str] = mapped_column(
        String(96), nullable=False, comment="重新检索生成的新 RAG 草稿编号。"
    )
    note: Mapped[str] = mapped_column(
        Text, nullable=False, comment="坐席发起重新生成时填写的原因。"
    )
    request_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="重新生成入口请求编号。"
    )
    trace_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="重新生成链路追踪编号。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="重新生成审计记录创建时间。"
    )


class RecommendationInteractionModel(Base):
    """坐席对已放行推荐的客观操作，不能代替采纳反馈。"""

    __tablename__ = "recommendation_interaction_event"
    __table_args__ = ({"schema": "app", "comment": "推荐的查看、复制等客观交互事件。"},)

    interaction_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="交互事件唯一编号。"
    )
    request_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="本次 HTTP 请求编号。"
    )
    recommendation_id: Mapped[str] = mapped_column(
        ForeignKey("app.recommendation_record.recommendation_id", ondelete="RESTRICT"),
        nullable=False,
        comment="已放行推荐编号。",
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
        nullable=False,
        comment="推荐所属业务会话编号。",
    )
    turn_id: Mapped[str] = mapped_column(
        ForeignKey("app.turn_record.turn_id", ondelete="RESTRICT"),
        nullable=False,
        comment="推荐所属客户轮次编号。",
    )
    actor_id: Mapped[str] = mapped_column(
        ForeignKey("app.subject.subject_id", ondelete="RESTRICT"),
        nullable=False,
        comment="执行交互的坐席主体编号。",
    )
    action: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="VIEWED=查看；COPIED=复制；INSERTED=插入；DISMISSED=忽略；ESCALATED=升级处理；均不代表已采纳。",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="交互发生时间。"
    )


class RecommendationFeedbackModel(Base):
    """坐席显式提交的推荐评价，与客观交互分表保存。"""

    __tablename__ = "recommendation_feedback"
    __table_args__ = ({"schema": "app", "comment": "坐席对推荐的采用、帮助程度和修改说明。"},)

    feedback_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="反馈唯一编号。")
    request_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="本次 HTTP 请求编号。"
    )
    recommendation_id: Mapped[str] = mapped_column(
        ForeignKey("app.recommendation_record.recommendation_id", ondelete="RESTRICT"),
        nullable=False,
        comment="已放行推荐编号。",
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
        nullable=False,
        comment="推荐所属业务会话编号。",
    )
    turn_id: Mapped[str] = mapped_column(
        ForeignKey("app.turn_record.turn_id", ondelete="RESTRICT"),
        nullable=False,
        comment="推荐所属客户轮次编号。",
    )
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, comment="原推荐的追踪编号。")
    actor_id: Mapped[str] = mapped_column(
        ForeignKey("app.subject.subject_id", ondelete="RESTRICT"),
        nullable=False,
        comment="提交反馈的坐席主体编号。",
    )
    adoption_status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        comment="ADOPTED=直接采用；ADOPTED_AFTER_EDIT=修改后采用；NOT_ADOPTED=未采用；NOT_RATED=未评价。",
    )
    helpfulness: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        comment="HELPFUL=有帮助；PARTIALLY_HELPFUL=部分有帮助；NOT_HELPFUL=无帮助；NOT_RATED=未评价。",
    )
    result_message_id: Mapped[str | None] = mapped_column(
        String(64), comment="修改后采用所关联的最终消息编号。"
    )
    edit_summary: Mapped[str | None] = mapped_column(
        Text, comment="修改后采用时的差异摘要，不复制敏感全文。"
    )
    reason_codes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, comment="坐席选择的原因代码数组。"
    )
    comment_text: Mapped[str | None] = mapped_column(Text, comment="坐席补充评价。")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="反馈提交时间。"
    )


class ConversationWrapUpModel(Base):
    """会话结束小结当前版本；内部滚动摘要不写入这里。"""

    __tablename__ = "conversation_wrap_up"
    __table_args__ = ({"schema": "app", "comment": "坐席可编辑确认的结构化会话结束小结当前版。"},)

    wrap_up_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="结束小结唯一编号。"
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
        comment="一条业务会话对应一份当前小结。",
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="从 1 起递增的小结版本号。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="DRAFT=待坐席确认；CONFIRMED=已确认。"
    )
    topic_summary: Mapped[str] = mapped_column(
        Text, nullable=False, comment="坐席可修改的咨询主题摘要。"
    )
    intent_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, comment="已确认的动作意图编号数组；未知时为空。"
    )
    resolved_items: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, comment="已解决事项数组；未确认时不得臆测。"
    )
    unresolved_items: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, comment="尚未解决事项数组。"
    )
    actions_taken: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, comment="实际已执行动作数组；不把模型建议当实际动作。"
    )
    pending_follow_ups: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, comment="后续待跟进事项数组。"
    )
    risk_flags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, comment="风险标记数组。")
    source_message_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, comment="支撑小结的原始消息编号数组。"
    )
    source_turn_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, comment="支撑小结的客户轮次编号数组。"
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="草稿生成时间。"
    )
    generated_by: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="生成方式及版本，例如 RULES_V1 或 LEGACY_IMPORT。"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="当前版本更新时间。"
    )
    last_edited_by: Mapped[str | None] = mapped_column(
        String(64), comment="最后编辑的坐席主体编号。"
    )
    confirmed_by: Mapped[str | None] = mapped_column(String(64), comment="确认小结的坐席主体编号。")
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="坐席确认时间。"
    )


class ConversationWrapUpAuditModel(Base):
    """逐次保存草稿、编辑、确认快照，便于追查谁改了什么。"""

    __tablename__ = "conversation_wrap_up_audit"
    __table_args__ = ({"schema": "app", "comment": "会话结束小结的逐版本审计快照。"},)

    audit_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="审计记录唯一编号。"
    )
    wrap_up_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation_wrap_up.wrap_up_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属结束小结编号。",
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属业务会话编号。",
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, comment="对应的小结版本号。")
    action: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="CREATE=生成草稿；EDIT=坐席编辑；CONFIRM=坐席确认。"
    )
    actor_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="生成程序或操作坐席的标识。"
    )
    snapshot: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, comment="本版本的小结内容快照。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="审计记录创建时间。"
    )


class ConversationEventModel(Base):
    """与消息事务同步提交的可补发通知；序号由数据库全局递增。"""

    __tablename__ = "conversation_event"
    __table_args__ = (
        Index("ix_conversation_event_replay", "conversation_id", "sequence"),
        {"schema": "app", "comment": "会话事件补发日志；消息正文仍以 chat_history 为准。"},
    )

    sequence: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
        comment="数据库分配的全局递增序号；同会话可有空号。",
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("app.conversation.conversation_id", ondelete="RESTRICT"),
        nullable=False,
        comment="事件所属业务会话编号。",
    )
    turn_id: Mapped[str | None] = mapped_column(
        String(64), comment="相关客户轮次编号；会话级事件可为空。"
    )
    event_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="MESSAGE_CREATED=新消息；CONVERSATION_UPDATED=会话状态改变。",
    )
    payload: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, comment="最小事件内容；客户端仍应按编号读取权威状态。"
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="事件写入时间。"
    )


apply_model_comments(Base.metadata, "app")
