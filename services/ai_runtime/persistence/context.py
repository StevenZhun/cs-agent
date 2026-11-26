"""保存不可变摘要记录，并为重新基线化读取有限摘要链。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from shared.contracts.context import ContextSummaryRecord


class ContextSummaryRepository:
    """在应用库追加摘要记录；不修改 LangGraph 框架表。"""

    def __init__(self, database_dsn: str) -> None:
        self._database_dsn = database_dsn

    async def append(
        self,
        *,
        state: dict[str, object],
        record_kind: str,
        summary_text: str,
        covered_turn_ids: tuple[str, ...],
        source_refs: tuple[str, ...],
        generation_method: str,
        parent_summary_record_id: str | None,
        prompt_version: str | None,
        model: str | None,
        context_cycle_id: str | None = None,
    ) -> ContextSummaryRecord:
        """写入一条不可变摘要；稳定编号使同一 Run 重试保持幂等。"""

        content_hash = hashlib.sha256(summary_text.encode("utf-8")).hexdigest()
        identity = ":".join(
            (
                str(state["run_id"]),
                str(state.get("context_cycle_sequence") or 0),
                record_kind,
                content_hash,
            )
        )
        record = ContextSummaryRecord(
            summary_record_id=f"summary_{uuid5(NAMESPACE_URL, identity).hex}",
            conversation_id=str(state["conversation_id"]),
            thread_id=str(state["thread_id"]),
            context_cycle_id=str(
                context_cycle_id or state.get("context_cycle_id") or f"ctx:{state['thread_id']}:0"
            ),
            record_kind=record_kind,
            parent_summary_record_id=parent_summary_record_id,
            summary_text=summary_text,
            covered_turn_ids=covered_turn_ids,
            source_refs=source_refs,
            generation_method=generation_method,
            prompt_version=prompt_version,
            model=model,
            validation_status=(
                "EXTRACTIVE_FALLBACK" if generation_method == "EXTRACTIVE_FALLBACK" else "VALID"
            ),
            content_hash=content_hash,
            created_at=datetime.now(UTC),
        )
        async with await AsyncConnection.connect(self._database_dsn) as connection:
            await connection.execute(
                "INSERT INTO app.context_summary_record "
                "(summary_record_id, conversation_id, thread_id, context_cycle_id, "
                "record_kind, parent_summary_record_id, summary_text, covered_turn_ids, "
                "source_refs, generation_method, prompt_version, model, validation_status, "
                "content_hash, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, "
                "%s, %s, %s) ON CONFLICT (summary_record_id) DO NOTHING",
                (
                    record.summary_record_id,
                    record.conversation_id,
                    record.thread_id,
                    record.context_cycle_id,
                    record.record_kind,
                    record.parent_summary_record_id,
                    record.summary_text,
                    Jsonb(list(record.covered_turn_ids)),
                    Jsonb(list(record.source_refs)),
                    record.generation_method,
                    record.prompt_version,
                    record.model,
                    record.validation_status,
                    record.content_hash,
                    record.created_at,
                ),
            )
        return record

    async def recent_segments(
        self,
        *,
        conversation_id: str,
        before_record_id: str | None,
        limit: int,
    ) -> tuple[ContextSummaryRecord, ...]:
        """读取当前会话最近的有限分段摘要，避免一次加载全部历史。"""

        if not 1 <= limit <= 64:
            raise ValueError("summary segment limit must be between 1 and 64")
        async with await AsyncConnection.connect(
            self._database_dsn, row_factory=dict_row
        ) as connection:
            parameters: list[object] = [conversation_id]
            condition = ""
            if before_record_id:
                cursor = await connection.execute(
                    "SELECT created_at FROM app.context_summary_record "
                    "WHERE summary_record_id = %s AND conversation_id = %s",
                    (before_record_id, conversation_id),
                )
                row = await cursor.fetchone()
                if row is not None:
                    condition = " AND created_at <= %s"
                    parameters.append(row["created_at"])
            parameters.append(limit)
            cursor = await connection.execute(
                "SELECT * FROM app.context_summary_record "
                "WHERE conversation_id = %s AND record_kind = 'SEGMENT'"
                + condition
                + " ORDER BY created_at DESC, summary_record_id DESC LIMIT %s",
                tuple(parameters),
            )
            rows = await cursor.fetchall()
        return tuple(
            ContextSummaryRecord.model_validate(
                {
                    **row,
                    "covered_turn_ids": tuple(row["covered_turn_ids"] or ()),
                    "source_refs": tuple(row["source_refs"] or ()),
                }
            )
            for row in reversed(rows)
        )
