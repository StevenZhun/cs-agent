"""在确定性上下文消解不足时，用受控 JSON 模型补全当前 Query。"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx

from prompts.query_rewrite import query_rewrite_system_prompt, query_rewrite_user_prompt
from services.ai_runtime.model_request import (
    ModelContextWindowExceeded,
    validate_chat_request_budget,
)
from services.ai_runtime.query.message_state import current_summary, prompt_message_dicts
from shared.contracts.enums import DialogueAct, ModelCallStatus, QueryResolutionStatus
from shared.contracts.query import QueryContext, QueryResolutionResult
from shared.contracts.runtime import ModelCallRecord


class LlmQueryRewriter:
    """只改写省略或指代，不做意图分类、不生成答案、不补造业务事实。"""

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
        """保存与分类模型一致的连接参数；空配置时安全跳过。"""

        self._provider = provider
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._context_window_tokens = context_window_tokens

    async def rewrite(
        self,
        query: QueryContext,
        *,
        state: dict[str, object],
        attempt: int = 0,
    ) -> tuple[QueryContext | None, ModelCallRecord]:
        """用近期消息和已确认事实请求一次结构化改写。"""

        started = time.perf_counter()
        call_key = f"{state['run_id']}:QUERY_REWRITE:{attempt}"
        call_id = f"mc_{uuid5(NAMESPACE_URL, call_key).hex}"
        if not self._base_url or not self._api_key:
            return None, self._failure(state, call_id, "QUERY_REWRITE_CONFIG_MISSING", started)
        payload = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": self._user_prompt(query, state)},
            ],
        }
        estimated_input_tokens: int | None = None
        try:
            estimated_input_tokens = validate_chat_request_budget(
                payload,
                context_window_tokens=self._context_window_tokens,
            )
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                response.raise_for_status()
                body = response.json()
            output = self._parse_json(body["choices"][0]["message"]["content"])
            rewritten = self._to_query(output, query, state)
            record = self._record(
                state=state,
                call_id=call_id,
                status=ModelCallStatus.COMPLETED,
                started=started,
                usage=body.get("usage") or {},
                provider_request_id=body.get("id"),
                estimated_input_tokens=estimated_input_tokens,
            )
            return rewritten, record
        except httpx.TimeoutException:
            return None, self._failure(
                state,
                call_id,
                "QUERY_REWRITE_TIMEOUT",
                started,
                ModelCallStatus.TIMEOUT,
            )
        except ModelContextWindowExceeded as error:
            return None, self._failure(
                state,
                call_id,
                "MODEL_CONTEXT_WINDOW_EXCEEDED",
                started,
                estimated_input_tokens=error.estimated_input_tokens,
            )
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            return None, self._failure(
                state,
                call_id,
                "QUERY_REWRITE_OUTPUT_INVALID",
                started,
                estimated_input_tokens=estimated_input_tokens,
            )

    @staticmethod
    def _system_prompt() -> str:
        """限制模型只解释会话行为并返回受控 Query Resolution 结果。"""

        return query_rewrite_system_prompt()

    @staticmethod
    def _user_prompt(query: QueryContext, state: dict[str, object]) -> str:
        """只向模型提供有界近期消息和已确认事实。"""

        return query_rewrite_user_prompt(query, state)

    @staticmethod
    def _parse_json(content: Any) -> dict[str, Any]:
        """解析 JSON，并兼容模型偶尔返回的代码围栏。"""

        if not isinstance(content, str):
            raise ValueError("query rewrite output must be text")
        text = content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
        if not isinstance(result, dict):
            raise ValueError("query rewrite output must be an object")
        return result

    @staticmethod
    def _to_query(
        output: dict[str, Any], query: QueryContext, state: dict[str, object]
    ) -> QueryContext | None:
        """校验引用与上一轮真实结果，再把模型建议收敛成 Query 契约。"""

        status = QueryResolutionStatus(str(output.get("status", "INSUFFICIENT")))
        dialogue_act = DialogueAct(str(output.get("dialogue_act", "NEW_REQUEST")))
        refs = tuple(str(item) for item in output.get("used_context_refs", []))
        allowed = {
            str(item.get("source_ref"))
            for item in state.get("confirmed_facts", [])
            if isinstance(item, dict) and item.get("authority") == "CONFIRMED"
        }
        allowed.update(
            str(item.get("source_ref") or f"turn:{item.get('turn_id')}")
            for item in prompt_message_dicts(state)
            if isinstance(item, dict) and item.get("turn_id")
        )
        allowed.update(
            str(item.get("source_ref"))
            for item in state.get("history_recalled_messages", [])
            if isinstance(item, dict) and item.get("source_ref")
        )
        summary, summary_ref = current_summary(state)
        if isinstance(summary_ref, str) and summary:
            allowed.add(summary_ref)
        previous_assistant_message = state.get("previous_assistant_message")
        if isinstance(previous_assistant_message, dict) and previous_assistant_message.get(
            "source_ref"
        ):
            allowed.add(str(previous_assistant_message["source_ref"]))
        pending_clarification = state.get("pending_clarification")
        clarification_origin_query = state.get("clarification_origin_query")
        clarification_origin_ref = None
        if (
            isinstance(pending_clarification, dict)
            and pending_clarification.get("clarification_id")
            and isinstance(clarification_origin_query, str)
            and clarification_origin_query
        ):
            clarification_origin_ref = (
                f"clarification:{pending_clarification['clarification_id']}:origin"
            )
            allowed.add(clarification_origin_ref)
        if not set(refs).issubset(allowed):
            raise ValueError("resolved query must cite allowed confirmed context")
        if status is QueryResolutionStatus.NOT_REQUIRED:
            if dialogue_act is DialogueAct.CORRECTION:
                raise ValueError("correction requires a resolved query")
            resolution = QueryResolutionResult(
                status=status,
                dialogue_act=dialogue_act,
                reason_codes=(str(output.get("reason", "MODEL_NOT_REQUIRED")),),
            )
            return query.model_copy(
                update={"resolution": resolution, "effective_query": query.normalized_query}
            )
        if status is not QueryResolutionStatus.RESOLVED:
            return None
        resolved = str(output.get("resolved_query", "")).strip()
        if not resolved or not refs or len(resolved) > 4000:
            raise ValueError("resolved query must cite allowed confirmed context")
        if dialogue_act is DialogueAct.CLARIFICATION_REPLY:
            if not isinstance(pending_clarification, dict) or not clarification_origin_ref:
                raise ValueError("clarification reply requires pending clarification context")
            if clarification_origin_ref not in refs:
                raise ValueError("clarification reply must cite its saved origin query")
        LlmQueryRewriter._reject_new_numeric_facts(resolved, query, state, refs)

        preserve_intent = None
        preserved_intent_id = None
        excluded_intent_id = None
        excluded_faq_item_id = None
        correction_source_ref = None
        reason_codes = [str(output.get("reason", "MODEL_RESOLVED"))]
        if dialogue_act is DialogueAct.CORRECTION:
            if not isinstance(previous_assistant_message, dict):
                raise ValueError("correction requires previous assistant message")
            correction_source_ref = str(previous_assistant_message.get("source_ref") or "")
            previous_turn_id = str(previous_assistant_message.get("turn_id") or "")
            if not correction_source_ref or correction_source_ref not in refs:
                raise ValueError("correction must cite previous assistant message")
            previous_routing = (
                state.get("routing_decision")
                if state.get("routing_turn_id") == previous_turn_id
                else None
            )
            previous_faq = (
                state.get("faq_resolution")
                if state.get("faq_turn_id") == previous_turn_id
                else None
            )
            requested_preserve = output.get("preserve_intent")
            previous_intent_id = (
                previous_routing.get("intent_decision", {}).get("intent_id")
                if isinstance(previous_routing, dict)
                else None
            )
            previous_faq_item_id = (
                previous_faq.get("faq_item_id") if isinstance(previous_faq, dict) else None
            )
            requested_faq_item_id = output.get("exclude_faq_item_id")
            if (
                requested_preserve is True
                and previous_intent_id
                and previous_faq_item_id
                and requested_faq_item_id == previous_faq_item_id
            ):
                preserve_intent = True
                preserved_intent_id = str(previous_intent_id)
                excluded_faq_item_id = str(previous_faq_item_id)
            elif requested_preserve is False:
                preserve_intent = False
                excluded_intent_id = str(previous_intent_id) if previous_intent_id else None
            else:
                # 对话行为和改写仍可保留，但未经真实上一轮结果证明的排除目标
                # 不进入路由约束，避免模型凭空指定内部编号。
                reason_codes.append("CORRECTION_TARGET_UNVERIFIED")
        resolution = QueryResolutionResult(
            status=QueryResolutionStatus.RESOLVED,
            resolved_query=resolved,
            used_context_refs=refs,
            reason_codes=tuple(reason_codes),
            dialogue_act=dialogue_act,
            preserve_intent=preserve_intent,
            preserved_intent_id=preserved_intent_id,
            excluded_intent_id=excluded_intent_id,
            excluded_faq_item_id=excluded_faq_item_id,
            correction_source_ref=correction_source_ref,
        )
        return query.model_copy(update={"resolution": resolution, "effective_query": resolved})

    @staticmethod
    def _reject_new_numeric_facts(
        resolved: str,
        query: QueryContext,
        state: dict[str, object],
        refs: tuple[str, ...],
    ) -> None:
        """拒绝改写器凭空增加账号、金额或其他数字事实。"""

        source_texts = [query.raw_query]
        ref_set = set(refs)
        for item in prompt_message_dicts(state):
            item_ref = (
                str(item.get("source_ref") or f"turn:{item.get('turn_id')}")
                if isinstance(item, dict)
                else ""
            )
            if isinstance(item, dict) and item_ref in ref_set:
                source_texts.append(str(item.get("content") or ""))
        for item in state.get("history_recalled_messages", []):
            if isinstance(item, dict) and str(item.get("source_ref") or "") in ref_set:
                source_texts.append(str(item.get("content") or ""))
        summary, summary_ref = current_summary(state)
        if isinstance(summary_ref, str) and summary_ref in ref_set:
            source_texts.append(str(summary or ""))
        previous = state.get("previous_assistant_message")
        if isinstance(previous, dict) and str(previous.get("source_ref")) in ref_set:
            source_texts.append(str(previous.get("content") or ""))
        pending = state.get("pending_clarification")
        origin = state.get("clarification_origin_query")
        if (
            isinstance(pending, dict)
            and pending.get("clarification_id")
            and isinstance(origin, str)
            and f"clarification:{pending['clarification_id']}:origin" in ref_set
        ):
            source_texts.append(origin)
        for item in state.get("confirmed_facts", []):
            if isinstance(item, dict) and str(item.get("source_ref")) in ref_set:
                source_texts.append(json.dumps(item.get("value"), ensure_ascii=False))
        available = "\n".join(source_texts)
        numeric_pattern = r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?%?"
        invented = set(re.findall(numeric_pattern, resolved)) - set(
            re.findall(numeric_pattern, available)
        )
        if invented:
            raise ValueError("resolved query introduced unverified numeric facts")

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
        """记录真实供应商用量，供应商缺少 usage 时保持空值。"""

        return ModelCallRecord(
            call_id=call_id,
            run_id=str(state["run_id"]),
            turn_id=str(state["turn_id"]),
            trace_id=str(state["trace_id"]),
            purpose="QUERY_REWRITE",
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

    def _failure(
        self,
        state: dict[str, object],
        call_id: str,
        error_code: str,
        started: float,
        status: ModelCallStatus = ModelCallStatus.FAILED,
        estimated_input_tokens: int | None = None,
    ) -> ModelCallRecord:
        """模型失败时保留原 Query，交给后续澄清或安全兜底。"""

        return self._record(
            state=state,
            call_id=call_id,
            status=status,
            started=started,
            usage={},
            error_code=error_code,
            estimated_input_tokens=estimated_input_tokens,
        )

    @staticmethod
    def _number(value: Any) -> int | None:
        """只接受非负整数 Token 用量。"""

        return int(value) if isinstance(value, (int, float)) and value >= 0 else None
