"""不记录客户正文的结构化运行日志。"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("cs_agent.structured")

_FORBIDDEN_KEY_FRAGMENTS = (
    "content",
    "message",
    "prompt",
    "query",
    "answer",
    "password",
    "api_key",
    "secret",
    "token_value",
)


def emit_structured_event(event: str, **fields: Any) -> None:
    """输出单行 JSON；调用方只能传 ID、状态、原因码和耗时等诊断字段。"""

    lowered = tuple(key.lower() for key in fields)
    if any(fragment in key for key in lowered for fragment in _FORBIDDEN_KEY_FRAGMENTS):
        raise ValueError("structured event cannot contain raw or secret fields")
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
