"""SOP 历史候选恢复的扩展边界。"""

from dataclasses import dataclass

from services.ai_runtime.query.history import HistoryRecallCoordinator
from shared.contracts.sop import SlotState
from shared.sop_config import SopConfig


@dataclass(frozen=True)
class SopHistoryRecovery:
    """一次有界历史回查的结果。"""

    slot: SlotState | None
    recalled_source_refs: tuple[str, ...]
    reason_code: str


async def recover_sop_slot_candidate(
    state: dict,
    *,
    config: SopConfig,
    field_name: str,
    target_source_refs: tuple[str, ...],
    reader: HistoryRecallCoordinator | None,
) -> SopHistoryRecovery:
    """公开默认流程不从历史消息自动恢复字段值。"""

    del state, config, field_name, target_source_refs, reader
    return SopHistoryRecovery(None, (), "SOP_HISTORY_RECALL_NOT_PUBLISHED")
