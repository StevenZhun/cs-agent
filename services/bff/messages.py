"""应用层受理客户消息，并创建这一轮图执行的首个 Run。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from services.bff.errors import AppError
from services.bff.infrastructure.idempotency import PostgresIdempotencyExecutor
from services.bff.infrastructure.runtime_client import (
    RuntimeExecutionRejected,
    RuntimeExecutionUncertain,
)
from services.bff.security import AuthPrincipal, ObjectAuthorizationPort
from services.content.policy_answers import load_policy_answer
from shared.contracts.conversation import CustomerTurnResult, SendMessageRequest, TurnTokenUsage
from shared.contracts.enums import (
    AnswerSourceType,
    ConversationModelProfile,
    DecisionMode,
    DecisionProfile,
    FaqMatchStatus,
    MessageRole,
    QualityAction,
    ResponsePath,
    RunStatus,
    TurnStatus,
)
from shared.contracts.quality import QualityDecision
from shared.contracts.recommendation import AnswerSource, Escalation, RecommendationResult
from shared.contracts.routing import RoutingDecision
from shared.contracts.runtime import (
    ChatHistoryMessage,
    DecisionSummary,
    DecisionTrace,
    ExecuteGraphTurnRequest,
    ExecuteGraphTurnResponse,
    GraphRunRecord,
    GraphTerminalSnapshot,
    InspectGraphTurnResponse,
    ModelCallRecord,
    PreviousAssistantMessage,
    TurnRecord,
)
from shared.guardrails import evaluate_output_content
from shared.safe_fallback import (
    HUMAN_HANDOFF_REPLY,
    RAG_REVIEW_PENDING_REPLY,
    SAFE_FALLBACK_ALLOWLIST,
)
from shared.sop_config import (
    SopConfig,
    load_sop_release,
    render_sop_lookup_message,
    render_sop_message,
)
from shared.sop_review import render_review_result
from shared.sop_tool_output import render_tool_reply

logger = logging.getLogger(__name__)


class MessageAcceptanceRepositoryPort(Protocol):
    """客户消息受理、运行恢复和终态持久化所需的仓储能力。"""

    async def claim_expired_run(self) -> tuple[ExecuteGraphTurnRequest, int] | None:
        """接管一条到期运行，并返回原请求与新的接管版本。"""
        ...

    async def renew_run_lease(self, request: ExecuteGraphTurnRequest) -> bool:
        """执行时间较长时，只延长当前活跃 Run 的租约。"""
        ...

    async def start_recovery_run(
        self, previous: ExecuteGraphTurnRequest
    ) -> ExecuteGraphTurnRequest | None:
        """从已接管但无终态的运行创建下一次尝试。"""
        ...

    async def lock_active_thread(
        self, session: AsyncSession, *, conversation_id: str, message_id: str
    ) -> str: ...

    async def record_customer_turn(
        self, session: AsyncSession, *, turn: TurnRecord, message: ChatHistoryMessage
    ) -> None: ...

    async def record_initial_run(self, session: AsyncSession, run: GraphRunRecord) -> None: ...

    async def previous_assistant_message(
        self,
        session: AsyncSession,
        *,
        conversation_id: str,
        before: datetime,
    ) -> PreviousAssistantMessage | None: ...

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
        recommendation: RecommendationResult | None = None,
    ) -> CustomerTurnResult: ...

    async def get_turn_result(self, conversation_id: str, turn_id: str) -> CustomerTurnResult: ...

    async def get_conversation_token_usage(self, conversation_id: str) -> TurnTokenUsage | None: ...


class GraphExecutionPort(Protocol):
    """客户消息服务调用图执行和只读检查所需的最小方法约定。"""

    async def execute(self, request: ExecuteGraphTurnRequest) -> ExecuteGraphTurnResponse: ...

    async def inspect(self, request: ExecuteGraphTurnRequest) -> InspectGraphTurnResponse: ...


@dataclass(frozen=True)
class AcceptedCustomerTurn:
    """已经提交到应用库的客户输入；受理成功不代表助手已经回答。"""

    conversation_id: str
    thread_id: str
    turn_id: str
    run_id: str
    message_id: str
    request_id: str
    trace_id: str
    raw_query: str
    previous_assistant_message: PreviousAssistantMessage | None = None
    conversation_model_profile: ConversationModelProfile = ConversationModelProfile.DEFAULT
    decision_mode: DecisionMode = DecisionMode.LLM

    def graph_request(self) -> ExecuteGraphTurnRequest:
        """把受理记录转换为 Runtime 能理解的内部请求。"""

        return ExecuteGraphTurnRequest(
            conversation_id=self.conversation_id,
            thread_id=self.thread_id,
            turn_id=self.turn_id,
            run_id=self.run_id,
            request_id=self.request_id,
            trace_id=self.trace_id,
            raw_query=self.raw_query,
            message_id=self.message_id,
            previous_assistant_message=self.previous_assistant_message,
            conversation_model_profile=self.conversation_model_profile,
            shadow_decision_profile=(
                DecisionProfile.JEV
                if self.decision_mode is DecisionMode.LLM_WITH_JEV_COMPARE
                else None
            ),
        )


class CustomerMessageService:
    """先受理客户原文，再调用图；图有可信终态后才保存客户可见回复。"""

    def __init__(
        self,
        repository: MessageAcceptanceRepositoryPort,
        authorizer: ObjectAuthorizationPort,
        idempotency: PostgresIdempotencyExecutor,
        graph: GraphExecutionPort,
        sop_redis: Redis | None = None,
        rag_review_ttl_minutes: int = 30,
    ) -> None:
        self._repository = repository
        self._authorizer = authorizer
        self._idempotency = idempotency
        self._graph = graph
        self._sop_redis = sop_redis
        self._rag_review_ttl = timedelta(minutes=rag_review_ttl_minutes)

    async def accept(
        self,
        principal: AuthPrincipal,
        conversation_id: str,
        request: SendMessageRequest,
        *,
        idempotency_key: str,
        request_id: str,
        trace_id: str,
    ) -> tuple[AcceptedCustomerTurn, bool]:
        """持久化受理客户输入；图执行要等这笔事务提交后才开始。"""

        if not await self._authorizer.can_access_conversation(
            principal.subject_id, conversation_id
        ):
            raise AppError(
                code="OBJECT_ACCESS_DENIED", message="无权访问该会话对象", status_code=403
            )

        async def mutation(session: AsyncSession) -> dict[str, object]:
            """在幂等执行器提供的同一数据库事务中受理一条新消息。

            执行器只在幂等键首次出现时调用这里：先锁定会话并确认没有
            其他正在处理的 Turn，再生成 ID，写入客户原文、Turn 和首个
            Run。返回的字典仅包含 ID，由执行器连同这些记录一起提交为
            幂等结果；这里不调用 AI Runtime，也不生成助手回复。
            """

            thread_id = await self._repository.lock_active_thread(
                session, conversation_id=conversation_id, message_id=request.message_id
            )
            now = datetime.now(UTC)
            previous_assistant_message = await self._repository.previous_assistant_message(
                session,
                conversation_id=conversation_id,
                before=now,
            )
            turn_id = f"turn_{uuid4().hex}"
            run_id = f"run_{uuid4().hex}"
            accepted = AcceptedCustomerTurn(
                conversation_id=conversation_id,
                thread_id=thread_id,
                turn_id=turn_id,
                run_id=run_id,
                message_id=request.message_id,
                request_id=request_id,
                trace_id=trace_id,
                raw_query=request.content,
                conversation_model_profile=request.conversation_model_profile,
                decision_mode=request.decision_mode,
            )
            turn = TurnRecord(
                turn_id=turn_id,
                conversation_id=conversation_id,
                thread_id=thread_id,
                request_id=request_id,
                trace_id=trace_id,
                user_message_id=request.message_id,
                status=TurnStatus.RUNNING,
                active_run_id=run_id,
                started_at=now,
            )
            message = ChatHistoryMessage(
                conversation_id=conversation_id,
                thread_id=thread_id,
                turn_id=turn_id,
                message_id=request.message_id,
                role=MessageRole.CUSTOMER,
                content=request.content,
                request_id=request_id,
                trace_id=trace_id,
                created_at=now,
            )
            run = GraphRunRecord(
                run_id=run_id,
                turn_id=turn_id,
                thread_id=thread_id,
                status=RunStatus.RUNNING,
                decision_profile=DecisionProfile.BASELINE,
                started_at=now,
            )
            await self._repository.record_customer_turn(session, turn=turn, message=message)
            await self._repository.record_initial_run(session, run)
            # The idempotency result stores identifiers, not a second copy of raw text.
            return {
                "conversation_id": accepted.conversation_id,
                "thread_id": accepted.thread_id,
                "turn_id": accepted.turn_id,
                "run_id": accepted.run_id,
                "message_id": accepted.message_id,
                "request_id": accepted.request_id,
                "trace_id": accepted.trace_id,
                "conversation_model_profile": accepted.conversation_model_profile.value,
                "decision_mode": accepted.decision_mode.value,
                "previous_assistant_message": (
                    previous_assistant_message.model_dump(mode="json")
                    if previous_assistant_message
                    else None
                ),
            }

        # execute() owns session.begin(); it calls mutation(session) before committing.
        result = await self._idempotency.execute(
            subject_id=principal.subject_id,
            operation=f"ACCEPT_CUSTOMER_MESSAGE:{conversation_id}",
            idempotency_key=idempotency_key,
            request_payload={
                "conversation_id": conversation_id,
                **request.model_dump(mode="json"),
            },
            response_status=202,
            mutation=mutation,
        )
        return (
            AcceptedCustomerTurn(
                **{
                    **result.response_body,
                    "previous_assistant_message": (
                        PreviousAssistantMessage.model_validate(
                            result.response_body["previous_assistant_message"]
                        )
                        if result.response_body.get("previous_assistant_message")
                        else None
                    ),
                    "conversation_model_profile": ConversationModelProfile(
                        result.response_body["conversation_model_profile"]
                    ),
                    "decision_mode": DecisionMode(result.response_body["decision_mode"]),
                },
                raw_query=request.content,
            ),
            result.replayed,
        )

    async def execute(self, accepted: AcceptedCustomerTurn) -> ExecuteGraphTurnResponse:
        """受理事务已经结束；执行图时按需续租，响应不明时保留 Run。"""

        return await self._execute_with_heartbeat(accepted.graph_request())

    async def _execute_with_heartbeat(
        self, request: ExecuteGraphTurnRequest
    ) -> ExecuteGraphTurnResponse:
        """图调用超过十五秒才续租；短请求不会多做一次数据库查询。"""

        stop = asyncio.Event()

        async def heartbeat() -> None:
            """仅在图仍运行时延长当前版本的租约，失去接管权就停止。"""

            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=15)
                except TimeoutError:
                    try:
                        if not await self._repository.renew_run_lease(request):
                            return
                    except Exception:
                        logger.exception("Run heartbeat failed for run_id=%s", request.run_id)
                        return

        task = asyncio.create_task(heartbeat())
        try:
            return await self._graph.execute(request)
        finally:
            stop.set()
            await task

    async def inspect_checkpoint(self, accepted: AcceptedCustomerTurn) -> InspectGraphTurnResponse:
        """响应不明时只读 checkpoint；不会再次执行图节点。"""

        return await self._graph.inspect(accepted.graph_request())

    async def send(
        self,
        principal: AuthPrincipal,
        conversation_id: str,
        request: SendMessageRequest,
        *,
        idempotency_key: str,
        request_id: str,
        trace_id: str,
    ) -> tuple[CustomerTurnResult, bool]:
        """处理客户消息，只放行经检查的回复；客户消息本身不能批准业务动作。"""

        accepted, replayed = await self.accept(
            principal,
            conversation_id,
            request,
            idempotency_key=idempotency_key,
            request_id=request_id,
            trace_id=trace_id,
        )
        current = await self._repository.get_turn_result(conversation_id, accepted.turn_id)
        if current.status in {TurnStatus.COMPLETED, TurnStatus.INTERRUPTED}:
            return current, replayed

        if replayed:
            inspected = await self._inspect_or_none(accepted)
            if inspected is not None and inspected.terminal is not None:
                return await self._release(
                    accepted,
                    inspected.terminal,
                    context_cycle_id=inspected.snapshot.context_cycle_id,
                    decision_trace=inspected.decision_trace,
                    routing_decision=inspected.routing_decision,
                    routing_trace=inspected.routing_trace,
                    model_calls=inspected.model_calls,
                ), replayed

        try:
            executed = await self.execute(accepted)
            return await self._release(
                accepted,
                executed.terminal,
                context_cycle_id=executed.context_cycle_id,
                decision_trace=executed.decision_trace,
                routing_decision=executed.routing_decision,
                routing_trace=executed.routing_trace,
                model_calls=executed.model_calls,
            ), replayed
        except (RuntimeExecutionUncertain, RuntimeExecutionRejected):
            logger.warning("Graph result unresolved for turn_id=%s", accepted.turn_id)
            inspected = await self._inspect_or_none(accepted)
            if inspected is not None and inspected.terminal is not None:
                return await self._release(
                    accepted,
                    inspected.terminal,
                    context_cycle_id=inspected.snapshot.context_cycle_id,
                    decision_trace=inspected.decision_trace,
                    routing_decision=inspected.routing_decision,
                    routing_trace=inspected.routing_trace,
                    model_calls=inspected.model_calls,
                ), replayed
            return (
                await self._repository.get_turn_result(conversation_id, accepted.turn_id),
                replayed,
            )

    async def get_result(
        self, principal: AuthPrincipal, conversation_id: str, turn_id: str
    ) -> CustomerTurnResult:
        """核对访问权限后，从应用消息库读取客户可见的 Turn 状态。"""
        if not await self._authorizer.can_access_conversation(
            principal.subject_id, conversation_id
        ):
            raise AppError(
                code="OBJECT_ACCESS_DENIED", message="无权访问该会话对象", status_code=403
            )
        return await self._repository.get_turn_result(conversation_id, turn_id)

    async def get_token_usage(
        self, principal: AuthPrincipal, conversation_id: str
    ) -> TurnTokenUsage | None:
        """读取当前主体有权访问会话的累计 Token 用量。"""

        if not await self._authorizer.can_access_conversation(
            principal.subject_id, conversation_id
        ):
            raise AppError(
                code="OBJECT_ACCESS_DENIED", message="无权访问该会话对象", status_code=403
            )
        return await self._repository.get_conversation_token_usage(conversation_id)

    async def recover_one(self) -> bool:
        """接管超时 Run；已有结果就补记，未完成则沿用已保存指令和原操作编号恢复。"""

        claim = await self._repository.claim_expired_run()
        if claim is None:
            return False
        request, lease_version = claim
        try:
            inspected = await self._graph.inspect(request)
        except RuntimeExecutionUncertain:
            logger.warning("Recovery inspection unresolved for run_id=%s", request.run_id)
            return True
        except RuntimeExecutionRejected as error:
            if error.status_code != 409:
                # 401 说明服务间凭据配置错误，不能盲目创建新 Run。
                logger.warning("Recovery inspection rejected for run_id=%s", request.run_id)
                return True
            # 409 的常见情况是：图还没有写入当前 Turn 的 checkpoint，
            # 只保留了同一会话上一轮的终态。此时应进入下面的恢复执行，
            # 而不是把这条 Run 永久留在 RUNNING。
            inspected = None
        if inspected is not None and inspected.terminal is not None:
            await self._release(
                request,
                inspected.terminal,
                context_cycle_id=inspected.snapshot.context_cycle_id,
                expected_lease_version=lease_version,
                decision_trace=inspected.decision_trace,
                routing_decision=inspected.routing_decision,
                routing_trace=inspected.routing_trace,
                model_calls=inspected.model_calls,
            )
            return True

        # 只对无业务副作用的最小图自动续跑；包含业务工具的流程必须依赖
        # 稳定操作号和结果核对，不能在这里盲目重放。
        next_request = await self._repository.start_recovery_run(request)
        if next_request is None:
            return True
        try:
            executed = await self._execute_with_heartbeat(next_request)
            await self._release(
                next_request,
                executed.terminal,
                context_cycle_id=executed.context_cycle_id,
                decision_trace=executed.decision_trace,
                routing_decision=executed.routing_decision,
                routing_trace=executed.routing_trace,
                model_calls=executed.model_calls,
            )
        except (RuntimeExecutionUncertain, RuntimeExecutionRejected):
            logger.warning("Resumed Run result unresolved for run_id=%s", next_request.run_id)
            try:
                latest = await self._graph.inspect(next_request)
            except (RuntimeExecutionUncertain, RuntimeExecutionRejected):
                return True
            if latest.terminal is not None:
                await self._release(
                    next_request,
                    latest.terminal,
                    context_cycle_id=latest.snapshot.context_cycle_id,
                    decision_trace=latest.decision_trace,
                    routing_decision=latest.routing_decision,
                    routing_trace=latest.routing_trace,
                    model_calls=latest.model_calls,
                )
        return True

    async def _inspect_or_none(
        self, accepted: AcceptedCustomerTurn
    ) -> InspectGraphTurnResponse | None:
        """只读取 checkpoint 终态；读取失败不等于图未执行。"""
        try:
            return await self.inspect_checkpoint(accepted)
        except (RuntimeExecutionUncertain, RuntimeExecutionRejected):
            logger.warning("Graph inspection unresolved for turn_id=%s", accepted.turn_id)
            return None

    async def _release(
        self,
        accepted: AcceptedCustomerTurn | ExecuteGraphTurnRequest,
        terminal: GraphTerminalSnapshot,
        *,
        expected_lease_version: int = 0,
        decision_trace: DecisionTrace | None = None,
        routing_decision: RoutingDecision | None = None,
        routing_trace: DecisionTrace | None = None,
        model_calls: tuple[ModelCallRecord, ...] = (),
        context_cycle_id: str,
    ) -> CustomerTurnResult:
        """核对安全提示、FAQ、澄清和 SOP 模板，再保存允许发出的回复。"""

        safe_fallback = (
            terminal.response_path is ResponsePath.SAFE_FALLBACK
            and terminal.reason_code in SAFE_FALLBACK_ALLOWLIST
            and terminal.content in SAFE_FALLBACK_ALLOWLIST[terminal.reason_code]
        )
        safe_policy_answer = False
        if (
            terminal.response_path is ResponsePath.SAFE_FALLBACK
            and terminal.reason_code == "POLICY_ANSWER"
            and terminal.policy_intent_id
            and terminal.policy_answer_version
            and terminal.answer_content_hash
            and routing_decision is not None
            and routing_decision.response_path is ResponsePath.SAFE_FALLBACK
            and routing_decision.request_kind.value == "HIGH_RISK"
            and routing_decision.intent_decision.intent_id == terminal.policy_intent_id
        ):
            try:
                published = await load_policy_answer(self._sop_redis, terminal.policy_intent_id)
                safe_policy_answer = bool(
                    published
                    and published["policy_answer_version"] == terminal.policy_answer_version
                    and published["answer_content_hash"] == terminal.answer_content_hash
                    and published["answer_text"] == terminal.content
                )
            except (ValueError, RedisError, TypeError, KeyError):
                safe_policy_answer = False
        faq = terminal.faq_resolution
        safe_faq = (
            terminal.response_path is ResponsePath.FAQ
            and terminal.reason_code in {"FAQ_EXACT_MATCH", "FAQ_SEMANTIC_MATCH"}
            and faq is not None
            and faq.match_status is FaqMatchStatus.MATCHED
            and faq.answer == terminal.content
            and faq.answer_id is not None
            and faq.answer_source_ref is not None
            and faq.answer_content_hash is not None
        )
        safe_clarification = (
            terminal.response_path is ResponsePath.CLARIFICATION
            and terminal.reason_code
            in {
                "QUERY_AMBIGUOUS",
                "INTENT_AMBIGUOUS",
                "FAQ_AMBIGUOUS",
                "HISTORY_CONTEXT_REQUIRED",
            }
            and terminal.pending_clarification is not None
            and terminal.pending_clarification.question == terminal.content
        )
        safe_human = (
            terminal.response_path is ResponsePath.HUMAN
            and terminal.reason_code == "HUMAN_HANDOFF_REQUESTED"
            and terminal.content == HUMAN_HANDOFF_REPLY
            and routing_decision is not None
            and routing_decision.response_path is ResponsePath.HUMAN
            and "CONTROL:HUMAN" in routing_decision.scope_reason_codes
        )
        rag = terminal.rag_draft
        safe_rag = (
            terminal.response_path is ResponsePath.RAG
            and terminal.reason_code == "RAG_REVIEW_REQUIRED"
            and terminal.content == RAG_REVIEW_PENDING_REPLY
            and terminal.retrieval_result is not None
            and terminal.retrieval_result.assessment.status.value == "SUFFICIENT"
            and rag is not None
            and rag.retrieval_id == terminal.retrieval_result.retrieval_id
        )
        safe_sop = False
        if terminal.response_path is ResponsePath.SOP and terminal.sop_progress is not None:
            try:
                # 用同一固定模板重建文字。未知流程、非法槽位或任意改写都不能放行。
                release = await load_sop_release(self._sop_redis, terminal.sop_release_version)
                payload = release.get("definitions", {}).get(terminal.sop_progress.sop_id)
                if payload:
                    config = SopConfig.model_validate(payload)
                    display_progress = terminal.sop_progress
                    if display_progress.status.value == "WAITING_APPROVAL":
                        display_progress = display_progress.model_copy(
                            update={"status": type(display_progress.status).SUSPENDED}
                        )
                    checked_content = (
                        render_review_result(
                            config,
                            terminal.sop_progress,
                            terminal.business_operation,
                            terminal.business_tool_results,
                        )
                        if terminal.business_operation
                        else (
                            render_sop_lookup_message(
                                config,
                                display_progress,
                                terminal.reason_code,
                                terminal.pending_sop_lookup,
                            )
                            if terminal.reason_code.startswith("SOP_LOOKUP_")
                            else render_tool_reply(
                                config,
                                display_progress,
                                terminal.business_tool_results,
                                terminal.reason_code,
                            )
                            if terminal.business_tool_results
                            else render_sop_message(
                                config,
                                display_progress,
                                terminal.reason_code,
                                include_notice=terminal.sop_notice_included,
                            )
                        )
                    )
                    safe_sop = terminal.content == checked_content
            except (ValueError, RedisError, KeyError, TypeError):
                safe_sop = False
        output_quality = evaluate_output_content(
            terminal.content,
            decision_id=f"output-quality:{accepted.run_id}",
        )
        if (
            (
                not safe_fallback
                and not safe_policy_answer
                and not safe_faq
                and not safe_clarification
                and not safe_human
                and not safe_sop
                and not safe_rag
            )
            or not terminal.content.strip()
            or output_quality.action is not QualityAction.PASS
        ):
            raise AppError(
                code="OUTPUT_NOT_RELEASABLE",
                message="当前回复未通过安全检查",
                status_code=503,
                retryable=True,
            )
        recommendation = None
        if safe_faq:
            if routing_decision is None or routing_trace is None:
                raise AppError(
                    code="OUTPUT_NOT_RELEASABLE",
                    message="FAQ 回复缺少路由依据",
                    status_code=503,
                    retryable=True,
                )
            recommendation = self._faq_recommendation(
                accepted,
                terminal,
                routing_decision,
                routing_trace,
                output_quality,
            )
        if safe_rag:
            if routing_decision is None or routing_trace is None:
                raise AppError(
                    code="OUTPUT_NOT_RELEASABLE",
                    message="RAG 草稿缺少路由依据",
                    status_code=503,
                    retryable=True,
                )
            recommendation = self._rag_recommendation(
                accepted,
                terminal,
                routing_decision,
                routing_trace,
                output_quality,
            )
        try:
            completion = {
                "conversation_id": accepted.conversation_id,
                "turn_id": accepted.turn_id,
                "run_id": accepted.run_id,
                "content": terminal.content,
                "terminal_response_path": terminal.response_path,
                "terminal_reason_code": terminal.reason_code,
                "context_cycle_id": context_cycle_id,
                "expected_lease_version": expected_lease_version,
                "decision_trace": decision_trace,
                "routing_decision": routing_decision,
                "routing_trace": routing_trace,
                "recommendation": recommendation,
            }
            if model_calls:
                completion["model_calls"] = model_calls
            if terminal.business_tool_results:
                completion["business_tool_results"] = terminal.business_tool_results
            if terminal.sop_progress is not None:
                completion["sop_snapshot"] = terminal.model_dump(mode="json")
            if terminal.response_path is ResponsePath.RAG:
                completion["rag_snapshot"] = terminal.model_dump(mode="json")
            return await self._repository.complete_customer_turn(**completion)
        except AppError as error:
            if error.code not in {"RUN_LEASE_REPLACED", "RUN_NOT_ACTIVE"}:
                raise
            # 老工作者被接管后只读新状态，不再尝试写第二条助手消息。
            return await self._repository.get_turn_result(
                accepted.conversation_id, accepted.turn_id
            )

    def _rag_recommendation(
        self,
        accepted: AcceptedCustomerTurn | ExecuteGraphTurnRequest,
        terminal: GraphTerminalSnapshot,
        routing_decision: RoutingDecision,
        routing_trace: DecisionTrace,
        quality_decision: QualityDecision,
    ) -> RecommendationResult:
        """把通过引用校验的 RAG 草稿保存为坐席待审核推荐，不写入客户答案。"""

        draft = terminal.rag_draft
        retrieval = terminal.retrieval_result
        if draft is None or retrieval is None:
            raise ValueError("RAG recommendation requires retrieval and draft")
        return RecommendationResult(
            request_id=accepted.request_id,
            trace_id=accepted.trace_id,
            recommendation_id=f"rec_{uuid5(NAMESPACE_URL, 'rag:' + accepted.turn_id).hex}",
            conversation_id=accepted.conversation_id,
            turn_id=accepted.turn_id,
            response_path=ResponsePath.RAG,
            intent_decision=routing_decision.intent_decision,
            decision_summary=DecisionSummary(
                decision_id=routing_trace.decision_id,
                active_profile=routing_trace.active_profile,
                fallback_from=routing_trace.fallback_from,
                fallback_reason=routing_trace.fallback_reason,
            ),
            answer=draft.answer,
            rag_draft_id=draft.draft_id,
            retrieval_id=draft.retrieval_id,
            faq_resolution_summary=terminal.faq_resolution,
            retrieval_assessment=retrieval.assessment,
            answer_sources=tuple(
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
            quality_decision=quality_decision.model_copy(
                update={
                    "reason_codes": (
                        *quality_decision.reason_codes,
                        "EVIDENCE_VALIDATED_MANUAL_REVIEW_REQUIRED",
                    )
                }
            ),
            notices=("该草稿仅供坐席审核，客户尚未看到生成内容。",),
            escalation=Escalation(required=False),
            review_status="DRAFT",
            review_expires_at=datetime.now(UTC) + self._rag_review_ttl,
        )

    @staticmethod
    def _faq_recommendation(
        accepted: AcceptedCustomerTurn | ExecuteGraphTurnRequest,
        terminal: GraphTerminalSnapshot,
        routing_decision: RoutingDecision,
        routing_trace: DecisionTrace,
        quality_decision: QualityDecision,
    ) -> RecommendationResult:
        """把已通过检查的 FAQ 终态转换成坐席可查看、可反馈的推荐。"""

        faq = terminal.faq_resolution
        if faq is None or faq.answer_content_hash is None or faq.faq_item_id is None:
            raise ValueError("FAQ recommendation requires a complete FAQ result")
        return RecommendationResult(
            request_id=accepted.request_id,
            trace_id=accepted.trace_id,
            recommendation_id=f"rec_{uuid5(NAMESPACE_URL, 'faq:' + accepted.turn_id).hex}",
            conversation_id=accepted.conversation_id,
            turn_id=accepted.turn_id,
            response_path=ResponsePath.FAQ,
            intent_decision=routing_decision.intent_decision,
            decision_summary=DecisionSummary(
                decision_id=routing_trace.decision_id,
                active_profile=routing_trace.active_profile,
                fallback_from=routing_trace.fallback_from,
                fallback_reason=routing_trace.fallback_reason,
            ),
            answer=terminal.content,
            faq_resolution_summary=faq,
            answer_sources=(
                AnswerSource(
                    source_type=AnswerSourceType.FAQ,
                    source_id=faq.faq_item_id,
                    version=faq.faq_version,
                    title=faq.faq_title,
                    content_hash=faq.answer_content_hash,
                ),
            ),
            quality_decision=quality_decision.model_copy(
                update={
                    "reason_codes": (
                        *quality_decision.reason_codes,
                        f"PUBLISHED_{terminal.reason_code}",
                    )
                }
            ),
            escalation=Escalation(required=False),
        )
