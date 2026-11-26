"""SOP 工具结果展示的通用扩展边界。"""

from shared.contracts.sop import BusinessToolResult, SopProgress
from shared.sop_config import SopConfig


def render_tool_reply(
    config: SopConfig,
    progress: SopProgress,
    results: tuple[BusinessToolResult, ...],
    reason: str,
) -> str:
    """拒绝展示未发布工具产生的结果。"""

    del config, progress, results, reason
    raise ValueError("当前 SOP 没有发布工具结果模板")


def result_cards(results: tuple[BusinessToolResult, ...]) -> tuple[object, ...]:
    """保留旧扩展接口；公开默认流程不解析行业对象。"""

    del results
    return ()
