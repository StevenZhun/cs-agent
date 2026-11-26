"""工作上下文摘要的状态外持久化契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from shared.contracts.common import ContractModel


class ContextSummaryRecord(ContractModel):
    """一次不可变摘要产物；Graph State 只保留当前聚合摘要和有限引用。"""

    summary_record_id: str = Field(min_length=1, max_length=96)
    conversation_id: str = Field(min_length=1, max_length=64)
    thread_id: str = Field(min_length=1, max_length=64)
    context_cycle_id: str = Field(min_length=1, max_length=96)
    record_kind: Literal["SEGMENT", "AGGREGATE", "BASELINE"]
    parent_summary_record_id: str | None = Field(default=None, max_length=96)
    summary_text: str = Field(min_length=1)
    covered_turn_ids: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    generation_method: str = Field(min_length=1, max_length=32)
    prompt_version: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    validation_status: Literal["VALID", "EXTRACTIVE_FALLBACK"]
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
