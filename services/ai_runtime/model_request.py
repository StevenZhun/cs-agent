"""在请求真正发给模型前，对最终载荷执行统一 Token 预算检查。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import convert_to_messages
from langchain_core.messages.utils import count_tokens_approximately


class ModelContextWindowExceeded(ValueError):
    """最终模型请求和输出预留已经超过配置的上下文窗口。"""

    def __init__(self, estimated_input_tokens: int) -> None:
        super().__init__("final model request exceeds the configured context window")
        self.estimated_input_tokens = estimated_input_tokens


def estimate_chat_input_tokens(payload: Mapping[str, Any]) -> int:
    """根据最终 messages 估算输入量；该值用于预检，不冒充供应商 usage。"""

    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, (str, bytes)):
        raise ValueError("chat completion payload requires messages")
    return count_tokens_approximately(convert_to_messages(list(raw_messages)))


def validate_chat_request_budget(
    payload: Mapping[str, Any],
    *,
    context_window_tokens: int,
    default_output_reserve_tokens: int = 1024,
) -> int:
    """在 HTTP 调用前检查最终输入和输出空间，返回可审计的预估输入量。"""

    estimated_input_tokens = estimate_chat_input_tokens(payload)
    raw_max_tokens = payload.get("max_tokens")
    output_reserve = (
        raw_max_tokens
        if isinstance(raw_max_tokens, int) and raw_max_tokens >= 0
        else default_output_reserve_tokens
    )
    if estimated_input_tokens + output_reserve > context_window_tokens:
        raise ModelContextWindowExceeded(estimated_input_tokens)
    return estimated_input_tokens
