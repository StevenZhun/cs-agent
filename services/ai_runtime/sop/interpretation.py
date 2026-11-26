"""用结构化语言模型辅助理解 SOP 消息，只提出候选，不执行或批准业务。"""

from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

import httpx
from pydantic import Field, model_validator

from prompts.sop import sop_interpretation_messages
from services.ai_runtime.model_request import (
    ModelContextWindowExceeded,
    validate_chat_request_budget,
)
from services.ai_runtime.query.history import allowed_history_targets
from services.ai_runtime.query.message_state import current_summary
from shared.contracts.common import ContractModel
from shared.contracts.enums import ModelCallStatus
from shared.contracts.runtime import ModelCallRecord
from shared.sop_config import SopConfig, valid_sop_value
from shared.web.settings import ServiceSettings


class SopMessageInterpretation(ContractModel):
    """模型仅提议有限动作和字段，不能写入身份核验或工具执行结果。"""

    action: Literal[
        "FILL_SLOTS",
        "CONFIRM",
        "CANCEL",
        "HUMAN",
        "SIDE_QUERY",
        "RESELECT_LOOKUP",
        "RECALL_HISTORY",
        "MULTI_INTENT",
        "SWITCH_TOPIC",
        "UNSURE",
    ]
    slots: dict[str, str] = Field(default_factory=dict)
    evidence: dict[str, str] | str = Field(default_factory=dict)
    side_query_kind: Literal["BUSINESS_FACT", "KNOWLEDGE"] | None = None
    requested_field: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    resolved_query: str | None = Field(default=None, max_length=4000)
    target_source_refs: tuple[str, ...] = Field(default=(), max_length=8)
    used_context_refs: tuple[str, ...] = ()
    reason: str = Field(max_length=200)

    @model_validator(mode="after")
    def validate_action_shape(self) -> SopMessageInterpretation:
        """旁路、补槽和控制动作只能携带各自需要的数据。"""

        if self.action == "FILL_SLOTS":
            if not self.slots or not isinstance(self.evidence, dict):
                raise ValueError("FILL_SLOTS requires slots and field evidence")
        elif self.slots:
            raise ValueError("only FILL_SLOTS may carry slots")
        if self.action == "SIDE_QUERY":
            if not self.side_query_kind or not isinstance(self.evidence, str):
                raise ValueError("SIDE_QUERY requires kind and text evidence")
            if not self.used_context_refs:
                raise ValueError("SIDE_QUERY requires context references")
            if self.side_query_kind == "BUSINESS_FACT" and (
                not self.requested_field or self.resolved_query is not None
            ):
                raise ValueError("business fact query requires only requested_field")
            if self.side_query_kind == "KNOWLEDGE" and (
                not self.resolved_query or self.requested_field is not None
            ):
                raise ValueError("knowledge side query requires only resolved_query")
        elif self.action == "RESELECT_LOOKUP":
            if (
                self.side_query_kind is not None
                or not self.requested_field
                or self.resolved_query is not None
                or not isinstance(self.evidence, str)
                or not self.used_context_refs
            ):
                raise ValueError("RESELECT_LOOKUP requires one field and text evidence")
        elif self.action == "RECALL_HISTORY":
            if (
                self.side_query_kind is not None
                or not self.requested_field
                or self.resolved_query is not None
                or not isinstance(self.evidence, str)
                or not self.target_source_refs
                or not self.used_context_refs
            ):
                raise ValueError("RECALL_HISTORY requires one field and bounded references")
        elif any((self.side_query_kind, self.requested_field, self.resolved_query)):
            raise ValueError("only SIDE_QUERY may carry side-query fields")
        if self.action != "RECALL_HISTORY" and self.target_source_refs:
            raise ValueError("only RECALL_HISTORY may carry history source references")
        return self


