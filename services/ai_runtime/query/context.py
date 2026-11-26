"""只做可解释的 Query 派生与小范围上下文组装，不调用模型猜测事实。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, datetime, timedelta

from langchain_core.messages import AnyMessage
from pydantic import ValidationError

from services.ai_runtime.query.message_state import (
    current_summary,
    message_source_ref,
    message_turn_id,
)
from shared.contracts.enums import FactAuthority, FactSourceType, QueryResolutionStatus
from shared.contracts.query import QueryContext, QueryResolutionResult, ResolvedContextFact
from shared.contracts.routing import RoutingDecision

TOPIC_ELLIPSIS = re.compile(r"(?:那)?手续费(?:呢|是多少|怎么算)?[?？]?")
TOPIC_FACT_TTL = timedelta(minutes=30)

# 主题事实用于改写省略表达，不承担答案生成。主题名称来自运行时目录，
# 不在 Query 层重复维护意图编号与中文名称映射。


def normalize_query(raw_query: str) -> str:
    """只统一 Unicode 与空白，保留否定词和原问题的关键措辞。"""

    # NFKC 将全角字母、数字、标点、空格等转换成兼容的标准形式，
    # 用来消除不同输入法和键盘带来的字符差异。
    # Python 还支持 NFC、NFD、NFKC、NFKD，详见 unicodedata.normalize 官方文档。
    # 这里再把连续空格、换行和 Tab 合并为一个空格，并删除首尾空格。
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", raw_query)).strip()


def active_confirmed_facts(
    raw_facts: list[dict[str, object]], *, now: datetime | None = None
) -> list[ResolvedContextFact]:
    """只保留契约有效、时间未过期的事实；未知或失效事实不能用于补全。"""

    current = now or datetime.now(UTC)
    facts: list[ResolvedContextFact] = []
    for raw in raw_facts:
        try:
            fact = ResolvedContextFact.model_validate(raw)
        except ValidationError:
            continue
        if fact.as_of > current or (fact.expires_at is not None and fact.expires_at <= current):
            continue
        facts.append(fact)
    return facts


def update_confirmed_topic(
    raw_facts: list[dict[str, object]],
    *,
    state: dict[str, object],
    routing: dict[str, object],
    catalog: dict[str, dict[str, object]] | None = None,
    faq_hint: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    """把本轮唯一确定的 FAQ/SOP 意图沉淀为可审计的当前主题。

    只有路由已经得到唯一动作意图并进入 FAQ 或 SOP 时才更新主题；UNKNOWN、
    AMBIGUOUS、MULTI_INTENT 和安全兜底不能覆盖上一主题。新主题替换旧主题，
    这样客户明确换题后，下一轮省略问句不会继续引用旧业务。
    """

    facts = active_confirmed_facts(raw_facts)
    current = RoutingDecision.model_validate(routing)
    intent = current.intent_decision
    eligible = (
        intent.status.value == "MATCHED"
        and intent.intent_id
        and current.response_path.value in {"FAQ", "SOP"}
    )
    retained = [fact for fact in facts if fact.fact_key != "current_topic"]
    if not eligible:
        return [fact.model_dump(mode="json") for fact in facts]

    intent_id = str(intent.intent_id)
    definition = (catalog or {}).get(intent_id, {})
    intent_name = str(definition.get("name") or definition.get("intent_name") or "").strip()
    if not intent_name:
        # 没有权威目录名称时不写入主题，避免内部 intent_id 进入业务 Query。
        return [fact.model_dump(mode="json") for fact in facts]
    topic = str(definition.get("topic") or intent_name).strip()
    turn_id = str(state.get("turn_id") or "unknown")
    source_ref = f"turn:{turn_id}:routing"
    if any(fact.source_ref == source_ref for fact in facts if fact.fact_key == "current_topic"):
        # 同一 Turn 重试不能刷新主题时间或生成第二条等价事实。
        return [fact.model_dump(mode="json") for fact in facts]
    retained.append(
        ResolvedContextFact(
            fact_key="current_topic",
            value={
                "intent_id": intent_id,
                "intent_name": intent_name,
                "topic": topic,
                **(
                    {
                        "faq_item_id": str(faq_hint["faq_item_id"]),
                        "faq_version": str(faq_hint["faq_version"]),
                    }
                    if faq_hint
                    else {}
                ),
            },
            value_type="intent_topic",
            source_type=FactSourceType.SESSION_CONFIRMED,
            source_ref=source_ref,
            authority=FactAuthority.CONFIRMED,
            subject_ref=str(state.get("conversation_id") or state.get("thread_id") or source_ref),
            as_of=datetime.now(UTC),
            expires_at=datetime.now(UTC) + TOPIC_FACT_TTL,
        )
    )
    return [fact.model_dump(mode="json") for fact in retained]


def derive_query(raw_query: str, facts: list[ResolvedContextFact]) -> QueryContext:
    """生成原文、规范化文本和谨慎的唯一消解结果。

    当前只处理“那手续费呢”这种受控省略句；没有唯一、已确认的主题时
    保留原话并标注需澄清，不从最近消息中擅自猜出产品或服务记录。
    """

    normalized = normalize_query(raw_query)
    if not normalized:
        raise ValueError("客户问题不能为空白")
    resolution = QueryResolutionResult(status=QueryResolutionStatus.NOT_REQUIRED)
    if TOPIC_ELLIPSIS.fullmatch(normalized):
        topics = [
            fact
            for fact in facts
            if fact.fact_key == "current_topic"
            and fact.source_type
            in {FactSourceType.USER_CONFIRMED, FactSourceType.SESSION_CONFIRMED}
            and fact.authority is FactAuthority.CONFIRMED
            and (
                isinstance(fact.value, str)
                or (
                    isinstance(fact.value, dict)
                    and isinstance(fact.value.get("topic"), str)
                    and 0 < len(str(fact.value["topic"])) <= 80
                )
            )
        ]

        def topic_text(fact: ResolvedContextFact) -> str:
            """从结构化主题事实中读取面向业务的短主题。"""

            if isinstance(fact.value, dict):
                return str(fact.value["topic"])
            return str(fact.value)

        unique_values = {topic_text(fact) for fact in topics}
        if len(unique_values) == 1:
            topic = next(iter(unique_values))
            resolution = QueryResolutionResult(
                status=QueryResolutionStatus.RESOLVED,
                resolved_query=f"{topic}的手续费如何计算?",
                used_context_refs=tuple(sorted({fact.source_ref for fact in topics})),
                reason_codes=("CONFIRMED_TOPIC",),
            )
        else:
            resolution = QueryResolutionResult(
                status=(
                    QueryResolutionStatus.INSUFFICIENT
                    if not topics
                    else QueryResolutionStatus.AMBIGUOUS
                ),
                missing_context=("current_topic",) if not topics else (),
                clarification="请问您想了解哪项业务的手续费？",
                reason_codes=("TOPIC_NOT_UNIQUE",),
            )
    effective = resolution.resolved_query or normalized
    return QueryContext(
        raw_query=raw_query,
        normalized_query=normalized,
        resolution=resolution,
        effective_query=effective,
        query_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    )


def context_refs(
    *,
    messages: list[AnyMessage],
    facts: list[ResolvedContextFact],
    pending_action: dict[str, object] | None,
    previous_assistant_message: dict[str, object] | None = None,
) -> dict[str, object]:
    """记录本轮实际可用的上下文来源，不再复制一份完整消息正文。"""

    message_state = {"messages": messages}
    summary, summary_ref = current_summary(message_state)
    return {
        "recent_turn_ids": list(
            dict.fromkeys(
                turn_id for message in messages if (turn_id := message_turn_id(message)) is not None
            )
        ),
        "message_source_refs": list(
            dict.fromkeys(
                source_ref
                for message in messages
                if (source_ref := message_source_ref(message)) is not None
            )
        ),
        "has_summary": bool(summary),
        "conversation_summary_ref": summary_ref if summary else None,
        "confirmed_fact_refs": [fact.source_ref for fact in facts],
        "pending_action_ref": (
            str(pending_action["action_id"])
            if pending_action and pending_action.get("action_id")
            else None
        ),
        "previous_assistant_message_ref": (
            str(previous_assistant_message["source_ref"])
            if previous_assistant_message and previous_assistant_message.get("source_ref")
            else None
        ),
    }
