"""可恢复的主图：先走精确路由，再按配置进入语义检索或安全回复。"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from psycopg import Error as PsycopgError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from services.ai_runtime.decision.jev import JevIntentDecisionAdapter
from services.ai_runtime.generation.grounded import (
    GroundedAnswerGenerator,
    semantic_evidence_review_allowed,
)
from services.ai_runtime.generation.review import (
    apply_rag_review,
    await_rag_review,
    next_after_rag_review,
)
from services.ai_runtime.intent.catalog import load_catalog_node
from services.ai_runtime.intent.classifier import LlmIntentClassifier
from services.ai_runtime.intent.routing import (
    current_intent_catalog,
    decide_route,
    faq_rag_fallback_allowed,
    is_policy_answer_decision,
    route_turn,
)
from services.ai_runtime.observability import observe_node
from services.ai_runtime.persistence.artifacts import (
    ArtifactValidationError,
    RuntimeArtifactRepository,
)
from services.ai_runtime.persistence.context import ContextSummaryRepository
from services.ai_runtime.query.context import (
    active_confirmed_facts,
    context_refs,
    derive_query,
    normalize_query,
)
from services.ai_runtime.query.decision import decide_query
from services.ai_runtime.query.history import (
    HISTORY_CONTEXT_CLARIFICATION,
    HistoryRecallCoordinator,
    HistoryRecallResult,
)
from services.ai_runtime.query.message_state import (
    ContextBudgetPolicy,
    compression_candidates,
    current_summary,
    customer_message,
    estimate_request_tokens,
    extractive_summary,
    merge_working_messages,
    message_source_ref,
    message_turn_id,
    message_upgrade_updates,
    previous_assistant_message,
    prompt_message_dicts,
    read_working_messages,
    summary_updates,
)
from services.ai_runtime.query.rewrite import LlmQueryRewriter
from services.ai_runtime.query.summary import ContextSummarizer
from services.ai_runtime.retrieval.faq import resolve_faq
from services.ai_runtime.retrieval.rag import RagRetriever
from services.ai_runtime.retrieval.reranking import DashScopeTextReranker
from services.ai_runtime.retrieval.semantic import SemanticSearch
from services.ai_runtime.sop.business import SopBusinessClient
from services.ai_runtime.sop.flow import check_active_sop, next_after_sop_check, run_sop
from services.ai_runtime.sop.review import (
    apply_review,
    await_review,
    next_after_review,
    next_review_step,
    prepare_review,
    run_approved_operation,
)
from services.ai_runtime.sop.side_query import run_sop_side_query
from services.ai_runtime.sop.tools import next_after_sop, run_sop_tools
from services.content.policy_answers import load_policy_answer
from shared.contracts.artifact import RuntimeArtifactRef
from shared.contracts.enums import (
    DecisionProfile,
    FaqMatchStatus,
    FaqMissReason,
    HistoryRecallMode,
    QualityAction,
    QueryResolutionStatus,
    ResponsePath,
)
from shared.contracts.faq import FaqResolutionResult
from shared.contracts.quality import QualityDecision
from shared.contracts.query import HistoryRecallDecision, QueryContext, QueryResolutionResult
from shared.contracts.retrieval import RetrievalFilters
from shared.contracts.routing import (
    ClarificationOption,
    PendingClarification,
    QueryClarificationProposal,
    RoutingDecision,
)
from shared.guardrails import evaluate_customer_input
from shared.safe_fallback import (
    HUMAN_HANDOFF_REPLY,
    RAG_EVIDENCE_CONFLICT_REPLY,
    RAG_EVIDENCE_INSUFFICIENT_REPLY,
    RAG_GENERATION_FAILED_REPLY,
    RAG_REVIEW_PENDING_REPLY,
    SAFE_FALLBACK_ALLOWLIST,
    SAFE_FALLBACK_REPLY,
    UNSUPPORTED_CAPABILITY_REPLY,
)
from shared.web.settings import ServiceSettings


class AgentGraphState(TypedDict, total=False):
    """图的工作状态；完整原始消息以 app.chat_history 为准。"""

    bound_conversation_id: str
    conversation_id: str
    thread_id: str
    turn_id: str
    run_id: str
    request_id: str
    trace_id: str
    channel: str
    raw_query: str
    message_id: str | None
    previous_assistant_message: dict[str, object] | None
    conversation_model_profile: str
    shadow_decision_profile: str | None
    normalized_query: str
    resolved_query: str | None
    effective_query: str
    query_context: dict[str, object]
    context_refs: dict[str, object]
    decision_trace: dict[str, object]
    routing_decision: dict[str, object]
    routing_trace: dict[str, object]
    routing_turn_id: str
    intent_catalog: dict[str, dict[str, str]]
    intent_rule_release: str | None
    intent_rules: list[dict[str, object]]
    faq_exact_hint: dict[str, object] | None
    faq_resolution: dict[str, object]
    faq_turn_id: str
    semantic_candidates: list[dict[str, object]]
    semantic_faq_candidates: list[dict[str, object]]
    semantic_faq_search_status: str
    semantic_thresholds: dict[str, float]
    rag_retrieval: dict[str, object] | None
    rag_retrieval_ref: dict[str, object] | None
    rag_retrieval_summary: dict[str, object] | None
    rag_turn_id: str
    rag_draft: dict[str, object] | None
    rag_draft_ref: dict[str, object] | None
    rag_generation_turn_id: str
    rag_review_decision: dict[str, object] | None
    rag_review_outcome: dict[str, object] | None
    last_rag_review_decision_id: str | None
    messages: Annotated[list[AnyMessage], merge_working_messages]
    message_state_schema_version: int
    # 旧 checkpoint 兼容字段。正常新 Turn 会升级成标准 messages 通道并清空。
    recent_messages: list[dict[str, str]]
    turn_sequence: int
    turn_started_at: str
    last_processed_turn_id: str
    last_processed_raw_query: str
    last_processed_turn_started_at: str
    last_prepared_turn_id: str
    last_prepared_run_id: str
    terminal_turn_id: str
    terminal_run_id: str
    terminal_response_path: str
    terminal_content: str
    terminal_reason_code: str
    policy_intent_id: str | None
    policy_answer_version: str | None
    answer_content_hash: str | None
    input_guardrail_blocked: bool
    input_quality_decision: dict[str, object] | None
    conversation_summary: str | None
    conversation_summary_ref: str | None
    summary_metadata: dict[str, object]
    context_budget: dict[str, object]
    context_cycle_id: str
    context_cycle_sequence: int
    history_recall_decision: dict[str, object] | None
    history_recalled_message_refs: list[str]
    history_recall_count: int
    confirmed_facts: list[dict[str, object]]
    pending_action: dict[str, object] | None
    pending_clarification: dict[str, object] | None
    clarification_origin_query: str
    clarification_faq_hints: dict[str, dict[str, object]]
    clarification_query_resolutions: dict[str, str]
    active_sop_execution: dict[str, object] | None
    sop_action: str
    sop_turn_id: str
    sop_content: str
    sop_reason_code: str
    sop_notice_included: bool
    sop_catalog: dict[str, dict[str, object]]
    sop_catalog_release: str | None
    sop_definition: dict[str, object]
    sop_release_version: str
    sop_review_reason: str | None
    sop_interpretation: dict[str, object] | None
    pending_sop_lookup: dict[str, object] | None
    sop_lookup_batches: list[dict[str, object]]
    sop_lookup_selection_events: list[dict[str, object]]
    sop_prefetch_due: bool
    sop_lookup_confirmed: bool
    sop_history_recall_count: int
    sop_history_recalled_refs: list[str]
    sop_history_recall_reason: str | None
    sop_history_recovered: bool
    business_customer_id: str
    sop_tool_results: list[dict[str, object]]
    sop_tool_results_ref: dict[str, object] | None
    artifact_hydration_metrics: dict[str, int]
    sop_tools_due: bool
    sop_tools_turn_id: str
    model_calls: list[dict[str, object]]
    review_decision: dict | None
    sop_operation_request: dict | None
    business_operation: dict | None
    last_review_decision_id: str | None
    sop_operation_due: bool
    trace_spans: list[dict[str, object]]


def prepare_turn(state: AgentGraphState, config: RunnableConfig) -> AgentGraphState:
    """绑定会话、升级旧消息状态，并把本轮消息增量写入标准通道。"""

    if state["thread_id"] != config.get("configurable", {}).get("thread_id"):
        raise ValueError("Graph State thread_id must match the checkpoint thread")
    conversation_id = state["conversation_id"]
    if state.get("bound_conversation_id") not in (None, conversation_id):
        raise ValueError("thread_id is already bound to another conversation")
    raw_query = state["raw_query"]
    normalized_query = normalize_query(raw_query)
    if not normalized_query:
        raise ValueError("raw_query must contain non-whitespace content")
    if state.get("last_processed_turn_id") == state["turn_id"]:
        if state.get("last_processed_raw_query") != raw_query:
            raise ValueError("same turn_id cannot carry different raw_query")
        # 同一 Turn 换 Run 时沿用首次派生结果，不因事实过期或配置变化改写原问题。
        query = (
            QueryContext.model_validate(state["query_context"])
            if state.get("query_context")
            else derive_query(raw_query, active_confirmed_facts(state.get("confirmed_facts", [])))
        )
        return {
            "normalized_query": query.normalized_query,
            "resolved_query": query.resolution.resolved_query,
            "effective_query": query.effective_query,
            "query_context": query.model_dump(mode="json"),
            "last_prepared_turn_id": state["turn_id"],
            "last_prepared_run_id": state["run_id"],
        }
    facts = active_confirmed_facts(state.get("confirmed_facts", []))
    query = derive_query(raw_query, facts)
    working_messages = read_working_messages(state)
    working_dicts = prompt_message_dicts(state)
    if any(item["turn_id"] == state["turn_id"] for item in working_dicts):
        raise ValueError("older turn_id cannot be replayed after a newer turn")
    upgrade = message_upgrade_updates(state)
    additions: list[AnyMessage] = []
    previous = previous_assistant_message(state)
    existing_ids = {message.id for message in working_messages if message.id}
    if previous is not None and previous.id not in existing_ids:
        additions.append(previous)
    additions.append(customer_message(state))
    message_delta = [*upgrade.get("messages", []), *additions]
    combined_messages = merge_working_messages(working_messages, additions)
    context_cycle_sequence = state.get("context_cycle_sequence", 0)
    context_cycle_id = f"ctx:{state['thread_id']}:{context_cycle_sequence}"
    updates: AgentGraphState = {
        **upgrade,
        "bound_conversation_id": conversation_id,
        "normalized_query": query.normalized_query,
        "resolved_query": query.resolution.resolved_query,
        "effective_query": query.effective_query,
        "query_context": query.model_dump(mode="json"),
        "messages": message_delta,
        "context_cycle_id": context_cycle_id,
        "context_cycle_sequence": context_cycle_sequence,
        "history_recall_decision": None,
        "history_recalled_message_refs": [],
        "history_recall_count": 0,
        "sop_history_recall_count": 0,
        "sop_history_recalled_refs": [],
        "sop_history_recall_reason": None,
        "sop_history_recovered": False,
        "confirmed_facts": [fact.model_dump(mode="json") for fact in facts],
        "context_refs": context_refs(
            messages=combined_messages,
            facts=facts,
            pending_action=state.get("pending_action"),
            previous_assistant_message=state.get("previous_assistant_message"),
        ),
        "turn_sequence": state.get("turn_sequence", 0) + 1,
        # 模型调用记录只属于当前 Turn；新一轮不能沿用上一轮的调用编号。
        # 同一 Turn 重试时会走上面的缓存分支，因此不会丢失本轮已有记录。
        "model_calls": [],
        "rag_retrieval": None,
        "rag_retrieval_ref": None,
        "rag_retrieval_summary": None,
        "rag_draft": None,
        "rag_draft_ref": None,
        "rag_review_decision": None,
        "rag_review_outcome": None,
        "policy_intent_id": None,
        "policy_answer_version": None,
        "answer_content_hash": None,
        "rag_turn_id": state["turn_id"],
        "rag_generation_turn_id": state["turn_id"],
        # Active SOP 可以跳过通用 Query 决策，不能把上一轮的决策记录冒充本轮记录。
        "decision_trace": {},
        "last_processed_turn_id": state["turn_id"],
        "last_processed_raw_query": raw_query,
        "last_prepared_turn_id": state["turn_id"],
        "last_prepared_run_id": state["run_id"],
    }
    if "turn_started_at" in state:
        updates["last_processed_turn_started_at"] = state["turn_started_at"]
    return updates


def guard_input(state: AgentGraphState) -> AgentGraphState:
    """在任何检索或模型调用前阻断明确注入和受保护秘密。"""

    decision = evaluate_customer_input(
        state["raw_query"],
        decision_id=f"input-quality:{state['run_id']}",
    )
    return {
        "input_guardrail_blocked": decision.action is QualityAction.BLOCK,
        "input_quality_decision": decision.model_dump(mode="json"),
    }


def next_after_input_guardrail(state: AgentGraphState) -> str:
    """阻断输入直接进入共享安全出口，其余请求继续上下文管理。"""

    return "safe_fallback" if state.get("input_guardrail_blocked") else "compress_context"


async def compress_context(
    state: AgentGraphState,
    *,
    policy: ContextBudgetPolicy,
    summarizer: ContextSummarizer | None,
    summary_repository: ContextSummaryRepository | None,
) -> AgentGraphState:
    """达到 Token 水位后先保存摘要产物，成功后才删除已覆盖消息。"""

    messages = read_working_messages(state)
    estimated_tokens = estimate_request_tokens(messages, policy)
    budget_status = {
        "estimated_request_tokens": estimated_tokens,
        "compression_threshold_tokens": policy.compression_threshold_tokens,
        "effective_window_tokens": policy.effective_window_tokens,
        "token_estimator": "LANGCHAIN_APPROXIMATE",
        "compression_triggered": estimated_tokens >= policy.compression_threshold_tokens,
    }
    if estimated_tokens < policy.compression_threshold_tokens:
        return {"context_budget": budget_status}

    candidates, _recent = compression_candidates(messages, policy)
    if not candidates:
        return {
            "context_budget": {
                **budget_status,
                "compression_outcome": "SINGLE_TURN_EXCEEDS_BUDGET",
            }
        }

    existing_summary, _summary_ref = current_summary(state)
    candidate_source_refs = [
        str(source_ref) for message in candidates if (source_ref := message_source_ref(message))
    ]
    metadata = state.get("summary_metadata")
    previous_source_refs: list[str] = []
    if isinstance(metadata, dict):
        previous_source_refs = [str(item) for item in metadata.get("source_refs", [])]
    source_refs = list(dict.fromkeys([*previous_source_refs, *candidate_source_refs]))
    compression_count = (
        int(metadata.get("compressions_since_rebaseline") or 0) + 1
        if isinstance(metadata, dict)
        else 1
    )
    rebaseline = (
        compression_count >= policy.rebaseline_after_compressions
        or len(source_refs) > policy.summary_source_ref_limit
    )

    # 重新基线化仍以当前聚合摘要为主，有限分段摘要只用于校正滚动压缩漂移；
    # 永远不会把整个历史或全部摘要一次性装回上下文。
    summary_input = existing_summary
    if rebaseline and summary_repository is not None:
        try:
            recent_segments = await summary_repository.recent_segments(
                conversation_id=str(state["conversation_id"]),
                before_record_id=(
                    str(metadata.get("summary_record_id"))
                    if isinstance(metadata, dict) and metadata.get("summary_record_id")
                    else None
                ),
                limit=min(policy.rebaseline_after_compressions, 16),
            )
        except (KeyError, TypeError, ValueError, OSError, PsycopgError):
            # 数据库不可用时不能先删除可恢复的原消息。
            return {
                "context_budget": {
                    **budget_status,
                    "compression_outcome": "SUMMARY_HISTORY_UNAVAILABLE",
                }
            }
        if recent_segments:
            summary_input = "\n".join(
                item
                for item in (
                    existing_summary,
                    *(segment.summary_text for segment in recent_segments),
                )
                if item
            )

    call_record = None
    bundle = None
    if summarizer is not None:
        bundle, call_record = await summarizer.summarize(
            state=state,
            existing_summary=summary_input,
            messages=candidates,
            max_tokens=policy.summary_budget_tokens,
        )
    generation_method = "MODEL"
    if bundle is None:
        segment_summary = extractive_summary(
            existing_summary=None,
            messages=candidates,
            token_budget=policy.summary_budget_tokens,
        )
        aggregate_summary = extractive_summary(
            existing_summary=summary_input,
            messages=candidates,
            token_budget=policy.summary_budget_tokens,
        )
        generation_method = "EXTRACTIVE_FALLBACK"
    else:
        segment_summary = bundle.segment_summary
        aggregate_summary = bundle.aggregate_summary

    next_sequence = int(state.get("context_cycle_sequence") or 0) + 1
    next_cycle_id = f"ctx:{state['thread_id']}:{next_sequence}"
    summary_record_id: str | None = None
    record_kind = "BASELINE" if rebaseline else "AGGREGATE"
    if summary_repository is not None:
        try:
            segment_record = await summary_repository.append(
                state=state,
                record_kind="SEGMENT",
                summary_text=segment_summary,
                covered_turn_ids=tuple(
                    dict.fromkeys(
                        turn_id for message in candidates if (turn_id := message_turn_id(message))
                    )
                ),
                source_refs=tuple(dict.fromkeys(candidate_source_refs)),
                generation_method=generation_method,
                parent_summary_record_id=None,
                prompt_version="context-summary-v2",
                model=(summarizer.model if summarizer is not None else None),
                context_cycle_id=next_cycle_id,
            )
            parent_record_id = (
                str(metadata.get("summary_record_id"))
                if isinstance(metadata, dict) and metadata.get("summary_record_id")
                else None
            )
            summary_record = await summary_repository.append(
                state=state,
                record_kind=record_kind,
                summary_text=aggregate_summary,
                covered_turn_ids=tuple(
                    dict.fromkeys(
                        [
                            *(
                                [str(item) for item in metadata.get("covered_turn_ids", [])]
                                if isinstance(metadata, dict)
                                else []
                            ),
                            *[
                                turn_id
                                for message in candidates
                                if (turn_id := message_turn_id(message))
                            ],
                        ]
                    )
                ),
                source_refs=tuple(source_refs[-policy.summary_source_ref_limit :]),
                generation_method=generation_method,
                parent_summary_record_id=parent_record_id,
                prompt_version="context-summary-v2",
                model=(summarizer.model if summarizer is not None else None),
                context_cycle_id=next_cycle_id,
            )
            summary_record_id = summary_record.summary_record_id
            # 该引用只证明分段摘要已经保存；当前 State 仍只保留聚合摘要。
            source_refs.append(f"context-summary-record:{segment_record.summary_record_id}")
        except (KeyError, TypeError, ValueError, OSError, PsycopgError):
            # 保存失败时保留原消息，下轮可以安全重试。
            return {
                "context_budget": {
                    **budget_status,
                    "compression_outcome": "SUMMARY_PERSISTENCE_FAILED",
                },
                **(
                    {
                        "model_calls": [
                            *state.get("model_calls", []),
                            call_record.model_dump(mode="json"),
                        ]
                    }
                    if call_record is not None
                    else {}
                ),
            }

    updates = summary_updates(
        state=state,
        covered_messages=candidates,
        summary=aggregate_summary,
        generation_method=generation_method,
        source_refs=source_refs[-policy.summary_source_ref_limit :],
        summary_record_id=summary_record_id,
        record_kind=record_kind,
    )
    updates["summary_metadata"]["compressions_since_rebaseline"] = (
        0 if rebaseline else compression_count
    )
    compressed_messages = merge_working_messages(messages, updates["messages"])
    updates["context_refs"] = context_refs(
        messages=compressed_messages,
        facts=active_confirmed_facts(state.get("confirmed_facts", [])),
        pending_action=state.get("pending_action"),
        previous_assistant_message=state.get("previous_assistant_message"),
    )
    updates["context_budget"] = {
        **budget_status,
        "compression_outcome": generation_method,
        "summary_record_kind": record_kind,
        "summary_persisted": summary_record_id is not None,
    }
    if call_record is not None:
        updates["model_calls"] = [
            *state.get("model_calls", []),
            call_record.model_dump(mode="json"),
        ]
    return updates


def safe_fallback(state: AgentGraphState) -> AgentGraphState:
    """无法提供有依据的业务答复时结束最小图，不编造政策内容。"""

    if state.get("last_prepared_turn_id") != state["turn_id"]:
        raise ValueError("safe fallback requires this turn's prepared query")
    routing = state.get("routing_decision", {})
    out_of_scope = (
        routing.get("request_scope") == "OUT_OF_SCOPE"
        and routing.get("request_kind") == "EXTERNAL_CAPABILITY"
    )
    reasons = tuple(str(reason) for reason in routing.get("scope_reason_codes", ()))
    guard_reason = next((reason for reason in reasons if reason.startswith("GUARD:")), "")
    guard_replies = {
        f"GUARD:{reason_code.lower()}": next(iter(contents))
        for reason_code, contents in SAFE_FALLBACK_ALLOWLIST.items()
        if reason_code
        not in {
            "NO_GROUNDED_ANSWER",
            "UNSUPPORTED_CAPABILITY",
            "RAG_EVIDENCE_CONFLICT",
            "RAG_EVIDENCE_INSUFFICIENT",
            "RAG_GENERATION_FAILED",
        }
    }
    input_decision = (
        QualityDecision.model_validate(state["input_quality_decision"])
        if state.get("input_quality_decision")
        else None
    )
    input_reason = (
        input_decision.reason_codes[0]
        if input_decision is not None and input_decision.action is QualityAction.BLOCK
        else None
    )
    if input_reason in SAFE_FALLBACK_ALLOWLIST:
        content = next(iter(SAFE_FALLBACK_ALLOWLIST[input_reason]))
        reason_code = input_reason
    elif out_of_scope:
        content = UNSUPPORTED_CAPABILITY_REPLY
        reason_code = "UNSUPPORTED_CAPABILITY"
    elif guard_reason:
        content = guard_replies.get(guard_reason, SAFE_FALLBACK_REPLY)
        # 终态契约要求大写原因码；内部路由规则 ID 保留小写便于阅读，
        # 在跨边界写入终态时统一转换，避免 BFF 因契约校验拒绝安全回复。
        reason_code = guard_reason.removeprefix("GUARD:").upper()
    elif any(
        isinstance(call, dict)
        and call.get("purpose") == "GROUNDED_ANSWER"
        and call.get("status") == "FAILED"
        for call in state.get("model_calls", [])
    ):
        content = RAG_GENERATION_FAILED_REPLY
        reason_code = "RAG_GENERATION_FAILED"
    elif (state.get("rag_retrieval_summary") or state.get("rag_retrieval") or {}).get(
        "assessment", {}
    ).get("status") == "CONFLICTING":
        content = RAG_EVIDENCE_CONFLICT_REPLY
        reason_code = "RAG_EVIDENCE_CONFLICT"
    elif state.get("rag_retrieval_ref") or state.get("rag_retrieval"):
        content = RAG_EVIDENCE_INSUFFICIENT_REPLY
        reason_code = "RAG_EVIDENCE_INSUFFICIENT"
    else:
        content = SAFE_FALLBACK_REPLY
        reason_code = "NO_GROUNDED_ANSWER"
    return {
        "terminal_turn_id": state["turn_id"],
        "terminal_run_id": state["run_id"],
        "terminal_response_path": "SAFE_FALLBACK",
        "terminal_content": content,
        "terminal_reason_code": reason_code,
    }


async def policy_answer(
    state: AgentGraphState,
    redis: Redis | None = None,
) -> AgentGraphState:
    """按已识别意图读取唯一已发布策略答复，缓存异常时安全降级。"""

    routing = RoutingDecision.model_validate(state.get("routing_decision", {}))
    if not is_policy_answer_decision(routing, current_intent_catalog(state)):
        raise ValueError("policy answer requires a matched high-risk strategy intent")
    intent_id = routing.intent_decision.intent_id
    if intent_id is None:
        raise ValueError("policy answer requires intent_id")
    try:
        answer = await load_policy_answer(redis, intent_id)
    except (RedisError, ValueError, TypeError):
        answer = None
    if answer is None:
        # 权威发布快照缺失或损坏时，只能给通用安全回复，不能回退到代码文案。
        return safe_fallback(state)
    return {
        "terminal_turn_id": state["turn_id"],
        "terminal_run_id": state["run_id"],
        "terminal_response_path": ResponsePath.SAFE_FALLBACK.value,
        "terminal_content": str(answer["answer_text"]),
        "terminal_reason_code": "POLICY_ANSWER",
        "policy_intent_id": intent_id,
        "policy_answer_version": str(answer["policy_answer_version"]),
        "answer_content_hash": str(answer["answer_content_hash"]),
    }


def human_handoff(state: AgentGraphState) -> AgentGraphState:
    """把已发布的人工转接控制命令转换成独立终态，不再伪装成兜底。"""

    routing = RoutingDecision.model_validate(state.get("routing_decision", {}))
    if routing.response_path is not ResponsePath.HUMAN:
        raise ValueError("human handoff requires HUMAN routing decision")
    return {
        "terminal_turn_id": state["turn_id"],
        "terminal_run_id": state["run_id"],
        "terminal_response_path": ResponsePath.HUMAN.value,
        "terminal_content": HUMAN_HANDOFF_REPLY,
        "terminal_reason_code": "HUMAN_HANDOFF_REQUESTED",
    }


async def resolve_query_with_model(
    state: AgentGraphState,
    rewriter: LlmQueryRewriter | None = None,
    history_recall: HistoryRecallCoordinator | None = None,
) -> AgentGraphState:
    """需要上下文理解时调用一次结构化改写模型，失败则保留原 Query。

    有上一条客户可见回复时，模型同时区分新问题与纠正；控制命令、已进入
    澄清状态和安全护栏仍由确定性状态机优先处理，不能为此多调一次模型。
    """

    query = QueryContext.model_validate(state["query_context"])
    if rewriter is None or not _query_rewrite_required(state, query):
        return {}
    rewritten, record = await rewriter.rewrite(query, state=state)
    updates: AgentGraphState = {
        "model_calls": [*state.get("model_calls", []), record.model_dump(mode="json")]
    }
    if rewritten is not None:
        updates.update(
            {
                "normalized_query": rewritten.normalized_query,
                "resolved_query": rewritten.resolution.resolved_query,
                "effective_query": rewritten.effective_query,
                "query_context": rewritten.model_dump(mode="json"),
            }
        )
        return updates
    if _is_open_history_clarification(state):
        pending = PendingClarification.model_validate(state["pending_clarification"])
        updates["history_recall_decision"] = HistoryRecallDecision(
            mode=HistoryRecallMode.CLARIFY,
            conversation_id=str(state["conversation_id"]),
            turn_id=str(state["turn_id"]),
            attempt=0,
            clarification=pending.question,
            reason_codes=("HISTORY_CLARIFICATION_REPLY_NOT_RESOLVED",),
        ).model_dump(mode="json")
        return updates
    if history_recall is None or query.resolution.status not in {
        QueryResolutionStatus.AMBIGUOUS,
        QueryResolutionStatus.INSUFFICIENT,
    }:
        return updates

    recall_state = {**state, **updates}
    decision, result, recall_record = await history_recall.recall(query, state=recall_state)
    model_calls = list(updates["model_calls"])
    if recall_record is not None:
        model_calls.append(recall_record.model_dump(mode="json"))
    updates.update(
        {
            "history_recall_decision": decision.model_dump(mode="json"),
            "model_calls": model_calls,
        }
    )
    if decision.mode is HistoryRecallMode.NOT_REQUIRED:
        return updates
    if decision.mode is HistoryRecallMode.CLARIFY:
        clarified = _history_clarification_query(query, decision)
        updates.update(
            {
                "resolved_query": None,
                "effective_query": clarified.effective_query,
                "query_context": clarified.model_dump(mode="json"),
            }
        )
        return updates
    if not result.messages:
        decision = _history_clarify_decision(decision, "HISTORY_RECALL_NO_MATCH")
        clarified = _history_clarification_query(query, decision)
        updates.update(
            {
                "history_recall_decision": decision.model_dump(mode="json"),
                "history_recalled_message_refs": [],
                "history_recall_count": decision.attempt,
                "resolved_query": None,
                "effective_query": clarified.effective_query,
                "query_context": clarified.model_dump(mode="json"),
            }
        )
        return updates

    recalled_messages = _recalled_message_dicts(result)
    retry_state = {
        **state,
        **updates,
        "history_recalled_messages": recalled_messages,
        "history_recall_count": int(state.get("history_recall_count") or 0) + 1,
    }
    retried, retry_record = await rewriter.rewrite(query, state=retry_state, attempt=1)
    updates.update(
        {
            "history_recalled_message_refs": [
                str(message["source_ref"]) for message in recalled_messages
            ],
            "history_recall_count": retry_state["history_recall_count"],
            "model_calls": [*model_calls, retry_record.model_dump(mode="json")],
        }
    )
    if retried is None:
        decision = _history_clarify_decision(decision, "HISTORY_REWRITE_NOT_RESOLVED")
        clarified = _history_clarification_query(query, decision)
        updates.update(
            {
                "history_recall_decision": decision.model_dump(mode="json"),
                "resolved_query": None,
                "effective_query": clarified.effective_query,
                "query_context": clarified.model_dump(mode="json"),
            }
        )
        return updates
    updates.update(
        {
            "normalized_query": retried.normalized_query,
            "resolved_query": retried.resolution.resolved_query,
            "effective_query": retried.effective_query,
            "query_context": retried.model_dump(mode="json"),
        }
    )
    return updates


def _recalled_message_dicts(result: HistoryRecallResult) -> list[dict[str, object]]:
    """把数据库历史消息转换成只供本轮改写器使用的有界证据。"""

    return [
        {
            "role": message.role.value,
            "content": message.content,
            "turn_id": message.turn_id,
            "source_ref": f"message:{message.message_id}",
            "created_at": message.created_at.isoformat(),
        }
        for message in result.messages
    ]


def _history_clarification_query(
    query: QueryContext,
    decision: HistoryRecallDecision,
) -> QueryContext:
    """回查失败或仍不唯一时生成可审计的最小澄清状态。"""

    resolution = QueryResolutionResult(
        status=QueryResolutionStatus.INSUFFICIENT,
        missing_context=("conversation_history",),
        clarification=decision.clarification or "请补充您所指的具体业务或对象。",
        reason_codes=decision.reason_codes or ("HISTORY_CONTEXT_NOT_UNIQUE",),
    )
    return query.model_copy(
        update={"resolution": resolution, "effective_query": query.normalized_query}
    )


def _history_clarify_decision(
    decision: HistoryRecallDecision,
    reason: str,
) -> HistoryRecallDecision:
    """把已执行但无唯一结果的回查收敛为不可循环的澄清决策。"""

    return HistoryRecallDecision(
        mode=HistoryRecallMode.CLARIFY,
        conversation_id=decision.conversation_id,
        turn_id=decision.turn_id,
        attempt=min(decision.attempt + 1, 3),
        max_attempts=decision.max_attempts,
        clarification=HISTORY_CONTEXT_CLARIFICATION,
        reason_codes=(reason,),
    )


def next_after_query_resolution(state: AgentGraphState) -> str:
    """历史仍无法唯一定位时直接澄清，否则继续全局意图路由。"""

    decision = state.get("history_recall_decision")
    if isinstance(decision, dict) and decision.get("mode") == HistoryRecallMode.CLARIFY.value:
        return "history_clarification"
    return "route_turn"


def history_clarification(state: AgentGraphState) -> AgentGraphState:
    """把受控回查失败转换成客户可补充信息的澄清终态。"""

    decision = HistoryRecallDecision.model_validate(state["history_recall_decision"])
    if decision.mode is not HistoryRecallMode.CLARIFY or not decision.clarification:
        raise ValueError("history clarification requires a CLARIFY recall decision")
    previous = (
        PendingClarification.model_validate(state["pending_clarification"])
        if _is_open_history_clarification(state)
        else None
    )
    attempt_count = previous.attempt_count + 1 if previous is not None else 0
    max_attempts = previous.max_attempts if previous is not None else 2
    if attempt_count >= max_attempts:
        return {
            **safe_fallback(state),
            "pending_clarification": None,
            "clarification_origin_query": "",
            "clarification_faq_hints": {},
            "clarification_query_resolutions": {},
        }
    origin_turn_id = previous.origin_turn_id if previous is not None else state["turn_id"]
    pending = PendingClarification(
        clarification_id=(
            previous.clarification_id if previous is not None else f"clar_history_{origin_turn_id}"
        ),
        source_layer="QUERY",
        origin_turn_id=origin_turn_id,
        missing_fields=("conversation_history",),
        question=decision.clarification,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        resume_node="resolve_query",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    query_context = state.get("query_context")
    origin_query = str(state.get("clarification_origin_query") or "").strip()
    if not origin_query and isinstance(query_context, dict):
        origin_query = str(query_context.get("raw_query") or "").strip()
    if not origin_query:
        origin_query = str(state.get("raw_query") or state.get("effective_query") or "").strip()
    return {
        "pending_clarification": pending.model_dump(mode="json"),
        "clarification_origin_query": origin_query,
        "terminal_turn_id": state["turn_id"],
        "terminal_run_id": state["run_id"],
        "terminal_response_path": ResponsePath.CLARIFICATION.value,
        "terminal_content": decision.clarification,
        "terminal_reason_code": "HISTORY_CONTEXT_REQUIRED",
    }


def _query_rewrite_required(state: AgentGraphState, query: QueryContext) -> bool:
    """用已有状态与已发布规则决定是否值得调用会话改写器。"""

    if state.get("input_guardrail_blocked"):
        return False
    deterministic = decide_route(query.normalized_query, state)
    if deterministic.request_kind.value in {"CONTROL", "EXTERNAL_CAPABILITY", "HIGH_RISK"} or any(
        code.startswith("GUARD:") for code in deterministic.scope_reason_codes
    ):
        return False
    if state.get("pending_clarification"):
        return _is_open_history_clarification(state)
    if query.resolution.status.value in {"AMBIGUOUS", "INSUFFICIENT"}:
        return True
    return bool(state.get("previous_assistant_message"))


def _is_open_history_clarification(state: AgentGraphState | dict[str, object]) -> bool:
    """只识别需要自然语言补充的历史上下文澄清，不接管有限选项。"""

    raw = state.get("pending_clarification")
    if not isinstance(raw, dict):
        return False
    try:
        pending = PendingClarification.model_validate(raw)
    except ValueError:
        return False
    return (
        pending.source_layer.value == "QUERY"
        and not pending.options
        and "conversation_history" in pending.missing_fields
        and pending.expires_at > datetime.now(UTC)
    )


def clarification(state: AgentGraphState) -> AgentGraphState:
    """把语义候选转换成可持久化的澄清问题，不调用模型生成文字。"""

    routing = state.get("routing_decision", {})
    raw_query_clarification = routing.get("query_clarification")
    if raw_query_clarification:
        proposal = QueryClarificationProposal.model_validate(raw_query_clarification)
        pending = PendingClarification(
            clarification_id=f"clar_query_{state['turn_id']}",
            source_layer="QUERY",
            origin_turn_id=state["turn_id"],
            candidate_refs=tuple(item.option_id for item in proposal.options),
            question=proposal.question,
            options=tuple(
                ClarificationOption(
                    option_id=item.option_id,
                    label=item.label,
                    candidate_ref=item.option_id,
                )
                for item in proposal.options
            ),
            resume_node="route_turn",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        return {
            "pending_clarification": pending.model_dump(mode="json"),
            "clarification_origin_query": state["effective_query"],
            "clarification_query_resolutions": {
                item.option_id: item.resolved_query for item in proposal.options
            },
            "terminal_turn_id": state["turn_id"],
            "terminal_run_id": state["run_id"],
            "terminal_response_path": ResponsePath.CLARIFICATION.value,
            "terminal_content": proposal.question,
            "terminal_reason_code": "QUERY_AMBIGUOUS",
        }
    intent = routing.get("intent_decision", {})
    candidates = list(intent.get("candidates", []))
    if len(candidates) < 2:
        return safe_fallback(state)
    candidates = candidates[:2]
    catalog = current_intent_catalog(state)
    labels = [
        catalog.get(str(item.get("intent_id")), {}).get("name", "业务咨询") for item in candidates
    ]
    readable = "；".join(f"{index}. {label}" for index, label in enumerate(labels, start=1))
    question = (
        f"您的问题包含多个可能的咨询方向，请先选择要优先处理的一项：{readable}。"
        "原问题内容会保留，当前事项处理后可以继续咨询其他部分。请回复序号或对应名称。"
    )
    pending = PendingClarification(
        clarification_id=f"clar_{state['turn_id']}",
        source_layer="INTENT",
        origin_turn_id=state["turn_id"],
        candidate_refs=tuple(str(item.get("intent_id")) for item in candidates),
        question=question,
        options=tuple(
            ClarificationOption(
                option_id=f"intent_{index}",
                label=catalog.get(str(item.get("intent_id")), {}).get("name", "业务咨询"),
                candidate_ref=str(item.get("intent_id")),
            )
            for index, item in enumerate(candidates, start=1)
        ),
        resume_node="route_turn",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    return {
        "pending_clarification": pending.model_dump(mode="json"),
        "clarification_origin_query": state["effective_query"],
        "terminal_turn_id": state["turn_id"],
        "terminal_run_id": state["run_id"],
        "terminal_response_path": ResponsePath.CLARIFICATION.value,
        "terminal_content": question,
        "terminal_reason_code": "INTENT_AMBIGUOUS",
    }


def next_after_routing(state: AgentGraphState) -> str:
    """按路由结果进入 SOP、FAQ、RAG 检索、澄清或安全退出节点。"""

    response_path = state.get("routing_decision", {}).get("response_path")
    intent_id = state.get("routing_decision", {}).get("intent_decision", {}).get("intent_id")
    current_ids = {item["intent_id"] for item in state.get("sop_catalog", {}).values()}
    # 活动目录移除流程后，已经绑定历史发布版本的咨询仍可继续；
    # 因此不能只核对当前活动目录。
    pinned_continuation = state.get("sop_action") in {
        "CONTINUE",
        "CANCEL",
        "HUMAN",
        "LOOKUP_PENDING",
        "REPLAY",
        "SENSITIVE",
    } and intent_id == state.get("sop_definition", {}).get("intent_id")
    if state.get("sop_action") in {"SIDE_QUERY_KNOWLEDGE", "MULTI_INTENT"} and (
        response_path == ResponsePath.SOP.value
    ):
        # 语义解释已经确认当前消息不是补槽；即使全局分类又落到同一 Scene，
        # 也不能把知识问句或复合请求塞回 SOP 字段。
        return "safe_fallback"
    routing = RoutingDecision.model_validate(state.get("routing_decision", {}))
    if is_policy_answer_decision(routing, current_intent_catalog(state)):
        return "policy_answer"
    if response_path == ResponsePath.SOP.value and (
        intent_id in current_ids or pinned_continuation
    ):
        return "run_sop"
    if response_path == ResponsePath.FAQ.value:
        return "resolve_faq"
    if response_path == ResponsePath.RAG.value:
        return "retrieve_rag"
    if response_path == ResponsePath.CLARIFICATION.value:
        return "clarification"
    if response_path == ResponsePath.HUMAN.value:
        return "human_handoff"
    return "safe_fallback"


async def retrieve_rag(
    state: AgentGraphState,
    retriever: RagRetriever | None = None,
    artifact_repository: RuntimeArtifactRepository | None = None,
) -> AgentGraphState:
    """执行已准入的 RAG 检索；全文证据外置后只把引用写入 checkpoint。

    检索充分只代表证据可供生成使用，不授权直接发送客户消息。草稿仍须经过
    输出校验和坐席审核；检索器缺失或异常时返回空结果，由后续安全出口处理。
    """

    if retriever is None:
        return {
            "rag_turn_id": state["turn_id"],
            "rag_retrieval": None,
            "rag_retrieval_ref": None,
            "rag_retrieval_summary": None,
        }
    try:
        result = await retriever.retrieve(
            state["effective_query"],
            trace_id=state["trace_id"],
            rerank=True,
            filters=_retrieval_filters(state),
        )
    except Exception:
        # 检索器异常不能阻止客户收到可审计的固定安全回复。
        return {
            "rag_turn_id": state["turn_id"],
            "rag_retrieval": None,
            "rag_retrieval_ref": None,
            "rag_retrieval_summary": None,
        }
    payload = result.model_dump(mode="json")
    # 检索依赖可以返回 RetrievalResult，也可以提供同结构的可序列化正文。
    # 摘要必须从同一份正文提取，避免状态外正文与 checkpoint 摘要口径不一致。
    retrieval_id = str(payload.get("retrieval_id") or state["turn_id"])
    assessment = payload.get("assessment") or {}
    documents = payload.get("documents") or []
    summary = {
        "retrieval_id": retrieval_id,
        "assessment": assessment,
        "document_count": len(documents),
    }
    if artifact_repository is not None:
        try:
            reference = await artifact_repository.save(
                state=state,
                artifact_kind="RAG_RETRIEVAL",
                logical_id=retrieval_id,
                payload=payload,
                schema_version="rag-retrieval-v1",
                index_version=getattr(result, "index_version", None),
            )
        except (KeyError, TypeError, ValueError, OSError, PsycopgError):
            return {
                "rag_turn_id": state["turn_id"],
                "rag_retrieval": None,
                "rag_retrieval_ref": None,
                "rag_retrieval_summary": None,
            }
        return {
            "rag_turn_id": state["turn_id"],
            "rag_retrieval": None,
            "rag_retrieval_ref": reference.model_dump(mode="json"),
            "rag_retrieval_summary": summary,
        }
    return {
        "rag_turn_id": state["turn_id"],
        "rag_retrieval": payload,
        "rag_retrieval_summary": summary,
    }


def _retrieval_filters(state: AgentGraphState) -> RetrievalFilters:
    """仅从受信会话属性和已确认事实构造 RAG 过滤条件。"""

    values: dict[str, str] = {}
    channel = str(state.get("channel") or "").strip()
    if channel:
        values["channel"] = channel
    for fact in active_confirmed_facts(list(state.get("confirmed_facts", []))):
        if fact.fact_key not in {"product_code", "region"}:
            continue
        if fact.authority.value not in {"AUTHORITATIVE", "CONFIRMED"}:
            continue
        if isinstance(fact.value, str) and fact.value.strip():
            values[fact.fact_key] = fact.value.strip()
    return RetrievalFilters(**values)


def next_after_rag_retrieval(state: AgentGraphState) -> str:
    """通过基础门槛的证据进入语义判定，其余请求安全结束。"""

    retrieval = state.get("rag_retrieval_summary") or state.get("rag_retrieval")
    if not retrieval:
        return "safe_fallback"
    try:
        from shared.contracts.retrieval import RetrievalAssessment

        assessment = RetrievalAssessment.model_validate(retrieval.get("assessment", {}))
    except (TypeError, ValueError):
        return "safe_fallback"
    if not semantic_evidence_review_allowed(assessment):
        return "safe_fallback"
    return "grounded_answer"


async def grounded_answer(
    state: AgentGraphState,
    generator: GroundedAnswerGenerator | None = None,
    artifact_repository: RuntimeArtifactRepository | None = None,
) -> AgentGraphState:
    """生成坐席草稿；客户本轮只收到等待人工核对的固定提示。"""

    retrieval_payload = state.get("rag_retrieval")
    hydration_latency_ms = 0
    if retrieval_payload is None and state.get("rag_retrieval_ref"):
        if artifact_repository is None:
            return safe_fallback(state)
        try:
            reference = RuntimeArtifactRef.model_validate(state["rag_retrieval_ref"])
            loaded = await artifact_repository.load_many(
                (reference,),
                authorized_conversation_id=str(state["conversation_id"]),
                expected_kinds={reference.artifact_id: "RAG_RETRIEVAL"},
            )
            retrieval_payload = loaded.payloads[reference.artifact_id]
            hydration_latency_ms = loaded.latency_ms
        except (
            ArtifactValidationError,
            KeyError,
            TypeError,
            ValueError,
            OSError,
            PsycopgError,
        ):
            return safe_fallback(state)
    if generator is None or not retrieval_payload:
        return safe_fallback(state)
    try:
        from shared.contracts.retrieval import RetrievalResult

        retrieval = RetrievalResult.model_validate(retrieval_payload)
        draft, assessment, records = await generator.generate(
            state["effective_query"], retrieval, state=state
        )
    except Exception:
        return {
            **safe_fallback(state),
            "model_calls": [
                *state.get("model_calls", []),
            ],
        }
    retrieval = retrieval.model_copy(update={"assessment": assessment})
    updates: AgentGraphState = {
        "model_calls": [
            *state.get("model_calls", []),
            *(record.model_dump(mode="json") for record in records),
        ],
        "rag_generation_turn_id": state["turn_id"],
        "artifact_hydration_metrics": {
            "retrieval_artifact_load_ms": hydration_latency_ms,
            "hydrated_context_tokens": math.ceil(
                len(
                    json.dumps(
                        retrieval_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8")
                )
                / 4
            ),
        },
    }
    retrieval_payload = retrieval.model_dump(mode="json")
    if artifact_repository is not None:
        try:
            retrieval_ref = await artifact_repository.save(
                state=state,
                artifact_kind="RAG_RETRIEVAL",
                logical_id=retrieval.retrieval_id,
                payload=retrieval_payload,
                schema_version="rag-retrieval-v1",
            )
            updates["rag_retrieval"] = None
            updates["rag_retrieval_ref"] = retrieval_ref.model_dump(mode="json")
            updates["rag_retrieval_summary"] = {
                "retrieval_id": retrieval.retrieval_id,
                "assessment": retrieval.assessment.model_dump(mode="json"),
                "document_count": len(retrieval.documents),
            }
        except (KeyError, TypeError, ValueError, OSError, PsycopgError):
            return {**safe_fallback(state), **updates}
    else:
        updates["rag_retrieval"] = retrieval_payload
    if draft is None:
        return {**safe_fallback({**state, **updates}), **updates}
    draft_payload = draft.model_copy(update={"model": generator.model}).model_dump(mode="json")
    if artifact_repository is not None:
        try:
            draft_ref = await artifact_repository.save(
                state=state,
                artifact_kind="RAG_DRAFT",
                logical_id=draft.draft_id,
                payload=draft_payload,
                schema_version="rag-draft-v1",
            )
        except (KeyError, TypeError, ValueError, OSError, PsycopgError):
            return {**safe_fallback(state), **updates}
        updates["rag_draft"] = None
        updates["rag_draft_ref"] = draft_ref.model_dump(mode="json")
    else:
        updates["rag_draft"] = draft_payload
    updates.update(
        {
            "terminal_turn_id": state["turn_id"],
            "terminal_run_id": state["run_id"],
            "terminal_response_path": ResponsePath.RAG.value,
            "terminal_content": RAG_REVIEW_PENDING_REPLY,
            "terminal_reason_code": "RAG_REVIEW_REQUIRED",
        }
    )
    return updates


def next_after_grounded_answer(state: AgentGraphState) -> str:
    """生成草稿成功后进入真实人工停点，失败则使用固定安全回复。"""

    return (
        "await_rag_review"
        if state.get("rag_draft_ref") or state.get("rag_draft")
        else "safe_fallback"
    )


def finalize_faq(state: AgentGraphState) -> AgentGraphState:
    """精确 FAQ 唯一命中且答案适用时直答，否则继续使用固定安全提示。"""

    result = FaqResolutionResult.model_validate(state["faq_resolution"])
    if result.match_status is FaqMatchStatus.AMBIGUOUS and state.get("pending_clarification"):
        pending = PendingClarification.model_validate(state["pending_clarification"])
        if pending.source_layer.value == "FAQ" and pending.origin_turn_id == state["turn_id"]:
            return {
                "terminal_turn_id": state["turn_id"],
                "terminal_run_id": state["run_id"],
                "terminal_response_path": ResponsePath.CLARIFICATION.value,
                "terminal_content": pending.question,
                "terminal_reason_code": "FAQ_AMBIGUOUS",
            }
    if result.match_status is FaqMatchStatus.MATCHED and result.answer:
        terminal_reason = (
            "FAQ_EXACT_MATCH" if "FAQ_EXACT_MATCH" in result.reason_codes else "FAQ_SEMANTIC_MATCH"
        )
        return {
            "terminal_turn_id": state["turn_id"],
            "terminal_run_id": state["run_id"],
            "terminal_response_path": ResponsePath.FAQ.value,
            "terminal_content": result.answer,
            "terminal_reason_code": terminal_reason,
        }
    return {
        "terminal_turn_id": state["turn_id"],
        "terminal_run_id": state["run_id"],
        "terminal_response_path": ResponsePath.SAFE_FALLBACK.value,
        "terminal_content": SAFE_FALLBACK_REPLY,
        "terminal_reason_code": "NO_GROUNDED_ANSWER",
    }


def next_after_faq(state: AgentGraphState, *, rag_enabled: bool) -> str:
    """FAQ 先完成条目判断；仅真实无候选且重新过安全门时顺序转 RAG。"""

    result = FaqResolutionResult.model_validate(state["faq_resolution"])
    if (
        rag_enabled
        and result.match_status is FaqMatchStatus.NOT_MATCHED
        and result.miss_reason is FaqMissReason.NO_CANDIDATE
        and faq_rag_fallback_allowed(
            RoutingDecision.model_validate(state["routing_decision"]), state
        )
    ):
        return "retrieve_rag"
    return "finalize_faq"


def finish_sop(state: AgentGraphState) -> AgentGraphState:
    """保留本轮 SOP 输出，交给下一条条件边判断是否需要人工停点。"""
    return {}


def build_graph(
    *,
    checkpointer: object,
    active_profile: DecisionProfile = DecisionProfile.BASELINE,
    decision_timeout_ms: int = 800,
    redis: Redis | None = None,
    semantic_search: SemanticSearch | None = None,
    classifier: LlmIntentClassifier | None = None,
    jev_intent_adapter: JevIntentDecisionAdapter | None = None,
    query_rewriter: LlmQueryRewriter | None = None,
    history_recall: HistoryRecallCoordinator | None = None,
    context_summarizer: ContextSummarizer | None = None,
    context_budget_policy: ContextBudgetPolicy | None = None,
    context_summary_repository: ContextSummaryRepository | None = None,
    artifact_repository: RuntimeArtifactRepository | None = None,
    tool_artifact_externalize_bytes: int = 4096,
    sop_definitions: dict[str, dict] | None = None,
    intent_catalog: dict[str, dict[str, str]] | None = None,
    intent_rules: list[dict[str, object]] | None = None,
    llm_settings: ServiceSettings | None = None,
    business_client: SopBusinessClient | None = None,
    business_timezone: str = "Asia/Shanghai",
    human_review_enabled: bool = False,
    rag_retriever: RagRetriever | None = None,
    rag_enabled: bool = False,
    grounded_generator: GroundedAnswerGenerator | None = None,
    faq_reranker: DashScopeTextReranker | None = None,
    faq_rerank_min_score: float = 0.50,
    faq_rerank_min_margin: float = 0.10,
):
    """创建当前项目唯一的 LangGraph 主流程，并注入可选语义检索服务。

    intent_catalog 和 intent_rules 允许调用方显式注入配置；未提供时从 Redis
    已发布投影加载意图目录与高精度规则。
    """

    graph = StateGraph(AgentGraphState)

    def add_node(name: str, action, *, receives_config: bool = False) -> None:
        """所有主图节点统一经过无正文计时包装。"""

        graph.add_node(name, observe_node(name, action, receives_config=receives_config))

    budget_policy = context_budget_policy or ContextBudgetPolicy(
        effective_window_tokens=32768,
        compression_trigger_ratio=0.8,
        dynamic_input_reserve_tokens=8192,
        output_reserve_tokens=4096,
        recent_turn_budget_tokens=11264,
        summary_budget_tokens=2048,
    )
    injected_rule_release = (
        str(intent_rules[0].get("release_version", "test-intent-rules-v1"))
        if intent_rules
        else None
    )
    add_node("prepare_turn", prepare_turn, receives_config=True)
    add_node("guard_input", guard_input)
    add_node(
        "compress_context",
        partial(
            compress_context,
            policy=budget_policy,
            summarizer=context_summarizer,
            summary_repository=context_summary_repository,
        ),
    )
    add_node(
        "load_catalog",
        (
            (
                lambda _state: {
                    "intent_catalog": dict(intent_catalog),
                    "intent_rule_release": injected_rule_release,
                    "intent_rules": list(intent_rules or []),
                }
            )
            if intent_catalog is not None
            else partial(load_catalog_node, redis=redis)
        ),
    )
    add_node(
        "resolve_query",
        partial(
            resolve_query_with_model,
            rewriter=query_rewriter,
            history_recall=history_recall,
        ),
    )
    add_node("history_clarification", history_clarification)
    add_node(
        "check_active_sop",
        partial(
            check_active_sop,
            redis=redis,
            definitions=sop_definitions,
            llm_settings=llm_settings,
            history_recall=history_recall,
        ),
    )
    add_node(
        "run_sop",
        partial(
            run_sop,
            llm_settings=llm_settings,
            artifact_repository=artifact_repository,
        ),
    )
    add_node(
        "run_sop_side_query",
        partial(
            run_sop_side_query,
            client=business_client,
            artifact_repository=artifact_repository,
            externalize_bytes=tool_artifact_externalize_bytes,
        ),
    )
    add_node(
        "run_sop_tools",
        partial(
            run_sop_tools,
            client=business_client,
            timezone=business_timezone,
            artifact_repository=artifact_repository,
            externalize_bytes=tool_artifact_externalize_bytes,
        ),
    )
    add_node(
        "decide_query",
        partial(
            decide_query,
            configured_profile=active_profile,
            timeout_ms=decision_timeout_ms,
        ),
    )
    add_node("safe_fallback", safe_fallback)
    add_node("policy_answer", partial(policy_answer, redis=redis))
    add_node("human_handoff", human_handoff)
    add_node(
        "retrieve_rag",
        partial(
            retrieve_rag,
            retriever=rag_retriever,
            artifact_repository=artifact_repository,
        ),
    )
    add_node(
        "grounded_answer",
        partial(
            grounded_answer,
            generator=grounded_generator,
            artifact_repository=artifact_repository,
        ),
    )
    add_node(
        "await_rag_review",
        partial(await_rag_review, artifact_repository=artifact_repository),
        receives_config=True,
    )
    add_node(
        "apply_rag_review",
        partial(apply_rag_review, artifact_repository=artifact_repository),
    )
    add_node(
        "route_turn",
        partial(
            route_turn,
            redis=redis,
            semantic_search=semantic_search,
            classifier=classifier,
            jev_intent_adapter=jev_intent_adapter,
            rag_enabled=rag_enabled,
        ),
    )
    add_node(
        "resolve_faq",
        partial(
            resolve_faq,
            redis=redis,
            reranker=faq_reranker,
            rerank_min_score=faq_rerank_min_score,
            rerank_min_margin=faq_rerank_min_margin,
        ),
    )
    add_node("finalize_faq", finalize_faq)
    add_node("clarification", clarification)
    graph.add_edge(START, "prepare_turn")
    graph.add_edge("prepare_turn", "guard_input")
    graph.add_conditional_edges(
        "guard_input",
        next_after_input_guardrail,
        {"safe_fallback": "safe_fallback", "compress_context": "compress_context"},
    )
    graph.add_edge("compress_context", "load_catalog")
    graph.add_edge("load_catalog", "check_active_sop")
    graph.add_conditional_edges(
        "check_active_sop",
        next_after_sop_check,
        {
            "route_turn": "route_turn",
            "decide_query": "decide_query",
            "safe_fallback": "safe_fallback",
            "run_sop_side_query": "run_sop_side_query",
        },
    )
    graph.add_edge("run_sop_side_query", END)
    graph.add_edge("decide_query", "resolve_query")
    graph.add_conditional_edges(
        "resolve_query",
        next_after_query_resolution,
        {
            "history_clarification": "history_clarification",
            "route_turn": "route_turn",
        },
    )
    graph.add_edge("history_clarification", END)
    graph.add_conditional_edges(
        "route_turn",
        next_after_routing,
        {
            "run_sop": "run_sop",
            "resolve_faq": "resolve_faq",
            "retrieve_rag": "retrieve_rag",
            "clarification": "clarification",
            "human_handoff": "human_handoff",
            "policy_answer": "policy_answer",
            "safe_fallback": "safe_fallback",
        },
    )
    graph.add_conditional_edges(
        "resolve_faq",
        partial(next_after_faq, rag_enabled=rag_enabled),
        {"retrieve_rag": "retrieve_rag", "finalize_faq": "finalize_faq"},
    )
    graph.add_conditional_edges(
        "retrieve_rag",
        next_after_rag_retrieval,
        {"grounded_answer": "grounded_answer", "safe_fallback": "safe_fallback"},
    )
    graph.add_conditional_edges(
        "grounded_answer",
        next_after_grounded_answer,
        {"await_rag_review": "await_rag_review", "safe_fallback": "safe_fallback"},
    )
    graph.add_edge("await_rag_review", "apply_rag_review")
    graph.add_conditional_edges(
        "apply_rag_review",
        next_after_rag_review,
        {"retrieve_rag": "retrieve_rag", "end": END},
    )
    graph.add_edge("finalize_faq", END)
    graph.add_edge("safe_fallback", END)
    graph.add_edge("policy_answer", END)
    graph.add_edge("human_handoff", END)
    graph.add_edge("clarification", END)
    if human_review_enabled:
        add_node(
            "prepare_review",
            partial(prepare_review, artifact_repository=artifact_repository),
        )
        add_node("await_review", await_review, receives_config=True)
        add_node(
            "apply_review",
            partial(apply_review, artifact_repository=artifact_repository),
        )
        add_node(
            "run_approved_operation",
            partial(
                run_approved_operation,
                client=business_client,
                artifact_repository=artifact_repository,
            ),
        )
        add_node("finish_sop", finish_sop)
        graph.add_conditional_edges(
            "run_sop",
            next_after_sop,
            {
                "run_sop_side_query": "run_sop_side_query",
                "run_sop_tools": "run_sop_tools",
                "end": "finish_sop",
            },
        )
        graph.add_edge("run_sop_tools", "finish_sop")
        graph.add_conditional_edges(
            "finish_sop", next_review_step, {"prepare_review": "prepare_review", "end": END}
        )
        graph.add_edge("prepare_review", "await_review")
        graph.add_edge("await_review", "apply_review")
        graph.add_conditional_edges(
            "apply_review",
            next_after_review,
            {
                "prepare_review": "prepare_review",
                "end": END,
                "run_approved_operation": "run_approved_operation",
                "run_sop_tools": "run_sop_tools",
            },
        )
        graph.add_conditional_edges(
            "run_approved_operation",
            next_review_step,
            {"prepare_review": "prepare_review", "end": END},
        )
    else:
        graph.add_conditional_edges(
            "run_sop",
            next_after_sop,
            {
                "run_sop_side_query": "run_sop_side_query",
                "run_sop_tools": "run_sop_tools",
                "end": END,
            },
        )
        graph.add_edge("run_sop_tools", END)
    return graph.compile(checkpointer=checkpointer)
