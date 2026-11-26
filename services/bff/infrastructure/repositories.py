"""PostgreSQL implementations of BFF authorization read ports."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.bff.infrastructure.events import append_event
from services.bff.infrastructure.models import (
    ChatHistoryMessageModel,
    ConversationHiddenModel,
    ConversationModel,
    ConversationPinModel,
    ConversationThreadMappingModel,
    GraphRunRecordModel,
    RecommendationRecordModel,
    SubjectCustomerBindingModel,
    SubjectModel,
    SubjectRoleModel,
    TurnRecordModel,
)
from services.bff.security import ObjectAuthorizationPort, Role, StoredIdentity
from shared.contracts.conversation import (
    ConversationSummary,
    CreateConversationResponse,
    RealtimeEventType,
)
from shared.contracts.enums import ConversationStatus


def default_conversation_title(first_customer_message: str | None) -> str:
    """用首条客户消息生成列表标题，空会话显示“新咨询”。"""

    if not first_customer_message:
        return "新咨询"
    # 标题只用于列表识别，因此先把换行和连续空格压成一个空格。
    compact = " ".join(first_customer_message.split())
    return compact if len(compact) <= 8 else f"{compact[:8]}..."


class SqlAlchemyIdentityRepository:
    """Load local application identities and their current roles."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def find_by_username(self, username: str) -> StoredIdentity | None:
        async with self._session_factory() as session:
            subject = await session.scalar(
                select(SubjectModel).where(SubjectModel.username == username)
            )
            if subject is None:
                return None
            role_codes = tuple(
                (
                    await session.scalars(
                        select(SubjectRoleModel.role_code).where(
                            SubjectRoleModel.subject_id == subject.subject_id
                        )
                    )
                ).all()
            )
            return StoredIdentity(
                subject_id=subject.subject_id,
                password_hash=subject.password_hash,
                status=subject.status,
                roles=frozenset(Role(code) for code in role_codes),
            )


