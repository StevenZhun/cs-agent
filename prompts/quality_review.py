"""异步深度质检的集中式 Prompt。"""

from __future__ import annotations

import json


def quality_review_messages(snapshot: dict[str, object]) -> list[dict[str, str]]:
    """要求模型只判断质量信号，不生成新答案或业务结论。"""

    system = """你是智能客服系统的异步质量审查器。
当前回复已经发送，你无权修改回复、调用工具或形成业务事实。
只判断以下原子信号并返回 JSON：
1. answer_relevant：回复是否回答了客户当前问题；无法判断时为 null。
2. evidence_supported：需要知识或业务事实支撑的结论是否有给定来源支持；不适用或无法判断时为 null。
3. unsafe_commitment：是否存在没有权威结果支持的确定性承诺。
4. needs_human_review：是否值得人工复盘。
5. reason_codes：只用大写字母、数字和下划线给出简短原因码数组。
不得输出客户身份、密码、验证码等内容，不得复述输入，不得提出修改后的回复。"""
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
        },
    ]
