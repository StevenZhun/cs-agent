"""Query 上下文改写提示词。"""

from __future__ import annotations

import json

from services.ai_runtime.query.message_state import current_summary, prompt_message_dicts
from shared.contracts.query import QueryContext


def query_rewrite_system_prompt() -> str:
    """限制模型只解释会话行为并返回受控 Query Resolution 结果。"""

    return (
        "你是智能客服的受控会话改写器，不是业务意图分类器，也不是答复生成器。"
        "先判断当前消息相对上一条客户可见回复的会话作用："
        "NEW_REQUEST（新问题）、CORRECTION（纠正上一答复）、"
        "CLARIFICATION_REPLY（回答系统澄清）、ACK（仅确认）、"
        "COMPLAINT（明确询问投诉或反馈渠道）。"
        "客户说上一答复不对、没问这个或要求重新理解时属于 CORRECTION，"
        "不是投诉渠道咨询。只能处理省略、指代和纠正；不得猜测身份、金额、"
        "账号、政策或办理结果。只能输出 JSON："
        "status(NOT_REQUIRED/RESOLVED/AMBIGUOUS/INSUFFICIENT)、"
        "dialogue_act、resolved_query(完整中文问题或null)、"
        "used_context_refs(实际使用的引用数组)、preserve_intent(boolean或null)、"
        "exclude_faq_item_id(只能从允许目标复制或为null)、reason(简短英文代码)。"
        "CORRECTION 必须引用 previous_assistant_message，并在上下文能唯一恢复原问题"
        "时返回 RESOLVED；若上一轮意图正确但 FAQ 条目答非所问，preserve_intent=true；"
        "若上一轮意图本身不可信，preserve_intent=false。普通新问题使用 NOT_REQUIRED。"
        "存在 pending_clarification 且没有有限 options 时，先判断本轮是回答该追问还是"
        "另起新问题。若是回答追问，dialogue_act=CLARIFICATION_REPLY，必须结合"
        "clarification_origin_query 形成可独立理解的完整问题，并引用"
        "clarification_origin_source_ref；不能只把客户补充原样当成新问题。"
        "conversation_summary 是脱敏的历史摘录，只能帮助恢复对话主题，不能作为金额、"
        "身份、服务状态、政策或办理结果的权威事实。"
    )


def query_rewrite_user_prompt(query: QueryContext, state: dict[str, object]) -> str:
    """只向模型提供有界近期消息、摘要来源和已确认事实。"""

    messages = prompt_message_dicts(state)[-6:]
    recalled_messages = [
        item for item in state.get("history_recalled_messages", []) if isinstance(item, dict)
    ][-8:]
    previous_assistant_message = state.get("previous_assistant_message")
    pending_clarification = state.get("pending_clarification")
    clarification_origin_query = state.get("clarification_origin_query")
    clarification_origin_source_ref = None
    if isinstance(pending_clarification, dict) and pending_clarification.get("clarification_id"):
        clarification_origin_source_ref = (
            f"clarification:{pending_clarification['clarification_id']}:origin"
        )
    conversation_summary, conversation_summary_ref = current_summary(state)
    facts = [
        item
        for item in state.get("confirmed_facts", [])
        if isinstance(item, dict) and item.get("authority") == "CONFIRMED"
    ]
    previous_turn_id = (
        str(previous_assistant_message.get("turn_id"))
        if isinstance(previous_assistant_message, dict)
        and previous_assistant_message.get("turn_id")
        else None
    )
    previous_routing = (
        state.get("routing_decision")
        if previous_turn_id and state.get("routing_turn_id") == previous_turn_id
        else None
    )
    previous_faq = (
        state.get("faq_resolution")
        if previous_turn_id and state.get("faq_turn_id") == previous_turn_id
        else None
    )
    return json.dumps(
        {
            "raw_query": query.raw_query,
            "normalized_query": query.normalized_query,
            "resolution_status": query.resolution.status.value,
            "recent_messages": messages,
            "recalled_messages": recalled_messages,
            "conversation_summary": (
                {
                    "content": conversation_summary,
                    "source_ref": conversation_summary_ref,
                    "authority": "UNCONFIRMED_CONTEXT",
                }
                if isinstance(conversation_summary, str)
                and conversation_summary
                and isinstance(conversation_summary_ref, str)
                and conversation_summary_ref
                else None
            ),
            "previous_assistant_message": previous_assistant_message,
            "pending_clarification": pending_clarification,
            "clarification_origin_query": clarification_origin_query,
            "clarification_origin_source_ref": clarification_origin_source_ref,
            "allowed_correction_targets": {
                "previous_intent_id": (
                    previous_routing.get("intent_decision", {}).get("intent_id")
                    if isinstance(previous_routing, dict)
                    else None
                ),
                "previous_faq_item_id": (
                    previous_faq.get("faq_item_id") if isinstance(previous_faq, dict) else None
                ),
            },
            "confirmed_facts": facts,
            "allowed_context_refs": [
                *[str(item.get("source_ref")) for item in facts],
                *[
                    str(item.get("source_ref") or f"turn:{item.get('turn_id')}")
                    for item in messages
                    if isinstance(item, dict) and item.get("turn_id")
                ],
                *[
                    str(item.get("source_ref"))
                    for item in recalled_messages
                    if item.get("source_ref")
                ],
                *(
                    [conversation_summary_ref]
                    if isinstance(conversation_summary_ref, str) and conversation_summary_ref
                    else []
                ),
                *(
                    [str(previous_assistant_message.get("source_ref"))]
                    if isinstance(previous_assistant_message, dict)
                    and previous_assistant_message.get("source_ref")
                    else []
                ),
                *(
                    [clarification_origin_source_ref]
                    if clarification_origin_source_ref
                    and isinstance(clarification_origin_query, str)
                    and clarification_origin_query
                    else []
                ),
            ],
        },
        ensure_ascii=False,
    )
