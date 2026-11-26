"""配置驱动的通用 SOP 恢复、填槽、确认与人工停点。"""

from __future__ import annotations

import re
from uuid import NAMESPACE_URL, uuid5

from redis.asyncio import Redis
from redis.exceptions import RedisError

from services.ai_runtime.persistence.artifacts import RuntimeArtifactRepository
from services.ai_runtime.query.history import HistoryRecallCoordinator
from services.ai_runtime.sop.interpretation import interpret_message
from shared.contracts.enums import SlotStatus, SopExecutionStatus
from shared.contracts.sop import SlotState, SopProgress
from shared.sop_config import SopConfig, load_sop_release, render_sop_message, valid_sop_value
from shared.web.settings import ServiceSettings

SENSITIVE_INPUT = re.compile(r"(?:密码|验证码|访问令牌|api\s*key|secret)", re.IGNORECASE)


async def check_active_sop(
    state: dict,
    redis: Redis | None = None,
    definitions: dict | None = None,
    llm_settings: ServiceSettings | None = None,
    history_recall: HistoryRecallCoordinator | None = None,
) -> dict:
    """恢复当前流程，并判断新消息是补充、控制动作还是新主题。"""

    del history_recall
    try:
        release = (
            {"release_version": "test-release", "definitions": definitions}
            if definitions is not None
            else await load_sop_release(redis)
        )
        active_catalog = release.get("definitions", {})
        updates = {
            "sop_catalog": active_catalog,
            "sop_catalog_release": release.get("release_version"),
        }
        raw = state.get("active_sop_execution")
        replaying = state.get("sop_turn_id") == state["turn_id"]
        if not raw or (raw.get("status") not in {"ACTIVE", "SUSPENDED"} and not replaying):
            return {**updates, "sop_action": "ROUTE"}
        pinned_version = state.get("sop_release_version")
        pinned = (
            release
            if not pinned_version or pinned_version == release.get("release_version")
            else await load_sop_release(redis, str(pinned_version))
        )
        payload = pinned.get("definitions", {}).get(raw["sop_id"])
        if payload is None or payload["version"] != raw["sop_version"]:
            return {**updates, "sop_action": "UNAVAILABLE"}
    except (RedisError, ValueError, KeyError, TypeError):
        return {"sop_action": "UNAVAILABLE" if state.get("active_sop_execution") else "ROUTE"}

    config = SopConfig.model_validate(payload)
    updates.update(
        {
            "sop_definition": config.model_dump(mode="json"),
            "sop_release_version": pinned["release_version"],
        }
    )
    if state.get("sop_turn_id") == state["turn_id"]:
        return {**updates, "sop_action": "REPLAY"}

    progress = SopProgress.model_validate(raw)
    query = str(state["normalized_query"]).rstrip("。!?？").strip()
    if query in config.cancel_phrases:
        return {**updates, "sop_action": "CANCEL"}
    if query in config.human_phrases:
        return {**updates, "sop_action": "HUMAN"}
    if SENSITIVE_INPUT.search(query):
        return {**updates, "sop_action": "SENSITIVE"}
    if progress.current_step_id == "manual_review":
        return {**updates, "sop_action": "CONTINUE"}

    switching = any(phrase in query for phrase in config.switch_phrases)
    switching = switching or any(
        phrase in query
        for sop_id, item in active_catalog.items()
        if sop_id != config.sop_id
        for phrase in item["entry_phrases"]
    )
    if progress.status is SopExecutionStatus.SUSPENDED and query in config.resume_phrases:
        progress.status = SopExecutionStatus.ACTIVE
    elif switching:
        progress.status = SopExecutionStatus.SUSPENDED
        return {
            **updates,
            "sop_action": "ROUTE",
            "active_sop_execution": progress.model_dump(mode="json"),
        }
    elif progress.status is SopExecutionStatus.SUSPENDED:
        return {**updates, "sop_action": "ROUTE"}

    updates.update(
        {
            "sop_action": "CONTINUE",
            "sop_interpretation": None,
            "active_sop_execution": progress.model_dump(mode="json"),
        }
    )
    expected = progress.missing_slots[0] if progress.missing_slots else ""
    extracted = _extract_slots(query, config, expected)
    needs_model = not extracted and query not in config.confirm_phrases
    needs_model = needs_model or any(word in query for word in ("不是", "改成", "纠正", "但是"))
    if needs_model and llm_settings is not None and llm_settings.sop_llm_enabled:
        interpreted, record = await interpret_message(query, config, state, llm_settings)
        updates["model_calls"] = [*state.get("model_calls", []), record.model_dump(mode="json")]
        if interpreted is not None:
            updates["sop_interpretation"] = interpreted.model_dump(mode="json")
            if interpreted.action in {"CANCEL", "HUMAN"}:
                updates["sop_action"] = interpreted.action
            elif interpreted.action in {"SWITCH_TOPIC", "MULTI_INTENT"}:
                progress.status = SopExecutionStatus.SUSPENDED
                updates.update(
                    sop_action="ROUTE",
                    active_sop_execution=progress.model_dump(mode="json"),
                )
    return updates


