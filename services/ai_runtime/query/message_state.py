"""统一读取、升级和压缩 LangGraph 工作消息。"""

from __future__ import annotations

import hashlib
import math
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    convert_to_messages,
)
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.graph.message import add_messages

MESSAGE_STATE_SCHEMA_VERSION = 2
SUMMARY_MESSAGE_PREFIX = "context-summary:"


@dataclass(frozen=True)
class ContextBudgetPolicy:
    """定义工作消息何时压缩，以及摘要和近期 Turn 各自可占多少空间。"""

    effective_window_tokens: int
    compression_trigger_ratio: float
    dynamic_input_reserve_tokens: int
    output_reserve_tokens: int
    recent_turn_budget_tokens: int
    summary_budget_tokens: int
    rebaseline_after_compressions: int = 4
    summary_source_ref_limit: int = 32

    @property
    def compression_threshold_tokens(self) -> int:
        """返回触发压缩的应用层 Token 水位。"""

        return math.floor(self.effective_window_tokens * self.compression_trigger_ratio)


def summary_message_id(thread_id: str) -> str:
    """同一线程始终使用同一个摘要消息 ID，以便 reducer 原位替换。"""

    return f"{SUMMARY_MESSAGE_PREFIX}{thread_id}"


def _legacy_message_id(
    *, thread_id: str, turn_id: str, role: str, sequence: int, content: str
) -> str:
    """为没有业务消息主键的旧 checkpoint 生成可重复计算的兼容 ID。"""

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:20]
    raw = f"{thread_id}:{turn_id}:{role}:{sequence}:{digest}"
    return f"legacy:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


def _message_metadata(message: BaseMessage) -> dict[str, Any]:
    """读取项目附加在标准消息上的来源元数据。"""

    return dict(message.additional_kwargs)


def message_source_ref(message: BaseMessage) -> str | None:
    """返回消息对应的业务来源引用；摘要消息使用自己的摘要引用。"""

    value = _message_metadata(message).get("source_ref")
    return str(value) if value else None


def message_turn_id(message: BaseMessage) -> str | None:
    """返回消息所属 Turn；摘要消息覆盖多个 Turn，因此没有单一 Turn。"""

    value = _message_metadata(message).get("turn_id")
    return str(value) if value else None


def is_summary_message(message: BaseMessage, *, thread_id: str | None = None) -> bool:
    """识别工作消息中的唯一聚合摘要。"""

    if not isinstance(message, SystemMessage) or not message.id:
        return False
    expected = summary_message_id(thread_id) if thread_id else SUMMARY_MESSAGE_PREFIX
    return message.id == expected if thread_id else message.id.startswith(expected)


def merge_working_messages(
    left: list[AnyMessage] | None, right: list[AnyMessage] | None
) -> list[AnyMessage]:
    """沿用标准消息 reducer，并把唯一摘要稳定放在近期消息之前。"""

    merged = list(add_messages(left, right))
    summaries = [message for message in merged if is_summary_message(message)]
    recent = [message for message in merged if not is_summary_message(message)]
    return [*summaries[-1:], *recent]


def _legacy_messages(state: dict[str, object]) -> list[AnyMessage]:
    """把旧字典消息和旧摘要转换成标准消息，但不修改旧 checkpoint。"""

    thread_id = str(state.get("thread_id") or "unknown")
    converted: list[AnyMessage] = []
    old_summary = state.get("conversation_summary")
    if isinstance(old_summary, str) and old_summary.strip():
        summary_ref = str(
            state.get("conversation_summary_ref")
            or f"summary:{hashlib.sha256(old_summary.encode('utf-8')).hexdigest()}"
        )
        converted.append(
            SystemMessage(
                id=summary_message_id(thread_id),
                content=old_summary,
                additional_kwargs={
                    "message_kind": "CONVERSATION_SUMMARY",
                    "authority": "UNCONFIRMED_CONTEXT",
                    "source_ref": summary_ref,
                },
            )
        )

    per_turn_sequence: dict[tuple[str, str], int] = {}
    for raw in state.get("recent_messages", []):
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "").upper()
        content = str(raw.get("content") or "")
        turn_id = str(raw.get("turn_id") or "unknown")
        if not content or role not in {"CUSTOMER", "ASSISTANT", "AGENT"}:
            continue
        key = (turn_id, role)
        sequence = per_turn_sequence.get(key, 0)
        per_turn_sequence[key] = sequence + 1
        message_id = str(raw.get("message_id") or "") or _legacy_message_id(
            thread_id=thread_id,
            turn_id=turn_id,
            role=role,
            sequence=sequence,
            content=content,
        )
        source_ref = str(raw.get("source_ref") or f"turn:{turn_id}")
        metadata = {
            "original_role": role,
            "turn_id": turn_id,
            "source_ref": source_ref,
        }
        message_type = HumanMessage if role == "CUSTOMER" else AIMessage
        converted.append(message_type(id=message_id, content=content, additional_kwargs=metadata))
    return converted


