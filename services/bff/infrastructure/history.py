"""Append-only PostgreSQL history and bounded, authorized-page read support."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.bff.errors import AppError
from services.bff.infrastructure.events import append_event
from services.bff.infrastructure.models import (
    ChatHistoryMessageModel,
    ConversationModel,
    ConversationThreadMappingModel,
    GraphRunRecordModel,
    ModelCallRecordModel,
    RecommendationRecordModel,
    TurnRecordModel,
)
from services.quality.models import AsyncQualityJobModel
from shared.contracts.conversation import (
    ConversationMessage,
    CustomerTurnResult,
    RealtimeEventType,
    TurnTokenUsage,
)
from shared.contracts.enums import MessageRole, ResponsePath, TurnStatus
from shared.contracts.recommendation import RecommendationResult
from shared.contracts.routing import RoutingDecision
from shared.contracts.runtime import (
    ChatHistoryMessage,
    DecisionTrace,
    ExecuteGraphTurnRequest,
    GraphRunRecord,
    ModelCallRecord,
    PreviousAssistantMessage,
    TurnRecord,
)
from shared.contracts.sop import BusinessToolResult
from shared.safe_fallback import RAG_REVIEW_PENDING_REPLY

RUN_LEASE = timedelta(seconds=60)
MAX_RUNS_PER_TURN = 3


class SqlAlchemyChatHistoryRepository:
    """Keep raw history in app.chat_history, separate from graph checkpoints."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        quality_async_enabled: bool = False,
        quality_max_attempts: int = 3,
    ) -> None:
        self._session_factory = session_factory
        self._quality_async_enabled = quality_async_enabled
        self._quality_max_attempts = quality_max_attempts

    async def previous_assistant_message(
        self,
        session: AsyncSession,
        *,
        conversation_id: str,
        before: datetime,
    ) -> PreviousAssistantMessage | None:
        """读取指定受理时间之前最后一条客户可见回复，供受控上下文理解使用。"""

        message = await session.scalar(
            select(ChatHistoryMessageModel)
            .where(
                ChatHistoryMessageModel.conversation_id == conversation_id,
                ChatHistoryMessageModel.role.in_(
                    [MessageRole.ASSISTANT.value, MessageRole.AGENT.value]
                ),
                ChatHistoryMessageModel.created_at < before,
            )
            .order_by(
                ChatHistoryMessageModel.created_at.desc(),
                ChatHistoryMessageModel.message_id.desc(),
            )
            .limit(1)
        )
        if message is None:
            return None
        return PreviousAssistantMessage(
            message_id=message.message_id,
            turn_id=message.turn_id,
            role=MessageRole(message.role),
            content=message.content,
            source_ref=f"message:{message.message_id}",
        )

    async def lock_active_thread(
        self, session: AsyncSession, *, conversation_id: str, message_id: str
    ) -> str:
        """Serialize new turns per conversation inside the acceptance transaction."""

        conversation = await session.scalar(
            select(ConversationModel)
            .where(ConversationModel.conversation_id == conversation_id)
            .with_for_update()
        )
        if conversation is None:
            raise AppError(code="CONVERSATION_NOT_FOUND", message="会话不存在", status_code=404)
        if conversation.status != "ACTIVE":
            raise AppError(
                code="CONVERSATION_NOT_ACTIVE", message="会话当前不能接收消息", status_code=409
            )
        if await session.get(ChatHistoryMessageModel, message_id) is not None:
            raise AppError(code="MESSAGE_ID_REUSED", message="消息编号已被使用", status_code=409)
        active_turn = await session.scalar(
            select(TurnRecordModel.turn_id)
            .where(
                TurnRecordModel.conversation_id == conversation_id,
                TurnRecordModel.status == "RUNNING",
            )
            .limit(1)
        )
        if active_turn is not None:
            raise AppError(
                code="TURN_IN_PROGRESS",
                message="上一条消息仍在处理中",
                status_code=409,
                retryable=True,
            )
        pending_rag_review = await session.scalar(
            select(RecommendationRecordModel.recommendation_id)
            .where(
                RecommendationRecordModel.conversation_id == conversation_id,
                RecommendationRecordModel.status == "DRAFT",
            )
            .limit(1)
        )
        if pending_rag_review is not None:
            raise AppError(
                code="RAG_REVIEW_PENDING",
                message="上一条知识草稿正在等待人工核对",
                status_code=409,
                retryable=True,
            )
        thread_id = await session.scalar(
            select(ConversationThreadMappingModel.thread_id).where(
                ConversationThreadMappingModel.conversation_id == conversation_id
            )
        )
        if thread_id is None:
            raise ValueError("conversation has no thread mapping")
        return thread_id

    async def record_customer_turn(
        self,
        session: AsyncSession,
        *,
        turn: TurnRecord,
        message: ChatHistoryMessage,
    ) -> None:
        """Stage the first message and its Turn in the caller's single transaction."""

        if (
            message.role is not MessageRole.CUSTOMER
            or message.message_id != turn.user_message_id
            or message.turn_id != turn.turn_id
            or message.conversation_id != turn.conversation_id
            or message.thread_id != turn.thread_id
            or message.request_id != turn.request_id
            or message.trace_id != turn.trace_id
        ):
            raise ValueError("customer message and Turn identity must agree")
        await self._require_thread(session, turn.conversation_id, turn.thread_id)
        session.add(
            TurnRecordModel(
                turn_id=turn.turn_id,
                conversation_id=turn.conversation_id,
                thread_id=turn.thread_id,
                request_id=turn.request_id,
                trace_id=turn.trace_id,
                user_message_id=turn.user_message_id,
                status=turn.status.value,
                routing_decision=(
                    turn.routing_decision.model_dump(mode="json")
                    if turn.routing_decision is not None
                    else None
                ),
                terminal_response_path=(
                    turn.terminal_response_path.value if turn.terminal_response_path else None
                ),
                terminal_reason_code=turn.terminal_reason_code,
                context_cycle_id=turn.context_cycle_id,
                active_run_id=turn.active_run_id,
                started_at=turn.started_at,
                completed_at=turn.completed_at,
            )
        )
        await session.flush()
        await self.append_message(session, message)
        conversation = await session.get(ConversationModel, message.conversation_id)
        if conversation is None:
            raise ValueError("customer message must reference an existing conversation")
        # 最近咨询按最后交互时间排序；仅查看或改名不应改变这个时间。
        conversation.updated_at = message.created_at
        await append_event(
            session,
            conversation_id=message.conversation_id,
            turn_id=message.turn_id,
            event_type=RealtimeEventType.MESSAGE_CREATED,
            payload={"message_id": message.message_id, "role": "CUSTOMER"},
        )

    async def append_message(self, session: AsyncSession, message: ChatHistoryMessage) -> None:
        """Append a validated raw message without modifying earlier messages."""

        turn = await session.get(TurnRecordModel, message.turn_id)
        if (
            turn is None
            or turn.conversation_id != message.conversation_id
            or turn.thread_id != message.thread_id
        ):
            raise ValueError("message must reference a Turn in the same conversation and thread")
        session.add(
            ChatHistoryMessageModel(
                message_id=message.message_id,
                conversation_id=message.conversation_id,
                thread_id=message.thread_id,
                turn_id=message.turn_id,
                role=message.role.value,
                content=message.content,
                tool_call_id=message.tool_call_id,
                request_id=message.request_id,
                trace_id=message.trace_id,
                created_at=message.created_at,
            )
        )
        await session.flush()

    async def record_initial_run(self, session: AsyncSession, run: GraphRunRecord) -> None:
        """在受理事务内记录首个 Run，并给在线请求一段有限处理时间。"""

        turn = await session.get(TurnRecordModel, run.turn_id)
        if turn is None or turn.thread_id != run.thread_id or turn.active_run_id != run.run_id:
            raise ValueError("initial Run must match the active Turn and thread")
        if run.resumed_from_run_id is not None:
            raise ValueError("initial Run cannot resume another Run")
        # 当前内部请求最长等待 10 秒；60 秒租约给网络抖动留余量，后续长任务需心跳续租。
        session.add(
            GraphRunRecordModel(
                run_id=run.run_id,
                turn_id=run.turn_id,
                thread_id=run.thread_id,
                status=run.status.value,
                decision_profile=run.decision_profile.value,
                decision_ids=list(run.decision_ids),
                resumed_from_run_id=run.resumed_from_run_id,
                started_at=run.started_at,
                finished_at=run.finished_at,
                error_code=run.error_code,
                lease_expires_at=datetime.now(UTC) + RUN_LEASE,
                lease_version=0,
                lease_owner="ONLINE",
            )
        )
        await session.flush()

    async def claim_expired_run(self) -> tuple[ExecuteGraphTurnRequest, int] | None:
        """锁住一条租约已过期的 Run，再原子更新接管版本。

        两个恢复者同时扫描时，数据库只把同一行交给其中一个。客户原文从
        app.chat_history 读取；恢复者绝不重新插入一条客户消息。
        """

        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            row = (
                await session.execute(
                    select(GraphRunRecordModel, TurnRecordModel, ChatHistoryMessageModel)
                    .join(TurnRecordModel, TurnRecordModel.turn_id == GraphRunRecordModel.turn_id)
                    .join(
                        ChatHistoryMessageModel,
                        ChatHistoryMessageModel.message_id == TurnRecordModel.user_message_id,
                    )
                    .where(
                        GraphRunRecordModel.status == "RUNNING",
                        GraphRunRecordModel.lease_expires_at <= now,
                        TurnRecordModel.status == "RUNNING",
                        TurnRecordModel.active_run_id == GraphRunRecordModel.run_id,
                        ChatHistoryMessageModel.role == MessageRole.CUSTOMER.value,
                    )
                    .order_by(GraphRunRecordModel.lease_expires_at)
                    .limit(1)
                    .with_for_update(of=GraphRunRecordModel, skip_locked=True)
                )
            ).first()
            if row is None:
                return None
            run, turn, message = row
            # 先提交新版本，再调用 Runtime。旧请求即使稍后收到图回复也不能落库。
            run.lease_version += 1
            run.lease_owner = "RECOVERY"
            run.lease_expires_at = now + RUN_LEASE
            request = ExecuteGraphTurnRequest(
                conversation_id=turn.conversation_id,
                thread_id=turn.thread_id,
                turn_id=turn.turn_id,
                run_id=run.run_id,
                lease_version=run.lease_version,
                request_id=turn.request_id,
                trace_id=turn.trace_id,
                raw_query=message.content,
                previous_assistant_message=await self.previous_assistant_message(
                    session,
                    conversation_id=turn.conversation_id,
                    before=turn.started_at,
                ),
            )
            return request, run.lease_version

    async def renew_run_lease(self, request: ExecuteGraphTurnRequest) -> bool:
        """仅当前活跃版本可续租；旧请求失去接管权后续租无效。"""

        async with self._session_factory() as session, session.begin():
            run = await session.scalar(
                select(GraphRunRecordModel)
                .where(GraphRunRecordModel.run_id == request.run_id)
                .with_for_update()
            )
            if run is None or run.status != "RUNNING" or run.lease_version != request.lease_version:
                return False
            turn = await session.get(TurnRecordModel, run.turn_id)
            if turn is None or turn.status != "RUNNING" or turn.active_run_id != run.run_id:
                return False
            run.lease_expires_at = datetime.now(UTC) + RUN_LEASE
            return True

    async def start_recovery_run(
        self, previous: ExecuteGraphTurnRequest
    ) -> ExecuteGraphTurnRequest | None:
        """核对接管权后为同一 Turn 新建 Run；最多三次，耗尽后关闭为处理失败。"""

        async with self._session_factory() as session, session.begin():
            turn = await session.scalar(
                select(TurnRecordModel)
                .where(TurnRecordModel.turn_id == previous.turn_id)
                .with_for_update()
            )
            old_run = await session.scalar(
                select(GraphRunRecordModel)
                .where(GraphRunRecordModel.run_id == previous.run_id)
                .with_for_update()
            )
            if (
                turn is None
                or old_run is None
                or turn.status != "RUNNING"
                or turn.active_run_id != previous.run_id
                or old_run.status != "RUNNING"
                or old_run.lease_owner != "RECOVERY"
                or old_run.lease_version != previous.lease_version
            ):
                return None
            retry_scope = getattr(old_run, "resume_payload", None) or {}
            retry_query = (
                select(func.count())
                .select_from(GraphRunRecordModel)
                .where(
                    GraphRunRecordModel.turn_id == turn.turn_id,
                    GraphRunRecordModel.lease_owner == "RECOVERY",
                )
            )
            # 每次明确的人工作业单独计算恢复次数，不把前一次更正的 Run 算进本次批准。
            if retry_scope.get("decision_id"):
                retry_query = retry_query.where(
                    GraphRunRecordModel.resume_payload["decision_id"].astext
                    == retry_scope["decision_id"]
                )
            else:
                retry_query = retry_query.where(GraphRunRecordModel.resume_payload.is_(None))
            run_count = await session.scalar(retry_query)
            if run_count is not None and run_count >= MAX_RUNS_PER_TURN:
                # 重试耗尽只说明本 Turn 无法提供答复，不推断旧 HTTP 请求一定没有执行。
                now = datetime.now(UTC)
                old_run.status = "FAILED"
                old_run.error_code = "RECOVERY_RETRY_EXHAUSTED"
                old_run.finished_at = now
                turn.status = "FAILED"
                turn.completed_at = now
                return None
            new_id = f"run_{uuid4().hex}"
            now = datetime.now(UTC)
            # 当前 Run 已被接管；新 Run 成为唯一可以继续执行和落库的版本。
            # 旧 Run 不再保留 RUNNING，避免后续排查时出现两个“正在执行”的记录。
            old_run.status = "FAILED"
            old_run.error_code = "RECOVERED_BY_NEW_RUN"
            old_run.finished_at = now
            session.add(
                GraphRunRecordModel(
                    run_id=new_id,
                    turn_id=turn.turn_id,
                    thread_id=turn.thread_id,
                    status="RUNNING",
                    decision_profile=old_run.decision_profile,
                    decision_ids=[],
                    resumed_from_run_id=old_run.run_id,
                    started_at=now,
                    lease_expires_at=now + RUN_LEASE,
                    lease_version=0,
                    lease_owner="RECOVERY",
                    resume_payload=getattr(old_run, "resume_payload", None),
                )
            )
            turn.active_run_id = new_id
            return previous.model_copy(update={"run_id": new_id, "lease_version": 0})

    async def complete_customer_turn(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        run_id: str,
        content: str,
        terminal_response_path: ResponsePath,
        terminal_reason_code: str,
        context_cycle_id: str,
        expected_lease_version: int = 0,
        decision_trace: DecisionTrace | None = None,
        routing_decision: RoutingDecision | None = None,
        routing_trace: DecisionTrace | None = None,
        model_calls: tuple[ModelCallRecord, ...] = (),
        business_tool_results: tuple[BusinessToolResult, ...] = (),
        sop_snapshot: dict[str, object] | None = None,
        rag_snapshot: dict[str, object] | None = None,
        recommendation: RecommendationResult | None = None,
    ) -> CustomerTurnResult:
        """原子写入助手回复并关闭 Run/Turn；过期版本不得在恢复后补写。"""

        async with self._session_factory() as session, session.begin():
            # 先锁会话，再锁 Turn/Run；与受理新消息的锁顺序一致。
            conversation = await session.scalar(
                select(ConversationModel)
                .where(ConversationModel.conversation_id == conversation_id)
                .with_for_update()
            )
            turn = await session.scalar(
                select(TurnRecordModel).where(TurnRecordModel.turn_id == turn_id).with_for_update()
            )
            if turn is None or turn.conversation_id != conversation_id:
                raise AppError(code="TURN_NOT_FOUND", message="处理轮次不存在", status_code=404)
            run = await session.scalar(
                select(GraphRunRecordModel)
                .where(GraphRunRecordModel.run_id == run_id)
                .with_for_update()
            )
            if run is None or run.turn_id != turn_id or run.thread_id != turn.thread_id:
                raise AppError(code="RUN_MISMATCH", message="运行记录不匹配", status_code=409)
            if turn.active_run_id != run_id:
                raise AppError(code="RUN_NOT_ACTIVE", message="运行记录已被替换", status_code=409)
            if run.lease_version != expected_lease_version:
                raise AppError(
                    code="RUN_LEASE_REPLACED", message="运行记录已由恢复者接管", status_code=409
                )
            if not context_cycle_id or len(context_cycle_id) > 96:
                raise ValueError("context cycle id is invalid")
            if turn.context_cycle_id not in (None, context_cycle_id):
                raise ValueError("completed Turn and context cycle disagree")
            turn.context_cycle_id = context_cycle_id

            # 同一 Turn 的人工恢复会产生新答复；恢复者沿用同一处理编号，不重复插入。
            resume = getattr(run, "resume_payload", None) or {}
            message_id = self._assistant_message_id(turn_id, resume.get("decision_id"))

            for result in business_tool_results:
                # 客户范围再次与会话核对，不能仅凭 Runtime 回复中的 customer_id 放行。
                if (
                    conversation is None
                    or result.customer_id != conversation.customer_id
                    or result.conversation_id != conversation_id
                ):
                    raise ValueError("只读工具结果不属于当前客户会话")
                if result.run_id == run_id:
                    if result.turn_id != turn_id or result.trace_id != turn.trace_id:
                        raise ValueError("只读工具结果轮次或追踪编号不一致")
                else:
                    # 人工停点继续展示上轮已查询事实时，必须能找到完全相同的原记录。
                    original = await session.get(GraphRunRecordModel, result.run_id)
                    if (
                        original is None
                        or original.thread_id != turn.thread_id
                        or result.model_dump(mode="json")
                        not in (original.business_tool_results or [])
                    ):
                        raise ValueError("复用工具结果找不到原始持久记录")

            existing = await session.get(ChatHistoryMessageModel, message_id)
            if turn.status in {"COMPLETED", "INTERRUPTED"} and run.status == turn.status:
                if (
                    existing is None
                    or existing.turn_id != turn_id
                    or existing.role != MessageRole.ASSISTANT.value
                    or existing.content != content
                ):
                    raise ValueError("completed Turn and assistant message disagree")
                stored_outcome = (turn.terminal_response_path, turn.terminal_reason_code)
                requested_outcome = (terminal_response_path.value, terminal_reason_code)
                if stored_outcome == (None, None):
                    # 迁移前闭合的历史 Turn 没有终态列。只有同一助手消息被可靠重放时，
                    # 才使用本次已校验的 Runtime 终态补齐，不能从旧路由快照猜测。
                    turn.terminal_response_path, turn.terminal_reason_code = requested_outcome
                elif stored_outcome != requested_outcome:
                    raise ValueError("completed Turn and terminal outcome disagree")
                stored_calls = await self._conversation_model_calls(
                    session,
                    conversation_id=conversation_id,
                    until=turn.completed_at,
                    context_cycle_id=turn.context_cycle_id or context_cycle_id,
                )
                return self._turn_result(
                    turn,
                    existing,
                    token_usage=self._summarize_token_usage(stored_calls),
                )
            if turn.status != "RUNNING" or run.status != "RUNNING" or existing is not None:
                raise AppError(
                    code="TURN_STATE_CONFLICT", message="处理轮次状态不允许完成", status_code=409
                )

            now = datetime.now(UTC)
            previous_calls = await self._conversation_model_calls(
                session,
                conversation_id=conversation_id,
                context_cycle_id=context_cycle_id,
            )
            cumulative_usage = self._summarize_token_usage((*previous_calls, *model_calls))
            message = ChatHistoryMessage(
                conversation_id=conversation_id,
                thread_id=turn.thread_id,
                turn_id=turn_id,
                message_id=message_id,
                role=MessageRole.ASSISTANT,
                content=content,
                request_id=turn.request_id,
                trace_id=turn.trace_id,
                created_at=now,
            )
            await self.append_message(session, message)
            await append_event(
                session,
                conversation_id=conversation_id,
                turn_id=turn_id,
                event_type=RealtimeEventType.MESSAGE_CREATED,
                payload={
                    "message_id": message_id,
                    "role": "ASSISTANT",
                    "business_changed": bool(
                        sop_snapshot
                        and (sop_snapshot.get("business_operation") or {}).get("status")
                        == "SUCCEEDED"
                    ),
                },
            )
            # 模型调用记录与助手消息在同一事务提交，失败时不会留下孤立用量。
            for model_call in model_calls:
                if (
                    model_call.run_id != run_id
                    or model_call.turn_id != turn_id
                    or model_call.trace_id != turn.trace_id
                ):
                    raise ValueError("model call must match the completed Run and Turn")
                session.add(
                    ModelCallRecordModel(
                        call_id=model_call.call_id,
                        run_id=model_call.run_id,
                        turn_id=model_call.turn_id,
                        trace_id=model_call.trace_id,
                        purpose=model_call.purpose,
                        provider=model_call.provider,
                        model=model_call.model,
                        provider_request_id=model_call.provider_request_id,
                        status=model_call.status.value,
                        estimated_input_tokens=model_call.estimated_input_tokens,
                        input_tokens=model_call.input_tokens,
                        output_tokens=model_call.output_tokens,
                        total_tokens=model_call.total_tokens,
                        context_window_tokens=model_call.context_window_tokens,
                        latency_ms=model_call.latency_ms,
                        error_code=model_call.error_code,
                        created_at=model_call.created_at,
                    )
                )
            final_status = (
                "INTERRUPTED"
                if (
                    (sop_snapshot and sop_snapshot.get("graph_interrupted"))
                    or (rag_snapshot and rag_snapshot.get("graph_interrupted"))
                    or (
                        routing_decision is not None
                        and routing_decision.response_path is ResponsePath.HUMAN
                    )
                )
                else "COMPLETED"
            )
            if sop_snapshot and sop_snapshot.get("business_operation"):
                operation = sop_snapshot["business_operation"]
                if operation["customer_id"] != conversation.customer_id:
                    raise ValueError("操作结果不属于当前会话客户")
            run.status = final_status
            run.business_tool_results = [r.model_dump(mode="json") for r in business_tool_results]
            run.sop_snapshot = sop_snapshot
            run.rag_snapshot = rag_snapshot
            run.finished_at = now
            if decision_trace is not None:
                run.decision_profile = decision_trace.active_profile.value
                run.decision_ids = [decision_trace.decision_id]
                run.decision_trace = decision_trace.model_dump(mode="json")
            if routing_decision is not None:
                turn.routing_decision = routing_decision.model_dump(mode="json")
            turn.terminal_response_path = terminal_response_path.value
            turn.terminal_reason_code = terminal_reason_code
            if routing_trace is not None:
                run.routing_trace = routing_trace.model_dump(mode="json")
                run.decision_ids = [*run.decision_ids, routing_trace.decision_id]
            if recommendation is not None:
                if (
                    recommendation.conversation_id != conversation_id
                    or recommendation.turn_id != turn_id
                    or recommendation.trace_id != turn.trace_id
                    or (
                        recommendation.answer != content
                        and not (
                            recommendation.response_path.value == "RAG"
                            and recommendation.review_status == "DRAFT"
                            and content == RAG_REVIEW_PENDING_REPLY
                        )
                    )
                ):
                    raise ValueError("recommendation must match the released assistant message")
                session.add(
                    RecommendationRecordModel(
                        recommendation_id=recommendation.recommendation_id,
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        status=("DRAFT" if recommendation.review_status == "DRAFT" else "RELEASED"),
                        trace_id=turn.trace_id,
                        payload=recommendation.model_dump(mode="json"),
                        review_expires_at=recommendation.review_expires_at,
                        created_at=now,
                    )
                )
            turn.status = final_status
            turn.completed_at = now
            if self._quality_async_enabled:
                session.add(
                    AsyncQualityJobModel(
                        job_id=f"qj_{uuid5(NAMESPACE_URL, run_id + ':async-quality').hex}",
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        run_id=run_id,
                        trace_id=turn.trace_id,
                        status="PENDING",
                        attempts=0,
                        max_attempts=self._quality_max_attempts,
                        available_at=now,
                        created_at=now,
                    )
                )
            return CustomerTurnResult(
                conversation_id=conversation_id,
                turn_id=turn_id,
                customer_message_id=turn.user_message_id,
                status=TurnStatus(final_status),
                assistant_message=ConversationMessage(
                    message_id=message_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    role=MessageRole.ASSISTANT,
                    content=content,
                    created_at=now,
                ),
                token_usage=cumulative_usage,
            )

    async def get_turn_result(self, conversation_id: str, turn_id: str) -> CustomerTurnResult:
        """Read the authoritative application status; never infer it from a checkpoint."""

        async with self._session_factory() as session:
            turn = await session.get(TurnRecordModel, turn_id)
            if turn is None or turn.conversation_id != conversation_id:
                raise AppError(code="TURN_NOT_FOUND", message="处理轮次不存在", status_code=404)
            run = (
                await session.get(GraphRunRecordModel, turn.active_run_id)
                if turn.active_run_id
                else None
            )
            resume = getattr(run, "resume_payload", None) or {}
            assistant = await session.get(
                ChatHistoryMessageModel,
                self._assistant_message_id(turn_id, resume.get("decision_id")),
            )
            if turn.status in {"COMPLETED", "INTERRUPTED"} and assistant is None:
                raise ValueError("completed Turn has no assistant message")
            if turn.status not in {"COMPLETED", "INTERRUPTED"} and assistant is not None:
                raise ValueError("unfinished Turn already has an assistant message")
            stored_calls = await self._conversation_model_calls(
                session,
                conversation_id=conversation_id,
                until=turn.completed_at,
                context_cycle_id=turn.context_cycle_id,
            )
            return self._turn_result(
                turn,
                assistant,
                token_usage=self._summarize_token_usage(stored_calls),
            )

    async def get_conversation_token_usage(self, conversation_id: str) -> TurnTokenUsage | None:
        """读取会话当前上下文周期内所有已落库模型调用的累计用量。"""

        async with self._session_factory() as session:
            calls = await self._conversation_model_calls(session, conversation_id=conversation_id)
        return self._summarize_token_usage(calls)

    @staticmethod
    async def _conversation_model_calls(
        session: AsyncSession,
        *,
        conversation_id: str,
        until: datetime | None = None,
        context_cycle_id: str | None = None,
    ) -> tuple[ModelCallRecordModel, ...]:
        """读取当前上下文周期的模型调用，并避免把未来轮次算进旧结果。"""

        if context_cycle_id is None:
            cycle_statement = (
                select(TurnRecordModel.context_cycle_id)
                .where(
                    TurnRecordModel.conversation_id == conversation_id,
                    TurnRecordModel.context_cycle_id.is_not(None),
                )
                .order_by(TurnRecordModel.completed_at.desc().nullslast())
                .limit(1)
            )
            if until is not None:
                cycle_statement = cycle_statement.where(TurnRecordModel.completed_at <= until)
            context_cycle_id = await session.scalar(cycle_statement)
        if context_cycle_id is None:
            return ()

        statement = (
            select(ModelCallRecordModel)
            .join(TurnRecordModel, TurnRecordModel.turn_id == ModelCallRecordModel.turn_id)
            .where(
                TurnRecordModel.conversation_id == conversation_id,
                TurnRecordModel.context_cycle_id == context_cycle_id,
            )
            .order_by(ModelCallRecordModel.created_at)
        )
        if until is not None:
            statement = statement.where(ModelCallRecordModel.created_at <= until)
        return tuple((await session.scalars(statement)).all())

    @staticmethod
    def _assistant_message_id(turn_id: str, decision_id: str | None = None) -> str:
        """普通回复按 Turn 去重，人工恢复回复另按稳定的处理编号去重。"""
        identity = (
            f"assistant:{turn_id}" if decision_id is None else f"assistant:{turn_id}:{decision_id}"
        )
        return f"msg_ai_{uuid5(NAMESPACE_URL, identity).hex}"

    @staticmethod
    def _turn_result(
        turn: TurnRecordModel,
        assistant: ChatHistoryMessageModel | None,
        *,
        token_usage: TurnTokenUsage | None = None,
    ) -> CustomerTurnResult:
        """把应用库记录转换为客户可见结果，并附上可用的真实 Token 汇总。"""

        return CustomerTurnResult(
            conversation_id=turn.conversation_id,
            turn_id=turn.turn_id,
            customer_message_id=turn.user_message_id,
            status=TurnStatus(turn.status),
            assistant_message=(
                ConversationMessage(
                    message_id=assistant.message_id,
                    conversation_id=assistant.conversation_id,
                    turn_id=assistant.turn_id,
                    role=MessageRole(assistant.role),
                    content=assistant.content,
                    created_at=assistant.created_at,
                )
                if assistant is not None
                else None
            ),
            token_usage=token_usage,
        )

    @staticmethod
    def _summarize_token_usage(
        model_calls: Iterable[ModelCallRecord | ModelCallRecordModel],
    ) -> TurnTokenUsage | None:
        """累计一个上下文周期的模型用量；不同窗口并存时采用较小上限。"""

        calls = tuple(model_calls)
        usable = [call for call in calls if call.total_tokens is not None]
        windows = [
            int(call.context_window_tokens)
            for call in calls
            if call.context_window_tokens is not None
        ]
        if not usable or not windows:
            return None
        input_tokens = sum(int(call.input_tokens or 0) for call in calls)
        output_tokens = sum(int(call.output_tokens or 0) for call in calls)
        total_tokens = sum(int(call.total_tokens or 0) for call in calls)
        context_window_tokens = min(windows)
        return TurnTokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            context_window_tokens=context_window_tokens,
            used_percentage=round(total_tokens / context_window_tokens * 100, 2),
        )

    async def page(
        self,
        conversation_id: str,
        *,
        limit: int,
        before: str | None = None,
    ) -> tuple[ConversationMessage, ...]:
        """Read newest page in chronological display order, excluding internal Tool messages."""

        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        async with self._session_factory() as session:
            statement = select(ChatHistoryMessageModel).where(
                ChatHistoryMessageModel.conversation_id == conversation_id,
                ChatHistoryMessageModel.role != MessageRole.TOOL.value,
            )
            if before is not None:
                cursor = await session.get(ChatHistoryMessageModel, before)
                if (
                    cursor is None
                    or cursor.conversation_id != conversation_id
                    or cursor.role == MessageRole.TOOL.value
                ):
                    raise AppError(
                        code="MESSAGE_CURSOR_INVALID",
                        message="消息分页位置无效",
                        status_code=400,
                    )
                statement = statement.where(
                    or_(
                        ChatHistoryMessageModel.created_at < cursor.created_at,
                        and_(
                            ChatHistoryMessageModel.created_at == cursor.created_at,
                            ChatHistoryMessageModel.message_id < cursor.message_id,
                        ),
                    )
                )
            statement = statement.order_by(
                ChatHistoryMessageModel.created_at.desc(),
                ChatHistoryMessageModel.message_id.desc(),
            ).limit(limit)
            rows = (await session.scalars(statement)).all()
            return tuple(
                ConversationMessage(
                    message_id=row.message_id,
                    conversation_id=row.conversation_id,
                    turn_id=row.turn_id,
                    role=MessageRole(row.role),
                    content=row.content,
                    created_at=row.created_at,
                )
                for row in reversed(rows)
            )

    @staticmethod
    async def _require_thread(session: AsyncSession, conversation_id: str, thread_id: str) -> None:
        current_thread = await session.scalar(
            select(ConversationThreadMappingModel.thread_id).where(
                ConversationThreadMappingModel.conversation_id == conversation_id
            )
        )
        if current_thread != thread_id:
            raise ValueError("thread_id must match the conversation mapping")
