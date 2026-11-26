"""LangGraph 节点的轻量计时与无正文诊断标签。"""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from langchain_core.runnables import RunnableConfig

from shared.contracts.enums import TraceSpanStatus
from shared.contracts.observability import TraceSpan
from shared.observability import emit_structured_event

NodeAction = Callable[..., dict[str, object] | Awaitable[dict[str, object]]]

_NODE_COMPONENTS = {
    "prepare_turn": "CONTEXT",
    "guard_input": "GUARDRAIL",
    "compress_context": "CONTEXT",
    "load_catalog": "ROUTING",
    "decide_query": "CONTEXT",
    "resolve_query": "CONTEXT",
    "history_clarification": "CONTEXT",
    "check_active_sop": "SOP",
    "run_sop": "SOP",
    "run_sop_side_query": "TOOL",
    "run_sop_tools": "TOOL",
    "prepare_review": "SOP",
    "await_review": "SOP",
    "apply_review": "SOP",
    "run_approved_operation": "TOOL",
    "finish_sop": "SOP",
    "route_turn": "ROUTING",
    "resolve_faq": "FAQ",
    "finalize_faq": "FAQ",
    "retrieve_rag": "RAG",
    "grounded_answer": "RAG",
    "await_rag_review": "RAG",
    "apply_rag_review": "RAG",
    "clarification": "ROUTING",
    "policy_answer": "GUARDRAIL",
    "safe_fallback": "GUARDRAIL",
    "human_handoff": "ROUTING",
}


def _attributes(update: dict[str, object]) -> dict[str, object]:
    """只抽取有限状态和计数，禁止把消息、Query 或答案写进 Span。"""

    attributes: dict[str, object] = {}
    scalar_fields = {
        "terminal_response_path": "response_path",
        "terminal_reason_code": "reason_code",
        "semantic_faq_search_status": "faq_search_status",
        "sop_action": "sop_action",
        "sop_reason_code": "sop_reason_code",
    }
    for source, target in scalar_fields.items():
        value = update.get(source)
        if isinstance(value, (str, bool, int, float)):
            attributes[target] = value
    for source, target in {
        "semantic_candidates": "intent_candidate_count",
        "semantic_faq_candidates": "faq_candidate_count",
        "sop_tool_results": "tool_result_count",
        "model_calls": "model_call_count",
    }.items():
        value = update.get(source)
        if isinstance(value, list):
            attributes[target] = len(value)
    routing = update.get("routing_decision")
    if isinstance(routing, dict):
        for key in ("intent_status", "request_scope", "request_kind", "response_path"):
            value = routing.get(key)
            if isinstance(value, (str, bool, int, float)):
                attributes[f"routing_{key}"] = value
    faq = update.get("faq_resolution")
    if isinstance(faq, dict):
        for key in ("match_status", "miss_reason"):
            value = faq.get(key)
            if isinstance(value, (str, bool, int, float)):
                attributes[f"faq_{key}"] = value
    retrieval = update.get("rag_retrieval_summary")
    if isinstance(retrieval, dict):
        for key in ("support_status", "candidate_count", "evidence_count"):
            value = retrieval.get(key)
            if isinstance(value, (str, bool, int, float)):
                attributes[f"rag_{key}"] = value
    return attributes


def observe_node(
    operation: str,
    action: NodeAction,
    *,
    receives_config: bool = False,
) -> NodeAction:
    """包装节点并把本轮 Span 作为临时状态增量返回。"""

    async def observed(state: dict[str, Any], config: RunnableConfig):
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        try:
            result = action(state, config) if receives_config else action(state)
            update = await result if inspect.isawaitable(result) else result
        except Exception as error:
            emit_structured_event(
                "graph_node_failed",
                trace_id=state.get("trace_id"),
                run_id=state.get("run_id"),
                operation=operation,
                error_code=type(error).__name__.upper(),
            )
            raise
        started_key = started_at.isoformat()
        finished_at = datetime.now(UTC)
        span_identity = f"{state.get('run_id')}:{operation}:{started_key}"
        span = TraceSpan(
            span_id=f"span_{uuid5(NAMESPACE_URL, span_identity).hex}",
            trace_id=str(state["trace_id"]),
            conversation_id=str(state["conversation_id"]),
            thread_id=str(state["thread_id"]),
            turn_id=str(state["turn_id"]),
            run_id=str(state["run_id"]),
            component=_NODE_COMPONENTS.get(operation, "RUNTIME"),
            operation=operation,
            status=TraceSpanStatus.COMPLETED,
            started_at=started_at,
            finished_at=finished_at,
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            attributes=_attributes(update),
        )
        current = [
            item
            for item in state.get("trace_spans", [])
            if isinstance(item, dict) and item.get("run_id") == state.get("run_id")
        ]
        return {**update, "trace_spans": [*current, span.model_dump(mode="json")]}

    return observed