def read_working_messages(state: dict[str, object]) -> list[AnyMessage]:
    """优先读取标准消息；旧暂停 Run 则在内存中转换旧字段。"""

    raw_messages = state.get("messages")
    if isinstance(raw_messages, list) and raw_messages:
        return list(convert_to_messages(raw_messages))
    return _legacy_messages(state)


def message_upgrade_updates(state: dict[str, object]) -> dict[str, object]:
    """为正常新 Turn 生成一次幂等升级写入；旧快照本身保持不变。"""

    version = int(state.get("message_state_schema_version") or 0)
    raw_messages = state.get("messages")
    if version >= MESSAGE_STATE_SCHEMA_VERSION and isinstance(raw_messages, list):
        return {}
    return {
        "message_state_schema_version": MESSAGE_STATE_SCHEMA_VERSION,
        "messages": read_working_messages(state),
        "recent_messages": [],
        "conversation_summary": None,
        "conversation_summary_ref": None,
    }


def customer_message(state: dict[str, object]) -> HumanMessage:
    """把本轮已落库客户消息转换为带稳定业务主键的标准消息。"""

    turn_id = str(state["turn_id"])
    message_id = str(state.get("message_id") or f"turn:{turn_id}:customer")
    return HumanMessage(
        id=message_id,
        content=str(state["raw_query"]),
        additional_kwargs={
            "original_role": "CUSTOMER",
            "turn_id": turn_id,
            "source_ref": f"message:{message_id}" if state.get("message_id") else f"turn:{turn_id}",
        },
    )


def previous_assistant_message(state: dict[str, object]) -> AIMessage | None:
    """把 BFF 核验过的上一条客户可见回复加入工作消息。"""

    previous = state.get("previous_assistant_message")
    if not isinstance(previous, dict):
        return None
    message_id = str(previous.get("message_id") or "")
    content = str(previous.get("content") or "")
    if not message_id or not content:
        return None
    original_role = str(getattr(previous.get("role"), "value", previous.get("role")))
    return AIMessage(
        id=message_id,
        content=content,
        additional_kwargs={
            "original_role": original_role,
            "turn_id": str(previous.get("turn_id") or "unknown"),
            "source_ref": str(previous.get("source_ref") or f"message:{message_id}"),
        },
    )


def prompt_message_dicts(state: dict[str, object]) -> list[dict[str, str]]:
    """把标准消息转换成 Prompt 可审计的精简字典。"""

    result: list[dict[str, str]] = []
    for message in read_working_messages(state):
        if is_summary_message(message):
            continue
        role = (
            "CUSTOMER"
            if isinstance(message, HumanMessage)
            else str(_message_metadata(message).get("original_role") or "ASSISTANT")
        )
        result.append(
            {
                "role": role,
                "content": str(message.content),
                "turn_id": str(message_turn_id(message) or "unknown"),
                "source_ref": str(message_source_ref(message) or ""),
            }
        )
    return result


def current_summary(state: dict[str, object]) -> tuple[str | None, str | None]:
    """返回当前聚合摘要正文和引用，同时兼容旧 checkpoint。"""

    for message in read_working_messages(state):
        if is_summary_message(message):
            return str(message.content), message_source_ref(message)
    summary = state.get("conversation_summary")
    summary_ref = state.get("conversation_summary_ref")
    return (
        str(summary) if isinstance(summary, str) and summary else None,
        str(summary_ref) if isinstance(summary_ref, str) and summary_ref else None,
    )


def estimate_request_tokens(messages: list[AnyMessage], policy: ContextBudgetPolicy) -> int:
    """在供应商未提供预请求 tokenizer 时，保守估算完整模型请求的 Token。"""

    return (
        count_tokens_approximately(messages)
        + policy.dynamic_input_reserve_tokens
        + policy.output_reserve_tokens
    )


