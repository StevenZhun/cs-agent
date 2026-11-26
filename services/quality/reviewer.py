"""只返回质量信号的 Shadow 模型适配器。"""

from __future__ import annotations

import json
import time

import httpx

from prompts.quality_review import quality_review_messages
from services.ai_runtime.model_request import validate_chat_request_budget
from shared.contracts.quality import DeepQualitySignals
from shared.guardrails import redact_protected_secrets


class ShadowQualityReviewer:
    """复用兼容接口做旁路评估；不生成回复，也不写模型用量到客户上下文周期。"""

    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        context_window_tokens: int,
    ) -> None:
        self._provider = provider
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._context_window_tokens = context_window_tokens

    @property
    def name(self) -> str:
        return f"{self._provider}:{self._model}"

    async def review(self, snapshot: dict[str, object]) -> tuple[DeepQualitySignals, int]:
        messages = quality_review_messages(snapshot)
        payload: dict[str, object] = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": messages,
        }
        validate_chat_request_budget(
            payload,
            context_window_tokens=self._context_window_tokens,
        )
        if self._provider.lower() == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
            response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return (
            DeepQualitySignals.model_validate(parsed),
            max(0, round((time.perf_counter() - started) * 1000)),
        )


def quality_snapshot(
    *,
    customer_content: str,
    assistant_content: str,
    response_path: str | None,
    reason_code: str | None,
) -> dict[str, object]:
    """进入 Shadow 前做脱敏，且只提供本轮判断所需的最小信息。"""

    return {
        "customer": redact_protected_secrets(customer_content),
        "assistant": redact_protected_secrets(assistant_content),
        "response_path": response_path,
        "reason_code": reason_code,
    }
