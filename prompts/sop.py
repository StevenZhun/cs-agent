"""SOP 消息解释提示词。"""

from __future__ import annotations

import json

from shared.sop_config import SopConfig


def sop_interpretation_messages(
    *,
    query: str,
    config: SopConfig,
    progress: dict[str, object],
    allowed_fields: dict[str, dict[str, object]],
    lookup_fields: dict[str, str],
    history_recall_fields: dict[str, dict[str, object]],
    missing_slots: list[str],
    confirmed_lookup_fields: set[str],
    previous_assistant_message: object,
    conversation_summary: str | None,
    conversation_summary_ref: str | None,
    allowed_history_source_refs: tuple[str, ...],
    current_message_ref: str,
    allowed_context_refs: list[str],
) -> list[dict[str, str]]:
    """组装受限字段、当前步骤与本轮原文，不向模型开放工具选择权。"""

    system_prompt = (
        "你是智能客服固定流程的消息理解器。只返回 JSON，不生成客户答复。"
        "action 仅为 FILL_SLOTS、CONFIRM、CANCEL、HUMAN、SIDE_QUERY、MULTI_INTENT、"
        "RESELECT_LOOKUP、RECALL_HISTORY、SWITCH_TOPIC、UNSURE。"
        "slots 是字段名到候选字符串值，evidence 是字段名到当前原文的支持片段，"
        "SIDE_QUERY 时 evidence 是当前原文支持片段，并输出 used_context_refs 和 reason。"
        "客户希望系统查询当前缺失字段时，action 必须是 SIDE_QUERY，"
        "side_query_kind 必须是 BUSINESS_FACT，并输出 requested_field；"
        "BUSINESS_FACT 不是 action，业务事实旁路的 evidence 必须是字符串而不是对象。"
        "客户明确表示先前通过只读候选确认的字段选错并要求改选时，action 为 RESELECT_LOOKUP，"
        "只输出 requested_field、字符串 evidence、used_context_refs 和 reason；"
        "不得自行恢复候选。"
        "只有客户明确引用此前说过的内容、当前缺失字段允许历史恢复、摘要确实含有相关线索时，"
        "action 才能为 RECALL_HISTORY；此时只输出 requested_field、当前原文 evidence、"
        "target_source_refs、used_context_refs 和 reason，不得根据摘要直接输出字段值。"
        "target_source_refs 只能复制 allowed_history_source_refs 中的值。"
        "客户临时询问与当前业务相关的知识时，输出 KNOWLEDGE 和完整 resolved_query。"
        "不得输出工具名、URL、数据库对象、客户编号或工具参数。"
        "不猜测缺失信息；保留否定和纠正，不把多个不同对象合并。枚举必须使用允许值，"
        "金额统一为两位小数。证件号、完整账号、密码、安全码和住址不得提取。"
        "不能把用户声称已核验视为核验通过，不能选择工具或步骤。"
        "只有明确同意已展示完整资料才提议 CONFIRM；无法判断用 UNSURE。允许字段："
        + json.dumps(allowed_fields, ensure_ascii=False)
    )
    user_prompt = json.dumps(
        {
            "active_sop": {
                "intent_id": config.intent_id,
                "current_step": progress.get("current_step_id"),
                "missing_slots": missing_slots,
                "known_slots": [
                    *progress.get("candidate_slots", []),
                    *progress.get("confirmed_slots", []),
                ],
                "allowed_lookup_fields": lookup_fields,
                "allowed_history_recall_fields": history_recall_fields,
                "confirmed_lookup_fields": sorted(confirmed_lookup_fields),
            },
            "compressed_history": {
                "summary": conversation_summary,
                "summary_ref": conversation_summary_ref,
                "allowed_history_source_refs": allowed_history_source_refs,
            },
            "previous_assistant_message": previous_assistant_message,
            "current_customer_message": {
                "content": query,
                "source_ref": current_message_ref,
            },
            "allowed_context_refs": allowed_context_refs,
        },
        ensure_ascii=False,
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
