"""把大工具结果移出 checkpoint，并为旧状态保留兼容读取。"""

from __future__ import annotations

import json

from services.ai_runtime.persistence.artifacts import RuntimeArtifactRepository
from shared.contracts.artifact import RuntimeArtifactRef
from shared.contracts.sop import BusinessToolResult


async def load_tool_results(
    state: dict,
    artifact_repository: RuntimeArtifactRepository | None,
) -> tuple[BusinessToolResult, ...]:
    """优先回装状态外结果；旧 checkpoint 继续读取原字段。"""

    if state.get("sop_tool_results_ref"):
        if artifact_repository is None:
            raise ValueError("SOP tool artifact repository is unavailable")
        payload = await artifact_repository.load(
            state["sop_tool_results_ref"],
            authorized_conversation_id=str(state["conversation_id"]),
            expected_kind="SOP_TOOL_RESULTS",
        )
        if not isinstance(payload, list):
            raise ValueError("SOP tool artifact must be a list")
        return tuple(BusinessToolResult.model_validate(item) for item in payload)
    return tuple(
        BusinessToolResult.model_validate(item) for item in state.get("sop_tool_results", [])
    )


async def load_tool_results_with_metrics(
    state: dict,
    artifact_repository: RuntimeArtifactRepository | None,
) -> tuple[tuple[BusinessToolResult, ...], dict[str, int]]:
    """回装工具结果并返回加载指标；旧内联状态的数据库读取数为零。"""

    if not state.get("sop_tool_results_ref"):
        return await load_tool_results(state, artifact_repository), {
            "tool_artifact_load_ms": 0,
            "tool_artifact_cache_hits": 0,
            "tool_artifact_database_reads": 0,
        }
    if artifact_repository is None:
        raise ValueError("SOP tool artifact repository is unavailable")
    reference = RuntimeArtifactRef.model_validate(state["sop_tool_results_ref"])
    loaded = await artifact_repository.load_many(
        (reference,),
        authorized_conversation_id=str(state["conversation_id"]),
        expected_kinds={reference.artifact_id: "SOP_TOOL_RESULTS"},
    )
    payload = loaded.payloads[reference.artifact_id]
    if not isinstance(payload, list):
        raise ValueError("SOP tool artifact must be a list")
    return (
        tuple(BusinessToolResult.model_validate(item) for item in payload),
        {
            "tool_artifact_load_ms": loaded.latency_ms,
            "tool_artifact_cache_hits": loaded.cache_hits,
            "tool_artifact_database_reads": loaded.database_reads,
        },
    )


async def tool_result_updates(
    state: dict,
    results: tuple[BusinessToolResult, ...],
    artifact_repository: RuntimeArtifactRepository | None,
    *,
    externalize_bytes: int,
) -> dict[str, object]:
    """小结果直接保留；超过阈值时保存正文并只返回稳定引用。"""

    payload = [result.model_dump(mode="json") for result in results]
    size = len(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    if artifact_repository is None or size < externalize_bytes:
        return {"sop_tool_results": payload, "sop_tool_results_ref": None}
    logical_id = ":".join(result.tool_call_id for result in results) or str(state["turn_id"])
    reference = await artifact_repository.save(
        state=state,
        artifact_kind="SOP_TOOL_RESULTS",
        logical_id=logical_id,
        payload=payload,
        schema_version="sop-tool-results-v1",
        release_version=(
            str(state["sop_release_version"]) if state.get("sop_release_version") else None
        ),
    )
    return {
        "sop_tool_results": [],
        "sop_tool_results_ref": reference.model_dump(mode="json"),
    }
