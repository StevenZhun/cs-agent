"""在已授权会话内按受控决策读取有限历史原文。"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx
from psycopg import AsyncConnection
from psycopg import Error as PsycopgError

from prompts.history_recall import history_recall_system_prompt, history_recall_user_prompt
from services.ai_runtime.model_request import (
    ModelContextWindowExceeded,
    validate_chat_request_budget,
)
from services.ai_runtime.query.message_state import current_summary, prompt_message_dicts
from shared.contracts.enums import HistoryRecallMode, MessageRole, ModelCallStatus
from shared.contracts.query import HistoryRecallDecision, QueryContext
from shared.contracts.runtime import ChatHistoryMessage, ModelCallRecord

HISTORY_CONTEXT_CLARIFICATION = (
    "我暂时无法从当前会话中唯一确定您指的是哪段历史内容，请补充具体业务或对象。"
)


@dataclass(frozen=True)
class HistoryRecallResult:
    """返回按时间排序的历史原文，并标明是否因数量上限被截断。"""

    messages: tuple[ChatHistoryMessage, ...]
    truncated: bool


def allowed_history_targets(
    state: dict[str, object],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """只暴露摘要元数据中存在、且已不在近期窗口内的有限引用。"""

    metadata = state.get("summary_metadata")
    if not isinstance(metadata, dict):
        return (), ()
    recent_refs = {str(message.get("source_ref") or "") for message in prompt_message_dicts(state)}
    refs = tuple(
        dict.fromkeys(
            str(reference)
            for reference in metadata.get("source_refs", [])
            if re.fullmatch(r"(?:message|turn):[A-Za-z0-9_.-]{1,64}", str(reference))
            and str(reference) not in recent_refs
        )
    )[:8]
    turn_ids = tuple(
        dict.fromkeys(
            str(turn_id)
            for turn_id in metadata.get("covered_turn_ids", [])
            if 0 < len(str(turn_id)) <= 64
        )
    )[:8]
    return refs, turn_ids


class HistoryRecallCoordinator:
    """让模型在有限回查动作中选择，再由代码执行同会话只读查询。"""

    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        context_window_tokens: int,
        database_dsn: str,
    ) -> None:
        self._provider = provider
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._context_window_tokens = context_window_tokens
        self._database_dsn = database_dsn

    async def recall(
        self,
        query: QueryContext,
        *,
        state: dict[str, object],
    ) -> tuple[HistoryRecallDecision, HistoryRecallResult, ModelCallRecord | None]:
        """最多规划并执行一次回查；读取失败时收敛为澄清。"""

        decision, record = await self._decide(query, state=state)
        if decision.mode not in {
            HistoryRecallMode.EXACT_REF,
            HistoryRecallMode.TURN_RANGE,
            HistoryRecallMode.CONVERSATION_SEARCH,
        }:
            return decision, HistoryRecallResult(messages=(), truncated=False), record
        try:
            result = await self.read(decision, state=state)
        except (KeyError, TypeError, ValueError, OSError, PsycopgError):
            decision = self._clarify(state, "HISTORY_RECALL_UNAVAILABLE")
            result = HistoryRecallResult(messages=(), truncated=False)
        return decision, result, record

    async def read(
        self,
        decision: HistoryRecallDecision,
        *,
        state: dict[str, object],
    ) -> HistoryRecallResult:
        """执行已经由上层校验的受控读取，不再次调用决策模型。"""

        before = datetime.fromisoformat(str(state["turn_started_at"]))
        async with await AsyncConnection.connect(self._database_dsn) as connection:
            return await recall_chat_history(
                connection,
                decision,
                authorized_conversation_id=str(state["conversation_id"]),
                before=before,
            )

    async def _decide(
        self,
        query: QueryContext,
        *,
        state: dict[str, object],
    ) -> tuple[HistoryRecallDecision, ModelCallRecord | None]:
        """只在存在被压缩历史时请求一次结构化选择。"""

        summary, _summary_ref = current_summary(state)
        if not summary:
            return self._not_required(state, "NO_COMPRESSED_HISTORY"), None
        attempt = int(state.get("history_recall_count") or 0)
        if attempt >= 1:
            return self._clarify(state, "HISTORY_RECALL_LIMIT_REACHED"), None

        allowed_source_refs, allowed_turn_ids = allowed_history_targets(state)
        started = time.perf_counter()
        call_id = f"mc_{uuid5(NAMESPACE_URL, str(state['run_id']) + ':HISTORY_RECALL').hex}"
        if not self._base_url or not self._api_key:
            return self._clarify(state, "HISTORY_RECALL_CONFIG_MISSING"), self._record(
                state=state,
                call_id=call_id,
                status=ModelCallStatus.FAILED,
                started=started,
                usage={},
                error_code="HISTORY_RECALL_CONFIG_MISSING",
            )
        payload = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": history_recall_system_prompt()},
                {
                    "role": "user",
                    "content": history_recall_user_prompt(
                        query,
                        state,
                        allowed_source_refs=allowed_source_refs,
                        allowed_turn_ids=allowed_turn_ids,
                    ),
                },
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
            decision = self._to_decision(
                output,
                state=state,
                allowed_source_refs=allowed_source_refs,
                allowed_turn_ids=allowed_turn_ids,
            )
            record = self._record(
                state=state,
                call_id=call_id,
                status=ModelCallStatus.COMPLETED,
                started=started,
                usage=body.get("usage") or {},
                provider_request_id=body.get("id"),
                estimated_input_tokens=estimated_input_tokens,
            )
            return decision, record
        except httpx.TimeoutException:
            error_code = "HISTORY_RECALL_TIMEOUT"
            status = ModelCallStatus.TIMEOUT
        except ModelContextWindowExceeded as error:
            error_code = "MODEL_CONTEXT_WINDOW_EXCEEDED"
            status = ModelCallStatus.FAILED
            estimated_input_tokens = error.estimated_input_tokens
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            error_code = "HISTORY_RECALL_OUTPUT_INVALID"
            status = ModelCallStatus.FAILED
        return self._clarify(state, error_code), self._record(
            state=state,
            call_id=call_id,
            status=status,
            started=started,
            usage={},
            error_code=error_code,
            estimated_input_tokens=estimated_input_tokens,
        )

    @staticmethod
    def _parse_json(content: Any) -> dict[str, Any]:
        """解析 JSON，并兼容供应商偶发的代码围栏。"""

        if not isinstance(content, str):
            raise ValueError("history recall output must be text")
        text = content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
        if not isinstance(result, dict):
            raise ValueError("history recall output must be an object")
        return result

    @staticmethod
    def _to_decision(
        output: dict[str, Any],
        *,
        state: dict[str, object],
        allowed_source_refs: tuple[str, ...],
        allowed_turn_ids: tuple[str, ...],
    ) -> HistoryRecallDecision:
        """把模型建议限制在代码提供的会话、引用和次数范围内。"""

        mode = HistoryRecallMode(str(output.get("mode", "CLARIFY")))
        reason = str(output.get("reason") or "MODEL_HISTORY_RECALL")[:64]
        common = {
            "mode": mode,
            "conversation_id": str(state["conversation_id"]),
            "turn_id": str(state["turn_id"]),
            "attempt": int(state.get("history_recall_count") or 0),
            "reason_codes": (reason,),
        }
        if mode is HistoryRecallMode.NOT_REQUIRED:
            return HistoryRecallDecision(**common)
        if mode is HistoryRecallMode.CLARIFY:
            return HistoryRecallDecision(
                **common,
                clarification=HISTORY_CONTEXT_CLARIFICATION,
            )
        if mode is HistoryRecallMode.EXACT_REF:
            refs = tuple(str(item) for item in output.get("target_source_refs", []))
            if not refs or not set(refs).issubset(allowed_source_refs):
                raise ValueError("history recall references are not allowlisted")
            return HistoryRecallDecision(**common, target_source_refs=refs)
        if mode is HistoryRecallMode.TURN_RANGE:
            turn_ids = tuple(str(item) for item in output.get("target_turn_ids", []))
            if not turn_ids or not set(turn_ids).issubset(allowed_turn_ids):
                raise ValueError("history recall turns are not allowlisted")
            return HistoryRecallDecision(**common, target_turn_ids=turn_ids)
        search_terms = tuple(str(item).strip() for item in output.get("search_terms", []))
        if any(re.search(r"\d{6,}", term) for term in search_terms):
            raise ValueError("history search cannot introduce long numeric identifiers")
        return HistoryRecallDecision(**common, search_terms=search_terms)

    @staticmethod
    def _not_required(state: dict[str, object], reason: str) -> HistoryRecallDecision:
        return HistoryRecallDecision(
            mode=HistoryRecallMode.NOT_REQUIRED,
            conversation_id=str(state["conversation_id"]),
            turn_id=str(state["turn_id"]),
            attempt=int(state.get("history_recall_count") or 0),
            reason_codes=(reason,),
        )

    @staticmethod
    def _clarify(state: dict[str, object], reason: str) -> HistoryRecallDecision:
        return HistoryRecallDecision(
            mode=HistoryRecallMode.CLARIFY,
            conversation_id=str(state["conversation_id"]),
            turn_id=str(state["turn_id"]),
            attempt=min(int(state.get("history_recall_count") or 0), 3),
            clarification=HISTORY_CONTEXT_CLARIFICATION,
            reason_codes=(reason,),
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
        """记录回查选择模型的真实耗时和 Token 用量。"""

        return ModelCallRecord(
            call_id=call_id,
            run_id=str(state["run_id"]),
            turn_id=str(state["turn_id"]),
            trace_id=str(state["trace_id"]),
            purpose="HISTORY_RECALL_DECISION",
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
        return int(value) if isinstance(value, (int, float)) and value >= 0 else None


def _like_pattern(term: str) -> str:
    """转义 SQL LIKE 通配符，确保模型提供的搜索词只按普通文本匹配。"""

    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def recall_chat_history(
    connection: AsyncConnection,
    decision: HistoryRecallDecision,
    *,
    authorized_conversation_id: str,
    before: datetime,
) -> HistoryRecallResult:
    """在同一会话和当前 Turn 之前执行一次有界历史读取。

    决策模型只提供引用或短搜索词；本函数负责会话授权、时间边界、角色过滤、
    数量上限和参数化 SQL。返回的原文仍只是对话证据，不会成为业务权威事实。
    """

    if decision.conversation_id != authorized_conversation_id:
        raise ValueError("history recall conversation does not match authorized conversation")
    if decision.mode in {HistoryRecallMode.NOT_REQUIRED, HistoryRecallMode.CLARIFY}:
        return HistoryRecallResult(messages=(), truncated=False)

    common = (
        "conversation_id = %s AND created_at < %s AND role IN ('CUSTOMER', 'ASSISTANT', 'AGENT')"
    )
    parameters: list[object] = [authorized_conversation_id, before]
    if decision.mode is HistoryRecallMode.EXACT_REF:
        message_ids = [
            reference.removeprefix("message:")
            for reference in decision.target_source_refs
            if reference.startswith("message:")
        ]
        turn_ids = [
            reference.removeprefix("turn:")
            for reference in decision.target_source_refs
            if reference.startswith("turn:")
        ]
        common += " AND (message_id = ANY(%s) OR turn_id = ANY(%s))"
        parameters.extend((message_ids, turn_ids))
    elif decision.mode is HistoryRecallMode.TURN_RANGE:
        common += " AND turn_id = ANY(%s)"
        parameters.append(list(decision.target_turn_ids))
    elif decision.mode is HistoryRecallMode.CONVERSATION_SEARCH:
        search_conditions = " OR ".join(
            "content ILIKE %s ESCAPE '\\\\'" for _term in decision.search_terms
        )
        common += f" AND ({search_conditions})"
        parameters.extend(_like_pattern(term) for term in decision.search_terms)
    else:
        raise ValueError("unsupported history recall mode")

    parameters.append(decision.max_results + 1)
    cursor = await connection.execute(
        "SELECT message_id, conversation_id, thread_id, turn_id, role, content, "
        "tool_call_id, request_id, trace_id, created_at FROM ("
        "SELECT message_id, conversation_id, thread_id, turn_id, role, content, "
        "tool_call_id, request_id, trace_id, created_at FROM app.chat_history WHERE "
        f"{common} ORDER BY created_at DESC, message_id DESC LIMIT %s"
        ") AS recalled ORDER BY created_at, message_id",
        tuple(parameters),
    )
    rows = list(await cursor.fetchall())
    truncated = len(rows) > decision.max_results
    messages = tuple(
        ChatHistoryMessage(
            message_id=row[0],
            conversation_id=row[1],
            thread_id=row[2],
            turn_id=row[3],
            role=MessageRole(row[4]),
            content=row[5],
            tool_call_id=row[6],
            request_id=row[7],
            trace_id=row[8],
            created_at=row[9],
        )
        for row in rows[: decision.max_results]
    )
    return HistoryRecallResult(messages=messages, truncated=truncated)
