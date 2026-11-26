"""会话事件只通知“哪里变了”；消息与会话表仍是事实来源。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.bff.infrastructure.models import (
    ConversationEventModel,
    ConversationModel,
    SubjectCustomerBindingModel,
)
from shared.contracts.conversation import RealtimeEventEnvelope, RealtimeEventType


async def latest_event_sequence(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """读取当前事件表的最大序号，供坐席首次建立监听时跳过旧历史。"""

    async with session_factory() as session:
        value = await session.scalar(select(func.max(ConversationEventModel.sequence)))
    return int(value or 0)


async def append_event(
    session: AsyncSession,
    *,
    conversation_id: str,
    turn_id: str | None,
    event_type: RealtimeEventType,
    payload: dict[str, object],
) -> None:
    """在调用者原有事务中追加通知，不能先发通知再提交业务结果。"""

    session.add(
        ConversationEventModel(
            conversation_id=conversation_id,
            turn_id=turn_id,
            event_type=event_type.value,
            payload=payload,
            occurred_at=datetime.now(UTC),
        )
    )


async def event_page(
    session_factory: async_sessionmaker[AsyncSession],
    conversation_id: str,
    after_sequence: int,
    *,
    limit: int = 100,
) -> tuple[RealtimeEventEnvelope, ...]:
    """按数据库序号补发单个会话的事件；全局序号在本会话允许有空号。"""

    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(ConversationEventModel)
                .where(
                    ConversationEventModel.conversation_id == conversation_id,
                    ConversationEventModel.sequence > after_sequence,
                )
                .order_by(ConversationEventModel.sequence)
                .limit(limit)
            )
        ).all()
    return tuple(
        RealtimeEventEnvelope(
            event_id=f"evt_{row.sequence}",
            sequence=row.sequence,
            conversation_id=row.conversation_id,
            turn_id=row.turn_id,
            event_type=RealtimeEventType(row.event_type),
            occurred_at=row.occurred_at,
            payload=row.payload,
        )
        for row in rows
    )


async def agent_event_page(
    session_factory: async_sessionmaker[AsyncSession],
    subject_id: str,
    after_sequence: int,
    *,
    limit: int = 100,
) -> tuple[RealtimeEventEnvelope, ...]:
    """读取坐席有权查看的全部客户事件，并把客户编号放入事件提示。"""

    now = datetime.now(UTC)
    customer_ids = select(SubjectCustomerBindingModel.customer_id).where(
        SubjectCustomerBindingModel.subject_id == subject_id,
        SubjectCustomerBindingModel.active.is_(True),
        or_(
            SubjectCustomerBindingModel.expires_at.is_(None),
            SubjectCustomerBindingModel.expires_at > now,
        ),
    )
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(ConversationEventModel, ConversationModel.customer_id)
                .join(
                    ConversationModel,
                    ConversationModel.conversation_id == ConversationEventModel.conversation_id,
                )
                .where(
                    ConversationModel.customer_id.in_(customer_ids),
                    ConversationEventModel.sequence > after_sequence,
                )
                .order_by(ConversationEventModel.sequence)
                .limit(limit)
            )
        ).all()
    return tuple(
        RealtimeEventEnvelope(
            event_id=f"evt_{row.sequence}",
            sequence=row.sequence,
            conversation_id=row.conversation_id,
            turn_id=row.turn_id,
            event_type=RealtimeEventType(row.event_type),
            occurred_at=row.occurred_at,
            payload={**row.payload, "customer_id": customer_id},
        )
        for row, customer_id in rows
    )