async def interpret_message(
    query: str, config: SopConfig, state: dict, settings: ServiceSettings
) -> tuple[SopMessageInterpretation | None, ModelCallRecord]:
    """请求结构化解释并校验原文依据；失败时保留候选和固定追问。

    调用前先检查敏感输入。只提供当前步骤、必要槽位和本轮文本，
    不提供整份历史或已确认身份信息，也不允许模型选择工具或业务步骤。
    """
    started = perf_counter()
    created_at = datetime.now(UTC)
    usage = {}
    provider_id = None
    result = None
    error = None
    status = ModelCallStatus.FAILED
    estimated_input_tokens: int | None = None
    if not settings.llm_base_url or not settings.llm_api_key.get_secret_value():
        error = "SOP_MODEL_CONFIG_MISSING"
    else:
        allowed = {
            f.name: {"type": f.kind, "choices": f.choices, "label": f.label} for f in config.fields
        }
        lookup_fields = {
            field.name: field.lookup_capability
            for field in config.fields
            if field.lookup_capability and field.lookup_mode == "READ_ONLY"
        }
        history_recall_fields = {
            field.name: {"type": field.kind, "label": field.label}
            for field in config.fields
            if field.history_recall_mode == "CANDIDATE_CONFIRMATION"
        }
        progress = state.get("active_sop_execution") or {}
        missing_slots = [str(item) for item in progress.get("missing_slots", [])]
        confirmed_lookup_fields = (
            {
                str(batch.get("field_name"))
                for batch in state.get("sop_lookup_batches", [])
                if isinstance(batch, dict)
                and batch.get("status") == "CONFIRMED"
                and batch.get("sop_execution_id") == progress.get("sop_execution_id")
            }
            if config.lookup_reselection_enabled
            else set()
        )
        current_message_ref = f"turn:{state['turn_id']}:customer-message"
        missing_refs = [f"sop:current:missing_slot:{name}" for name in missing_slots]
        confirmed_lookup_refs = [
            f"sop:current:confirmed_lookup:{name}" for name in sorted(confirmed_lookup_fields)
        ]
        previous = state.get("previous_assistant_message")
        summary, summary_ref = current_summary(state)
        allowed_history_source_refs, _allowed_turn_ids = allowed_history_targets(state)
        allowed_context_refs = [
            current_message_ref,
            *missing_refs,
            *confirmed_lookup_refs,
        ]
        if isinstance(previous, dict) and previous.get("source_ref"):
            allowed_context_refs.append(str(previous["source_ref"]))
        if summary and summary_ref:
            allowed_context_refs.append(summary_ref)
        payload = {
            "model": settings.llm_model,
            "temperature": 0,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "messages": sop_interpretation_messages(
                query=query,
                config=config,
                progress=progress,
                allowed_fields=allowed,
                lookup_fields=lookup_fields,
                history_recall_fields=history_recall_fields,
                missing_slots=missing_slots,
                confirmed_lookup_fields=confirmed_lookup_fields,
                previous_assistant_message=previous,
                conversation_summary=summary,
                conversation_summary_ref=summary_ref,
                allowed_history_source_refs=allowed_history_source_refs,
                current_message_ref=current_message_ref,
                allowed_context_refs=allowed_context_refs,
            ),
        }
        try:
            estimated_input_tokens = validate_chat_request_budget(
                payload,
                context_window_tokens=settings.llm_context_window_tokens,
            )
            async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
                response = await client.post(
                    settings.llm_base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + settings.llm_api_key.get_secret_value()},
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
            if not isinstance(body, dict):
                raise ValueError("模型响应必须是对象")
            usage = body.get("usage") or {}
            provider_id = body.get("id") if isinstance(body.get("id"), str) else None
            result = SopMessageInterpretation.model_validate_json(
                body["choices"][0]["message"]["content"]
            )
            fields = {f.name: f for f in config.fields}
            for name, value in result.slots.items():
                if name not in fields or not valid_sop_value(fields[name], value):
                    raise ValueError("模型提出非法字段或值")
                proof = result.evidence.get(name, "") if isinstance(result.evidence, dict) else ""
                if not proof or proof not in query:
                    raise ValueError("模型候选缺少本轮原文依据")
            if not set(result.used_context_refs).issubset(allowed_context_refs):
                raise ValueError("模型使用了未授权的上下文引用")
            if result.action == "SIDE_QUERY":
                if not isinstance(result.evidence, str) or result.evidence not in query:
                    raise ValueError("旁路解释缺少当前消息证据")
                if result.side_query_kind == "BUSINESS_FACT":
                    field = fields.get(str(result.requested_field))
                    if (
                        field is None
                        or result.requested_field not in missing_slots
                        or field.lookup_mode != "READ_ONLY"
                        or not field.lookup_capability
                    ):
                        raise ValueError("业务事实旁路不属于当前可查询缺失字段")
            if result.action == "RESELECT_LOOKUP":
                if not isinstance(result.evidence, str) or result.evidence not in query:
                    raise ValueError("改选解释缺少当前消息证据")
                if result.requested_field not in confirmed_lookup_fields:
                    raise ValueError("改选字段不是本流程已确认的只读候选")
            if result.action == "RECALL_HISTORY":
                if not isinstance(result.evidence, str) or result.evidence not in query:
                    raise ValueError("历史恢复解释缺少当前消息证据")
                field = fields.get(str(result.requested_field))
                if (
                    field is None
                    or result.requested_field not in missing_slots
                    or field.history_recall_mode != "CANDIDATE_CONFIRMATION"
                    or not set(result.target_source_refs).issubset(allowed_history_source_refs)
                ):
                    raise ValueError("历史恢复不属于当前允许恢复的缺失字段或来源")
            status = ModelCallStatus.COMPLETED
        except ModelContextWindowExceeded as exc:
            result = None
            error = "MODEL_CONTEXT_WINDOW_EXCEEDED"
            estimated_input_tokens = exc.estimated_input_tokens
        except httpx.TimeoutException:
            result = None
            status = ModelCallStatus.TIMEOUT
            error = "SOP_MODEL_TIMEOUT"
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
            result = None
            error = "SOP_MODEL_INVALID_OR_UNAVAILABLE"
    # 一轮中可能先理解换题、再理解新 SOP；两次实际调用不能共用一个编号。
    sequence = len(state.get("model_calls", []))
    call_identity = f"{state['run_id']}:SOP_INTERPRETATION:{sequence}"
    record = ModelCallRecord(
        call_id=f"mc_{uuid5(NAMESPACE_URL, call_identity).hex}",
        run_id=str(state["run_id"]),
        turn_id=str(state["turn_id"]),
        trace_id=str(state["trace_id"]),
        purpose="SOP_MESSAGE_INTERPRETATION",
        provider=settings.llm_provider,
        model=settings.llm_model,
        provider_request_id=provider_id,
        status=status,
        estimated_input_tokens=estimated_input_tokens,
        input_tokens=usage_number(usage, "prompt_tokens"),
        output_tokens=usage_number(usage, "completion_tokens"),
        total_tokens=usage_number(usage, "total_tokens"),
        context_window_tokens=settings.llm_context_window_tokens,
        latency_ms=max(0, round((perf_counter() - started) * 1000)),
        error_code=error,
        created_at=created_at,
    )
    return result, record


def usage_number(usage: object, name: str) -> int | None:
    """只记录供应商返回的非负整数，不猜测或制造 Token 数。"""
    value = usage.get(name) if isinstance(usage, dict) else None
    return value if type(value) is int and value >= 0 else None
