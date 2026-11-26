"""活动 SOP 旁路查询的扩展节点。"""

from services.ai_runtime.persistence.artifacts import RuntimeArtifactRepository
from services.ai_runtime.sop.business import SopBusinessClient


async def run_sop_side_query(
    state: dict,
    client: SopBusinessClient | None,
    artifact_repository: RuntimeArtifactRepository | None = None,
    externalize_bytes: int = 4096,
) -> dict:
    """未发布旁路查询能力时保留流程，并返回安全说明。"""

    del client, artifact_repository, externalize_bytes
    content = "当前流程没有发布可自动调用的查询能力，原流程进度已保留。"
    return {
        "sop_prefetch_due": False,
        "terminal_turn_id": state["turn_id"],
        "terminal_run_id": state["run_id"],
        "terminal_response_path": "SOP",
        "terminal_content": content,
        "terminal_reason_code": "SOP_LOOKUP_NOT_PUBLISHED",
    }
