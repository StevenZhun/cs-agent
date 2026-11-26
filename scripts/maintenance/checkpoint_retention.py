"""以计划、审批、执行三步清理明确列出的 LangGraph 线程。

本脚本只删除官方 checkpoint 保存器拥有的线程状态，不删除 ``app.chat_history``、
Turn/Run、模型调用、审核记录或业务事实。执行前后都重新核对业务会话已经结束，且
不存在运行中或等待恢复的 Turn/Run。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from services.ai_runtime.persistence.checkpoint import psycopg_url
from shared.web.settings import ServiceSettings


def canonical_manifest(manifest: dict[str, Any]) -> str:
    """生成与输入顺序无关的清单正文，用于审批后防篡改核对。"""

    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def manifest_hash(manifest: dict[str, Any]) -> str:
    """返回审批和执行共同核对的 SHA256。"""

    return hashlib.sha256(canonical_manifest(manifest).encode("utf-8")).hexdigest()


def normalize_thread_ids(thread_ids: list[str]) -> tuple[str, ...]:
    """拒绝空清单、重复项和通配符，只保留精确线程编号。"""

    normalized = tuple(sorted({item.strip() for item in thread_ids if item.strip()}))
    if not normalized:
        raise ValueError("at least one exact thread_id is required")
    if len(normalized) != len(thread_ids):
        raise ValueError("thread_id list must not contain blanks or duplicates")
    if any(any(token in item for token in ("*", "%", "?")) for item in normalized):
        raise ValueError("thread_id wildcards are forbidden")
    return normalized


async def _validate_threads(
    connection: AsyncConnection,
    thread_ids: tuple[str, ...],
    *,
    lock: bool,
) -> list[dict[str, Any]]:
    """证明每个线程属于已结束会话，且没有可恢复中的工作。"""

    suffix = " FOR UPDATE OF mapping, conversation" if lock else ""
    cursor = await connection.execute(
        "SELECT mapping.thread_id, mapping.conversation_id, conversation.status "
        "FROM app.conversation_thread_mapping AS mapping "
        "JOIN app.conversation AS conversation "
        "ON conversation.conversation_id = mapping.conversation_id "
        "WHERE mapping.thread_id = ANY(%s) ORDER BY mapping.thread_id" + suffix,
        (list(thread_ids),),
    )
    rows = [dict(row) for row in await cursor.fetchall()]
    if tuple(row["thread_id"] for row in rows) != thread_ids:
        raise ValueError("every thread must have an application conversation mapping")
    if any(row["status"] != "FINISHED" for row in rows):
        raise ValueError("all conversations must be FINISHED before checkpoint deletion")
    cursor = await connection.execute(
        "SELECT DISTINCT thread_id FROM app.turn_record "
        "WHERE thread_id = ANY(%s) AND status IN ('RUNNING', 'INTERRUPTED') "
        "UNION SELECT DISTINCT thread_id FROM app.graph_run_record "
        "WHERE thread_id = ANY(%s) AND status IN ('RUNNING', 'INTERRUPTED')",
        (list(thread_ids), list(thread_ids)),
    )
    blocked = sorted(str(row[0]) for row in await cursor.fetchall())
    if blocked:
        raise ValueError("threads still contain running or interrupted work: " + ",".join(blocked))
    return rows


async def plan(
    database_dsn: str,
    *,
    thread_ids: tuple[str, ...],
    reason: str,
    requested_by: str,
) -> str:
    """生成不可执行的候选清单；审批前不会修改 checkpoint。"""

    if not reason.strip() or not requested_by.strip():
        raise ValueError("reason and requested_by are required")
    async with await AsyncConnection.connect(database_dsn, row_factory=dict_row) as connection:
        rows = await _validate_threads(connection, thread_ids, lock=False)
        checkpoint_cursor = await connection.execute(
            "SELECT thread_id, count(*) AS checkpoint_count "
            "FROM langgraph.checkpoints WHERE thread_id = ANY(%s) "
            "GROUP BY thread_id ORDER BY thread_id",
            (list(thread_ids),),
        )
        counts = {
            str(row["thread_id"]): int(row["checkpoint_count"])
            for row in await checkpoint_cursor.fetchall()
        }
        manifest = {
            "scope": "MAPPED_FINISHED_CONVERSATION",
            "threads": [
                {
                    "thread_id": row["thread_id"],
                    "conversation_id": row["conversation_id"],
                    "checkpoint_count": counts.get(str(row["thread_id"]), 0),
                }
                for row in rows
            ],
        }
        request_id = f"retention_{uuid4().hex}"
        await connection.execute(
            "INSERT INTO app.retention_request "
            "(request_id, resource_kind, status, manifest, manifest_hash, reason, "
            "requested_by, requested_at) VALUES (%s, 'LANGGRAPH_THREAD', 'PLANNED', "
            "%s, %s, %s, %s, %s)",
            (
                request_id,
                Jsonb(manifest),
                manifest_hash(manifest),
                reason.strip(),
                requested_by.strip(),
                datetime.now(UTC),
            ),
        )
    return request_id


async def approve(database_dsn: str, *, request_id: str, approved_by: str) -> None:
    """批准一份哈希仍与保存正文一致的计划；审批本身不执行删除。"""

    if not approved_by.strip():
        raise ValueError("approved_by is required")
    async with await AsyncConnection.connect(database_dsn, row_factory=dict_row) as connection:
        cursor = await connection.execute(
            "SELECT status, manifest, manifest_hash FROM app.retention_request "
            "WHERE request_id = %s FOR UPDATE",
            (request_id,),
        )
        row = await cursor.fetchone()
        if row is None or row["status"] != "PLANNED":
            raise ValueError("retention request is not awaiting approval")
        if manifest_hash(row["manifest"]) != row["manifest_hash"]:
            raise ValueError("retention manifest hash mismatch")
        await connection.execute(
            "UPDATE app.retention_request SET status = 'APPROVED', approved_by = %s, "
            "approved_at = %s WHERE request_id = %s AND status = 'PLANNED'",
            (approved_by.strip(), datetime.now(UTC), request_id),
        )


async def execute(database_dsn: str, *, request_id: str) -> None:
    """再次核对清单和业务状态，再用官方 Saver 精确删除每个线程。"""

    async with await AsyncConnection.connect(database_dsn, row_factory=dict_row) as connection:
        cursor = await connection.execute(
            "SELECT status, manifest, manifest_hash FROM app.retention_request "
            "WHERE request_id = %s FOR UPDATE",
            (request_id,),
        )
        row = await cursor.fetchone()
        if row is None or row["status"] != "APPROVED":
            raise ValueError("retention request is not approved")
        manifest = row["manifest"]
        if manifest_hash(manifest) != row["manifest_hash"]:
            raise ValueError("retention manifest hash mismatch")
        thread_ids = tuple(item["thread_id"] for item in manifest["threads"])
        await _validate_threads(connection, thread_ids, lock=True)
        serializer = JsonPlusSerializer(allowed_msgpack_modules=[])
        async with AsyncPostgresSaver.from_conn_string(database_dsn, serde=serializer) as saver:
            await saver.conn.execute("SET search_path TO langgraph")
            for thread_id in thread_ids:
                await saver.adelete_thread(thread_id)
        await connection.execute(
            "UPDATE app.retention_request SET status = 'EXECUTED', executed_at = %s "
            "WHERE request_id = %s AND status = 'APPROVED'",
            (datetime.now(UTC), request_id),
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("--thread-id", action="append", required=True)
    plan_parser.add_argument("--reason", required=True)
    plan_parser.add_argument("--requested-by", required=True)
    approve_parser = commands.add_parser("approve")
    approve_parser.add_argument("--request-id", required=True)
    approve_parser.add_argument("--approved-by", required=True)
    execute_parser = commands.add_parser("execute")
    execute_parser.add_argument("--request-id", required=True)
    return parser


async def _run(arguments: argparse.Namespace) -> None:
    settings = ServiceSettings()
    database_dsn = psycopg_url(settings.database_url)
    if arguments.command == "plan":
        request_id = await plan(
            database_dsn,
            thread_ids=normalize_thread_ids(arguments.thread_id),
            reason=arguments.reason,
            requested_by=arguments.requested_by,
        )
        print(request_id)
    elif arguments.command == "approve":
        await approve(
            database_dsn,
            request_id=arguments.request_id,
            approved_by=arguments.approved_by,
        )
    else:
        await execute(database_dsn, request_id=arguments.request_id)


def main() -> None:
    """解析一次人工操作；执行动作必须明确写出 request_id。"""

    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()
