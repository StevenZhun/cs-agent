"""RAG 草稿的坐席审核、图恢复、最终发送与审计。"""

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.bff.errors import AppError
from services.bff.infrastructure.events import append_event
from services.bff.infrastructure.idempotency import PostgresIdempotencyExecutor
from services.bff.infrastructure.models import (
    ChatHistoryMessageModel,
    ConversationModel,
    GraphRunRecordModel,
    RagRegenerationRecordModel,
    RagReviewRecordModel,
    RecommendationRecordModel,
    TurnRecordModel,
)
from services.bff.infrastructure.repositories import SqlAlchemyObjectAuthorizer
from services.bff.infrastructure.runtime_client import (
    AiRuntimeClient,
    RuntimeExecutionRejected,
    RuntimeExecutionUncertain,
)
from services.bff.security import AuthPrincipal, Role, authorize_roles
from shared.contracts.conversation import ConversationMessage, RealtimeEventType
from shared.contracts.enums import AnswerSourceType, MessageRole, QualityAction, ResponsePath
from shared.contracts.rag import (
    RagReviewInstruction,
    RagReviewRequest,
    RagReviewResult,
)
from shared.contracts.recommendation import AnswerSource, RecommendationResult
from shared.contracts.runtime import ExecuteGraphTurnRequest, ExecuteGraphTurnResponse
from shared.guardrails import evaluate_output_content


