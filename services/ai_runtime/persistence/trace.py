"""一次 Run 结束后批量保存节点 Span，避免图节点逐个写数据库。"""

from __future__ import annotations

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from shared.contracts.observability import TraceSpan


class TraceSpanRepository:
    """把已经过契约校验的无正文 Span 幂等写入 app.trace_span。"""

    def __init__(self, database_dsn: str) -> None:
        self._database_dsn = database_dsn

    async def save_many(self, spans: tuple[TraceSpan, ...]) -> None:
        if not spans:
            return
        values = [
            (
                span.span_id,
                span.trace_id,
                span.conversation_id,
                span.thread_id,
                span.turn_id,
                span.run_id,
                span.parent_span_id,
                span.component,
                span.operation,
                span.status.value,
                span.started_at,
                span.finished_at,
                span.latency_ms,
                Jsonb(span.attributes),
                span.error_code,
            )
            for span in spans
        ]
        async with await AsyncConnection.connect(self._database_dsn) as connection:
            async with connection.cursor() as cursor:
                await cursor.executemany(
                    "INSERT INTO app.trace_span "
                    "(span_id, trace_id, conversation_id, thread_id, turn_id, run_id, "
                    "parent_span_id, component, operation, status, started_at, finished_at, "
                    "latency_ms, attributes, error_code) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (span_id) DO NOTHING",
                    values,
                )
            await connection.commit()
