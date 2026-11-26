"""核对已受理的客户轮次，再交给可恢复的最小图执行。"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from langgraph.types import Command
from psycopg import AsyncConnection
from psycopg import Error as PsycopgError

from services.ai_runtime.persistence.artifacts import ArtifactValidationError
from services.ai_runtime.persistence.checkpoint import GraphRuntime, psycopg_url
from services.ai_runtime.query.message_state import prompt_message_dicts
from services.ai_runtime.sop.artifacts import load_tool_results
from shared.contracts.artifact import RuntimeArtifactRef
from shared.contracts.enums import TraceSpanStatus
from shared.contracts.faq import FaqResolutionResult
from shared.contracts.observability import TraceSpan
from shared.contracts.rag import RagDraft, RagReviewOutcome
from shared.contracts.retrieval import RetrievalResult
from shared.contracts.routing import RoutingDecision
from shared.contracts.runtime import (
    DecisionTrace,
    ExecuteGraphTurnRequest,
    ExecuteGraphTurnResponse,
    GraphPreparationSnapshot,
    GraphTerminalSnapshot,
    InspectGraphTurnResponse,
    ModelCallRecord,
)
from shared.observability import emit_structured_event


class RuntimeTurnError(Exception):
    """内部调用传来的轮次与已落库记录不一致，不能执行图。"""


def checkpoint_graph_input(
    request: ExecuteGraphTurnRequest,
    *,
    started_at: datetime,
    customer_id: str,
    channel: str,
) -> dict[str, object]:
    """把内部请求转换为只含基础类型的图输入，避免 checkpoint 保存自定义枚举。"""

    return {
        **request.model_dump(mode="json"),
        "turn_started_at": started_at.isoformat(),
        "business_customer_id": customer_id,
        "channel": channel,
    }


class RuntimeTurnProcessor:
    """同线程串行执行图，并核对应用库中的消息、轮次和执行权。"""

    def __init__(self, runtime: GraphRuntime, database_url: str) -> None:
        """保存图运行时和 PostgreSQL 连接地址。"""
        self._runtime = runtime
        self._database_dsn = psycopg_url(database_url)

    async def _persist_trace_spans(
        self,
        *,
        request: ExecuteGraphTurnRequest,
        state: dict,
        started_at: datetime,
        elapsed_ms: int,
    ) -> None:
        """批量保存本 Run 的节点耗时；观测失败不能改变客户答复。"""

        repository = getattr(self._runtime, "trace_repository", None)
        if repository is None:
            return
        raw_spans = [
            item
            for item in state.get("trace_spans", [])
            if isinstance(item, dict) and item.get("run_id") == request.run_id
        ]
        finished_at = datetime.now(UTC)
        raw_spans.append(
            TraceSpan(
                span_id=f"span_{uuid5(NAMESPACE_URL, f'{request.run_id}:runtime_total').hex}",
                trace_id=request.trace_id,
                conversation_id=request.conversation_id,
                thread_id=request.thread_id,
                turn_id=request.turn_id,
                run_id=request.run_id,
                component="RUNTIME",
                operation="runtime_total",
                status=TraceSpanStatus.COMPLETED,
                started_at=started_at,
                finished_at=finished_at,
                latency_ms=elapsed_ms,
                attributes={
                    "response_path": str(state.get("terminal_response_path") or "UNKNOWN"),
                    "reason_code": str(state.get("terminal_reason_code") or "UNKNOWN"),
                },
            ).model_dump(mode="json")
        )
        try:
            spans = tuple(TraceSpan.model_validate(item) for item in raw_spans)
            await repository.save_many(spans)
        except (PsycopgError, OSError, TypeError, ValueError) as error:
            emit_structured_event(
                "trace_span_persistence_failed",
                trace_id=request.trace_id,
                run_id=request.run_id,
                error_code=type(error).__name__.upper(),
            )

    async def _artifact_payload(
        self,
        values: dict,
        *,
        legacy_field: str,
        reference_field: str,
        expected_kind: str,
    ) -> object | None:
        """优先按引用回装，旧 checkpoint 没有引用时继续读取原字段。"""

        if values.get(reference_field):
            repository = getattr(self._runtime, "artifact_repository", None)
            if repository is None:
                raise RuntimeTurnError("运行产物仓库不可用")
            try:
                return await repository.load(
                    values[reference_field],
                    authorized_conversation_id=str(values["conversation_id"]),
                    expected_kind=expected_kind,
                )
            except (ArtifactValidationError, KeyError, TypeError, ValueError, OSError) as error:
                raise RuntimeTurnError("运行产物缺失、过期或校验失败") from error
        return values.get(legacy_field)

    async def _artifact_payloads(
        self,
        values: dict,
        *specifications: tuple[str, str, str, str],
    ) -> dict[str, object | None]:
        """一次回装同一状态所需的多份产物，未迁移字段继续读取旧正文。"""

        payloads = {
            result_name: values.get(legacy_field)
            for result_name, legacy_field, _reference_field, _expected_kind in specifications
        }
        references: list[RuntimeArtifactRef] = []
        result_by_artifact_id: dict[str, str] = {}
        expected_kinds: dict[str, str] = {}
        for result_name, _legacy_field, reference_field, expected_kind in specifications:
            raw_reference = values.get(reference_field)
            if not raw_reference:
                continue
            reference = RuntimeArtifactRef.model_validate(raw_reference)
            references.append(reference)
            result_by_artifact_id[reference.artifact_id] = result_name
            expected_kinds[reference.artifact_id] = expected_kind
        if not references:
            return payloads
        repository = getattr(self._runtime, "artifact_repository", None)
        if repository is None:
            raise RuntimeTurnError("运行产物仓库不可用")
        try:
            loaded = await repository.load_many(
                tuple(references),
                authorized_conversation_id=str(values["conversation_id"]),
                expected_kinds=expected_kinds,
            )
        except (ArtifactValidationError, KeyError, TypeError, ValueError, OSError) as error:
            raise RuntimeTurnError("运行产物缺失、过期或校验失败") from error
        for artifact_id, payload in loaded.payloads.items():
            payloads[result_by_artifact_id[artifact_id]] = payload
        return payloads

    @asynccontextmanager
    async def _verified_turn(
        self, request: ExecuteGraphTurnRequest
    ) -> AsyncIterator[tuple[datetime, str, str, dict | None]]:
        """锁定图线程，核对当前 Run 的版本、租约和客户原文。"""

        # 同一把数据库锁会一直持有到图执行完成，避免同一线程同时推进两轮。
        # 哈希碰撞最多让无关线程短暂排队，不会把它们的状态混在一起。
        async with await AsyncConnection.connect(self._database_dsn) as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (request.thread_id,),
            )
            cursor = await connection.execute(
                "SELECT h.content, h.message_id, t.started_at, c.customer_id, c.channel, "
                "r.resume_payload "
                "FROM app.turn_record AS t "
                "JOIN app.conversation AS c ON c.conversation_id = t.conversation_id "
                "JOIN app.conversation_thread_mapping AS m "
                "ON m.conversation_id = t.conversation_id AND m.thread_id = t.thread_id "
                "JOIN app.chat_history AS h ON h.message_id = t.user_message_id "
                "AND h.turn_id = t.turn_id AND h.conversation_id = t.conversation_id "
                "AND h.thread_id = t.thread_id AND h.role = 'CUSTOMER' "
                "JOIN app.graph_run_record AS r ON r.turn_id = t.turn_id "
                "WHERE t.turn_id = %s AND t.conversation_id = %s AND t.thread_id = %s "
                "AND t.status = 'RUNNING' AND t.active_run_id = r.run_id AND r.run_id = %s "
                "AND r.thread_id = t.thread_id AND r.status = 'RUNNING' "
                "AND r.lease_version = %s AND r.lease_expires_at > CURRENT_TIMESTAMP",
                (
                    request.turn_id,
                    request.conversation_id,
                    request.thread_id,
                    request.run_id,
                    request.lease_version,
                ),
            )
            row = await cursor.fetchone()
            if row is None or row[0] != request.raw_query:
                raise RuntimeTurnError("Turn, Run and original customer message must agree")
            if request.message_id is not None and row[1] != request.message_id:
                raise RuntimeTurnError("customer message_id must match the accepted turn")
            previous = request.previous_assistant_message
            if previous is not None:
                previous_cursor = await connection.execute(
                    "SELECT 1 FROM app.chat_history "
                    "WHERE message_id = %s AND conversation_id = %s AND turn_id = %s "
                    "AND role = %s AND content = %s AND created_at < %s",
                    (
                        previous.message_id,
                        request.conversation_id,
                        previous.turn_id,
                        previous.role.value,
                        previous.content,
                        row[2],
                    ),
                )
                if await previous_cursor.fetchone() is None:
                    raise RuntimeTurnError("previous assistant message must match chat history")
            # 客户范围从已受理会话读取，不接受客户文本或内部请求自行指定。
            yield row[2], row[3], row[4], row[5]

    async def execute(self, request: ExecuteGraphTurnRequest) -> ExecuteGraphTurnResponse:
        """从 checkpoint 继续执行本轮，并返回图产生的终态。"""
        execution_started_at = datetime.now(UTC)
        execution_started = time.perf_counter()
        async with self._verified_turn(request) as (
            started_at,
            customer_id,
            channel,
            resume_payload,
        ):
            config = {"configurable": {"thread_id": request.thread_id}}
            before = await self._runtime.graph.aget_state(config)
            if before.values.get("bound_conversation_id") not in (None, request.conversation_id):
                raise RuntimeTurnError("thread_id is bound to another conversation")
            if before.values.get("last_processed_turn_id") == request.turn_id:
                if before.values.get("last_processed_raw_query") != request.raw_query:
                    raise RuntimeTurnError("same turn_id cannot carry another message")
                replayed = True
            elif (
                (latest_started_at := before.values.get("last_processed_turn_started_at"))
                and started_at <= datetime.fromisoformat(latest_started_at)
            ) or any(
                item["turn_id"] == request.turn_id for item in prompt_message_dicts(before.values)
            ):
                raise RuntimeTurnError("older turn cannot be replayed after a newer turn")
            else:
                replayed = False

            graph_input = checkpoint_graph_input(
                request,
                started_at=started_at,
                customer_id=customer_id,
                channel=channel,
            )
            waiting = any(task.interrupts for task in before.tasks)
            if resume_payload:
                if resume_payload.get("review_type") == "RAG":
                    draft = (
                        await self._artifact_payload(
                            before.values,
                            legacy_field="rag_draft",
                            reference_field="rag_draft_ref",
                            expected_kind="RAG_DRAFT",
                        )
                        or {}
                    )
                    if resume_payload.get("turn_id") != request.turn_id or resume_payload.get(
                        "draft_id"
                    ) != draft.get("draft_id"):
                        raise RuntimeTurnError("待审核 RAG 草稿或轮次已变化")
                else:
                    progress = before.values.get("active_sop_execution") or {}
                    approval = progress.get("pending_approval") or {}
                    if progress.get("sop_execution_id") != resume_payload["sop_execution_id"] or (
                        resume_payload.get("approval_id")
                        and resume_payload["approval_id"] != approval.get("approval_id")
                    ):
                        raise RuntimeTurnError("待处理流程或审批已变化")
            rag_review_replayed = bool(
                resume_payload
                and resume_payload.get("review_type") == "RAG"
                and before.values.get("last_rag_review_decision_id")
                == resume_payload.get("decision_id")
                and not waiting
                and not before.next
                and before.values.get("terminal_turn_id") == request.turn_id
            )
            if rag_review_replayed:
                # Runtime 已完成审核恢复、BFF 还未收到响应时，直接返回保存的终态。
                # 不能重新经过 START，否则会重复检索和生成另一份草稿。
                state = before.values
            elif (
                not waiting
                and before.next
                and before.values.get("last_prepared_turn_id") == request.turn_id
                and resume_payload
            ):
                # 审批已提交、工具节点尚未完成时从保存的下一节点继续，不能从 START 重走补槽。
                await self._runtime.graph.aupdate_state(
                    config,
                    {
                        "run_id": request.run_id,
                        "last_prepared_run_id": request.run_id,
                        "terminal_run_id": request.run_id,
                    },
                )
                state = await self._runtime.graph.ainvoke(None, config)
            elif (
                waiting
                and replayed
                and (
                    not resume_payload
                    or before.values.get("last_review_decision_id")
                    == resume_payload.get("decision_id")
                )
            ):
                # 节点已保存等待/处理结果、但 BFF 还未落库时，只补上新的 Run 身份。
                # 重新停在等待节点，不重复执行先前的批准或业务操作。
                await self._runtime.graph.aupdate_state(
                    config,
                    {
                        "run_id": request.run_id,
                        "last_prepared_run_id": request.run_id,
                        "terminal_run_id": request.run_id,
                    },
                    as_node="prepare_review",
                )
                state = await self._runtime.graph.ainvoke(None, config)
            elif waiting:
                state = await self._runtime.graph.ainvoke(
                    Command(
                        resume={
                            **(resume_payload or {"action": "CUSTOMER"}),
                            "graph_input": graph_input,
                        }
                    ),
                    config,
                )
            else:
                state = await self._runtime.graph.ainvoke(graph_input, config)
                # 兼容已经保存为 SUSPENDED、但还没有真实 interrupt 的旧咨询。
                # 先在原图补建停点，再消费经 BFF 授权落库的恢复指令。
                if (
                    resume_payload
                    and state.get("__interrupt__")
                    and state.get("last_review_decision_id") != resume_payload.get("decision_id")
                ):
                    state = await self._runtime.graph.ainvoke(
                        Command(resume={**resume_payload, "graph_input": graph_input}), config
                    )
            if (
                state.get("terminal_turn_id") != request.turn_id
                or state.get("terminal_run_id") != request.run_id
            ):
                raise RuntimeTurnError("graph did not produce this Run's terminal output")
            rag_payloads = await self._artifact_payloads(
                state,
                ("retrieval", "rag_retrieval", "rag_retrieval_ref", "RAG_RETRIEVAL"),
                ("draft", "rag_draft", "rag_draft_ref", "RAG_DRAFT"),
            )
            retrieval_payload = rag_payloads["retrieval"]
            draft_payload = rag_payloads["draft"]
            try:
                tool_results = await load_tool_results(
                    state, getattr(self._runtime, "artifact_repository", None)
                )
            except (ArtifactValidationError, KeyError, TypeError, ValueError, OSError) as error:
                raise RuntimeTurnError("工具结果缺失、过期或校验失败") from error
            response = ExecuteGraphTurnResponse(
                conversation_id=request.conversation_id,
                thread_id=request.thread_id,
                turn_id=request.turn_id,
                run_id=request.run_id,
                normalized_query=state["normalized_query"],
                effective_query=state["effective_query"],
                turn_sequence=state["turn_sequence"],
                context_cycle_id=str(state.get("context_cycle_id") or f"ctx:{request.thread_id}:0"),
                replayed=replayed,
                terminal=GraphTerminalSnapshot(
                    graph_interrupted=bool(state.get("__interrupt__")),
                    business_operation=state.get("business_operation")
                    if state["terminal_response_path"] == "SOP"
                    else None,
                    sop_notice_included=(
                        state.get("sop_notice_included", False)
                        if state["terminal_response_path"] == "SOP"
                        else False
                    ),
                    response_path=state["terminal_response_path"],
                    content=state["terminal_content"],
                    reason_code=state["terminal_reason_code"],
                    policy_intent_id=state.get("policy_intent_id"),
                    policy_answer_version=state.get("policy_answer_version"),
                    answer_content_hash=state.get("answer_content_hash"),
                    sop_progress=(
                        state.get("active_sop_execution")
                        if state.get("terminal_response_path") == "SOP"
                        else None
                    ),
                    sop_release_version=(
                        state.get("sop_release_version")
                        if state.get("terminal_response_path") == "SOP"
                        else None
                    ),
                    business_tool_results=(
                        tool_results if state.get("terminal_response_path") == "SOP" else ()
                    ),
                    pending_sop_lookup=(
                        state.get("pending_sop_lookup")
                        if state.get("terminal_response_path") == "SOP"
                        else None
                    ),
                    sop_lookup_batches=(
                        state.get("sop_lookup_batches", ())
                        if state.get("terminal_response_path") == "SOP"
                        else ()
                    ),
                    sop_lookup_selection_events=(
                        state.get("sop_lookup_selection_events", ())
                        if state.get("terminal_response_path") == "SOP"
                        else ()
                    ),
                    faq_resolution=(
                        FaqResolutionResult.model_validate(state["faq_resolution"])
                        if state.get("faq_turn_id") == request.turn_id
                        and state.get("faq_resolution")
                        else None
                    ),
                    pending_clarification=(
                        state.get("pending_clarification")
                        if state.get("terminal_response_path") == "CLARIFICATION"
                        else None
                    ),
                    retrieval_result=(
                        RetrievalResult.model_validate(retrieval_payload)
                        if state.get("terminal_response_path") == "RAG"
                        and state.get("rag_turn_id") == request.turn_id
                        and retrieval_payload
                        else None
                    ),
                    rag_draft=(
                        RagDraft.model_validate(draft_payload)
                        if state.get("terminal_response_path") == "RAG"
                        and state.get("rag_generation_turn_id") == request.turn_id
                        and draft_payload
                        else None
                    ),
                    rag_review=(
                        RagReviewOutcome.model_validate(state["rag_review_outcome"])
                        if state.get("terminal_response_path") == "RAG"
                        and state.get("rag_review_outcome")
                        else None
                    ),
                ),
                decision_trace=(
                    DecisionTrace.model_validate(state["decision_trace"])
                    if state.get("decision_trace")
                    else None
                ),
                routing_decision=(
                    RoutingDecision.model_validate(state["routing_decision"])
                    if state.get("routing_turn_id") == request.turn_id
                    and state.get("routing_decision")
                    else None
                ),
                routing_trace=(
                    DecisionTrace.model_validate(state["routing_trace"])
                    if state.get("routing_turn_id") == request.turn_id
                    and state.get("routing_trace")
                    else None
                ),
                model_calls=tuple(
                    ModelCallRecord.model_validate(item)
                    for item in state.get("model_calls", [])
                    if item.get("turn_id") == request.turn_id
                    and item.get("run_id") == request.run_id
                ),
            )
            await self._persist_trace_spans(
                request=request,
                state=state,
                started_at=execution_started_at,
                elapsed_ms=max(0, round((time.perf_counter() - execution_started) * 1000)),
            )
            return response

    async def inspect_sop(
        self, conversation_id: str, thread_id: str
    ) -> GraphTerminalSnapshot | None:
        """读取当前流程供坐席查看；只核对会话映射，不执行节点、不解除暂停。

        已完成的 Run 不能再通过执行入口进入图，但其流程进度仍需供坐席查看。
        因此这里独立核对业务会话与图线程，并使用同一线程锁读取一致的快照。
        """
        async with await AsyncConnection.connect(self._database_dsn) as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (thread_id,)
            )
            cursor = await connection.execute(
                "SELECT thread_id FROM app.conversation_thread_mapping "
                "WHERE conversation_id = %s AND thread_id = %s",
                (conversation_id, thread_id),
            )
            if await cursor.fetchone() is None:
                raise RuntimeTurnError("会话与图线程不匹配")
            state = await self._runtime.graph.aget_state({"configurable": {"thread_id": thread_id}})
            values = state.values
            if values.get("bound_conversation_id") not in (None, conversation_id):
                raise RuntimeTurnError("图线程不属于当前会话")
            if not values.get("active_sop_execution"):
                return None
            tool_results = await load_tool_results(
                values, getattr(self._runtime, "artifact_repository", None)
            )
            return GraphTerminalSnapshot(
                graph_interrupted=any(task.interrupts for task in state.tasks),
                business_operation=values.get("business_operation"),
                response_path="SOP",
                content=values.get("sop_content") or "流程进度已保存。",
                sop_notice_included=values.get("sop_notice_included", False),
                reason_code=values.get("sop_reason_code") or "SOP_MANUAL_REVIEW",
                sop_progress=values["active_sop_execution"],
                sop_release_version=values["sop_release_version"],
                business_tool_results=tool_results,
                pending_sop_lookup=values.get("pending_sop_lookup"),
                sop_lookup_batches=values.get("sop_lookup_batches", ()),
                sop_lookup_selection_events=values.get("sop_lookup_selection_events", ()),
            )

    async def inspect(self, request: ExecuteGraphTurnRequest) -> InspectGraphTurnResponse:
        """只读检查本轮是否已准备或结束，不再执行图节点。"""

        async with self._verified_turn(request):
            state = await self._runtime.graph.aget_state(
                {"configurable": {"thread_id": request.thread_id}}
            )
            values = state.values
            if values.get("bound_conversation_id") not in (None, request.conversation_id):
                raise RuntimeTurnError("thread_id is bound to another conversation")
            if values.get("last_processed_turn_id") not in (None, request.turn_id):
                raise RuntimeTurnError("latest checkpoint belongs to another turn")
            if (
                values.get("last_processed_turn_id") == request.turn_id
                and values.get("last_processed_raw_query") != request.raw_query
            ):
                raise RuntimeTurnError("same turn_id cannot carry another message")
            prepared = (
                values.get("last_prepared_turn_id") == request.turn_id
                and values.get("last_prepared_run_id") == request.run_id
            )
            terminal = (
                prepared
                and values.get("terminal_turn_id") == request.turn_id
                and values.get("terminal_run_id") == request.run_id
                and (not state.next or any(task.interrupts for task in state.tasks))
            )
            rag_payloads = await self._artifact_payloads(
                values,
                ("retrieval", "rag_retrieval", "rag_retrieval_ref", "RAG_RETRIEVAL"),
                ("draft", "rag_draft", "rag_draft_ref", "RAG_DRAFT"),
            )
            retrieval_payload = rag_payloads["retrieval"]
            draft_payload = rag_payloads["draft"]
            tool_results = await load_tool_results(
                values, getattr(self._runtime, "artifact_repository", None)
            )
            return InspectGraphTurnResponse(
                conversation_id=request.conversation_id,
                thread_id=request.thread_id,
                turn_id=request.turn_id,
                run_id=request.run_id,
                prepared=prepared,
                snapshot=(
                    GraphPreparationSnapshot(
                        normalized_query=values["normalized_query"],
                        effective_query=values["effective_query"],
                        turn_sequence=values["turn_sequence"],
                        context_cycle_id=str(
                            values.get("context_cycle_id") or f"ctx:{request.thread_id}:0"
                        ),
                    )
                    if prepared
                    else None
                ),
                terminal=(
                    GraphTerminalSnapshot(
                        graph_interrupted=any(task.interrupts for task in state.tasks),
                        business_operation=values.get("business_operation")
                        if values["terminal_response_path"] == "SOP"
                        else None,
                        sop_notice_included=(
                            values.get("sop_notice_included", False)
                            if values["terminal_response_path"] == "SOP"
                            else False
                        ),
                        response_path=values["terminal_response_path"],
                        content=values["terminal_content"],
                        reason_code=values["terminal_reason_code"],
                        policy_intent_id=values.get("policy_intent_id"),
                        policy_answer_version=values.get("policy_answer_version"),
                        answer_content_hash=values.get("answer_content_hash"),
                        sop_progress=(
                            values.get("active_sop_execution")
                            if values.get("terminal_response_path") == "SOP"
                            else None
                        ),
                        sop_release_version=(
                            values.get("sop_release_version")
                            if values.get("terminal_response_path") == "SOP"
                            else None
                        ),
                        business_tool_results=(
                            tool_results if values.get("terminal_response_path") == "SOP" else ()
                        ),
                        pending_sop_lookup=(
                            values.get("pending_sop_lookup")
                            if values.get("terminal_response_path") == "SOP"
                            else None
                        ),
                        sop_lookup_batches=(
                            values.get("sop_lookup_batches", ())
                            if values.get("terminal_response_path") == "SOP"
                            else ()
                        ),
                        sop_lookup_selection_events=(
                            values.get("sop_lookup_selection_events", ())
                            if values.get("terminal_response_path") == "SOP"
                            else ()
                        ),
                        faq_resolution=(
                            FaqResolutionResult.model_validate(values["faq_resolution"])
                            if values.get("faq_turn_id") == request.turn_id
                            and values.get("faq_resolution")
                            else None
                        ),
                        pending_clarification=(
                            values.get("pending_clarification")
                            if values.get("terminal_response_path") == "CLARIFICATION"
                            else None
                        ),
                        retrieval_result=(
                            RetrievalResult.model_validate(retrieval_payload)
                            if values.get("terminal_response_path") == "RAG"
                            and values.get("rag_turn_id") == request.turn_id
                            and retrieval_payload
                            else None
                        ),
                        rag_draft=(
                            RagDraft.model_validate(draft_payload)
                            if values.get("terminal_response_path") == "RAG"
                            and values.get("rag_generation_turn_id") == request.turn_id
                            and draft_payload
                            else None
                        ),
                        rag_review=(
                            RagReviewOutcome.model_validate(values["rag_review_outcome"])
                            if values.get("terminal_response_path") == "RAG"
                            and values.get("rag_review_outcome")
                            else None
                        ),
                    )
                    if terminal
                    else None
                ),
                decision_trace=(
                    DecisionTrace.model_validate(values["decision_trace"])
                    if values.get("decision_trace")
                    else None
                ),
                routing_decision=(
                    RoutingDecision.model_validate(values["routing_decision"])
                    if values.get("routing_turn_id") == request.turn_id
                    and values.get("routing_decision")
                    else None
                ),
                routing_trace=(
                    DecisionTrace.model_validate(values["routing_trace"])
                    if values.get("routing_turn_id") == request.turn_id
                    and values.get("routing_trace")
                    else None
                ),
                model_calls=tuple(
                    ModelCallRecord.model_validate(item)
                    for item in values.get("model_calls", [])
                    if item.get("turn_id") == request.turn_id
                    and item.get("run_id") == request.run_id
                ),
            )
