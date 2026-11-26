"""SOP 只读工具节点的通用占位实现。"""

from services.ai_runtime.persistence.artifacts import RuntimeArtifactRepository
from services.ai_runtime.sop.business import SopBusinessClient


async def run_sop_tools(
    state: dict,
    client: SopBusinessClient | None,
    timezone: str,
    artifact_repository: RuntimeArtifactRepository | None = None,
    externalize_bytes: int = 4096,
) -> dict:
    """拒绝执行未由发布配置声明的行业工具。"""

    del state, client, timezone, artifact_repository, externalize_bytes
    raise ValueError("当前 SOP 没有发布只读业务工具")


def next_after_sop(state: dict) -> str:
    """无工具流程直接结束当前轮。"""

    del state
    return "end"