def next_after_sop_check(state: dict) -> str:
    """把活动流程判断结果映射到主图下一节点。"""

    if state.get("sop_action") == "UNAVAILABLE":
        return "safe_fallback"
    return "decide_query" if state.get("sop_action") == "ROUTE" else "route_turn"


async def run_sop(
    state: dict,
    llm_settings: ServiceSettings | None = None,
    artifact_repository: RuntimeArtifactRepository | None = None,
) -> dict:
    """推进一轮通用表单流程；人工复核前不产生外部业务副作用。"""

    del artifact_repository
    if state.get("sop_turn_id") == state["turn_id"]:
        return {
            "terminal_turn_id": state["turn_id"],
            "terminal_run_id": state["run_id"],
            "terminal_response_path": "SOP",
            "terminal_content": state["sop_content"],
            "terminal_reason_code": state["sop_reason_code"],
        }

    intent_id = state["routing_decision"]["intent_decision"]["intent_id"]
    existing = state.get("active_sop_execution")
    continuing = bool(
        existing
        and state.get("sop_action") != "ROUTE"
        and existing.get("status") in {"ACTIVE", "SUSPENDED"}
    )
    if continuing:
        config = SopConfig.model_validate(state["sop_definition"])
        progress = SopProgress.model_validate(existing)
        release_version = state["sop_release_version"]
    else:
        config = next(
            SopConfig.model_validate(item)
            for item in state["sop_catalog"].values()
            if item["intent_id"] == intent_id
        )
        release_version = state["sop_catalog_release"]
        progress = SopProgress(
            sop_execution_id=f"sop_{uuid5(NAMESPACE_URL, 'sop:' + str(state['turn_id'])).hex}",
            sop_id=config.sop_id,
            sop_version=config.version,
            current_step_id=config.fields[0].name,
            status=SopExecutionStatus.ACTIVE,
            missing_slots=tuple(field.name for field in config.required_fields({})),
        )

    query = str(state["normalized_query"]).rstrip("。!?？").strip()
    action = state.get("sop_action")
    model_calls = list(state.get("model_calls", []))
    if action == "CANCEL":
        progress.status = SopExecutionStatus.CANCELLED
        reason = "SOP_CANCELLED"
    elif action == "HUMAN":
        progress.status = SopExecutionStatus.SUSPENDED
        progress.current_step_id = "manual_review"
        reason = "SOP_HUMAN_REQUESTED"
    elif action == "SENSITIVE" or SENSITIVE_INPUT.search(query):
        progress.status = SopExecutionStatus.SUSPENDED
        progress.current_step_id = "manual_review"
        reason = "SOP_SENSITIVE_REVIEW"
    elif (
        progress.current_step_id == "manual_review"
        and progress.status is SopExecutionStatus.SUSPENDED
    ):
        reason = "SOP_MANUAL_REVIEW"
    else:
        expected = progress.missing_slots[0] if progress.missing_slots else ""
        extracted = _extract_slots(query, config, expected)
        interpreted = state.get("sop_interpretation") if continuing else None
        if (
            not continuing
            and not extracted
            and llm_settings is not None
            and llm_settings.sop_llm_enabled
        ):
            result, record = await interpret_message(
                query,
                config,
                {**state, "active_sop_execution": progress.model_dump(mode="json")},
                llm_settings,
            )
            model_calls.append(record.model_dump(mode="json"))
            interpreted = result.model_dump(mode="json") if result else None
        if interpreted and interpreted["action"] == "FILL_SLOTS":
            extracted.update(interpreted["slots"])
        elif any(word in query for word in ("不是", "改成", "纠正", "但是")):
            extracted = {}

        values = {
            slot.name: slot for slot in (*progress.candidate_slots, *progress.confirmed_slots)
        }
        for name, value in extracted.items():
            values[name] = SlotState(
                name=name,
                value=value,
                status=SlotStatus.CANDIDATE,
                source_ref=f"customer-turn:{state['turn_id']}",
            )
        required = config.required_fields({name: str(slot.value) for name, slot in values.items()})
        allowed_names = {field.name for field in required}
        values = {
            name: slot.model_copy(update={"status": SlotStatus.CANDIDATE})
            for name, slot in values.items()
            if name in allowed_names
        }
        progress.confirmed_slots = ()
        progress.candidate_slots = tuple(values.values())
        progress.missing_slots = tuple(field.name for field in required if field.name not in values)
        confirmed = query in config.confirm_phrases or bool(
            interpreted and interpreted["action"] == "CONFIRM"
        )
        if (
            not progress.missing_slots
            and progress.current_step_id == "confirm_slots"
            and not extracted
            and confirmed
        ):
            progress.confirmed_slots = tuple(
                slot.model_copy(update={"status": SlotStatus.CONFIRMED})
                for slot in progress.candidate_slots
            )
            progress.candidate_slots = ()
            progress.status = SopExecutionStatus.SUSPENDED
            progress.current_step_id = "manual_review"
            reason = "SOP_MANUAL_REVIEW"
        elif progress.missing_slots:
            progress.current_step_id = progress.missing_slots[0]
            reason = "SOP_COLLECT_SLOT" if extracted or not continuing else "SOP_INVALID_SLOT"
        else:
            progress.current_step_id = "confirm_slots"
            reason = "SOP_CONFIRM_SLOTS"

    include_notice = not continuing and reason in {"SOP_COLLECT_SLOT", "SOP_INVALID_SLOT"}
    content = render_sop_message(config, progress, reason, include_notice=include_notice)
    return {
        "sop_definition": config.model_dump(mode="json"),
        "sop_release_version": release_version,
        "active_sop_execution": progress.model_dump(mode="json"),
        "sop_review_reason": reason,
        "pending_action": (
            {
                "kind": "SOP_MANUAL_REVIEW",
                "sop_execution_id": progress.sop_execution_id,
                "reason_code": reason,
            }
            if progress.current_step_id == "manual_review"
            and progress.status is SopExecutionStatus.SUSPENDED
            else None
        ),
        "model_calls": model_calls,
        "sop_tools_due": False,
        "sop_tool_results": [],
        "sop_tool_results_ref": None,
        "business_operation": None,
        "sop_operation_request": None,
        "pending_clarification": None,
        "sop_prefetch_due": False,
        "sop_interpretation": None,
        "sop_lookup_confirmed": False,
        "sop_turn_id": state["turn_id"],
        "sop_content": content,
        "sop_reason_code": reason,
        "sop_notice_included": include_notice,
        "terminal_turn_id": state["turn_id"],
        "terminal_run_id": state["run_id"],
        "terminal_response_path": "SOP",
        "terminal_content": content,
        "terminal_reason_code": reason,
    }


def _extract_slots(query: str, config: SopConfig, expected: str) -> dict[str, str]:
    """从有限选项和当前文本字段中提取候选值。"""

    extracted: dict[str, str] = {}
    for field in config.fields:
        if field.kind == "CHOICE":
            for phrase, canonical in field.aliases.items():
                if phrase in query:
                    extracted[field.name] = canonical
                    break
    field = next((item for item in config.fields if item.name == expected), None)
    if field and field.kind == "TEXT":
        excluded = {
            *config.entry_phrases,
            *config.confirm_phrases,
            *config.cancel_phrases,
            *config.human_phrases,
            *config.resume_phrases,
        }
        if query not in excluded and valid_sop_value(field, query):
            extracted[field.name] = query
    return extracted
