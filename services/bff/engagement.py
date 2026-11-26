"""坐席对推荐的交互反馈，以及可审计的会话结束小结。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.bff.errors import AppError
from services.bff.infrastructure.events import append_event
from services.bff.infrastructure.idempotency import PostgresIdempotencyExecutor
from services.bff.infrastructure.models import (
    ChatHistoryMessageModel,
    ConversationModel,
    ConversationWrapUpAuditModel,
    ConversationWrapUpModel,
    RecommendationFeedbackModel,
    RecommendationInteractionModel,
    RecommendationRecordModel,
    TurnRecordModel,
)
from services.bff.security import AuthPrincipal, Role, authorize_roles
from services.quality.governance import (
    CandidateObservation,
    observe_candidate,
    stable_hash,
    text_hash,
)
from shared.contracts.common import ContractModel
from shared.contracts.conversation import RealtimeEventType
from shared.contracts.enums import (
    AdoptionStatus,
    Helpfulness,
    InteractionAction,
    SampleCandidateSourceType,
    SampleCandidateType,
    WrapUpStatus,
)
from shared.contracts.feedback import RecommendationFeedback, RecommendationInteractionEvent
from shared.contracts.recommendation import RecommendationResult
from shared.contracts.runtime import ConversationWrapUp


class RecordInteractionRequest(ContractModel):
    """仅记录发生了哪种操作，不从点击动作推断坐席是否采纳。"""

    action: InteractionAction


class SubmitFeedbackRequest(ContractModel):
    """坐席主动填写的采用和帮助程度。"""

    adoption_status: AdoptionStatus
    helpfulness: Helpfulness
    result_message_id: str | None = None
    edit_summary: str | None = None
    reason_codes: tuple[str, ...] = ()
    comment: str | None = Field(default=None, max_length=2000)


class EditWrapUpRequest(ContractModel):
    """坐席修改小结时提交完整可见内容和当前版本，防止覆盖别人改动。"""

    expected_version: int = Field(ge=1)
    topic_summary: str = Field(min_length=1, max_length=2000)
    intent_ids: tuple[str, ...] = ()
    resolved_items: tuple[str, ...] = ()
    unresolved_items: tuple[str, ...] = ()
    actions_taken: tuple[str, ...] = ()
    pending_follow_ups: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()
    source_message_ids: tuple[str, ...] = Field(min_length=1)
    source_turn_ids: tuple[str, ...] = Field(min_length=1)


class ConfirmWrapUpRequest(ContractModel):
    """确认前核对版本，避免确认到一份已被别人修改的草稿。"""

    expected_version: int = Field(ge=1)


def wrap_up_contract(row: ConversationWrapUpModel) -> ConversationWrapUp:
    """将数据库当前版本转换成带来源的公开小结。"""

    return ConversationWrapUp(
        wrap_up_id=row.wrap_up_id,
        conversation_id=row.conversation_id,
        version=str(row.version),
        status=WrapUpStatus(row.status),
        topic_summary=row.topic_summary,
        intent_ids=tuple(row.intent_ids),
        resolved_items=tuple(row.resolved_items),
        unresolved_items=tuple(row.unresolved_items),
        actions_taken=tuple(row.actions_taken),
        pending_follow_ups=tuple(row.pending_follow_ups),
        risk_flags=tuple(row.risk_flags),
        source_message_ids=tuple(row.source_message_ids),
        source_turn_ids=tuple(row.source_turn_ids),
        generated_at=row.generated_at,
        generated_by=row.generated_by,
        updated_at=row.updated_at,
        last_edited_by=row.last_edited_by,
        confirmed_by=row.confirmed_by,
        confirmed_at=row.confirmed_at,
    )


class EngagementService:
    """直接在一处完成权限、幂等与应用表写入，不额外拆多层接口。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        authorizer,
        idempotency: PostgresIdempotencyExecutor,
    ) -> None:
        """复用现有应用库会话工厂、对象授权和幂等事务。"""

        self._sessions = session_factory
        self._authorizer = authorizer
        self._idempotency = idempotency

    async def _authorized(self, principal: AuthPrincipal, conversation_id: str) -> None:
        """仅允许当前有此客户访问权的坐席处理推荐或小结。"""

        authorize_roles(principal, {Role.AGENT, Role.ADMIN})
        if not await self._authorizer.can_access_conversation(
            principal.subject_id, conversation_id
        ):
            raise AppError(
                code="OBJECT_ACCESS_DENIED", message="无权访问该会话对象", status_code=403
            )

    @staticmethod
    async def _released_recommendation(
        session: AsyncSession, conversation_id: str, recommendation_id: str
    ) -> RecommendationRecordModel:
        """只有与当前会话和轮次一致的已放行推荐才能产生交互或反馈。"""

        record = await session.get(RecommendationRecordModel, recommendation_id)
        if (
            record is None
            or record.conversation_id != conversation_id
            or record.status not in {"RELEASED", "SENT"}
        ):
            raise AppError(
                code="RECOMMENDATION_NOT_FOUND", message="推荐不存在或尚未放行", status_code=404
            )
        turn = await session.get(TurnRecordModel, record.turn_id)
        if turn is None or turn.conversation_id != conversation_id:
            raise AppError(
                code="RECOMMENDATION_MISMATCH", message="推荐所属轮次不一致", status_code=409
            )
        return record

    async def record_interaction(
        self,
        principal: AuthPrincipal,
        conversation_id: str,
        recommendation_id: str,
        payload: RecordInteractionRequest,
        *,
        key: str,
        request_id: str,
    ) -> tuple[RecommendationInteractionEvent, bool]:
        """幂等追加一条客观操作，不修改推荐采纳状态。"""

        await self._authorized(principal, conversation_id)

        async def mutation(session: AsyncSession) -> dict[str, object]:
            """在幂等事务内核对推荐并新增唯一的交互记录。"""

            recommendation = await self._released_recommendation(
                session, conversation_id, recommendation_id
            )
            event = RecommendationInteractionEvent(
                interaction_id=f"interaction_{uuid4().hex}",
                request_id=request_id,
                recommendation_id=recommendation_id,
                conversation_id=conversation_id,
                turn_id=recommendation.turn_id,
                actor_id=principal.subject_id,
                action=payload.action,
                created_at=datetime.now(UTC),
            )
            session.add(RecommendationInteractionModel(**event.model_dump(mode="python")))
            return event.model_dump(mode="json")

        result = await self._idempotency.execute(
            subject_id=principal.subject_id,
            operation=f"INTERACTION:{recommendation_id}",
            idempotency_key=key,
            request_payload={"conversation_id": conversation_id, "action": payload.action.value},
            response_status=201,
            mutation=mutation,
        )
        return RecommendationInteractionEvent.model_validate(result.response_body), result.replayed

    async def list_recommendations(
        self, principal: AuthPrincipal, conversation_id: str
    ) -> tuple[RecommendationResult, ...]:
        """按生成顺序返回坐席可见的推荐草稿和已放行推荐。"""

        await self._authorized(principal, conversation_id)
        async with self._sessions() as session, session.begin():
            rows = (
                await session.scalars(
                    select(RecommendationRecordModel)
                    .where(
                        RecommendationRecordModel.conversation_id == conversation_id,
                        RecommendationRecordModel.status.in_(
                            ("DRAFT", "RELEASED", "SENT", "REJECTED", "EXPIRED")
                        ),
                    )
                    .order_by(
                        RecommendationRecordModel.created_at,
                        RecommendationRecordModel.recommendation_id,
                    )
                )
            ).all()
            now = datetime.now(UTC)
            for row in rows:
                if (
                    row.status == "DRAFT"
                    and row.review_expires_at is not None
                    and row.review_expires_at <= now
                ):
                    result = RecommendationResult.model_validate(row.payload)
                    row.status = "EXPIRED"
                    row.payload = result.model_copy(update={"review_status": "EXPIRED"}).model_dump(
                        mode="json"
                    )
        # 旧演示记录可能没有完整 payload；它们不应伪装成可展示推荐。
        return tuple(
            RecommendationResult.model_validate(row.payload) for row in rows if row.payload
        )

    async def submit_feedback(
        self,
        principal: AuthPrincipal,
        conversation_id: str,
        recommendation_id: str,
        payload: SubmitFeedbackRequest,
        *,
        key: str,
        request_id: str,
    ) -> tuple[RecommendationFeedback, bool]:
        """坐席显式提交反馈，校验修改后采用确实引用本会话的最终消息。"""

        await self._authorized(principal, conversation_id)

        async def mutation(session: AsyncSession) -> dict[str, object]:
            """推荐、结果消息和反馈在同一事务中核对并写入。"""

            recommendation = await self._released_recommendation(
                session, conversation_id, recommendation_id
            )
            if payload.result_message_id is not None:
                message = await session.get(ChatHistoryMessageModel, payload.result_message_id)
                if (
                    message is None
                    or message.conversation_id != conversation_id
                    or message.turn_id != recommendation.turn_id
                    or message.role not in {"AGENT", "ASSISTANT"}
                ):
                    raise AppError(
                        code="RESULT_MESSAGE_MISMATCH",
                        message="最终消息与推荐不属于同一轮",
                        status_code=409,
                    )
            feedback = RecommendationFeedback(
                feedback_id=f"feedback_{uuid4().hex}",
                request_id=request_id,
                recommendation_id=recommendation_id,
                conversation_id=conversation_id,
                turn_id=recommendation.turn_id,
                trace_id=recommendation.trace_id,
                actor_id=principal.subject_id,
                adoption_status=payload.adoption_status,
                helpfulness=payload.helpfulness,
                result_message_id=payload.result_message_id,
                edit_summary=payload.edit_summary,
                reason_codes=payload.reason_codes,
                comment=payload.comment,
                created_at=datetime.now(UTC),
            )
            data = feedback.model_dump(mode="python", exclude={"comment"})
            session.add(RecommendationFeedbackModel(**data, comment_text=feedback.comment))
            if self._feedback_needs_governance(feedback):
                turn = await session.get(TurnRecordModel, recommendation.turn_id)
                customer_message = (
                    await session.get(ChatHistoryMessageModel, turn.user_message_id)
                    if turn is not None
                    else None
                )
                if customer_message is not None:
                    customer_hash = text_hash(customer_message.content)
                    feedback_hash = stable_hash(
                        {
                            "adoption_status": feedback.adoption_status.value,
                            "helpfulness": feedback.helpfulness.value,
                            "reason_codes": feedback.reason_codes,
                            "edit_summary": feedback.edit_summary,
                        }
                    )
                    await observe_candidate(
                        session,
                        CandidateObservation(
                            candidate_type=SampleCandidateType.RESPONSE_QUALITY,
                            subject_hash=customer_hash,
                            source_type=SampleCandidateSourceType.RECOMMENDATION_FEEDBACK,
                            source_id=feedback.feedback_id,
                            trace_id=feedback.trace_id,
                            reason_codes=self._feedback_reason_codes(feedback),
                            source_refs=(
                                f"feedback:{feedback.feedback_id}",
                                f"recommendation:{recommendation_id}",
                                f"turn:{recommendation.turn_id}",
                                f"message:{customer_message.message_id}",
                            ),
                            content_hashes={
                                "customer": customer_hash,
                                "feedback": feedback_hash,
                            },
                            observed_outcome=(
                                f"{feedback.adoption_status.value}:{feedback.helpfulness.value}"
                            ),
                            occurred_at=feedback.created_at,
                        ),
                    )
            return feedback.model_dump(mode="json")

        result = await self._idempotency.execute(
            subject_id=principal.subject_id,
            operation=f"FEEDBACK:{recommendation_id}",
            idempotency_key=key,
            request_payload={"conversation_id": conversation_id, **payload.model_dump(mode="json")},
            response_status=201,
            mutation=mutation,
        )
        return RecommendationFeedback.model_validate(result.response_body), result.replayed

    @staticmethod
    def _feedback_needs_governance(feedback: RecommendationFeedback) -> bool:
        """正向采用只保留原反馈；修改、拒用或负面评价才进入治理候选。"""

        return feedback.adoption_status in {
            AdoptionStatus.ADOPTED_AFTER_EDIT,
            AdoptionStatus.NOT_ADOPTED,
        } or feedback.helpfulness in {Helpfulness.PARTIALLY_HELPFUL, Helpfulness.NOT_HELPFUL}

    @staticmethod
    def _feedback_reason_codes(feedback: RecommendationFeedback) -> tuple[str, ...]:
        generated = [*feedback.reason_codes]
        if feedback.adoption_status is AdoptionStatus.ADOPTED_AFTER_EDIT:
            generated.append("RECOMMENDATION_EDITED")
        elif feedback.adoption_status is AdoptionStatus.NOT_ADOPTED:
            generated.append("RECOMMENDATION_NOT_ADOPTED")
        if feedback.helpfulness is Helpfulness.PARTIALLY_HELPFUL:
            generated.append("RECOMMENDATION_PARTIALLY_HELPFUL")
        elif feedback.helpfulness is Helpfulness.NOT_HELPFUL:
            generated.append("RECOMMENDATION_NOT_HELPFUL")
        return tuple(dict.fromkeys(generated))

    async def list_interactions(
        self, principal: AuthPrincipal, conversation_id: str, recommendation_id: str
    ) -> tuple[RecommendationInteractionEvent, ...]:
        """经对象授权读取某条推荐的客观操作，最多返回最近一百条。"""

        await self._authorized(principal, conversation_id)
        async with self._sessions() as session:
            await self._released_recommendation(session, conversation_id, recommendation_id)
            rows = (
                await session.scalars(
                    select(RecommendationInteractionModel)
                    .where(
                        RecommendationInteractionModel.conversation_id == conversation_id,
                        RecommendationInteractionModel.recommendation_id == recommendation_id,
                    )
                    .order_by(
                        RecommendationInteractionModel.created_at.desc(),
                        RecommendationInteractionModel.interaction_id.desc(),
                    )
                    .limit(100)
                )
            ).all()
        return tuple(
            RecommendationInteractionEvent.model_validate(
                {column.name: getattr(row, column.name) for column in row.__table__.columns}
            )
            for row in rows
        )

    async def list_feedback(
        self, principal: AuthPrincipal, conversation_id: str, recommendation_id: str
    ) -> tuple[RecommendationFeedback, ...]:
        """经对象授权读取显式反馈，不把查看/复制操作混进评价。"""

        await self._authorized(principal, conversation_id)
        async with self._sessions() as session:
            await self._released_recommendation(session, conversation_id, recommendation_id)
            rows = (
                await session.scalars(
                    select(RecommendationFeedbackModel)
                    .where(
                        RecommendationFeedbackModel.conversation_id == conversation_id,
                        RecommendationFeedbackModel.recommendation_id == recommendation_id,
                    )
                    .order_by(
                        RecommendationFeedbackModel.created_at.desc(),
                        RecommendationFeedbackModel.feedback_id.desc(),
                    )
                    .limit(100)
                )
            ).all()
        return tuple(
            RecommendationFeedback.model_validate(
                {
                    **{
                        column.name: getattr(row, column.name)
                        for column in row.__table__.columns
                        if column.name != "comment_text"
                    },
                    "comment": row.comment_text,
                }
            )
            for row in rows
        )

    async def get_wrap_up(
        self, principal: AuthPrincipal, conversation_id: str
    ) -> ConversationWrapUp:
        """经对象授权读取当前小结，不用图内滚动摘要代替。"""

        await self._authorized(principal, conversation_id)
        async with self._sessions() as session:
            row = await session.scalar(
                select(ConversationWrapUpModel).where(
                    ConversationWrapUpModel.conversation_id == conversation_id
                )
            )
            if row is None:
                raise AppError(
                    code="WRAP_UP_NOT_FOUND", message="会话尚无结束小结", status_code=404
                )
            return wrap_up_contract(row)

    async def create_wrap_up(
        self, principal: AuthPrincipal, conversation_id: str, *, key: str
    ) -> tuple[ConversationWrapUp, bool]:
        """从已落库消息建立保守草稿，不把固定兜底当已解决事项。"""

        await self._authorized(principal, conversation_id)

        async def mutation(session: AsyncSession) -> dict[str, object]:
            """锁定会话，核对无进行中轮次后保存草稿和第一份审计快照。"""

            conversation = await session.scalar(
                select(ConversationModel)
                .where(ConversationModel.conversation_id == conversation_id)
                .with_for_update()
            )
            if conversation is None or conversation.status not in {"ACTIVE", "WRAP_UP"}:
                raise AppError(
                    code="CONVERSATION_NOT_ACTIVE", message="会话不能生成新小结", status_code=409
                )
            latest_status = await session.scalar(
                select(TurnRecordModel.status)
                .where(
                    TurnRecordModel.conversation_id == conversation_id,
                )
                .order_by(TurnRecordModel.started_at.desc())
                .limit(1)
            )
            if latest_status == "INTERRUPTED":
                raise AppError(
                    code="SOP_WAITING_REVIEW",
                    message="请先处理或取消当前办理，再生成结束小结",
                    status_code=409,
                )
            if latest_status == "RUNNING":
                raise AppError(
                    code="TURN_IN_PROGRESS", message="请等待当前咨询处理结束", status_code=409
                )
            messages = (
                await session.scalars(
                    select(ChatHistoryMessageModel)
                    .where(ChatHistoryMessageModel.conversation_id == conversation_id)
                    .order_by(
                        ChatHistoryMessageModel.created_at, ChatHistoryMessageModel.message_id
                    )
                )
            ).all()
            customer_messages = [message for message in messages if message.role == "CUSTOMER"]
            if not customer_messages:
                raise AppError(
                    code="WRAP_UP_NO_SOURCES", message="尚无可供核对的客户咨询消息", status_code=409
                )
            turns = (
                await session.scalars(
                    select(TurnRecordModel)
                    .where(TurnRecordModel.conversation_id == conversation_id)
                    .order_by(TurnRecordModel.started_at, TurnRecordModel.turn_id)
                )
            ).all()
            recommendation_rows = (
                await session.scalars(
                    select(RecommendationRecordModel)
                    .where(
                        RecommendationRecordModel.conversation_id == conversation_id,
                        RecommendationRecordModel.status.in_(("RELEASED", "SENT")),
                    )
                    .order_by(RecommendationRecordModel.created_at)
                )
            ).all()
            recommendations = [
                RecommendationResult.model_validate(row.payload)
                for row in recommendation_rows
                if row.payload
            ]
            feedback_rows = (
                await session.scalars(
                    select(RecommendationFeedbackModel)
                    .where(RecommendationFeedbackModel.conversation_id == conversation_id)
                    .order_by(RecommendationFeedbackModel.created_at)
                )
            ).all()
            latest_feedback = {row.recommendation_id: row for row in feedback_rows}
            recommended_turn_ids = {item.turn_id for item in recommendations}
            intent_ids = list(
                dict.fromkeys(
                    intent_id
                    for turn in turns
                    if turn.routing_decision
                    for intent_id in [
                        turn.routing_decision.get("intent_decision", {}).get("intent_id")
                    ]
                    if intent_id
                )
            )
            resolved_items = [
                item.faq_resolution_summary.faq_title
                for item in recommendations
                if item.faq_resolution_summary and item.faq_resolution_summary.faq_title
            ]
            unresolved_items = [
                f"待核实：{message.content[:80]}"
                for message in customer_messages
                if message.turn_id not in recommended_turn_ids
            ]
            actions_taken = [
                f"FAQ 固定答案：{source.source_id}@{source.version}"
                for item in recommendations
                for source in item.answer_sources
                if source.source_type.value == "FAQ"
            ]
            actions_taken.extend(
                f"坐席反馈：{latest_feedback[item.recommendation_id].adoption_status}"
                for item in recommendations
                if item.recommendation_id in latest_feedback
            )
            pending_follow_ups = [
                f"推荐 {item.recommendation_id} 尚未评价"
                for item in recommendations
                if item.recommendation_id not in latest_feedback
            ]
            now = datetime.now(UTC)
            row = ConversationWrapUpModel(
                wrap_up_id=f"wrap_{uuid4().hex}",
                conversation_id=conversation_id,
                version=1,
                status="DRAFT",
                topic_summary=(
                    "、".join(resolved_items[:3]) if resolved_items else "客户咨询待坐席核实"
                ),
                intent_ids=intent_ids,
                resolved_items=resolved_items,
                unresolved_items=unresolved_items or [],
                actions_taken=actions_taken,
                pending_follow_ups=pending_follow_ups,
                risk_flags=[],
                source_message_ids=[message.message_id for message in messages],
                source_turn_ids=list(
                    dict.fromkeys(message.turn_id for message in customer_messages)
                ),
                generated_at=now,
                generated_by="RULES_V1",
                updated_at=now,
            )
            session.add(row)
            conversation.status = "WRAP_UP"
            conversation.updated_at = now
            await session.flush()
            await self._audit(session, row, "CREATE", "RULES_V1")
            await append_event(
                session,
                conversation_id=conversation_id,
                turn_id=None,
                event_type=RealtimeEventType.CONVERSATION_UPDATED,
                payload={"status": "WRAP_UP", "wrap_up_id": row.wrap_up_id},
            )
            return wrap_up_contract(row).model_dump(mode="json")

        result = await self._idempotency.execute(
            subject_id=principal.subject_id,
            operation=f"CREATE_WRAP_UP:{conversation_id}",
            idempotency_key=key,
            request_payload={"conversation_id": conversation_id},
            response_status=201,
            mutation=mutation,
        )
        return ConversationWrapUp.model_validate(result.response_body), result.replayed

    async def edit_wrap_up(
        self,
        principal: AuthPrincipal,
        conversation_id: str,
        payload: EditWrapUpRequest,
        *,
        key: str,
    ) -> tuple[ConversationWrapUp, bool]:
        """坐席编辑草稿并留审计；版本不一致时拒绝覆盖。"""

        await self._authorized(principal, conversation_id)

        async def mutation(session: AsyncSession) -> dict[str, object]:
            """核实所有来源确属本会话，再写入下一版本。"""

            await session.scalar(
                select(ConversationModel)
                .where(ConversationModel.conversation_id == conversation_id)
                .with_for_update()
            )
            row = await session.scalar(
                select(ConversationWrapUpModel)
                .where(ConversationWrapUpModel.conversation_id == conversation_id)
                .with_for_update()
            )
            if row is None or row.status != "DRAFT":
                raise AppError(
                    code="WRAP_UP_NOT_EDITABLE", message="没有可编辑的草稿", status_code=409
                )
            if row.version != payload.expected_version:
                raise AppError(
                    code="WRAP_UP_VERSION_CONFLICT",
                    message="小结已被修改，请刷新后重试",
                    status_code=409,
                )
            message_ids = set(
                (
                    await session.scalars(
                        select(ChatHistoryMessageModel.message_id).where(
                            ChatHistoryMessageModel.conversation_id == conversation_id,
                            ChatHistoryMessageModel.message_id.in_(payload.source_message_ids),
                        )
                    )
                ).all()
            )
            turn_ids = set(
                (
                    await session.scalars(
                        select(TurnRecordModel.turn_id).where(
                            TurnRecordModel.conversation_id == conversation_id,
                            TurnRecordModel.turn_id.in_(payload.source_turn_ids),
                        )
                    )
                ).all()
            )
            if message_ids != set(payload.source_message_ids) or turn_ids != set(
                payload.source_turn_ids
            ):
                raise AppError(
                    code="WRAP_UP_SOURCE_MISMATCH",
                    message="小结引用了其他会话的消息或轮次",
                    status_code=409,
                )
            for field, value in payload.model_dump(exclude={"expected_version"}).items():
                setattr(row, field, list(value) if isinstance(value, tuple) else value)
            row.version += 1
            row.updated_at = datetime.now(UTC)
            row.last_edited_by = principal.subject_id
            await self._audit(session, row, "EDIT", principal.subject_id)
            await append_event(
                session,
                conversation_id=conversation_id,
                turn_id=None,
                event_type=RealtimeEventType.CONVERSATION_UPDATED,
                payload={"status": "WRAP_UP", "wrap_up_id": row.wrap_up_id, "version": row.version},
            )
            return wrap_up_contract(row).model_dump(mode="json")

        result = await self._idempotency.execute(
            subject_id=principal.subject_id,
            operation=f"EDIT_WRAP_UP:{conversation_id}",
            idempotency_key=key,
            request_payload={"conversation_id": conversation_id, **payload.model_dump(mode="json")},
            response_status=200,
            mutation=mutation,
        )
        return ConversationWrapUp.model_validate(result.response_body), result.replayed

    async def confirm_wrap_up(
        self,
        principal: AuthPrincipal,
        conversation_id: str,
        payload: ConfirmWrapUpRequest,
        *,
        key: str,
    ) -> tuple[ConversationWrapUp, bool]:
        """坐席显式确认当前小结，同时结束业务会话。"""

        await self._authorized(principal, conversation_id)

        async def mutation(session: AsyncSession) -> dict[str, object]:
            """锁定会话与草稿，确认后同事务改成已结束并审计。"""

            conversation = await session.scalar(
                select(ConversationModel)
                .where(ConversationModel.conversation_id == conversation_id)
                .with_for_update()
            )
            row = await session.scalar(
                select(ConversationWrapUpModel)
                .where(ConversationWrapUpModel.conversation_id == conversation_id)
                .with_for_update()
            )
            if (
                conversation is None
                or conversation.status != "WRAP_UP"
                or row is None
                or row.status != "DRAFT"
            ):
                raise AppError(
                    code="WRAP_UP_NOT_CONFIRMABLE", message="会话没有待确认的小结", status_code=409
                )
            if row.version != payload.expected_version:
                raise AppError(
                    code="WRAP_UP_VERSION_CONFLICT",
                    message="小结已被修改，请刷新后重试",
                    status_code=409,
                )
            now = datetime.now(UTC)
            row.version += 1
            row.status = "CONFIRMED"
            row.confirmed_by = principal.subject_id
            row.confirmed_at = now
            row.updated_at = now
            conversation.status = "FINISHED"
            conversation.updated_at = now
            await self._audit(session, row, "CONFIRM", principal.subject_id)
            if row.unresolved_items or row.pending_follow_ups or row.risk_flags:
                wrap_up_hash = stable_hash(
                    {
                        "unresolved_items": row.unresolved_items,
                        "pending_follow_ups": row.pending_follow_ups,
                        "risk_flags": row.risk_flags,
                    }
                )
                reason_codes = []
                if row.unresolved_items:
                    reason_codes.append("WRAP_UP_UNRESOLVED_ITEMS")
                if row.pending_follow_ups:
                    reason_codes.append("WRAP_UP_PENDING_FOLLOW_UPS")
                if row.risk_flags:
                    reason_codes.append("WRAP_UP_RISK_FLAGS")
                await observe_candidate(
                    session,
                    CandidateObservation(
                        candidate_type=SampleCandidateType.PROCESS_GAP,
                        subject_hash=stable_hash(conversation_id),
                        source_type=SampleCandidateSourceType.CONVERSATION_WRAP_UP,
                        source_id=row.wrap_up_id,
                        trace_id=f"wrap-up:{row.wrap_up_id}",
                        reason_codes=tuple(reason_codes),
                        source_refs=(
                            f"wrap_up:{row.wrap_up_id}",
                            *(f"turn:{turn_id}" for turn_id in row.source_turn_ids),
                        ),
                        content_hashes={"wrap_up": wrap_up_hash},
                        observed_outcome="CONFIRMED_WITH_OPEN_ITEMS",
                        occurred_at=now,
                    ),
                )
            await append_event(
                session,
                conversation_id=conversation_id,
                turn_id=None,
                event_type=RealtimeEventType.CONVERSATION_UPDATED,
                payload={"status": "FINISHED", "wrap_up_id": row.wrap_up_id},
            )
            return wrap_up_contract(row).model_dump(mode="json")

        result = await self._idempotency.execute(
            subject_id=principal.subject_id,
            operation=f"CONFIRM_WRAP_UP:{conversation_id}",
            idempotency_key=key,
            request_payload={"conversation_id": conversation_id, **payload.model_dump(mode="json")},
            response_status=200,
            mutation=mutation,
        )
        return ConversationWrapUp.model_validate(result.response_body), result.replayed

    @staticmethod
    async def _audit(
        session: AsyncSession, row: ConversationWrapUpModel, action: str, actor: str
    ) -> None:
        """每次状态变化都保存完整当时版本，供审计回看。"""

        snapshot = wrap_up_contract(row).model_dump(mode="json")
        session.add(
            ConversationWrapUpAuditModel(
                audit_id=f"wrap_audit_{uuid4().hex}",
                wrap_up_id=row.wrap_up_id,
                conversation_id=row.conversation_id,
                version=row.version,
                action=action,
                actor_id=actor,
                snapshot=snapshot,
                created_at=datetime.now(UTC),
            )
        )
