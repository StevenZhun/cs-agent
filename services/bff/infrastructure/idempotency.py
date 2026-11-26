"""BFF 写操作使用的 PostgreSQL 事务和幂等执行器。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.bff.errors import AppError
from services.bff.infrastructure.models import IdempotencyRecordModel

Mutation = Callable[[AsyncSession], Awaitable[dict[str, object]]]


@dataclass(frozen=True)
class IdempotencyResult:
    """已经保存的操作响应，以及本次是否只是重放原结果。"""

    response_status: int
    response_body: dict[str, object]
    replayed: bool


def canonical_request_hash(payload: dict[str, object]) -> str:
    """只按稳定的业务字段计算哈希，不受 HTTP 字段顺序影响。"""

    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


class PostgresIdempotencyExecutor:
    """在同一事务中保存写操作和可重放的响应。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        retention: timedelta = timedelta(hours=24),
    ) -> None:
        self._session_factory = session_factory
        self._retention = retention

    async def execute(
        self,
        *,
        subject_id: str,
        operation: str,
        idempotency_key: str,
        request_payload: dict[str, object],
        response_status: int,
        mutation: Mutation,
    ) -> IdempotencyResult:
        """首次请求执行写操作；重复请求返回已保存结果或明确提示处理中。"""

        request_hash = canonical_request_hash(request_payload)
        now = datetime.now(UTC)
        primary_key = {
            "subject_id": subject_id,
            "operation": operation,
            "idempotency_key": idempotency_key,
        }

        async with self._session_factory() as session, session.begin():
            insert_statement = (
                insert(IdempotencyRecordModel)
                .values(
                    **primary_key,
                    request_hash=request_hash,
                    status="PROCESSING",
                    expires_at=now + self._retention,
                )
                .on_conflict_do_nothing(
                    index_elements=("subject_id", "operation", "idempotency_key")
                )
                .returning(IdempotencyRecordModel.idempotency_key)
            )
            inserted_key = await session.scalar(insert_statement)
            if inserted_key is None:
                existing = (
                    await session.execute(
                        select(IdempotencyRecordModel).where(
                            IdempotencyRecordModel.subject_id == subject_id,
                            IdempotencyRecordModel.operation == operation,
                            IdempotencyRecordModel.idempotency_key == idempotency_key,
                        )
                    )
                ).scalar_one()
                if existing.request_hash != request_hash:
                    raise AppError(
                        code="IDEMPOTENCY_KEY_REUSED",
                        message="同一幂等键不能用于不同请求",
                        status_code=409,
                    )
                if (
                    existing.status == "COMPLETED"
                    and existing.response_status is not None
                    and existing.response_body is not None
                ):
                    return IdempotencyResult(
                        response_status=existing.response_status,
                        response_body=existing.response_body,
                        replayed=True,
                    )
                raise AppError(
                    code="IDEMPOTENCY_IN_PROGRESS",
                    message="相同请求正在处理，请稍后重试",
                    status_code=409,
                    retryable=True,
                )

            response_body = await mutation(session)
            await session.execute(
                update(IdempotencyRecordModel)
                .where(
                    IdempotencyRecordModel.subject_id == subject_id,
                    IdempotencyRecordModel.operation == operation,
                    IdempotencyRecordModel.idempotency_key == idempotency_key,
                )
                .values(
                    status="COMPLETED",
                    response_status=response_status,
                    response_body=response_body,
                    locked_until=None,
                    updated_at=now,
                )
            )
            return IdempotencyResult(
                response_status=response_status,
                response_body=response_body,
                replayed=False,
            )
