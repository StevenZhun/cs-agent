"""保存可恢复运行产物，并按会话权限、版本和哈希批量回装。"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from shared.contracts.artifact import RuntimeArtifactRef


class ArtifactValidationError(ValueError):
    """产物不存在、越权、过期、版本或内容哈希不一致。"""


@dataclass(frozen=True)
class ArtifactLoadResult:
    """批量回装结果及可观测耗时；payload 不写回长期 State。"""

    payloads: dict[str, Any]
    latency_ms: int
    cache_hits: int
    database_reads: int


class RuntimeArtifactRepository:
    """PostgreSQL 是恢复事实源，同一进程中的有界缓存只减少重复查询。"""

    def __init__(
        self,
        database_dsn: str,
        *,
        retention_days: int = 30,
        cache_size: int = 128,
    ) -> None:
        self._database_dsn = database_dsn
        self._retention_days = retention_days
        self._cache_size = cache_size
        self._cache: OrderedDict[str, tuple[RuntimeArtifactRef, Any]] = OrderedDict()

    async def save(
        self,
        *,
        state: dict[str, object],
        artifact_kind: str,
        logical_id: str,
        payload: Any,
        schema_version: str,
        release_version: str | None = None,
        index_version: str | None = None,
    ) -> RuntimeArtifactRef:
        """幂等保存一份产物；同一 Run 的相同正文得到同一个编号。"""

        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        identity = ":".join((str(state["run_id"]), artifact_kind, logical_id, content_hash))
        created_at = datetime.now(UTC)
        reference = RuntimeArtifactRef(
            artifact_id=f"artifact_{uuid5(NAMESPACE_URL, identity).hex}",
            artifact_kind=artifact_kind,
            conversation_id=str(state["conversation_id"]),
            thread_id=str(state["thread_id"]),
            turn_id=str(state["turn_id"]),
            run_id=str(state["run_id"]),
            schema_version=schema_version,
            content_hash=content_hash,
            release_version=release_version,
            index_version=index_version,
            expires_at=created_at + timedelta(days=self._retention_days),
            created_at=created_at,
        )
        async with await AsyncConnection.connect(self._database_dsn) as connection:
            await connection.execute(
                "INSERT INTO app.runtime_artifact "
                "(artifact_id, artifact_kind, conversation_id, thread_id, turn_id, run_id, "
                "schema_version, payload, content_hash, release_version, index_version, status, "
                "expires_at, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, 'ACTIVE', %s, %s) ON CONFLICT (artifact_id) DO NOTHING",
                (
                    reference.artifact_id,
                    reference.artifact_kind,
                    reference.conversation_id,
                    reference.thread_id,
                    reference.turn_id,
                    reference.run_id,
                    reference.schema_version,
                    Jsonb(payload),
                    reference.content_hash,
                    reference.release_version,
                    reference.index_version,
                    reference.expires_at,
                    reference.created_at,
                ),
            )
        self._remember(reference, payload)
        return reference

    async def load(
        self,
        reference: RuntimeArtifactRef | dict[str, object],
        *,
        authorized_conversation_id: str,
        expected_kind: str,
    ) -> Any:
        """读取单个产物；内部仍复用批量校验路径。"""

        normalized = RuntimeArtifactRef.model_validate(reference)
        result = await self.load_many(
            (normalized,),
            authorized_conversation_id=authorized_conversation_id,
            expected_kinds={normalized.artifact_id: expected_kind},
        )
        return result.payloads[normalized.artifact_id]

    async def load_many(
        self,
        references: tuple[RuntimeArtifactRef, ...],
        *,
        authorized_conversation_id: str,
        expected_kinds: dict[str, str],
    ) -> ArtifactLoadResult:
        """一次读取缺失产物，并对缓存和数据库结果执行相同校验。"""

        started = time.perf_counter()
        payloads: dict[str, Any] = {}
        missing: list[RuntimeArtifactRef] = []
        cache_hits = 0
        for reference in references:
            self._validate_reference(
                reference,
                authorized_conversation_id=authorized_conversation_id,
                expected_kind=expected_kinds[reference.artifact_id],
            )
            cached = self._cache.get(reference.artifact_id)
            if cached is None:
                missing.append(reference)
                continue
            cached_ref, payload = cached
            self._validate_payload(reference, cached_ref, payload)
            self._cache.move_to_end(reference.artifact_id)
            payloads[reference.artifact_id] = payload
            cache_hits += 1

        if missing:
            ids = [reference.artifact_id for reference in missing]
            async with await AsyncConnection.connect(
                self._database_dsn, row_factory=dict_row
            ) as connection:
                cursor = await connection.execute(
                    "SELECT * FROM app.runtime_artifact WHERE artifact_id = ANY(%s)",
                    (ids,),
                )
                rows = {row["artifact_id"]: row for row in await cursor.fetchall()}
            for reference in missing:
                row = rows.get(reference.artifact_id)
                if row is None:
                    raise ArtifactValidationError("runtime artifact not found")
                stored = RuntimeArtifactRef.model_validate(
                    {name: row[name] for name in RuntimeArtifactRef.model_fields}
                )
                payload = row["payload"]
                self._validate_reference(
                    stored,
                    authorized_conversation_id=authorized_conversation_id,
                    expected_kind=expected_kinds[reference.artifact_id],
                )
                self._validate_payload(reference, stored, payload)
                payloads[reference.artifact_id] = payload
                self._remember(stored, payload)
        return ArtifactLoadResult(
            payloads=payloads,
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            cache_hits=cache_hits,
            database_reads=len(missing),
        )

    def _remember(self, reference: RuntimeArtifactRef, payload: Any) -> None:
        self._cache[reference.artifact_id] = (reference, payload)
        self._cache.move_to_end(reference.artifact_id)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    @staticmethod
    def _validate_reference(
        reference: RuntimeArtifactRef,
        *,
        authorized_conversation_id: str,
        expected_kind: str,
    ) -> None:
        if reference.conversation_id != authorized_conversation_id:
            raise ArtifactValidationError("runtime artifact belongs to another conversation")
        if reference.artifact_kind != expected_kind:
            raise ArtifactValidationError("runtime artifact kind mismatch")
        if reference.status != "ACTIVE" or reference.expires_at <= datetime.now(UTC):
            raise ArtifactValidationError("runtime artifact expired")

    @staticmethod
    def _validate_payload(
        requested: RuntimeArtifactRef,
        stored: RuntimeArtifactRef,
        payload: Any,
    ) -> None:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if requested != stored or content_hash != requested.content_hash:
            raise ArtifactValidationError("runtime artifact hash or version mismatch")