class SqlAlchemyObjectAuthorizer(ObjectAuthorizationPort):
    """Resolve current subject-to-customer bindings from the app schema."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def can_access_customer(self, subject_id: str, customer_id: str) -> bool:
        now = datetime.now(UTC)
        statement = select(SubjectCustomerBindingModel.binding_id).where(
            and_(
                SubjectCustomerBindingModel.subject_id == subject_id,
                SubjectCustomerBindingModel.customer_id == customer_id,
                SubjectCustomerBindingModel.active.is_(True),
                or_(
                    SubjectCustomerBindingModel.expires_at.is_(None),
                    SubjectCustomerBindingModel.expires_at > now,
                ),
            )
        )
        async with self._session_factory() as session:
            return (await session.scalar(statement)) is not None

    async def accessible_customer_ids(self, subject_id: str) -> tuple[str, ...]:
        """Return only active, unexpired bindings for a customer picker."""

        now = datetime.now(UTC)
        statement = (
            select(SubjectCustomerBindingModel.customer_id)
            .where(
                SubjectCustomerBindingModel.subject_id == subject_id,
                SubjectCustomerBindingModel.active.is_(True),
                or_(
                    SubjectCustomerBindingModel.expires_at.is_(None),
                    SubjectCustomerBindingModel.expires_at > now,
                ),
            )
            .distinct()
            .order_by(SubjectCustomerBindingModel.customer_id)
        )
        async with self._session_factory() as session:
            return tuple((await session.scalars(statement)).all())

    async def can_access_conversation(self, subject_id: str, conversation_id: str) -> bool:
        now = datetime.now(UTC)
        statement = (
            select(ConversationModel.conversation_id)
            .join(
                SubjectCustomerBindingModel,
                and_(
                    SubjectCustomerBindingModel.customer_id == ConversationModel.customer_id,
                    SubjectCustomerBindingModel.subject_id == subject_id,
                    SubjectCustomerBindingModel.active.is_(True),
                    or_(
                        SubjectCustomerBindingModel.expires_at.is_(None),
                        SubjectCustomerBindingModel.expires_at > now,
                    ),
                ),
            )
            .where(ConversationModel.conversation_id == conversation_id)
        )
        async with self._session_factory() as session:
            return (await session.scalar(statement)) is not None


class SqlAlchemyConversationRepository:
    """Persist the business conversation and internal thread mapping atomically."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        session: AsyncSession,
        *,
        conversation_id: str,
        thread_id: str,
        customer_id: str,
        created_by_subject_id: str,
        assigned_agent_subject_id: str | None,
        channel: str,
    ) -> CreateConversationResponse:
        now = datetime.now(UTC)
        conversation = ConversationModel(
            conversation_id=conversation_id,
            customer_id=customer_id,
            created_by_subject_id=created_by_subject_id,
            assigned_agent_subject_id=assigned_agent_subject_id,
            channel=channel,
            status=ConversationStatus.ACTIVE.value,
            created_at=now,
            updated_at=now,
        )
        mapping = ConversationThreadMappingModel(
            conversation_id=conversation_id,
            thread_id=thread_id,
            created_at=now,
            updated_at=now,
        )
        session.add_all((conversation, mapping))
        await session.flush()
        return CreateConversationResponse(
            conversation_id=conversation.conversation_id,
            status=ConversationStatus(conversation.status),
            created_at=conversation.created_at,
        )

    async def get(self, conversation_id: str) -> CreateConversationResponse | None:
        async with self._session_factory() as session:
            conversation = await session.get(ConversationModel, conversation_id)
            if conversation is None:
                return None
            return CreateConversationResponse(
                conversation_id=conversation.conversation_id,
                status=ConversationStatus(conversation.status),
                created_at=conversation.created_at,
            )

    async def list_for_customer(
        self, subject_id: str, customer_id: str | tuple[str, ...], *, limit: int | None = 50
    ) -> tuple[ConversationSummary, ...]:
        """按置顶和最后消息时间返回咨询，结束/改名不改变消息排序；支持批量统计客户。"""

        first_customer_message = (
            select(ChatHistoryMessageModel.content)
            .where(
                ChatHistoryMessageModel.conversation_id == ConversationModel.conversation_id,
                ChatHistoryMessageModel.role == "CUSTOMER",
            )
            .order_by(
                ChatHistoryMessageModel.created_at,
                ChatHistoryMessageModel.message_id,
            )
            .limit(1)
            .correlate(ConversationModel)
            .scalar_subquery()
        )
        last_message_at = (
            select(func.max(ChatHistoryMessageModel.created_at))
            .where(
                ChatHistoryMessageModel.conversation_id == ConversationModel.conversation_id,
                ChatHistoryMessageModel.role.in_(["CUSTOMER", "ASSISTANT", "AGENT"]),
            )
            .correlate(ConversationModel)
            .scalar_subquery()
        )
        # 一次列表查询带回各会话最近已完成轮次的进度，页面不必逐条请求 Runtime。
        # 不能只找非空快照：新轮次已退出 SOP 时，应清除旧的“待核对”标记。
        latest_sop_snapshot = (
            select(GraphRunRecordModel.sop_snapshot)
            .join(TurnRecordModel, TurnRecordModel.turn_id == GraphRunRecordModel.turn_id)
            .where(
                TurnRecordModel.conversation_id == ConversationModel.conversation_id,
                GraphRunRecordModel.status.in_(["COMPLETED", "INTERRUPTED"]),
            )
            .order_by(
                TurnRecordModel.started_at.desc(),
                GraphRunRecordModel.finished_at.desc(),
                GraphRunRecordModel.run_id.desc(),
            )
            .limit(1)
            .correlate(ConversationModel)
            .scalar_subquery()
        )
        pending_rag_recommendation = (
            select(RecommendationRecordModel.recommendation_id)
            .where(
                RecommendationRecordModel.conversation_id == ConversationModel.conversation_id,
                RecommendationRecordModel.status == "DRAFT",
            )
            .limit(1)
            .correlate(ConversationModel)
            .scalar_subquery()
        )
        latest_routing_decision = (
            select(TurnRecordModel.routing_decision)
            .where(
                TurnRecordModel.conversation_id == ConversationModel.conversation_id,
                TurnRecordModel.status.in_(["COMPLETED", "INTERRUPTED"]),
            )
            .order_by(TurnRecordModel.started_at.desc(), TurnRecordModel.turn_id.desc())
            .limit(1)
            .correlate(ConversationModel)
            .scalar_subquery()
        )
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        ConversationModel,
                        first_customer_message.label("first_customer_message"),
                        ConversationPinModel.pinned_at,
                        latest_sop_snapshot.label("latest_sop_snapshot"),
                        pending_rag_recommendation.label("pending_rag_recommendation"),
                        latest_routing_decision.label("latest_routing_decision"),
                        last_message_at.label("last_message_at"),
                    )
                    .outerjoin(
                        ConversationPinModel,
                        and_(
                            ConversationPinModel.conversation_id
                            == ConversationModel.conversation_id,
                            ConversationPinModel.subject_id == subject_id,
                        ),
                    )
                    .outerjoin(
                        ConversationHiddenModel,
                        and_(
                            ConversationHiddenModel.conversation_id
                            == ConversationModel.conversation_id,
                            ConversationHiddenModel.subject_id == subject_id,
                        ),
                    )
                    .where(
                        ConversationModel.customer_id.in_(customer_id)
                        if isinstance(customer_id, tuple)
                        else ConversationModel.customer_id == customer_id,
                        ConversationHiddenModel.conversation_id.is_(None),
                    )
                    .order_by(
                        ConversationPinModel.pinned_at.desc().nulls_last(),
                        func.coalesce(last_message_at, ConversationModel.created_at).desc(),
                        ConversationModel.conversation_id.desc(),
                    )
                    .limit(limit)
                )
            ).all()
        summaries = []
        for row in rows:
            # 查询实现可能只返回基础会话字段，也可能同时返回 RAG 与人工转接状态。
            if len(row) == 5:
                conversation, first_message, pinned_at, snapshot, last_message = row
                rag_pending = None
                routing = None
            elif len(row) == 6:
                conversation, first_message, pinned_at, snapshot, rag_pending, last_message = row
                routing = None
            else:
                (
                    conversation,
                    first_message,
                    pinned_at,
                    snapshot,
                    rag_pending,
                    routing,
                    last_message,
                ) = row
            # 与流程处理面板保持同一判断条件，换题暂停不冒充人工核对。
            progress = (snapshot or {}).get("sop_progress") or {}
            needs_sop_review = (
                conversation.status == ConversationStatus.ACTIVE.value
                and progress.get("status") in {"SUSPENDED", "WAITING_APPROVAL"}
                and (
                    progress.get("current_step_id") == "manual_review"
                    or (snapshot or {}).get("reason_code") == "SOP_HUMAN_REQUESTED"
                )
            )
            needs_handoff = (
                conversation.status == ConversationStatus.ACTIVE.value
                and (routing or {}).get("response_path") == "HUMAN"
            )
            needs_manual_review = needs_sop_review or needs_handoff or (
                conversation.status == ConversationStatus.ACTIVE.value and rag_pending is not None
            )
            summaries.append(
                ConversationSummary(
                    conversation_id=conversation.conversation_id,
                    title=conversation.title or default_conversation_title(first_message),
                    is_pinned=pinned_at is not None,
                    customer_id=conversation.customer_id,
                    channel=conversation.channel,
                    status=ConversationStatus(conversation.status),
                    needs_manual_review=needs_manual_review,
                    created_at=conversation.created_at,
                    updated_at=conversation.updated_at,
                    last_message_at=last_message,
                )
            )
        return tuple(summaries)

    async def rename_title(self, conversation_id: str, title: str) -> ConversationSummary | None:
        """保存用户输入的标题，并返回更新后的最近咨询条目。"""

        async with self._session_factory.begin() as session:
            conversation = await session.get(ConversationModel, conversation_id)
            if conversation is None:
                return None
            # 不修改 updated_at，避免单纯改名把旧咨询移动到最近列表顶部。
            conversation_key = conversation.conversation_id
            customer_id = conversation.customer_id
            channel = conversation.channel
            status = ConversationStatus(conversation.status)
            created_at = conversation.created_at
            updated_at = conversation.updated_at
            await session.execute(
                update(ConversationModel)
                .where(ConversationModel.conversation_id == conversation_id)
                .values(title=title)
            )
            await append_event(
                session,
                conversation_id=conversation_id,
                turn_id=None,
                event_type=RealtimeEventType.CONVERSATION_UPDATED,
                payload={"change": "TITLE_UPDATED"},
            )
        # 事务提交后 ORM 实例可能被过期，先把时间字段复制成普通值再返回。
        return ConversationSummary(
            conversation_id=conversation_key,
            title=title,
            customer_id=customer_id,
            channel=channel,
            status=status,
            created_at=created_at,
            updated_at=updated_at,
        )

    async def set_pinned(
        self,
        subject_id: str,
        conversation_id: str,
        pinned: bool,
    ) -> ConversationSummary | None:
        """只修改当前主体的个人置顶关系，不改变会话最近交互时间。"""

        async with self._session_factory.begin() as session:
            conversation = await session.get(ConversationModel, conversation_id)
            if conversation is None:
                return None
            key = (subject_id, conversation_id)
            existing = await session.get(ConversationPinModel, key)
            if pinned:
                if existing is None:
                    session.add(
                        ConversationPinModel(
                            subject_id=subject_id,
                            conversation_id=conversation_id,
                            pinned_at=datetime.now(UTC),
                        )
                    )
                else:
                    existing.pinned_at = datetime.now(UTC)
            elif existing is not None:
                await session.delete(existing)
            customer_id = conversation.customer_id

        # 复用列表查询生成默认标题和最终排序字段，避免维护第二套 DTO 组装逻辑。
        summaries = await self.list_for_customer(subject_id, customer_id)
        return next(
            (item for item in summaries if item.conversation_id == conversation_id),
            None,
        )
