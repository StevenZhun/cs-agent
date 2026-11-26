"""坐席查看流程、发送说明并执行明确的人工处理动作。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.bff.errors import AppError
from services.bff.infrastructure.events import append_event
from services.bff.infrastructure.idempotency import PostgresIdempotencyExecutor
from services.bff.infrastructure.models import (
    ChatHistoryMessageModel,
    ConversationModel,
    ConversationThreadMappingModel,
    GraphRunRecordModel,
    TurnRecordModel,
)
from services.bff.infrastructure.repositories import SqlAlchemyObjectAuthorizer
from services.bff.infrastructure.runtime_client import (
    AiRuntimeClient,
    RuntimeExecutionRejected,
    RuntimeExecutionUncertain,
)
from services.bff.messages import CustomerMessageService
from services.bff.security import AuthPrincipal, Role, authorize_roles
from shared.contracts.conversation import ConversationMessage, RealtimeEventType
from shared.contracts.runtime import ExecuteGraphTurnRequest, GraphTerminalSnapshot
from shared.contracts.sop import (
    SopAssistanceReply,
    SopAssistanceView,
    SopReviewField,
    SopReviewRequest,
)
from shared.sop_config import SopConfig, load_sop_release, valid_sop_value


class SopAssistanceService:
    """向有客户访问权的坐席开放资料、文字回复及明确的人工处理动作。"""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        authorizer: SqlAlchemyObjectAuthorizer,
        runtime: AiRuntimeClient,
        redis: Redis,
    ) -> None:
        """复用现有数据库、授权检查和 Runtime 客户端，不另建流程引擎。"""
        self._sessions = sessions
        self._authorizer = authorizer
        self._runtime = runtime
        self._redis = redis

    async def _authorize(self, principal: AuthPrincipal, conversation_id: str) -> None:
        """先查角色，再查当前客户对象的访问权限。"""
        authorize_roles(principal, {Role.AGENT, Role.ADMIN})
        if not await self._authorizer.can_access_conversation(
            principal.subject_id, conversation_id
        ):
            raise AppError(code="OBJECT_ACCESS_DENIED", message="无权访问该会话", status_code=403)

    async def get(self, principal: AuthPrincipal, conversation_id: str) -> SopAssistanceView | None:
        """读取当前流程；图还在运行时只返回上次已提交资料，避免等待线程锁。"""
        await self._authorize(principal, conversation_id)
        async with self._sessions() as session:
            thread_id = await session.scalar(
                select(ConversationThreadMappingModel.thread_id).where(
                    ConversationThreadMappingModel.conversation_id == conversation_id
                )
            )
            latest_turn = await session.scalar(
                select(TurnRecordModel)
                .where(TurnRecordModel.conversation_id == conversation_id)
                .order_by(TurnRecordModel.started_at.desc())
                .limit(1)
            )
            processing = latest_turn is not None and latest_turn.status == "RUNNING"
            cached = await session.scalar(
                select(GraphRunRecordModel.sop_snapshot)
                .join(TurnRecordModel, TurnRecordModel.turn_id == GraphRunRecordModel.turn_id)
                .where(
                    TurnRecordModel.conversation_id == conversation_id,
                    GraphRunRecordModel.sop_snapshot.is_not(None),
                )
                .order_by(GraphRunRecordModel.finished_at.desc())
                .limit(1)
            )
        if thread_id is None:
            raise AppError(code="CONVERSATION_NOT_FOUND", message="会话不存在", status_code=404)
        if latest_turn is None:
            return None
        try:
            snapshot = (
                (GraphTerminalSnapshot.model_validate(cached) if cached else None)
                if processing
                else (await self._runtime.inspect_sop(conversation_id, thread_id))
            )
        except RuntimeExecutionUncertain as error:
            raise AppError(
                code="SOP_STATUS_UNAVAILABLE",
                message="流程状态暂时无法读取，请稍后重试",
                status_code=503,
                retryable=True,
            ) from error
        if snapshot is None:
            return None
        release = await load_sop_release(self._redis, snapshot.sop_release_version)
        progress = snapshot.sop_progress
        config = SopConfig.model_validate(release["definitions"][progress.sop_id])
        fields = {f.name: f for f in config.fields}
        reasons = {
            "SOP_REVIEW_COMPLETED": "服务请求已经人工复核并受理。",
            "SOP_COLLECT_SLOT": "正在逐项收集必要资料。",
            "SOP_INVALID_SLOT": "本轮资料未能确定，等待客户补充。",
            "SOP_CONFIRM_SLOTS": "资料齐全，等待客户确认。",
            "SOP_CANCELLED": "客户已取消当前流程。",
            "SOP_TOOL_FAILED": "已发布查询能力暂时不可用，等待人工协助。",
            "SOP_MANUAL_REVIEW": "资料已确认，等待人工坐席复核和受理。",
            "SOP_SENSITIVE_REVIEW": "消息可能包含敏感资料，需要改用受保护渠道。",
            "SOP_PREREQUISITE_REQUIRED": "客户未满足该分支的办理前置条件。",
            "SOP_UNSUPPORTED_TYPE": "当前请求类型需要由人工处理。",
            "SOP_HUMAN_REQUESTED": "客户请求人工协助。",
        }
        approval = progress.pending_approval
        expired = bool(
            approval
            and approval.status.value == "PENDING"
            and approval.expires_at
            and datetime.now(UTC) >= approval.expires_at
        )
        waiting = progress.status.value in {"SUSPENDED", "WAITING_APPROVAL"} and (
            progress.current_step_id == "manual_review"
            or snapshot.reason_code == "SOP_HUMAN_REQUESTED"
        )
        slot_values = {
            s.name: str(s.value) for s in (*progress.candidate_slots, *progress.confirmed_slots)
        }
        return SopAssistanceView(
            sop_id=progress.sop_id,
            sop_release_version=snapshot.sop_release_version,
            sop_execution_id=progress.sop_execution_id,
            turn_id=latest_turn.turn_id,
            name=config.name,
            status=progress.status,
            current_step_id=progress.current_step_id,
            reason_code=snapshot.reason_code,
            reason=reasons.get(snapshot.reason_code, "请核对已收集资料和聊天记录。"),
            fields={
                fields[s.name].label: fields[s.name].choices.get(str(s.value), str(s.value))
                for s in (*progress.candidate_slots, *progress.confirmed_slots)
                if s.name in fields
            },
            needs_manual_review=waiting,
            processing=processing,
            guidance=(
                "文字意见只用于沟通，不能替代批准或退回动作。"
                "退回更正后需要客户重新确认；批准前请核对已收集资料。"
            ),
            approval_id=approval.approval_id if approval else None,
            operation_id=approval.operation_id if approval else None,
            approval_status=approval.status if approval else None,
            can_approve=waiting
            and snapshot.reason_code == "SOP_MANUAL_REVIEW"
            and (
                approval is None
                or (
                    approval.status.value == "PENDING"
                    and (approval.expires_at is None or datetime.now(UTC) < approval.expires_at)
                )
            ),
            can_retry=False,
            approval_expired=expired,
            can_recheck=False,
            editable_fields=tuple(
                SopReviewField(
                    name=f.name, label=f.label, value=slot_values.get(f.name, ""), choices=f.choices
                )
                for f in config.fields
                if f.name in slot_values or f.name in progress.missing_slots
            ),
        )

    async def review(
        self,
        principal: AuthPrincipal,
        conversation_id: str,
        payload: SopReviewRequest,
        *,
        key: str,
        request_id: str,
        trace_id: str,
        messages: CustomerMessageService,
    ):
        """先提交坐席处理记录及新 Run，再恢复图；失败由现有租约恢复机制接管。

        会话行锁使客户发消息与坐席处理串行受理。页面上的执行编号、轮次及审批
        必须仍是当前版本。审核声明来自明确按钮，而不是客户原文或模型推断。
        """
        await self._authorize(principal, conversation_id)
        view = await self.get(principal, conversation_id)

        async def mutation(session: AsyncSession) -> dict:
            """在一个事务中保存操作者、原参数、更正原因、新执行和 SSE 事件。"""
            conversation = await session.scalar(
                select(ConversationModel)
                .where(ConversationModel.conversation_id == conversation_id)
                .with_for_update()
            )
            turn = await session.scalar(
                select(TurnRecordModel)
                .where(TurnRecordModel.conversation_id == conversation_id)
                .order_by(TurnRecordModel.started_at.desc())
                .limit(1)
                .with_for_update()
            )
            if (
                not view
                or not view.needs_manual_review
                or view.sop_execution_id != payload.sop_execution_id
                or view.turn_id != payload.turn_id
                or view.approval_id != payload.approval_id
                or conversation is None
                or conversation.status != "ACTIVE"
                or turn is None
                or turn.turn_id != payload.turn_id
                or (
                    turn.status not in {"COMPLETED", "INTERRUPTED"}
                    and not (
                        turn.status == "FAILED" and payload.action == "RETRY" and view.can_retry
                    )
                )
            ):
                raise AppError(
                    code="SOP_REVIEW_CHANGED",
                    message="流程或轮次已变化，请刷新后重新核对",
                    status_code=409,
                )
            if payload.action == "APPROVE" and not view.can_approve:
                raise AppError(
                    code="SOP_APPROVAL_NOT_ALLOWED",
                    message="资料未核对通过或审批已过期，不能批准",
                    status_code=409,
                )
            if payload.action == "RETRY" and not view.can_retry:
                raise AppError(
                    code="SOP_RETRY_NOT_ALLOWED",
                    message="当前没有结果不明的业务操作",
                    status_code=409,
                )
            if payload.action == "RECHECK" and not view.can_recheck:
                raise AppError(
                    code="SOP_RECHECK_NOT_ALLOWED",
                    message="当前流程不能重新核对办理资料",
                    status_code=409,
                )
            if view.can_retry and payload.action in {"CORRECT", "REJECT", "APPROVE", "RECHECK"}:
                raise AppError(
                    code="SOP_RESULT_UNCERTAIN",
                    message="先查询原操作结果，不能更换参数或当作未执行而取消",
                    status_code=409,
                )
            old = await session.get(GraphRunRecordModel, turn.active_run_id)
            if old is None or (
                old.status not in {"COMPLETED", "INTERRUPTED"}
                and not (old.status == "FAILED" and payload.action == "RETRY" and view.can_retry)
            ):
                raise AppError(
                    code="RUN_STATE_CONFLICT", message="流程正在处理，请稍后重试", status_code=409
                )
            if old.sop_snapshot:
                latest = GraphTerminalSnapshot.model_validate(old.sop_snapshot).sop_progress
                if (
                    latest is None
                    or latest.sop_execution_id != payload.sop_execution_id
                    or (latest.pending_approval.approval_id if latest.pending_approval else None)
                    != payload.approval_id
                ):
                    raise AppError(
                        code="SOP_REVIEW_CHANGED", message="待处理审批已变化", status_code=409
                    )
            if payload.action == "CORRECT":
                raw = await load_sop_release(self._redis, view.sop_release_version)
                config = SopConfig.model_validate(raw["definitions"][view.sop_id])
                fields = {f.name: f for f in config.fields}
                if any(
                    name not in fields or not valid_sop_value(fields[name], value)
                    for name, value in payload.corrections.items()
                ):
                    raise AppError(
                        code="SOP_CORRECTION_INVALID",
                        message="更正字段或格式不合法",
                        status_code=422,
                    )
            now = datetime.now(UTC)
            decision_id, run_id = "review_" + uuid4().hex, "run_" + uuid4().hex
            instruction = {
                **payload.model_dump(mode="json"),
                "decision_id": decision_id,
                "actor_id": principal.subject_id,
                "acted_at": now.isoformat(),
                "request_id": request_id,
                "trace_id": trace_id,
            }
            session.add(
                GraphRunRecordModel(
                    run_id=run_id,
                    turn_id=turn.turn_id,
                    thread_id=turn.thread_id,
                    status="RUNNING",
                    decision_profile=old.decision_profile,
                    decision_ids=[],
                    resumed_from_run_id=old.run_id,
                    started_at=now,
                    lease_expires_at=now + timedelta(seconds=60),
                    lease_version=0,
                    lease_owner="ONLINE",
                    resume_payload=instruction,
                )
            )
            turn.status, turn.active_run_id, turn.completed_at = "RUNNING", run_id, None
            label = {
                "APPROVE": "已复核并受理服务请求",
                "CORRECT": "已退回更正资料，需客户重新确认",
                "REJECT": "已拒绝本次办理",
                "RETRY": "正在重新核对处理结果",
                "RECHECK": "正在重新核对已收集资料",
            }[payload.action]
            message_id = "msg_" + decision_id
            session.add(
                ChatHistoryMessageModel(
                    message_id=message_id,
                    conversation_id=conversation_id,
                    thread_id=turn.thread_id,
                    turn_id=turn.turn_id,
                    role="AGENT",
                    content=label + ("。\n处理说明：" + payload.note if payload.note else "。"),
                    request_id=request_id,
                    trace_id=trace_id,
                    created_at=now,
                )
            )
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
                    "sop_execution_id": payload.sop_execution_id,
                },
            )
            return {"conversation_id": conversation_id, "turn_id": turn.turn_id, "run_id": run_id}

        accepted = await PostgresIdempotencyExecutor(self._sessions).execute(
            subject_id=principal.subject_id,
            operation="SOP_REVIEW:" + conversation_id,
            idempotency_key=key,
            request_payload=payload.model_dump(mode="json"),
            response_status=202,
            mutation=mutation,
        )
        async with self._sessions() as session:
            run = await session.get(GraphRunRecordModel, accepted.response_body["run_id"])
            turn = await session.get(TurnRecordModel, run.turn_id)
            message = await session.get(ChatHistoryMessageModel, turn.user_message_id)
            request = ExecuteGraphTurnRequest(
                conversation_id=conversation_id,
                thread_id=turn.thread_id,
                turn_id=turn.turn_id,
                run_id=run.run_id,
                lease_version=run.lease_version,
                request_id=turn.request_id,
                trace_id=turn.trace_id,
                raw_query=message.content,
            )
            finished = run.status != "RUNNING"
        if finished:
            return await messages._repository.get_turn_result(
                conversation_id, turn.turn_id
            ), accepted.replayed
        try:
            result = await messages._execute_with_heartbeat(request)
            released = await messages._release(
                request,
                result.terminal,
                context_cycle_id=result.context_cycle_id,
                decision_trace=result.decision_trace,
                routing_decision=result.routing_decision,
                routing_trace=result.routing_trace,
                model_calls=result.model_calls,
            )
        except (RuntimeExecutionUncertain, RuntimeExecutionRejected):
            released = await messages._repository.get_turn_result(conversation_id, turn.turn_id)
        return released, accepted.replayed

    async def reply(
        self,
        principal: AuthPrincipal,
        conversation_id: str,
        payload: SopAssistanceReply,
        *,
        key: str,
        request_id: str,
        trace_id: str,
    ) -> tuple[ConversationMessage, bool]:
        """幂等保存人工处理意见，通知客户；不改写候选槽位或审批状态。"""
        view = await self.get(principal, conversation_id)
        if (
            not view
            or view.sop_execution_id != payload.sop_execution_id
            or not view.needs_manual_review
        ):
            raise AppError(
                code="SOP_REVIEW_CHANGED", message="待处理流程已变化，请刷新后核对", status_code=409
            )

        async def mutation(session: AsyncSession) -> dict[str, object]:
            """锁住会话，复核运行状态；文字、操作者事件与幂等响应同一次提交。"""
            conversation = await session.scalar(
                select(ConversationModel)
                .where(ConversationModel.conversation_id == conversation_id)
                .with_for_update()
            )
            if conversation is None or conversation.status != "ACTIVE":
                raise AppError(
                    code="CONVERSATION_NOT_ACTIVE",
                    message="会话已结束，不能发送处理意见",
                    status_code=409,
                )
            turn = await session.scalar(
                select(TurnRecordModel)
                .where(TurnRecordModel.conversation_id == conversation_id)
                .order_by(TurnRecordModel.started_at.desc())
                .limit(1)
            )
            if turn is None or turn.status == "RUNNING":
                raise AppError(
                    code="TURN_IN_PROGRESS", message="请等待当前消息处理完成后回复", status_code=409
                )
            if turn.turn_id != payload.turn_id:
                raise AppError(
                    code="SOP_REVIEW_CHANGED",
                    message="客户已发送新消息，请刷新后核对",
                    status_code=409,
                )
            # 若核对后客户又发送了一轮，要求重新读取进度，而不是把意见发给新流程。
            latest = await session.get(GraphRunRecordModel, turn.active_run_id)
            if latest and latest.sop_snapshot:
                current = GraphTerminalSnapshot.model_validate(latest.sop_snapshot).sop_progress
                if (
                    current is None
                    or current.sop_execution_id != payload.sop_execution_id
                    or current.status.value not in {"SUSPENDED", "WAITING_APPROVAL"}
                ):
                    raise AppError(
                        code="SOP_REVIEW_CHANGED",
                        message="待处理流程已变化，请刷新后核对",
                        status_code=409,
                    )
            now = datetime.now(UTC)
            message = ConversationMessage(
                message_id=f"msg_{uuid4().hex}",
                conversation_id=conversation_id,
                turn_id=turn.turn_id,
                role="AGENT",
                content=payload.content,
                created_at=now,
            )
            session.add(
                ChatHistoryMessageModel(
                    **message.model_dump(exclude={"role"}),
                    role="AGENT",
                    thread_id=turn.thread_id,
                    request_id=request_id,
                    trace_id=trace_id,
                )
            )
            conversation.updated_at = now
            await append_event(
                session,
                conversation_id=conversation_id,
                turn_id=turn.turn_id,
                event_type=RealtimeEventType.MESSAGE_CREATED,
                payload={
                    "message_id": message.message_id,
                    "role": "AGENT",
                    "actor_id": principal.subject_id,
                    "sop_execution_id": payload.sop_execution_id,
                },
            )
            return message.model_dump(mode="json")

        result = await PostgresIdempotencyExecutor(self._sessions).execute(
            subject_id=principal.subject_id,
            operation="SOP_ASSISTANCE_REPLY",
            idempotency_key=key,
            request_payload={"conversation_id": conversation_id, **payload.model_dump(mode="json")},
            response_status=201,
            mutation=mutation,
        )
        return ConversationMessage.model_validate(result.response_body), result.replayed