def _turn_groups(messages: list[AnyMessage]) -> list[list[AnyMessage]]:
    """按 Turn 保持原顺序分组，避免压缩时拆开同轮客户消息和回复。"""

    groups: OrderedDict[str, list[AnyMessage]] = OrderedDict()
    for index, message in enumerate(messages):
        if is_summary_message(message):
            continue
        turn_id = message_turn_id(message) or f"unbound:{index}"
        groups.setdefault(turn_id, []).append(message)
    return list(groups.values())


def compression_candidates(
    messages: list[AnyMessage], policy: ContextBudgetPolicy
) -> tuple[list[AnyMessage], list[AnyMessage]]:
    """选择较早完整 Turn 进入摘要，并保留预算内的最近完整 Turn。"""

    groups = _turn_groups(messages)
    if len(groups) <= 1:
        return [], [message for group in groups for message in group]
    kept: list[list[AnyMessage]] = []
    used = 0
    for group in reversed(groups):
        group_tokens = count_tokens_approximately(group)
        if kept and used + group_tokens > policy.recent_turn_budget_tokens:
            break
        kept.append(group)
        used += group_tokens
    kept.reverse()
    split = len(groups) - len(kept)
    candidates = [message for group in groups[:split] for message in group]
    recent = [message for group in kept for message in group]
    return candidates, recent


def redact_summary_text(content: str) -> str:
    """摘要不得复制完整长数字标识符。"""

    return re.sub(r"(?<!\d)\d{6,}(?!\d)", "[长数字已脱敏]", content)


def extractive_summary(
    *, existing_summary: str | None, messages: list[AnyMessage], token_budget: int
) -> str:
    """模型不可用时生成显式降级摘要，不推断任何未出现的事实。"""

    lines = [existing_summary.strip()] if existing_summary and existing_summary.strip() else []
    role_names = {"CUSTOMER": "客户", "ASSISTANT": "助手", "AGENT": "坐席"}
    for message in messages:
        metadata = _message_metadata(message)
        role = str(metadata.get("original_role") or "ASSISTANT")
        turn_id = str(metadata.get("turn_id") or "unknown")
        content = redact_summary_text(str(message.content)).strip()
        if content:
            lines.append(f"{role_names.get(role, '消息')}[{turn_id}]：{content}")
    limit = max(1, token_budget * 4)
    result = "\n".join(lines)
    if len(result) <= limit:
        return result
    return result[-limit:].lstrip()


def summary_updates(
    *,
    state: dict[str, object],
    covered_messages: list[AnyMessage],
    summary: str,
    generation_method: str,
    source_refs: list[str],
    summary_record_id: str | None = None,
    record_kind: str = "AGGREGATE",
) -> dict[str, object]:
    """生成摘要替换、旧消息删除及新的上下文周期元数据。"""

    thread_id = str(state["thread_id"])
    sequence = int(state.get("context_cycle_sequence") or 0) + 1
    summary_ref = (
        f"context-summary-record:{summary_record_id}"
        if summary_record_id
        else f"summary:{hashlib.sha256(summary.encode('utf-8')).hexdigest()}"
    )
    summary_message = SystemMessage(
        id=summary_message_id(thread_id),
        content=summary,
        additional_kwargs={
            "message_kind": "CONVERSATION_SUMMARY",
            "authority": "UNCONFIRMED_CONTEXT",
            "source_ref": summary_ref,
        },
    )
    removals = [RemoveMessage(id=message.id) for message in covered_messages if message.id]
    return {
        "messages": [*removals, summary_message],
        "message_state_schema_version": MESSAGE_STATE_SCHEMA_VERSION,
        "summary_metadata": {
            "summary_ref": summary_ref,
            "source_refs": list(dict.fromkeys(source_refs)),
            "covered_turn_ids": list(
                dict.fromkeys(
                    turn_id for message in covered_messages if (turn_id := message_turn_id(message))
                )
            ),
            "generation_method": generation_method,
            "summary_record_id": summary_record_id,
            "record_kind": record_kind,
            "authority": "UNCONFIRMED_CONTEXT",
            "token_estimator": "LANGCHAIN_APPROXIMATE",
            "updated_at": datetime.now(UTC).isoformat(),
        },
        "context_cycle_sequence": sequence,
        "context_cycle_id": f"ctx:{thread_id}:{sequence}",
    }
