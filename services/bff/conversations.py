"""BFF application service for authorized, idempotent conversation creation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from services.bff.errors import AppError
from services.bff.infrastructure.events import append_event
from services.bff.infrastructure.idempotency import PostgresIdempotencyExecutor
from services.bff.infrastructure.models import (
    ChatHistoryMessageModel,
    ConversationModel,
    TurnRecordModel,
)
from services.bff.security import (
    AuthPrincipal,
    ObjectAuthorizationPort,
    Role,
    authorize_customer,
    authorize_roles,
)
from shared.contracts.conversation import (
    ConversationSummary,
    CreateConversationRequest,
    CreateConversationResponse,
    RealtimeEventType,
)
from shared.contracts.enums import ConversationStatus


class ConversationRepositoryPort(Protocol):
    """会话服务需要的最小仓储方法约定。"""

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
    ) -> CreateConversationResponse: ...

    async def get(self, conversation_id: str) -> CreateConversationResponse | None: ...

    async def list_for_customer(
        self, subject_id: str, customer_id: str, *, limit: int | None = 50
    ) -> tuple[ConversationSummary, ...]:
        """返回某个客户最近更新的咨询列表；``None`` 表示不截断。"""

        ...

    async def set_pinned(
        self,
        subject_id: str,
        conversation_id: str,
        pinned: bool,
    ) -> ConversationSummary | None:
        """保存当前主体自己的会话置顶状态。"""

        ...

    async def rename_title(self, conversation_id: str, title: str) -> ConversationSummary | None:
        """保存自定义会话标题。"""

        ...


class ConversationService:
    """在 BFF 边界内完成身份、对象权限和会话写操作幂等处理。"""

    def __init__(
        self,
        repository: ConversationRepositoryPort,
        authorizer: ObjectAuthorizationPort,
        idempotency: PostgresIdempotencyExecutor,
    ) -> None:
        self._repository = repository
        self._authorizer = authorizer
        self._idempotency = idempotency

    async def create(
        self,
        principal: AuthPrincipal,
        request: CreateConversationRequest,
        idempotency_key: str,
    ) -> tuple[CreateConversationResponse, bool]:
        await authorize_customer(principal, request.customer_id, self._authorizer)
        assigned_agent = (
            principal.subject_id if principal.roles.intersection({Role.AGENT, Role.ADMIN}) else None
        )

        async def mutation(session: AsyncSession) -> dict[str, object]:
            """在幂等执行器的事务中创建会话及其线程映射。"""

            created = await self._repository.create(
                session,
                conversation_id=f"conv_{uuid4().hex}",
                thread_id=f"thread_{uuid4().hex}",
                customer_id=request.customer_id,
                created_by_subject_id=principal.subject_id,
                assigned_agent_subject_id=assigned_agent,
                channel=request.channel,
            )
            return created.model_dump(mode="json")

        result = await self._idempotency.execute(
            subject_id=principal.subject_id,
            operation="CREATE_CONVERSATION",
            idempotency_key=idempotency_key,
            request_payload=request.model_dump(mode="json"),
            response_status=201,
            mutation=mutation,
        )
        return CreateConversationResponse.model_validate(result.response_body), result.replayed

    async def get(
        self,
        principal: AuthPrincipal,
        conversation_id: str,
    ) -> CreateConversationResponse:
        if not await self._authorizer.can_access_conversation(
            principal.subject_id, conversation_id
        ):
            raise AppError(
                code="OBJECT_ACCESS_DENIED",
                message="无权访问该会话对象",
                status_code=403,
            )
        conversation = await self._repository.get(conversation_id)
        if conversation is None:
            raise AppError(
                code="CONVERSATION_NOT_FOUND",
                message="会话不存在",
                status_code=404,
            )
        return conversation

    async def list_for_customer(
        self, principal: AuthPrincipal, customer_id: str
    ) -> tuple[ConversationSummary, ...]:
        """先核对客户对象权限，再返回可用的咨询列表。

        客户页和坐席页都需要回看当前主体有权访问的完整会话历史。坐席客户
        徽标统计的是全部未结束会话，因此不能让同一个列表接口再截断历史，
        否则客户和坐席都会看到不完整的会话集合。大规模生产场景应在此处
        增加服务端分页或“未结束会话 + 最近已结束会话”的专用查询。
        """

        await authorize_customer(principal, customer_id, self._authorizer)
        return await self._repository.list_for_customer(
            principal.subject_id, customer_id, limit=None
        )

    async def rename_title(
        self,
        principal: AuthPrincipal,
        conversation_id: str,
        title: str,
    ) -> ConversationSummary:
        """核对会话访问权限后保存用户输入的新标题。"""

        if not await self._authorizer.can_access_conversation(
            principal.subject_id, conversation_id
        ):
            raise AppError(
                code="OBJECT_ACCESS_DENIED",
                message="无权访问该会话对象",
                status_code=403,
            )
        renamed = await self._repository.rename_title(conversation_id, title)
        if renamed is None:
            raise AppError(
                code="CONVERSATION_NOT_FOUND",
                message="会话不存在",
                status_code=404,
            )
        return renamed

    async def set_pinned(
        self,
        principal: AuthPrincipal,
        conversation_id: str,
        pinned: bool,
    ) -> ConversationSummary:
        """核对会话权限后，只更新当前登录主体自己的置顶状态。"""

        if not await self._authorizer.can_access_conversation(
            principal.subject_id, conversation_id
        ):
            raise AppError(
                code="OBJECT_ACCESS_DENIED",
                message="无权访问该会话对象",
                status_code=403,
            )
        summary = await self._repository.set_pinned(
            principal.subject_id,
            conversation_id,
            pinned,
        )
        if summary is None:
            raise AppError(
                code="CONVERSATION_NOT_FOUND",
                message="会话不存在",
                status_code=404,
            )
        return summary

    async def request_finish(
        self,
        principal: AuthPrincipal,
        conversation_id: str,
        *,
        idempotency_key: str,
    ) -> tuple[CreateConversationResponse, bool]:
        """由客户结束自己的当前咨询，并阻止后续继续发送消息。

        有历史消息的会话先进入 WRAP_UP，留给坐席生成和确认小结；没有
        任何客户消息的空会话不需要小结，可以直接变为 FINISHED。
        """

        authorize_roles(principal, {Role.CUSTOMER})
        if not await self._authorizer.can_access_conversation(
            principal.subject_id, conversation_id
        ):
            raise AppError(
                code="OBJECT_ACCESS_DENIED", message="无权访问该会话对象", status_code=403
            )

        async def mutation(session: AsyncSession) -> dict[str, object]:
            """锁定会话后检查处理中轮次，再在同一事务内更新状态和事件。"""

            conversation = await session.scalar(
                select(ConversationModel)
                .where(ConversationModel.conversation_id == conversation_id)
                .with_for_update()
            )
            if conversation is None:
                raise AppError(code="CONVERSATION_NOT_FOUND", message="会话不存在", status_code=404)
            if conversation.status in {"WRAP_UP", "FINISHED"}:
                return CreateConversationResponse(
                    conversation_id=conversation.conversation_id,
                    status=ConversationStatus(conversation.status),
                    created_at=conversation.created_at,
                ).model_dump(mode="json")
            running_turn = await session.scalar(
                select(TurnRecordModel.status)
                .where(
                    TurnRecordModel.conversation_id == conversation_id,
                )
                .order_by(TurnRecordModel.started_at.desc())
                .limit(1)
            )
            if running_turn == "INTERRUPTED":
                raise AppError(
                    code="SOP_WAITING_REVIEW",
                    message="当前办理等待人工处理，请先取消或由坐席处理后再结束咨询",
                    status_code=409,
                )
            if running_turn == "RUNNING":
                raise AppError(
                    code="TURN_IN_PROGRESS",
                    message="请等待当前消息处理完成后再结束咨询",
                    status_code=409,
                    retryable=True,
                )
            customer_message_count = await session.scalar(
                select(func.count())
                .select_from(ChatHistoryMessageModel)
                .where(
                    ChatHistoryMessageModel.conversation_id == conversation_id,
                    ChatHistoryMessageModel.role == "CUSTOMER",
                )
            )
            conversation.status = "WRAP_UP" if customer_message_count else "FINISHED"
            conversation.updated_at = datetime.now(UTC)
            await append_event(
                session,
                conversation_id=conversation_id,
                turn_id=None,
                event_type=RealtimeEventType.CONVERSATION_UPDATED,
                payload={
                    "status": conversation.status,
                    "closed_by": "CUSTOMER",
                },
            )
            return CreateConversationResponse(
                conversation_id=conversation.conversation_id,
                status=ConversationStatus(conversation.status),
                created_at=conversation.created_at,
            ).model_dump(mode="json")

        result = await self._idempotency.execute(
            subject_id=principal.subject_id,
            operation=f"REQUEST_FINISH_CONVERSATION:{conversation_id}",
            idempotency_key=idempotency_key,
            request_payload={"conversation_id": conversation_id},
            response_status=200,
            mutation=mutation,
        )
        return CreateConversationResponse.model_validate(result.response_body), result.replayed
