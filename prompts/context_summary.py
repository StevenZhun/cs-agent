"""上下文压缩摘要提示词。"""

from __future__ import annotations

import json

from langchain_core.messages import AnyMessage

from services.ai_runtime.query.message_state import (
    message_source_ref,
    message_turn_id,
    redact_summary_text,
)


def context_summary_messages(
    *, existing_summary: str | None, messages: list[AnyMessage], max_tokens: int
) -> list[dict[str, str]]:
    """要求模型压缩已给材料，不把摘要提升为业务事实。"""

    system_prompt = (
        "你是智能客服工作上下文的压缩器，不是答复生成器。"
        "只能概括输入中明确出现的内容，不得补充金额、身份、政策、办理结果或因果结论。"
        "优先保留未完成事项、客户纠正、否定条件、已讨论主题和来源边界。"
        "摘要属于未确认上下文，不能写成数据库已核验事实。"
        f"segment_summary 与 aggregate_summary 各自最多约 {max_tokens} Token。"
        "segment_summary 只概括 new_messages；aggregate_summary 将 existing_summary "
        "与 new_messages 合并为一份新的有界工作摘要。"
        "只输出 JSON 对象，字段仅为 segment_summary、aggregate_summary。"
    )
    source_messages = [
        {
            "turn_id": message_turn_id(message),
            "source_ref": message_source_ref(message),
            "content": redact_summary_text(str(message.content)),
        }
        for message in messages
    ]
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "existing_summary": (
                        redact_summary_text(existing_summary) if existing_summary else None
                    ),
                    "new_messages": source_messages,
                },
                ensure_ascii=False,
            ),
        },
    ]
