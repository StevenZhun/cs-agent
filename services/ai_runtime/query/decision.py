"""确定性 Query 决策：保留统一 Trace，但不假装已有分类模型。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import perf_counter
from uuid import NAMESPACE_URL, uuid5

from shared.contracts.enums import DecisionProfile, DecisionTask
from shared.contracts.query import QueryContext
from shared.contracts.runtime import DecisionSignal, DecisionTrace


async def baseline_query_signal(query: QueryContext) -> DecisionSignal:
    """只回报确定性 Query 消解状态；不执行意图、FAQ 或业务域判断。"""

    return DecisionSignal(
        signal_name="query_resolution_status",
        value=query.resolution.status.value,
        confidence=1.0,
        reason_codes=query.resolution.reason_codes,
    )


async def decide_query(
    state: dict[str, object],
    *,
    configured_profile: DecisionProfile,
    timeout_ms: int,
) -> dict[str, object]:
    """用同一 Graph 生成可追踪的基线信号；Jev 未接入时显式回退。"""

    query = QueryContext.model_validate(state["query_context"])
    started = perf_counter()
    fallback_from = DecisionProfile.JEV if configured_profile is DecisionProfile.JEV else None
    fallback_reason = "JEV_PROVIDER_NOT_CONFIGURED" if fallback_from else None
    try:
        signal = await asyncio.wait_for(baseline_query_signal(query), timeout=timeout_ms / 1000)
    except TimeoutError:
        # 连基线判断都超时，也只能记录“不确定”，不能编造一个模型分类结果。
        signal = DecisionSignal(
            signal_name="query_resolution_status",
            value="UNSURE",
            confidence=0.0,
            reason_codes=("BASELINE_TIMEOUT",),
        )
        if fallback_from is not None:
            fallback_reason = "JEV_UNAVAILABLE_AND_BASELINE_TIMEOUT"
    run_id = str(state["run_id"])
    trace = DecisionTrace(
        decision_id=f"decision_{uuid5(NAMESPACE_URL, 'query:' + run_id).hex}",
        decision_task=DecisionTask.QUERY,
        active_profile=DecisionProfile.BASELINE,
        provider="baseline-query-stub",
        provider_model="deterministic-v1",
        input_snapshot_ref=f"graph_state:{state['thread_id']}:{state['turn_id']}",
        threshold_policy_version="query-stub-v1",
        signals=(signal,),
        final_decision_ref=f"query_resolution:{state['turn_id']}",
        latency_ms=max(0, int((perf_counter() - started) * 1000)),
        fallback_from=fallback_from,
        fallback_reason=fallback_reason,
        created_at=datetime.now(UTC),
    )
    return {"decision_trace": trace.model_dump(mode="json")}
