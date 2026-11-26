"""版本化 SOP 定义与向外暴露的执行进度契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from shared.contracts.common import ContractModel, RawContractModel
from shared.contracts.enums import ApprovalStatus, SlotStatus, SopExecutionStatus


class SopReviewField(ContractModel):
    """坐席可更正的流程字段，不包含密码、令牌等秘密信息。"""

    name: str
    label: str
    value: str
    choices: dict[str, str] = Field(default_factory=dict)


class SopAssistanceView(ContractModel):
    """坐席可见的流程状态与脱敏核对资料，不表示人工核验已经通过。"""

    sop_execution_id: str
    turn_id: str
    name: str
    status: SopExecutionStatus
    current_step_id: str
    reason_code: str
    reason: str
    fields: dict[str, str]
    needs_manual_review: bool
    processing: bool = False
    guidance: str
    sop_id: str = ""
    sop_release_version: str = ""
    approval_id: str | None = None
    operation_id: str | None = None
    approval_status: ApprovalStatus | None = None
    can_approve: bool = False
    can_retry: bool = False
    can_recheck: bool = False
    approval_expired: bool = False
    editable_fields: tuple[SopReviewField, ...] = ()


class SopReviewRequest(ContractModel):
    """独立的坐席处理动作；文字意见和客户一句确认不能替代此请求。"""

    sop_execution_id: str = Field(min_length=1, max_length=64)
    turn_id: str = Field(min_length=1, max_length=64)
    approval_id: str | None = Field(default=None, max_length=64)
    action: Literal["APPROVE", "CORRECT", "REJECT", "RETRY", "RECHECK"]
    identity_verified: bool = False
    corrections: dict[str, str] = Field(default_factory=dict)
    note: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def validate_action(self) -> SopReviewRequest:
        """批准需明确核验；更正、拒绝需说明原因，其他动作不能夹带修改。"""
        if self.action == "APPROVE" and not self.identity_verified:
            raise ValueError("请先记录已通过受保护渠道完成身份与业务核验")
        if self.action in {"CORRECT", "REJECT"} and not self.note.strip():
            raise ValueError("退回更正或拒绝需要填写原因")
        if self.action != "CORRECT" and self.corrections:
            raise ValueError("只有退回更正动作可以修改字段")
        if self.action == "CORRECT" and not self.corrections:
            raise ValueError("请至少更正一个字段")
        return self


class SopAssistanceReply(RawContractModel):
    """坐席为当前暂停流程发送说明；文字意见不能替代审批动作。"""

    sop_execution_id: str = Field(min_length=1, max_length=64)
    turn_id: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_content(self) -> SopAssistanceReply:
        """拒绝空白和不可见控制字符，保留坐席输入的换行排版。"""
        if not self.content.strip() or any(ord(c) < 32 and c not in "\n\r\t" for c in self.content):
            raise ValueError("处理意见不能是空白或含异常控制字符")
        return self


class SopStep(ContractModel):
    """SOP 定义中的一个固定步骤。"""

    step_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    required_slots: tuple[str, ...] = ()
    requires_approval: bool = False


class SopDefinition(ContractModel):
    """与一个 Scene 意图绑定的版本化流程定义。"""

    sop_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    bound_intent_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    steps: tuple[SopStep, ...]
    published: bool = False

    @model_validator(mode="after")
    def validate_unique_steps(self) -> SopDefinition:
        """步骤 ID 在同一个流程版本内必须唯一。"""

        step_ids = [step.step_id for step in self.steps]
        if not step_ids or len(step_ids) != len(set(step_ids)):
            raise ValueError("SOP requires unique non-empty steps")
        return self


class SlotState(ContractModel):
    """一个候选或已确认槽位及其来源。"""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    value: JsonValue
    status: SlotStatus
    source_ref: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)


class SopLookupCandidate(ContractModel):
    """只读业务查询返回的一个脱敏候选；真实值只保存在内部 checkpoint。"""

    candidate_ref: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=128)
    display_label: str = Field(min_length=1, max_length=160)


class PendingSopLookup(ContractModel):
    """等待客户确认的 SOP 只读查询候选，不能直接冒充已确认槽位。"""

    sop_execution_id: str = Field(min_length=1, max_length=64)
    batch_id: str | None = Field(default=None, min_length=1, max_length=64)
    field_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    candidates: tuple[SopLookupCandidate, ...]
    tool_result_ref: str = Field(min_length=1, max_length=128)
    customer_scope_ref: str = Field(min_length=1, max_length=128)
    expires_at: datetime

    @model_validator(mode="after")
    def validate_candidates(self) -> PendingSopLookup:
        """候选编号和值都必须唯一，避免序号确认落到错误业务对象。"""

        refs = [item.candidate_ref for item in self.candidates]
        values = [item.value for item in self.candidates]
        if not refs or len(refs) != len(set(refs)) or len(values) != len(set(values)):
            raise ValueError("SOP lookup candidates must be non-empty and unique")
        return self


class SopLookupCandidateBatch(ContractModel):
    """独立保存的一批只读候选；清空当前待选项后仍可用于安全改选。"""

    batch_id: str = Field(min_length=1, max_length=64)
    sop_execution_id: str = Field(min_length=1, max_length=64)
    field_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    candidates: tuple[SopLookupCandidate, ...]
    tool_result_ref: str = Field(min_length=1, max_length=128)
    customer_scope_ref: str = Field(min_length=1, max_length=128)
    generated_at: datetime
    expires_at: datetime
    status: Literal["ACTIVE", "CONFIRMED", "SUPERSEDED", "EXPIRED"] = "ACTIVE"
    selected_candidate_ref: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_batch(self) -> SopLookupCandidateBatch:
        """候选批次必须有唯一候选，且确认状态与选中项相互一致。"""

        refs = [item.candidate_ref for item in self.candidates]
        values = [item.value for item in self.candidates]
        if not refs or len(refs) != len(set(refs)) or len(values) != len(set(values)):
            raise ValueError("SOP lookup batch candidates must be non-empty and unique")
        if self.expires_at <= self.generated_at:
            raise ValueError("SOP lookup batch must expire after it is generated")
        if (self.status == "CONFIRMED") != bool(self.selected_candidate_ref):
            raise ValueError("only a confirmed lookup batch may keep a selected candidate")
        if self.selected_candidate_ref is not None and self.selected_candidate_ref not in refs:
            raise ValueError("selected candidate must belong to the lookup batch")
        return self


class SopLookupSelectionEvent(ContractModel):
    """一次候选选择或改选审计，不把客户自由文本当成业务批准。"""

    event_id: str = Field(min_length=1, max_length=64)
    batch_id: str = Field(min_length=1, max_length=64)
    sop_execution_id: str = Field(min_length=1, max_length=64)
    field_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    action: Literal[
        "SELECTED",
        "RESELECT_REQUESTED",
        "RESELECT_CONFIRMED",
        "EXPIRED",
        "HANDOFF",
    ]
    candidate_ref: str | None = Field(default=None, max_length=128)
    turn_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    trace_id: str = Field(min_length=1, max_length=128)
    created_at: datetime

    @model_validator(mode="after")
    def validate_candidate_reference(self) -> SopLookupSelectionEvent:
        """只有已经完成的选择事件必须记录具体候选。"""

        completed = self.action in {"SELECTED", "RESELECT_CONFIRMED"}
        if completed != bool(self.candidate_ref):
            raise ValueError("completed lookup selection must identify one candidate")
        return self


class PendingApproval(ContractModel):
    """等待坐席确认的有副作用业务动作。"""

    approval_id: str = Field(min_length=1)
    action_type: str = Field(min_length=1)
    action_summary: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: datetime
    expires_at: datetime | None = None
    operation_id: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None

    @model_validator(mode="after")
    def validate_expiry(self) -> PendingApproval:
        """确认期限不能早于发起时间。"""

        if self.expires_at is not None and self.expires_at < self.requested_at:
            raise ValueError("expires_at must not be earlier than requested_at")
        return self


class SopProgress(ContractModel):
    """BFF 和前端可消费的 SOP 执行轻量摘要。"""

    sop_execution_id: str = Field(min_length=1)
    sop_id: str = Field(min_length=1)
    sop_version: str = Field(min_length=1)
    current_step_id: str = Field(min_length=1)
    status: SopExecutionStatus
    candidate_slots: tuple[SlotState, ...] = ()
    confirmed_slots: tuple[SlotState, ...] = ()
    missing_slots: tuple[str, ...] = ()
    pending_approval: PendingApproval | None = None

    @model_validator(mode="after")
    def validate_progress(self) -> SopProgress:
        """防止候选槽位冒充已确认值或待确认状态缺失动作。"""

        if any(slot.status is not SlotStatus.CANDIDATE for slot in self.candidate_slots):
            raise ValueError("candidate_slots may only contain CANDIDATE values")
        if any(slot.status is not SlotStatus.CONFIRMED for slot in self.confirmed_slots):
            raise ValueError("confirmed_slots may only contain CONFIRMED values")
        if self.status is SopExecutionStatus.WAITING_APPROVAL and self.pending_approval is None:
            raise ValueError("WAITING_APPROVAL requires pending_approval")
        return self


class BusinessToolResult(ContractModel):
    """一次白名单只读调用的输入、事实结果和耗时，便于按轮次追踪。"""

    tool_call_id: str = Field(min_length=1, max_length=96)
    tool_name: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=64)
    conversation_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    sop_execution_id: str = Field(min_length=1)
    customer_id: str = Field(min_length=1)
    arguments: dict[str, str]
    status: Literal["SUCCEEDED", "NOT_FOUND", "REJECTED", "TIMEOUT", "UNAVAILABLE"]
    data: dict[str, JsonValue] | None = None
    error_code: str | None = None
    source_ref: str = Field(min_length=1)
    observed_at: datetime
    latency_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_result(self) -> BusinessToolResult:
        """成功调用必须有事实；失败不能夹带仍可被误当答案的数据。"""
        if (self.status == "SUCCEEDED") != (self.data is not None):
            raise ValueError("工具状态与事实数据不一致")
        return self


class BusinessOperationRequest(ContractModel):
    """已获人工批准的外部动作，绑定稳定操作编号和固定参数。"""

    operation_id: str = Field(min_length=1, max_length=64)
    sop_execution_id: str = Field(min_length=1, max_length=64)
    approval_id: str = Field(min_length=1, max_length=64)
    approved_by: str = Field(min_length=1, max_length=64)
    subject_id: str = Field(min_length=1, max_length=64)
    expected_subject_version: str = Field(min_length=1, max_length=32)
    action: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=64)
    arguments: dict[str, str]


class BusinessOperationResult(ContractModel):
    """业务服务保存的最终操作结果；未知表示尚不能确认成功或失败。"""

    operation_id: str
    customer_id: str
    subject_id: str
    action: str
    status: Literal["SUCCEEDED", "REJECTED", "UNKNOWN"]
    result_reference: str | None = None
    subject_status: str | None = None
    error_code: str | None = None
