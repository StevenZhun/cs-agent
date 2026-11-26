"""SOP 人工停点、受控更正与恢复。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from services.ai_runtime.persistence.artifacts import RuntimeArtifactRepository
from shared.contracts.enums import SlotStatus
from shared.contracts.sop import PendingApproval, SlotState, SopProgress, SopReviewRequest
from shared.sop_config import SopConfig, format_sop_text, render_sop_message, valid_sop_value


def next_review_step(state: dict) -> str:
    """只有受保护的人工停点进入 interrupt，其他流程结束当前轮。"""

    progress = state.get("active_sop_execution") or {}
    return (
        "prepare_review"
        if progress.get("status") in {"SUSPENDED", "WAITING_APPROVAL"}
        and progress.get("current_step_id") == "manual_review"
        else "end"
    )


async def prepare_review(
    state: dict,
    artifact_repository: RuntimeArtifactRepository | None = None,
) -> dict:
    """在中断前保存稳定审批编号，避免恢复时重复创建待办。"""

    del artifact_repository
    progress = SopProgress.model_validate(state["active_sop_execution"])
    if progress.pending_approval is None:
        identity = json.dumps(
            {
                "execution": progress.sop_execution_id,
                "slots": [slot.model_dump(mode="json") for slot in progress.confirmed_slots],
                "review_decision": state.get("last_review_decision_id"),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        operation_id = "review_" + uuid5(NAMESPACE_URL, identity).hex
        progress.pending_approval = PendingApproval(
            approval_id="approval_" + uuid5(NAMESPACE_URL, operation_id).hex,
            action_type=state["sop_definition"]["intent_id"],
            action_summary=state["sop_definition"]["name"],
            tool_call_id=operation_id,
            operation_id=operation_id,
            requested_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    progress.status = "WAITING_APPROVAL"
    progress.current_step_id = "manual_review"
    return {"active_sop_execution": progress.model_dump(mode="json")}


def await_review(state: dict, config: RunnableConfig) -> dict:
    """暂停图执行，等待 Runtime 注入已落库的人工决定或新客户消息。"""

    progress = SopProgress.model_validate(state["active_sop_execution"])
    decision = interrupt(progress.pending_approval.model_dump(mode="json"))
    from services.ai_runtime.graph import prepare_turn

    supplied = decision["graph_input"]
    prepared = prepare_turn({**state, **supplied}, config)
    return {**supplied, **prepared, "review_decision": decision, "sop_notice_included": False}


async def apply_review(
    state: dict,
    artifact_repository: RuntimeArtifactRepository | None = None,
) -> dict:
    """应用批准、更正或拒绝；本节点不调用外部业务写接口。"""

    del artifact_repository
    decision = state["review_decision"]
    config = SopConfig.model_validate(state["sop_definition"])
    progress = SopProgress.model_validate(state["active_sop_execution"])
    approval = progress.pending_approval
    if approval is None:
        raise ValueError("当前流程没有等待处理的人工审批")
    query = state["normalized_query"].rstrip("。!?？")
    action = decision.get("action", "CUSTOMER")
    updates = {
        "terminal_turn_id": state["turn_id"],
        "terminal_run_id": state["run_id"],
        "terminal_response_path": "SOP",
        "sop_turn_id": state["turn_id"],
        "model_calls": [],
        "review_decision": None,
        "last_review_decision_id": decision.get("decision_id"),
        "sop_operation_due": False,
        "sop_operation_request": None,
        "business_operation": None,
    }
    if action == "CUSTOMER":
        if query in config.cancel_phrases and approval.status.value == "PENDING":
            action = "REJECT"
        else:
            return {
                **updates,
                "terminal_content": state["sop_content"],
                "terminal_reason_code": state["sop_reason_code"],
            }

    if action == "REJECT":
        if approval.status.value == "APPROVED":
            raise ValueError("已经批准的请求不能按未处理请求取消")
        progress.status = "CANCELLED"
        approval.status = "REJECTED"
        reason = "SOP_CANCELLED"
        content = render_sop_message(config, progress, reason)
    elif action == "CORRECT":
        payload = SopReviewRequest.model_validate(
            {name: decision[name] for name in SopReviewRequest.model_fields if name in decision}
        )
        fields = {field.name: field for field in config.fields}
        if any(
            name not in fields or not valid_sop_value(fields[name], value)
            for name, value in payload.corrections.items()
        ):
            raise ValueError("更正字段或格式不合法")
        values = {
            slot.name: str(slot.value)
            for slot in (*progress.candidate_slots, *progress.confirmed_slots)
        }
        values.update(payload.corrections)
        required = config.required_fields(values)
        progress.confirmed_slots = ()
        progress.candidate_slots = tuple(
            SlotState(
                name=field.name,
                value=values[field.name],
                status=SlotStatus.CANDIDATE,
                source_ref="agent-review:" + decision["decision_id"],
            )
            for field in required
            if field.name in values
        )
        progress.missing_slots = tuple(
            field.name for field in required if field.name not in values
        )
        progress.status = "ACTIVE"
        progress.pending_approval = None
        progress.current_step_id = (
            progress.missing_slots[0] if progress.missing_slots else "confirm_slots"
        )
        reason = "SOP_COLLECT_SLOT" if progress.missing_slots else "SOP_CONFIRM_SLOTS"
        content = render_sop_message(config, progress, reason)
    elif action == "APPROVE":
        payload = SopReviewRequest.model_validate(
            {name: decision[name] for name in SopReviewRequest.model_fields if name in decision}
        )
        del payload
        acted_at = (
            datetime.fromisoformat(decision["acted_at"])
            if decision.get("acted_at")
            else datetime.now(UTC)
        )
        if approval.status.value != "PENDING" or (
            approval.expires_at is not None and acted_at >= approval.expires_at
        ):
            raise ValueError("审批已过期或已经处理")
        if progress.missing_slots or progress.candidate_slots:
            raise ValueError("信息尚未完成确认，不能批准")
        approval.status = "APPROVED"
        approval.approved_by = decision["actor_id"]
        approval.approved_at = acted_at
        progress.status = "COMPLETED"
        progress.current_step_id = "review_complete"
        reason = "SOP_REVIEW_COMPLETED"
        content = format_sop_text("服务请求已由人工坐席复核并受理，后续进展将通过约定渠道通知。")
    else:
        raise ValueError("当前通用流程不支持该人工处理动作")

    return {
        **updates,
        "active_sop_execution": progress.model_dump(mode="json"),
        "sop_content": content,
        "sop_reason_code": reason,
        "sop_review_reason": reason,
        "terminal_content": content,
        "terminal_reason_code": reason,
        "pending_action": None,
    }


def next_after_review(state: dict) -> str:
    """更正后的流程可再次进入人工停点，其余处理结束当前轮。"""

    return next_review_step(state)


async def run_approved_operation(
    state: dict,
    client,
    artifact_repository: RuntimeArtifactRepository | None = None,
) -> dict:
    """公开默认流程没有外部写操作；保留节点接口供部署方扩展。"""

    del state, client, artifact_repository
    raise ValueError("当前流程未发布外部业务写操作")
