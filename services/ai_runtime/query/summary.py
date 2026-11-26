"""调用主语言模型生成受控的工作上下文摘要。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx
from langchain_core.messages import AnyMessage

from prompts.context_summary import context_summary_messages
from services.ai_runtime.model_request import (
    ModelContextWindowExceeded,
    validate_chat_request_budget,
)
from services.ai_runtime.query.message_state import redact_summary_text
from shared.contracts.enums import ModelCallStatus
from shared.contracts.runtime import ModelCallRecord


@dataclass(frozen=True)
class ContextSummaryBundle:
    """一次调用同时产生不可变分段摘要和当前聚合摘要。"""

    segment_summary: str
    aggregate_summary: str


class ContextSummarizer:
    """只压缩给定消息；调用失败时由编排层决定是否使用确定性降级。"""

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
    def model(self) -> str:
        """返回实际摘要模型名，写入不可变摘要记录。"""

        return self._model

    async def summarize(
        self,
        *,
        state: dict[str, object],
        existing_summary: str | None,
        messages: list[AnyMessage],
        max_tokens: int,
    ) -> tuple[ContextSummaryBundle | None, ModelCallRecord]:
        """请求结构化摘要并保留真实供应商用量；非法输出不会写入状态。"""

        started = time.perf_counter()
        call_id = f"mc_{uuid5(NAMESPACE_URL, str(state['run_id']) + ':CONTEXT_SUMMARY').hex}"
        if not self._base_url or not self._api_key:
            return None, self._record(
                state=state,
                call_id=call_id,
                status=ModelCallStatus.FAILED,
                started=started,
                usage={},
                error_code="CONTEXT_SUMMARY_CONFIG_MISSING",
            )
        payload = {
            "model": self._model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": context_summary_messages(
                existing_summary=existing_summary,
                messages=messages,
                max_tokens=max_tokens,
            ),
        }
        estimated_input_tokens: int | None = None
        try:
            estimated_input_tokens = validate_chat_request_budget(
                payload,
                context_window_tokens=self._context_window_tokens,
                default_output_reserve_tokens=max_tokens,
            )
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
            output = json.loads(body["choices"][0]["message"]["content"])
            segment_summary = redact_summary_text(str(output.get("segment_summary") or "").strip())
            aggregate_summary = redact_summary_text(
                str(output.get("aggregate_summary") or output.get("summary") or "").strip()
            )
            if (
                not segment_summary
                or not aggregate_summary
                or len(segment_summary) > max_tokens * 4
                or len(aggregate_summary) > max_tokens * 4
            ):
                raise ValueError("context summary exceeds the configured budget")
            return ContextSummaryBundle(segment_summary, aggregate_summary), self._record(
                state=state,
                call_id=call_id,
                status=ModelCallStatus.COMPLETED,
                started=started,
                usage=body.get("usage") or {},
                provider_request_id=body.get("id"),
                estimated_input_tokens=estimated_input_tokens,
            )
        except ModelContextWindowExceeded as error:
            return None, self._record(
                state=state,
                call_id=call_id,
                status=ModelCallStatus.FAILED,
                started=started,
                usage={},
                error_code="MODEL_CONTEXT_WINDOW_EXCEEDED",
                estimated_input_tokens=error.estimated_input_tokens,
            )
        except (
            httpx.HTTPError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return None, self._record(
                state=state,
                call_id=call_id,
                status=ModelCallStatus.FAILED,
                started=started,
                usage={},
                error_code="CONTEXT_SUMMARY_FAILED",
                estimated_input_tokens=estimated_input_tokens,
            )

    def _record(
        self,
        *,
        state: dict[str, object],
        call_id: str,
        status: ModelCallStatus,
        started: float,
        usage: dict[str, Any],
        provider_request_id: str | None = None,
        error_code: str | None = None,
        estimated_input_tokens: int | None = None,
    ) -> ModelCallRecord:
        """生成与其他模型调用一致的审计记录。"""

        return ModelCallRecord(
            call_id=call_id,
            run_id=str(state["run_id"]),
            turn_id=str(state["turn_id"]),
            trace_id=str(state["trace_id"]),
            purpose="CONTEXT_SUMMARY",
            provider=self._provider,
            model=self._model,
            provider_request_id=provider_request_id,
            status=status,
            estimated_input_tokens=estimated_input_tokens,
            input_tokens=self._number(usage.get("prompt_tokens")),
            output_tokens=self._number(usage.get("completion_tokens")),
            total_tokens=self._number(usage.get("total_tokens")),
            context_window_tokens=self._context_window_tokens,
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            error_code=error_code,
            created_at=datetime.now(UTC),
        )

    @staticmethod
    def _number(value: Any) -> int | None:
        """只接受非负 Token 数。"""

        return int(value) if isinstance(value, (int, float)) and value >= 0 else None
