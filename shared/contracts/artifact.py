"""状态外运行产物的稳定引用契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from shared.contracts.common import ContractModel


class RuntimeArtifactRef(ContractModel):
    """checkpoint 保存的最小引用；正文由 Artifact Repository 按权限回装。"""

    artifact_id: str = Field(min_length=1, max_length=96)
    artifact_kind: Literal["RAG_RETRIEVAL", "RAG_DRAFT", "SOP_TOOL_RESULTS"]
    conversation_id: str = Field(min_length=1, max_length=64)
    thread_id: str = Field(min_length=1, max_length=64)
    turn_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    schema_version: str = Field(min_length=1, max_length=32)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_version: str | None = Field(default=None, max_length=64)
    index_version: str | None = Field(default=None, max_length=64)
    status: Literal["ACTIVE", "EXPIRED"] = "ACTIVE"
    expires_at: datetime
    created_at: datetime