class RagReviewService:
    """坐席只处理已持久化的 RAG 草稿，不接受浏览器提交任意原始草稿。"""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        authorizer: SqlAlchemyObjectAuthorizer,
        runtime: AiRuntimeClient,
        review_ttl_minutes: int = 30,
    ) -> None:
        self._sessions = sessions
        self._authorizer = authorizer
        self._runtime = runtime
        self._idempotency = PostgresIdempotencyExecutor(sessions)
        self._review_ttl = timedelta(minutes=review_ttl_minutes)

    async def _authorize(self, principal: AuthPrincipal, conversation_id: str) -> None:
        """角色权限与客户对象权限必须同时成立。"""

        authorize_roles(principal, {Role.AGENT, Role.ADMIN})
        if not await self._authorizer.can_access_conversation(
            principal.subject_id, conversation_id
        ):
            raise AppError(code="OBJECT_ACCESS_DENIED", message="无权访问该会话", status_code=403)

    async def review(
        self,
        principal: AuthPrincipal,
        conversation_id: str,
        payload: RagReviewRequest,
        *,
        key: str,
        request_id: str,
        trace_id: str,
    ) -> tuple[RagReviewResult, bool]:
        """先原子受理处理动作，再恢复图，最后原子保存终态和客户消息。"""

        await self._authorize(principal, conversation_id)

        async def mutation(session: AsyncSession) -> dict[str, object]:
            conversation = await session.scalar(
                select(ConversationModel)
                .where(ConversationModel.conversation_id == conversation_id)
                .with_for_update()
            )
            recommendation = await session.scalar(
                select(RecommendationRecordModel)
                .where(RecommendationRecordModel.recommendation_id == payload.recommendation_id)
                .with_for_update()
            )
            turn = await session.scalar(
                select(TurnRecordModel)
                .where(TurnRecordModel.turn_id == payload.turn_id)
                .with_for_update()
            )
            if (
                conversation is None
                or conversation.status != "ACTIVE"
                or recommendation is None
                or recommendation.conversation_id != conversation_id
                or recommendation.turn_id != payload.turn_id
                or recommendation.status
                not in ({"DRAFT", "EXPIRED"} if payload.action == "REGENERATE" else {"DRAFT"})
                or turn is None
                or turn.conversation_id != conversation_id
                or turn.status != "INTERRUPTED"
            ):
                raise AppError(
                    code="RAG_REVIEW_CHANGED",
                    message="草稿、会话或处理轮次已变化，请刷新后重新核对",
                    status_code=409,
                )
            now = datetime.now(UTC)
            expired = bool(
                recommendation.review_expires_at is not None
                and recommendation.review_expires_at <= now
            )
            stored = RecommendationResult.model_validate(recommendation.payload)
            if payload.action == "REGENERATE":
                if recommendation.status not in {"DRAFT", "EXPIRED"} or not expired:
                    raise AppError(
                        code="RAG_REGENERATION_NOT_ALLOWED",
                        message="只有已过审核时限的知识草稿可以重新生成",
                        status_code=409,
                    )
                if recommendation.status == "DRAFT":
                    recommendation.status = "EXPIRED"
                    stored = stored.model_copy(update={"review_status": "EXPIRED"})
                    recommendation.payload = stored.model_dump(mode="json")
            elif expired:
                raise AppError(
                    code="RAG_REVIEW_EXPIRED",
                    message="知识草稿审核时间已过，请刷新后重新检索生成",
                    status_code=409,
                )
            if (
                stored.response_path is not ResponsePath.RAG
                or stored.review_status
                != ("EXPIRED" if payload.action == "REGENERATE" else "DRAFT")
                or stored.rag_draft_id != payload.draft_id
            ):
                raise AppError(
                    code="RAG_REVIEW_CHANGED", message="待审核草稿已变化", status_code=409
                )
            if payload.action == "EDIT" and payload.edited_answer == stored.answer:
                raise AppError(
                    code="RAG_REVIEW_UNCHANGED",
                    message="修改后发送需要实际调整草稿内容",
                    status_code=422,
                )
            old_run = await session.get(GraphRunRecordModel, turn.active_run_id)
            if old_run is None or old_run.status != "INTERRUPTED" or old_run.rag_snapshot is None:
                raise AppError(
                    code="RUN_STATE_CONFLICT", message="草稿仍在处理中，请稍后重试", status_code=409
                )

            decision_id = "rag_decision_" + uuid4().hex
            review_id = (
                "rag_regeneration_" if payload.action == "REGENERATE" else "rag_review_"
            ) + uuid4().hex
            run_id = "run_" + uuid4().hex
            instruction = RagReviewInstruction(
                **payload.model_dump(mode="python"),
                decision_id=decision_id,
                actor_id=principal.subject_id,
                acted_at=now,
                request_id=request_id,
                trace_id=trace_id,
            )
            session.add(
                GraphRunRecordModel(
                    run_id=run_id,
                    turn_id=turn.turn_id,
                    thread_id=turn.thread_id,
                    status="RUNNING",
                    decision_profile=old_run.decision_profile,
                    decision_ids=[],
                    resumed_from_run_id=old_run.run_id,
                    started_at=now,
                    lease_expires_at=now + timedelta(seconds=60),
                    lease_version=0,
                    lease_owner="ONLINE",
                    resume_payload=instruction.model_dump(mode="json"),
                )
            )
            turn.status = "RUNNING"
            turn.active_run_id = run_id
            turn.completed_at = None
            return {
                "review_id": review_id,
                "decision_id": decision_id,
                "run_id": run_id,
                "turn_id": turn.turn_id,
            }

        accepted = await self._idempotency.execute(
            subject_id=principal.subject_id,
            operation="RAG_REVIEW:" + conversation_id,
            idempotency_key=key,
            request_payload=payload.model_dump(mode="json"),
            response_status=200,
            mutation=mutation,
        )
        review_id = str(accepted.response_body["review_id"])
        existing = await self._stored_result(review_id)
        if existing is not None:
            return existing, accepted.replayed

        request = await self._runtime_request(str(accepted.response_body["run_id"]))
        try:
            graph_result = await self._runtime.execute(request)
        except RuntimeExecutionUncertain as error:
            inspected = await self._runtime.inspect(request)
            if inspected.terminal is None:
                raise AppError(
                    code="RAG_REVIEW_PENDING",
                    message="处理结果仍在确认，请稍后使用原操作重试",
                    status_code=503,
                    retryable=True,
                ) from error
            graph_result = ExecuteGraphTurnResponse(
                conversation_id=request.conversation_id,
                thread_id=request.thread_id,
                turn_id=request.turn_id,
                run_id=request.run_id,
                normalized_query=inspected.snapshot.normalized_query,
                effective_query=inspected.snapshot.effective_query,
                turn_sequence=inspected.snapshot.turn_sequence,
                context_cycle_id=inspected.snapshot.context_cycle_id,
                replayed=True,
                terminal=inspected.terminal,
                decision_trace=inspected.decision_trace,
                routing_decision=inspected.routing_decision,
                routing_trace=inspected.routing_trace,
                model_calls=inspected.model_calls,
            )
        except RuntimeExecutionRejected as error:
            raise AppError(
                code="RAG_REVIEW_REJECTED_BY_RUNTIME",
                message="处理状态已变化，请刷新后重新核对",
                status_code=409,
            ) from error
        persist = (
            self._persist_regeneration if payload.action == "REGENERATE" else self._persist_result
        )
        result = await persist(
            principal,
            conversation_id,
            payload,
            review_id=review_id,
            decision_id=str(accepted.response_body["decision_id"]),
            graph_result=graph_result,
        )
        return result, accepted.replayed

    async def _runtime_request(self, run_id: str) -> ExecuteGraphTurnRequest:
        """从应用库读取原客户消息和当前租约，不相信浏览器提供运行身份。"""

        async with self._sessions() as session:
            run = await session.get(GraphRunRecordModel, run_id)
            turn = await session.get(TurnRecordModel, run.turn_id) if run else None
            message = (
                await session.get(ChatHistoryMessageModel, turn.user_message_id) if turn else None
            )
            if run is None or turn is None or message is None:
                raise AppError(code="RUN_NOT_FOUND", message="处理运行不存在", status_code=404)
            return ExecuteGraphTurnRequest(
                conversation_id=turn.conversation_id,
                thread_id=turn.thread_id,
                turn_id=turn.turn_id,
                run_id=run.run_id,
                lease_version=run.lease_version,
                request_id=str((run.resume_payload or {}).get("request_id") or turn.request_id),
                trace_id=str((run.resume_payload or {}).get("trace_id") or turn.trace_id),
                raw_query=message.content,
            )

    async def _stored_result(self, review_id: str) -> RagReviewResult | None:
        """幂等重放直接读取权威审核与消息，不再次恢复图。"""

        async with self._sessions() as session:
            review = await session.get(RagReviewRecordModel, review_id)
            if review is None:
                regeneration = await session.get(RagRegenerationRecordModel, review_id)
                if regeneration is None:
                    return None
                recommendation = await session.get(
                    RecommendationRecordModel, regeneration.next_recommendation_id
                )
                if recommendation is None:
                    raise ValueError("RAG 重新生成记录存在但新推荐缺失")
                return self._regeneration_result(regeneration, recommendation)
            message = await session.get(ChatHistoryMessageModel, review.final_message_id)
            if message is None:
                raise ValueError("RAG 审核存在但最终消息缺失")
            recommendation = await session.get(RecommendationRecordModel, review.recommendation_id)
            if recommendation is None:
                raise ValueError("RAG 审核存在但推荐记录缺失")
            return self._result(review, recommendation.status, message)

    async def _persist_regeneration(
        self,
        principal: AuthPrincipal,
        conversation_id: str,
        payload: RagReviewRequest,
        *,
        review_id: str,
        decision_id: str,
        graph_result: ExecuteGraphTurnResponse,
    ) -> RagReviewResult:
        """保存重新检索形成的新草稿，旧草稿只保留为过期审计事实。"""

        terminal = graph_result.terminal
        draft = terminal.rag_draft
        retrieval = terminal.retrieval_result
        if (
            payload.action != "REGENERATE"
            or terminal.response_path is not ResponsePath.RAG
            or not terminal.graph_interrupted
            or terminal.reason_code != "RAG_REVIEW_REQUIRED"
            or draft is None
            or retrieval is None
            or draft.retrieval_id != retrieval.retrieval_id
        ):
            raise AppError(
                code="RAG_REGENERATION_RESULT_INVALID",
                message="重新检索结果未形成可审核草稿",
                status_code=503,
                retryable=True,
            )
        quality = evaluate_output_content(
            draft.answer,
            decision_id=f"rag-regeneration-quality:{decision_id}",
        )
        if quality.action is QualityAction.BLOCK:
            raise AppError(
                code="RAG_REGENERATION_RESULT_INVALID",
                message="重新生成的草稿未通过输出检查",
                status_code=503,
                retryable=True,
            )

        async with self._sessions() as session, session.begin():
            existing = await session.get(RagRegenerationRecordModel, review_id)
            if existing is not None:
                recommendation = await session.get(
                    RecommendationRecordModel, existing.next_recommendation_id
                )
                return self._regeneration_result(existing, recommendation)
            conversation = await session.scalar(
                select(ConversationModel)
                .where(ConversationModel.conversation_id == conversation_id)
                .with_for_update()
            )
            previous = await session.scalar(
                select(RecommendationRecordModel)
                .where(RecommendationRecordModel.recommendation_id == payload.recommendation_id)
                .with_for_update()
            )
            turn = await session.scalar(
                select(TurnRecordModel)
                .where(TurnRecordModel.turn_id == payload.turn_id)
                .with_for_update()
            )
            run = await session.scalar(
                select(GraphRunRecordModel)
                .where(GraphRunRecordModel.run_id == graph_result.run_id)
                .with_for_update()
            )
            if (
                conversation is None
                or previous is None
                or previous.status != "EXPIRED"
                or turn is None
                or turn.active_run_id != graph_result.run_id
                or turn.status != "RUNNING"
                or run is None
                or run.status != "RUNNING"
            ):
                raise AppError(
                    code="RAG_REVIEW_CHANGED",
                    message="重新检索落库前状态已变化",
                    status_code=409,
                )
            old = RecommendationResult.model_validate(previous.payload)
            if old.rag_draft_id != payload.draft_id:
                raise AppError(
                    code="RAG_REVIEW_CHANGED",
                    message="过期草稿已变化",
                    status_code=409,
                )
            now = datetime.now(UTC)
            instruction = RagReviewInstruction.model_validate(run.resume_payload)
            recommendation_id = (
                "rec_"
                + uuid5(
                    NAMESPACE_URL,
                    f"rag-regenerated:{turn.turn_id}:{draft.draft_id}:{run.run_id}",
                ).hex
            )
            regenerated = old.model_copy(
                update={
                    "request_id": instruction.request_id,
                    "trace_id": instruction.trace_id,
                    "recommendation_id": recommendation_id,
                    "answer": draft.answer,
                    "rag_draft_id": draft.draft_id,
                    "retrieval_id": draft.retrieval_id,
                    "retrieval_assessment": retrieval.assessment,
                    "answer_sources": tuple(
                        AnswerSource(
                            source_type=AnswerSourceType.RAG,
                            source_id=citation.parent_doc_id,
                            version=next(
                                (
                                    getattr(document.metadata, "release_version", None)
                                    or getattr(document.metadata, "version", None)
                                    or retrieval.index_version
                                    for document in retrieval.documents
                                    if document.document_id == citation.document_id
                                ),
                                retrieval.index_version,
                            ),
                            title=citation.title,
                            excerpt=citation.excerpt,
                            content_hash=citation.content_hash,
                        )
                        for citation in draft.citations
                    ),
                    "quality_decision": quality.model_copy(
                        update={
                            "reason_codes": (
                                *quality.reason_codes,
                                "EVIDENCE_VALIDATED_MANUAL_REVIEW_REQUIRED",
                            )
                        }
                    ),
                    "review_status": "DRAFT",
                    "review_expires_at": now + self._review_ttl,
                }
            )
            session.add(
                RecommendationRecordModel(
                    recommendation_id=recommendation_id,
                    conversation_id=conversation_id,
                    turn_id=turn.turn_id,
                    status="DRAFT",
                    trace_id=instruction.trace_id,
                    payload=regenerated.model_dump(mode="json"),
                    review_expires_at=regenerated.review_expires_at,
                    created_at=now,
                )
            )
            # 重新生成审计通过外键引用新推荐。这里没有 ORM 对象关系可供
            # SQLAlchemy 自动推断插入顺序，先落新推荐再写审计，避免真实
            # PostgreSQL 在同一轮 flush 中先插审计而触发外键冲突。
            await session.flush()
            regeneration = RagRegenerationRecordModel(
                regeneration_id=review_id,
                previous_recommendation_id=payload.recommendation_id,
                next_recommendation_id=recommendation_id,
                conversation_id=conversation_id,
                turn_id=turn.turn_id,
                run_id=run.run_id,
                decision_id=decision_id,
                actor_id=principal.subject_id,
                previous_draft_id=payload.draft_id,
                next_draft_id=draft.draft_id,
                note=payload.note,
                request_id=instruction.request_id,
                trace_id=instruction.trace_id,
                created_at=now,
            )
            session.add(regeneration)
            run.status = "INTERRUPTED"
            run.rag_snapshot = terminal.model_dump(mode="json")
            run.finished_at = now
            turn.status = "INTERRUPTED"
            turn.completed_at = now
            conversation.updated_at = now
            await append_event(
                session,
                conversation_id=conversation_id,
                turn_id=turn.turn_id,
                event_type=RealtimeEventType.CONVERSATION_UPDATED,
                payload={
                    "recommendation_id": recommendation_id,
                    "decision_id": decision_id,
                    "action": "REGENERATE",
                },
            )
            await session.flush()
            return self._regeneration_result(regeneration, regenerated)

    async def _persist_result(
        self,
        principal: AuthPrincipal,
        conversation_id: str,
        payload: RagReviewRequest,
        *,
        review_id: str,
        decision_id: str,
        graph_result: ExecuteGraphTurnResponse,
    ) -> RagReviewResult:
        """核对图结果后，在一个事务内保存审计、人工消息和终局状态。"""

        terminal = graph_result.terminal
        outcome = terminal.rag_review
        if (
            terminal.response_path is not ResponsePath.RAG
            or terminal.graph_interrupted
            or outcome is None
            or outcome.decision_id != decision_id
            or outcome.recommendation_id != payload.recommendation_id
            or outcome.draft_id != payload.draft_id
            or outcome.action != payload.action
            or outcome.actor_id != principal.subject_id
            or outcome.final_answer != terminal.content
        ):
            raise AppError(
                code="RAG_REVIEW_RESULT_INVALID",
                message="审核结果未通过一致性检查",
                status_code=503,
                retryable=True,
            )

        async with self._sessions() as session, session.begin():
            existing = await session.get(RagReviewRecordModel, review_id)
            if existing is not None:
                message = await session.get(ChatHistoryMessageModel, existing.final_message_id)
                recommendation = await session.get(
                    RecommendationRecordModel, existing.recommendation_id
                )
                return self._result(existing, recommendation.status, message)
            conversation = await session.scalar(
                select(ConversationModel)
                .where(ConversationModel.conversation_id == conversation_id)
                .with_for_update()
            )
            recommendation = await session.scalar(
                select(RecommendationRecordModel)
                .where(RecommendationRecordModel.recommendation_id == payload.recommendation_id)
                .with_for_update()
            )
            turn = await session.scalar(
                select(TurnRecordModel)
                .where(TurnRecordModel.turn_id == payload.turn_id)
                .with_for_update()
            )
            run = await session.scalar(
                select(GraphRunRecordModel)
                .where(GraphRunRecordModel.run_id == graph_result.run_id)
                .with_for_update()
            )
            if (
                conversation is None
                or recommendation is None
                or recommendation.status != "DRAFT"
                or turn is None
                or turn.active_run_id != graph_result.run_id
                or turn.status != "RUNNING"
                or run is None
                or run.status != "RUNNING"
            ):
                raise AppError(
                    code="RAG_REVIEW_CHANGED", message="审核落库前状态已变化", status_code=409
                )
            stored = RecommendationResult.model_validate(recommendation.payload)
            if stored.rag_draft_id != payload.draft_id:
                raise AppError(
                    code="RAG_REVIEW_CHANGED", message="待审核草稿已变化", status_code=409
                )
            now = datetime.now(UTC)
            instruction = RagReviewInstruction.model_validate(run.resume_payload)
            message_id = (
                "msg_ai_" + uuid5(NAMESPACE_URL, f"assistant:{turn.turn_id}:{decision_id}").hex
            )
            message = ChatHistoryMessageModel(
                message_id=message_id,
                conversation_id=conversation_id,
                thread_id=turn.thread_id,
                turn_id=turn.turn_id,
                role=MessageRole.AGENT.value,
                content=terminal.content,
                request_id=instruction.request_id,
                trace_id=instruction.trace_id,
                created_at=now,
            )
            session.add(message)
            status = "REJECTED" if payload.action == "REJECT" else "SENT"
            recommendation.status = status
            recommendation.payload = stored.model_copy(update={"review_status": status}).model_dump(
                mode="json"
            )
            review = RagReviewRecordModel(
                review_id=review_id,
                recommendation_id=payload.recommendation_id,
                conversation_id=conversation_id,
                turn_id=turn.turn_id,
                run_id=run.run_id,
                decision_id=decision_id,
                actor_id=principal.subject_id,
                action=payload.action,
                original_draft=stored.answer,
                edited_answer=payload.edited_answer,
                final_answer=terminal.content,
                note=payload.note,
                final_message_id=message_id,
                request_id=instruction.request_id,
                trace_id=instruction.trace_id,
                created_at=now,
            )
            session.add(review)
            run.status = "COMPLETED"
            run.rag_snapshot = terminal.model_dump(mode="json")
            run.finished_at = now
            turn.status = "COMPLETED"
            turn.completed_at = now
            conversation.updated_at = now
            await append_event(
                session,
                conversation_id=conversation_id,
                turn_id=turn.turn_id,
                event_type=RealtimeEventType.MESSAGE_CREATED,
                payload={
                    "message_id": message_id,
                    "role": "AGENT",
                    "actor_id": principal.subject_id,
                    "decision_id": decision_id,
                    "action": payload.action,
                },
            )
            await session.flush()
            return self._result(review, status, message)

    @staticmethod
    def _result(
        review: RagReviewRecordModel,
        recommendation_status: str,
        message: ChatHistoryMessageModel,
    ) -> RagReviewResult:
        """把同一事务中的数据库事实转换为外部契约。"""

        return RagReviewResult(
            review_id=review.review_id,
            recommendation_id=review.recommendation_id,
            conversation_id=review.conversation_id,
            turn_id=review.turn_id,
            action=review.action,
            recommendation_status=recommendation_status,
            customer_message=ConversationMessage(
                message_id=message.message_id,
                conversation_id=message.conversation_id,
                turn_id=message.turn_id,
                role=MessageRole(message.role),
                content=message.content,
                created_at=message.created_at,
            ),
            reviewed_at=review.created_at,
        )

    @staticmethod
    def _regeneration_result(
        regeneration: RagRegenerationRecordModel,
        recommendation: RecommendationRecordModel | RecommendationResult,
    ) -> RagReviewResult:
        """重新生成只返回新草稿标识，不伪造一条客户可见消息。"""

        result = (
            recommendation
            if isinstance(recommendation, RecommendationResult)
            else RecommendationResult.model_validate(recommendation.payload)
        )
        return RagReviewResult(
            review_id=regeneration.regeneration_id,
            recommendation_id=regeneration.previous_recommendation_id,
            conversation_id=regeneration.conversation_id,
            turn_id=regeneration.turn_id,
            action="REGENERATE",
            recommendation_status="DRAFT",
            regenerated_recommendation_id=result.recommendation_id,
            regenerated_draft_id=result.rag_draft_id,
            reviewed_at=regeneration.created_at,
        )
