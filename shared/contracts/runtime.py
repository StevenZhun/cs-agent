"""会话映射、Turn/Run、完整消息、小结与决策追踪契约。"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, JsonValue, model_validator

from shared.contracts.common import ContractModel, RawContractModel
from shared.contracts.enums import (
    ConversationModelProfile,
    DecisionProfile,
    DecisionTask,
    MessageRole,
    ModelCallStatus,
    ResponsePath,
    RunStatus,
    TurnStatus,
    WrapUpStatus,
)
from shared.contracts.faq import FaqResolutionResult
from shared.contracts.rag import RagDraft, RagReviewOutcome
from shared.contracts.retrieval import RetrievalResult
from shared.contracts.routing import PendingClarification, RoutingDecision
from shared.contracts.sop import (
    BusinessOperationResult,
    BusinessToolResult,
    PendingSopLookup,
    SopLookupCandidateBatch,
    SopLookupSelectionEvent,
    SopProgress,
)


class ConversationThreadMapping(ContractModel):
    """业务会话与 LangGraph 状态序列的稳定一对一映射。"""

    conversation_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    created_at: datetime
    updated_at: datetime


class PreviousAssistantMessage(RawContractModel):
    """本轮受理前，客户最后实际看到的一条助手或坐席消息。"""

    message_id: str = Field(min_length=1, max_length=64)
    turn_id: str = Field(min_length=1, max_length=64)
    role: MessageRole
    content: str = Field(min_length=1, max_length=4000)
    source_ref: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_customer_visible_source(self) -> PreviousAssistantMessage:
        """只允许客户可见角色，引用必须唯一指向这条落库消息。"""

        if self.role not in {MessageRole.ASSISTANT, MessageRole.AGENT}:
            raise ValueError("previous assistant message must be ASSISTANT or AGENT")
        if self.source_ref != f"message:{self.message_id}":
            raise ValueError("previous assistant source_ref must match message_id")
        return self


class ExecuteGraphTurnRequest(RawContractModel):
    """BFF 内部调用：指明已受理的 Turn、当前 Run 和接管版本。"""

    conversation_id: str = Field(min_length=1, max_length=64)
    thread_id: str = Field(min_length=1, max_length=64)
    turn_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    lease_version: int = Field(default=0, ge=0)
    request_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)
    raw_query: str = Field(min_length=1, max_length=4000)
    message_id: str | None = Field(default=None, min_length=1, max_length=64)
    previous_assistant_message: PreviousAssistantMessage | None = None
    conversation_model_profile: ConversationModelProfile = ConversationModelProfile.DEFAULT
    shadow_decision_profile: DecisionProfile | None = None

    @model_validator(mode="after")
    def validate_non_blank_query(self) -> ExecuteGraphTurnRequest:
        if not self.raw_query.strip():
            raise ValueError("raw_query must contain non-whitespace content")
        if any(ord(character) < 32 and character not in "\n\r\t" for character in self.raw_query):
            raise ValueError("raw_query contains unsupported control characters")
        return self


class GraphTerminalSnapshot(ContractModel):
    """图产生的可持久化终态，交给应用层决定是否展示给客户。"""

    response_path: ResponsePath
    content: str = Field(min_length=1, max_length=1800)
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    policy_intent_id: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$", max_length=64)
    policy_answer_version: str | None = Field(default=None, min_length=1, max_length=64)
    answer_content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    faq_resolution: FaqResolutionResult | None = None
    pending_clarification: PendingClarification | None = None
    sop_progress: SopProgress | None = None
    sop_release_version: str | None = None
    sop_notice_included: bool = False
    business_tool_results: tuple[BusinessToolResult, ...] = ()
    business_operation: BusinessOperationResult | None = None
    pending_sop_lookup: PendingSopLookup | None = None
    sop_lookup_batches: tuple[SopLookupCandidateBatch, ...] = ()
    sop_lookup_selection_events: tuple[SopLookupSelectionEvent, ...] = ()
    graph_interrupted: bool = False
    retrieval_result: RetrievalResult | None = None
    rag_draft: RagDraft | None = None
    rag_review: RagReviewOutcome | None = None

    @model_validator(mode="after")
    def validate_faq_output(self) -> GraphTerminalSnapshot:
        """核对 FAQ 答案、澄清问题和 SOP 进度与各自回复路径的一致性。"""

        if self.response_path is ResponsePath.FAQ:
            if self.faq_resolution is None or self.faq_resolution.answer != self.content:
                raise ValueError("FAQ terminal output requires the matching FAQ answer")
        elif (
            self.faq_resolution is not None and self.faq_resolution.match_status.value == "MATCHED"
        ):
            raise ValueError("matched FAQ result requires FAQ response path")
        if self.response_path is ResponsePath.CLARIFICATION:
            if self.pending_clarification is None:
                raise ValueError("clarification output requires pending_clarification")
            if self.pending_clarification.question != self.content:
                raise ValueError("clarification content must equal its question")
        elif self.pending_clarification is not None:
            raise ValueError("pending clarification requires CLARIFICATION response path")
        if (self.response_path is ResponsePath.SOP) != (self.sop_progress is not None):
            raise ValueError("SOP 回复必须携带执行进度，其他回复不能冒充 SOP")
        if (self.response_path is ResponsePath.SOP) != bool(self.sop_release_version):
            raise ValueError("SOP 回复必须携带使用的发布版本，其他路径不能夹带 SOP 版本")
        if self.business_tool_results and self.response_path is not ResponsePath.SOP:
            raise ValueError("当前只有 SOP 能携带只读业务工具结果")
        if self.sop_notice_included and self.response_path is not ResponsePath.SOP:
            raise ValueError("只有 SOP 首次追问可以携带流程提醒")
        if self.business_operation is not None and self.response_path is not ResponsePath.SOP:
            raise ValueError("只有 SOP 可以携带业务操作结果")
        if self.pending_sop_lookup is not None and self.response_path is not ResponsePath.SOP:
            raise ValueError("只有 SOP 可以携带待确认的只读查询候选")
        if (
            self.sop_lookup_batches or self.sop_lookup_selection_events
        ) and self.response_path is not ResponsePath.SOP:
            raise ValueError("只有 SOP 可以携带只读候选批次与选择审计")
        if self.response_path is ResponsePath.RAG:
            if self.retrieval_result is None or self.rag_draft is None:
                raise ValueError("RAG 终态必须携带检索结果和证据草稿")
            if self.retrieval_result.assessment.status.value != "SUFFICIENT":
                raise ValueError("RAG 草稿只能基于充分证据生成")
            if self.rag_review is None:
                if self.reason_code != "RAG_REVIEW_REQUIRED" or not self.graph_interrupted:
                    raise ValueError("未审核 RAG 草稿必须停在人工审核节点")
            elif (
                self.graph_interrupted
                or self.content != self.rag_review.final_answer
                or self.reason_code
                not in {"RAG_REVIEW_APPROVED", "RAG_REVIEW_EDITED", "RAG_REVIEW_REJECTED"}
            ):
                raise ValueError("RAG 审核结果、终态文本和原因必须一致")
        elif self.retrieval_result is not None or self.rag_draft is not None:
            raise ValueError("非 RAG 终态不能携带 RAG 证据或草稿")
        elif self.rag_review is not None:
            raise ValueError("非 RAG 终态不能携带 RAG 审核结果")
        policy_fields = (
            self.policy_intent_id,
            self.policy_answer_version,
            self.answer_content_hash,
        )
        if self.reason_code == "POLICY_ANSWER":
            if self.response_path is not ResponsePath.SAFE_FALLBACK or not all(policy_fields):
                raise ValueError("策略答案必须使用 SAFE_FALLBACK 并携带意图、版本和内容哈希")
        elif any(policy_fields):
            raise ValueError("只有 POLICY_ANSWER 终态可以携带策略答案元数据")
        if self.graph_interrupted:
            sop_waiting = (
                self.sop_progress is not None and self.sop_progress.pending_approval is not None
            )
            rag_waiting = (
                self.response_path is ResponsePath.RAG
                and self.rag_draft is not None
                and self.rag_review is None
            )
            if not sop_waiting and not rag_waiting:
                raise ValueError("人工中断必须携带已保存的 SOP 审批或 RAG 草稿")
        return self


class ModelCallRecord(ContractModel):
    """一次真实模型调用的用量、耗时和结束原因。"""

    call_id: str = Field(min_length=1, max_length=96)
    run_id: str = Field(min_length=1, max_length=64)
    turn_id: str = Field(min_length=1, max_length=64)
    trace_id: str = Field(min_length=1, max_length=128)
    purpose: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    provider_request_id: str | None = Field(default=None, max_length=128)
    status: ModelCallStatus
    estimated_input_tokens: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    context_window_tokens: int | None = Field(default=None, ge=1)
    latency_ms: int = Field(ge=0)
    error_code: str | None = Field(default=None, max_length=64)
    created_at: datetime


class ExecuteGraphTurnResponse(ContractModel):
    """图执行结果；BFF 只放行已到终态且允许展示的助手消息。"""

    conversation_id: str
    thread_id: str
    turn_id: str
    run_id: str
    normalized_query: str
    effective_query: str
    turn_sequence: int = Field(ge=1)
    context_cycle_id: str = Field(min_length=1, max_length=96)
    replayed: bool
    terminal: GraphTerminalSnapshot
    decision_trace: DecisionTrace | None = None
    routing_decision: RoutingDecision | None = None
    routing_trace: DecisionTrace | None = None
    model_calls: tuple[ModelCallRecord, ...] = ()


class GraphPreparationSnapshot(ContractModel):
    """Recoverable output of the preparation step, not an AI answer."""

    normalized_query: str
    effective_query: str
    turn_sequence: int = Field(ge=1)
    context_cycle_id: str = Field(min_length=1, max_length=96)


class InspectGraphTurnResponse(ContractModel):
    """Read-only checkpoint check; prepared is not the same as terminal completion."""

    conversation_id: str
    thread_id: str
    turn_id: str
    run_id: str
    prepared: bool
    snapshot: GraphPreparationSnapshot | None = None
    terminal: GraphTerminalSnapshot | None = None
    decision_trace: DecisionTrace | None = None
    routing_decision: RoutingDecision | None = None
    routing_trace: DecisionTrace | None = None
    model_calls: tuple[ModelCallRecord, ...] = ()

    @model_validator(mode="after")
    def validate_snapshot(self) -> InspectGraphTurnResponse:
        if self.prepared != (self.snapshot is not None):
            raise ValueError("prepared and snapshot must agree")
        if self.terminal is not None and not self.prepared:
            raise ValueError("terminal output requires prepared state")
        return self


class DecisionSignal(ContractModel):
    """不同 Decision Provider 归一化后的一个原子判断信号。"""

    signal_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    value: JsonValue
    probabilities: dict[str, float] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    candidate_refs: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_probabilities(self) -> DecisionSignal:
        """概率分布中的每个值都必须位于闭区间。"""

        if self.probabilities is not None and any(
            value < 0 or value > 1 for value in self.probabilities.values()
        ):
            raise ValueError("probabilities must be between 0 and 1")
        return self


class DecisionShadowObservation(ContractModel):
    """旁路 Provider 的观测结果；它不能替代 active 结果或改变业务状态。"""

    profile: DecisionProfile
    provider: str = Field(min_length=1)
    provider_model: str = Field(min_length=1)
    provider_request_id: str | None = None
    input_snapshot_ref: str = Field(min_length=1)
    candidate_set_version: str = Field(min_length=1)
    signals: tuple[DecisionSignal, ...] = ()
    latency_ms: int = Field(ge=0)
    provider_usage: dict[str, int] = Field(default_factory=dict)
    error_code: str | None = None

    @model_validator(mode="after")
    def validate_result(self) -> DecisionShadowObservation:
        """成功观测必须带信号，失败观测必须带稳定错误码。"""

        if (self.error_code is None) != bool(self.signals):
            raise ValueError("shadow observation must contain signals or an error code")
        if any(value < 0 for value in self.provider_usage.values()):
            raise ValueError("provider_usage values must be non-negative")
        return self


class DecisionTrace(ContractModel):
    """能够还原 active Provider 如何形成最终业务状态的运行记录。"""

    decision_id: str = Field(min_length=1)
    decision_task: DecisionTask
    active_profile: DecisionProfile
    shadow_profile: DecisionProfile | None = None
    provider: str = Field(min_length=1)
    provider_model: str = Field(min_length=1)
    provider_request_id: str | None = None
    input_snapshot_ref: str = Field(min_length=1)
    candidate_set_version: str | None = None
    threshold_policy_version: str = Field(min_length=1)
    signals: tuple[DecisionSignal, ...]
    final_decision_ref: str = Field(min_length=1)
    latency_ms: int = Field(ge=0)
    latency_breakdown_ms: dict[str, int] = Field(default_factory=dict)
    provider_usage: dict[str, int] = Field(default_factory=dict)
    fallback_from: DecisionProfile | None = None
    fallback_reason: str | None = None
    created_at: datetime
    shadow_observation: DecisionShadowObservation | None = None

    @model_validator(mode="after")
    def validate_profiles_and_fallback(self) -> DecisionTrace:
        """shadow 不能冒充 active，回退来源必须带原因。"""

        if self.shadow_profile is self.active_profile:
            raise ValueError("shadow_profile must differ from active_profile")
        if (self.shadow_profile is None) != (self.shadow_observation is None):
            raise ValueError("shadow_profile and shadow_observation must be set together")
        if self.shadow_observation is not None and (
            self.shadow_observation.profile is not self.shadow_profile
        ):
            raise ValueError("shadow observation must match shadow_profile")
        if (self.fallback_from is None) != (self.fallback_reason is None):
            raise ValueError("fallback_from and fallback_reason must be set together")
        if self.fallback_from is self.active_profile:
            raise ValueError("fallback_from must differ from active_profile")
        if not self.signals:
            raise ValueError("DecisionTrace requires at least one signal")
        if any(value < 0 for value in self.latency_breakdown_ms.values()):
            raise ValueError("latency_breakdown_ms values must be non-negative")
        if any(value < 0 for value in self.provider_usage.values()):
            raise ValueError("provider_usage values must be non-negative")
        return self


class DecisionSummary(ContractModel):
    """普通推荐 DTO 可携带但前端默认隐藏的决策摘要。"""

    decision_id: str = Field(min_length=1)
    active_profile: DecisionProfile
    fallback_from: DecisionProfile | None = None
    fallback_reason: str | None = None


class TurnRecord(ContractModel):
    """一次用户输入从开始到业务处理结束的记录。"""

    turn_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    user_message_id: str = Field(min_length=1)
    status: TurnStatus
    routing_decision: RoutingDecision | None = None
    terminal_response_path: ResponsePath | None = None
    terminal_reason_code: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[A-Z][A-Z0-9_]+$",
    )
    context_cycle_id: str | None = Field(default=None, min_length=1, max_length=96)
    active_run_id: str | None = None
    started_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> TurnRecord:
        """完成或失败的 Turn 必须闭合时间，活跃 Turn 不得提前闭合。"""

        is_closed = self.status in {TurnStatus.COMPLETED, TurnStatus.FAILED}
        if is_closed and self.completed_at is None:
            raise ValueError("completed and failed turns require completed_at")
        if not is_closed and self.completed_at is not None:
            raise ValueError("running and interrupted turns must not carry completed_at")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at must not be earlier than started_at")
        if (self.terminal_response_path is None) != (self.terminal_reason_code is None):
            raise ValueError("terminal response path and reason code must be stored together")
        if self.status is TurnStatus.RUNNING and self.terminal_response_path is not None:
            raise ValueError("running turn cannot carry a terminal outcome")
        return self


class GraphRunRecord(ContractModel):
    """一次 Graph 执行、恢复、中断或失败的应用运行记录。"""

    run_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    status: RunStatus
    decision_profile: DecisionProfile
    decision_ids: tuple[str, ...] = ()
    resumed_from_run_id: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    error_code: str | None = None
    business_tool_results: tuple[BusinessToolResult, ...] = ()

    @model_validator(mode="after")
    def validate_lifecycle(self) -> GraphRunRecord:
        """区分活跃、已闭合和恢复执行，并保留失败原因。"""

        is_closed = self.status is not RunStatus.RUNNING
        if not is_closed and self.finished_at is not None:
            raise ValueError("RUNNING run must not carry finished_at")
        if is_closed and self.finished_at is None:
            raise ValueError("closed runs require finished_at")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at must not be earlier than started_at")
        if self.status is RunStatus.FAILED and not self.error_code:
            raise ValueError("FAILED run requires error_code")
        elif self.error_code is not None:
            raise ValueError("only FAILED run may carry error_code")
        if self.resumed_from_run_id == self.run_id:
            raise ValueError("run cannot resume from itself")
        return self


class ChatHistoryMessage(RawContractModel):
    """PostgreSQL app.chat_history 追加写的一条原始消息。"""

    conversation_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    role: MessageRole
    content: str = Field(min_length=1)
    tool_call_id: str | None = None
    request_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    created_at: datetime

    @model_validator(mode="after")
    def validate_raw_message(self) -> ChatHistoryMessage:
        """保留原文，同时拒绝空白消息和无配对 Tool Message。"""

        if not self.content.strip():
            raise ValueError("content must contain non-whitespace content")
        if self.role is MessageRole.TOOL and not self.tool_call_id:
            raise ValueError("TOOL messages require tool_call_id")
        return self


class ConversationWrapUp(ContractModel):
    """会话结束后由坐席检查并确认的结构化业务产物。"""

    wrap_up_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    status: WrapUpStatus
    topic_summary: str = Field(min_length=1)
    intent_ids: tuple[str, ...] = ()
    resolved_items: tuple[str, ...] = ()
    unresolved_items: tuple[str, ...] = ()
    actions_taken: tuple[str, ...] = ()
    pending_follow_ups: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()
    source_message_ids: tuple[str, ...]
    source_turn_ids: tuple[str, ...]
    generated_at: datetime
    generated_by: str = Field(min_length=1)
    updated_at: datetime
    last_edited_by: str | None = None
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_confirmation_and_sources(self) -> ConversationWrapUp:
        """确认版本必须有确认人和来源，草稿不能伪装成已确认。"""

        if not self.source_message_ids or not self.source_turn_ids:
            raise ValueError("ConversationWrapUp requires message and turn sources")
        if self.status is WrapUpStatus.CONFIRMED:
            if not self.confirmed_by or self.confirmed_at is None:
                raise ValueError("CONFIRMED wrap-up requires confirmer and timestamp")
        elif self.confirmed_by is not None or self.confirmed_at is not None:
            raise ValueError("DRAFT wrap-up must not carry confirmation fields")
        return self
