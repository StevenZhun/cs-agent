"""历史回查决策提示词。"""

from __future__ import annotations

import json

from services.ai_runtime.query.message_state import current_summary, prompt_message_dicts
from shared.contracts.query import QueryContext


def history_recall_system_prompt() -> str:
    """限制模型只选择回查方式，不生成答案或数据库条件。"""

    return (
        "你是智能客服的历史上下文回查决策器，不是业务意图分类器，也不回答客户问题。"
        "只有当前问题必须依赖已被压缩的更早对话时才回查。当前账户状态、服务记录"
        "和办理结果必须由业务工具核验，不能通过聊天历史确认。只能输出 JSON："
        "mode(NOT_REQUIRED/EXACT_REF/TURN_RANGE/CONVERSATION_SEARCH/CLARIFY)、"
        "target_source_refs、target_turn_ids、search_terms、reason。"
        "EXACT_REF 只能复制 allowed_source_refs；TURN_RANGE 只能复制 allowed_turn_ids；"
        "CONVERSATION_SEARCH 最多给三个不超过八十字的短语，不得加入原输入和摘要中没有的"
        "长数字；不能唯一判断时选择 CLARIFY。不得输出 SQL、答案、业务结论或新的内部编号。"
    )


def history_recall_user_prompt(
    query: QueryContext,
    state: dict[str, object],
    *,
    allowed_source_refs: tuple[str, ...],
    allowed_turn_ids: tuple[str, ...],
) -> str:
    """提供当前问题、聚合摘要和有限引用候选。"""

    summary, summary_ref = current_summary(state)
    return json.dumps(
        {
            "raw_query": query.raw_query,
            "normalized_query": query.normalized_query,
            "query_resolution": query.resolution.model_dump(mode="json"),
            "recent_messages": prompt_message_dicts(state)[-6:],
            "conversation_summary": {
                "content": summary,
                "source_ref": summary_ref,
                "authority": "UNCONFIRMED_CONTEXT",
            },
            "allowed_source_refs": list(allowed_source_refs),
            "allowed_turn_ids": list(allowed_turn_ids),
        },
        ensure_ascii=False,
    )
