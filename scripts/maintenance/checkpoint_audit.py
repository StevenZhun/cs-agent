"""只读统计 LangGraph checkpoint 的体积与线程归属。

从项目根目录运行：

``uv run python -m scripts.maintenance.checkpoint_audit``

脚本不读取状态正文，也不执行删除。输出用于制定保留策略，不能直接当作清理清单。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from psycopg import connect
from psycopg.rows import dict_row

from services.ai_runtime.persistence.checkpoint import psycopg_url
from shared.web.settings import ServiceSettings

TABLE_SIZE_SQL = """
SELECT relname AS table_name,
       pg_total_relation_size(format('langgraph.%I', relname)::regclass) AS total_bytes,
       n_live_tup AS estimated_rows
FROM pg_stat_user_tables
WHERE schemaname = 'langgraph'
ORDER BY total_bytes DESC
"""

CHANNEL_SIZE_SQL = """
SELECT channel,
       count(*) AS blob_versions,
       sum(coalesce(octet_length(blob), 0)) AS payload_bytes,
       max(coalesce(octet_length(blob), 0)) AS largest_blob_bytes
FROM langgraph.checkpoint_blobs
GROUP BY channel
ORDER BY payload_bytes DESC
"""

THREAD_OWNERSHIP_SQL = """
WITH thread_counts AS (
    SELECT thread_id, count(*) AS checkpoints
    FROM langgraph.checkpoints
    GROUP BY thread_id
), classified AS (
    SELECT tc.*,
           CASE WHEN mapping.thread_id IS NULL
                THEN 'UNMAPPED'
                ELSE 'APP_MAPPED'
           END AS ownership
    FROM thread_counts AS tc
    LEFT JOIN app.conversation_thread_mapping AS mapping
      ON mapping.thread_id = tc.thread_id
)
SELECT ownership,
       count(*) AS threads,
       sum(checkpoints) AS checkpoints,
       max(checkpoints) AS max_checkpoints_per_thread
FROM classified
GROUP BY ownership
ORDER BY checkpoints DESC
"""


def main() -> None:
    """执行只读聚合，并输出不含客户正文与数据库凭据的 JSON。"""

    settings = ServiceSettings()
    with (
        connect(psycopg_url(settings.database_url), row_factory=dict_row) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(TABLE_SIZE_SQL)
        tables = cursor.fetchall()
        cursor.execute(CHANNEL_SIZE_SQL)
        channels = cursor.fetchall()
        cursor.execute(THREAD_OWNERSHIP_SQL)
        ownership = cursor.fetchall()
    print(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "tables": tables,
                "channels": channels,
                "thread_ownership": ownership,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
