"""SOP 人工处理结果的通用展示。"""

from shared.contracts.sop import BusinessOperationResult, BusinessToolResult, SopProgress
from shared.sop_config import SopConfig, format_sop_text


def render_review_result(
    config: SopConfig,
    progress: SopProgress,
    result: BusinessOperationResult,
    tool_results: tuple[BusinessToolResult, ...],
) -> str:
    """根据通用操作状态生成不夸大结果的说明。"""

    del config, progress, tool_results
    if result.status == "SUCCEEDED":
        return format_sop_text("人工处理已完成，结果已记录。")
    if result.status == "UNKNOWN":
        return format_sop_text("处理结果暂时无法确认，请勿重复提交，系统将继续核对。")
    return format_sop_text("当前请求未能完成，已保留处理记录。")
