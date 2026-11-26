"""读取已发布 FAQ 副本，完成精确或语义条目匹配及答案适用性检查。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError

from services.ai_runtime.retrieval.reranking import DashScopeTextReranker
from services.content.faq_cache import (
    FAQ_ACTIVE_KEY,
    faq_item_field,
    faq_items_key,
    faq_question_digest,
)
from shared.contracts.enums import (
    AnswerEligibility,
    ConfidenceBand,
    FaqMatchStatus,
    FaqMissReason,
)
from shared.contracts.faq import FaqCandidate, FaqExactHint, FaqResolutionResult
from shared.contracts.routing import ClarificationOption, PendingClarification, RoutingDecision

FAQ_SEMANTIC_MIN_SCORE = 0.60
FAQ_SEMANTIC_MARGIN = 0.03


def _contract_score(value: float) -> float:
    """把向量相似度限制到 FAQ 候选契约允许的 0 到 1。"""

    return max(0.0, min(1.0, value))


def _miss(
    *,
    intent_id: str,
    release_version: str,
    reason: FaqMissReason,
    eligibility: AnswerEligibility,
    reason_code: str,
) -> FaqResolutionResult:
    """统一构造不允许直答的 FAQ 结果，避免错误分支漏写原因。"""

    return FaqResolutionResult(
        intent_id=intent_id,
        confidence_band=ConfidenceBand.LOW,
        match_status=FaqMatchStatus.NOT_MATCHED,
        miss_reason=reason,
        answer_eligibility=eligibility,
        faq_version=release_version,
        index_version=release_version,
        reason_codes=(reason_code,),
    )


def _scope_matches(answer: dict[str, object], state: dict[str, object]) -> bool:
    """空范围表示不限；有范围时必须存在完全相同的已知值。"""

    for answer_field, state_field in (
        ("product_codes", "product_code"),
        ("channels", "channel"),
        ("regions", "region"),
    ):
        allowed = answer.get(answer_field) or []
        if allowed and state.get(state_field) not in allowed:
            return False
    return True


def _within_validity(answer: dict[str, object], now: datetime) -> bool:
    """按带时区的生效起止时间判断固定答案当前是否可用。"""

    start = answer.get("effective_from")
    end = answer.get("effective_to")
    return not (
        (start and now < datetime.fromisoformat(str(start)))
        or (end and now > datetime.fromisoformat(str(end)))
    )


async def resolve_faq(
    state: dict[str, object],
    redis: Redis | None,
    *,
    reranker: DashScopeTextReranker | None = None,
    rerank_min_score: float = 0.50,
    rerank_min_margin: float = 0.10,
) -> dict[str, object]:
    """消费精确或语义路由留下的条目线索，读取答案包并检查当前请求是否适用。"""

    if state.get("faq_turn_id") == state["turn_id"] and state.get("faq_resolution"):
        return {}
    routing = RoutingDecision.model_validate(state["routing_decision"])
    intent_id = routing.intent_decision.intent_id or "UNKNOWN"
    hint_data = state.get("faq_exact_hint")
    if not hint_data and state.get("semantic_faq_candidates"):
        return await _resolve_semantic_faq(
            state,
            redis,
            intent_id,
            reranker=reranker,
            rerank_min_score=rerank_min_score,
            rerank_min_margin=rerank_min_margin,
        )
    if not hint_data:
        # 只有明确完成过语义检索才能证明“确实没有内容候选”。旧 checkpoint、
        # 未执行检索或依赖异常都不能借 NO_CANDIDATE 绕到 RAG。
        search_unavailable = state.get("semantic_faq_search_status") != "COMPLETED"
        result = _miss(
            intent_id=intent_id,
            release_version="unavailable",
            reason=(
                FaqMissReason.DEPENDENCY_UNAVAILABLE
                if search_unavailable
                else FaqMissReason.NO_CANDIDATE
            ),
            eligibility=AnswerEligibility.INELIGIBLE,
            reason_code=(
                "FAQ_SEMANTIC_SEARCH_UNAVAILABLE"
                if search_unavailable
                else "FAQ_SEMANTIC_NO_CANDIDATE"
            ),
        )
        return {"faq_turn_id": state["turn_id"], "faq_resolution": result.model_dump(mode="json")}
    hint = FaqExactHint.model_validate(hint_data)
    release_version = hint.release_version
    if hint.intent_id != intent_id:
        result = _miss(
            intent_id=intent_id,
            release_version=release_version,
            reason=FaqMissReason.CONTENT_INCONSISTENT,
            eligibility=AnswerEligibility.INELIGIBLE,
            reason_code="FAQ_HINT_INTENT_MISMATCH",
        )
        return {"faq_turn_id": state["turn_id"], "faq_resolution": result.model_dump(mode="json")}
    if redis is None:
        result = _miss(
            intent_id=intent_id,
            release_version=release_version,
            reason=FaqMissReason.DEPENDENCY_UNAVAILABLE,
            eligibility=AnswerEligibility.INELIGIBLE,
            reason_code="FAQ_CACHE_NOT_CONFIGURED",
        )
        return {"faq_turn_id": state["turn_id"], "faq_resolution": result.model_dump(mode="json")}
    try:
        raw = await redis.hget(
            faq_items_key(release_version),
            faq_item_field(hint.faq_item_id, hint.faq_version),
        )
    except RedisError:
        result = _miss(
            intent_id=intent_id,
            release_version=release_version,
            reason=FaqMissReason.DEPENDENCY_UNAVAILABLE,
            eligibility=AnswerEligibility.INELIGIBLE,
            reason_code="FAQ_CACHE_UNAVAILABLE",
        )
    except LookupError as error:
        result = _miss(
            intent_id=intent_id,
            release_version=release_version,
            reason=FaqMissReason.CONTENT_INCONSISTENT,
            eligibility=AnswerEligibility.INELIGIBLE,
            reason_code=str(error),
        )
    else:
        if raw is None:
            result = _miss(
                intent_id=intent_id,
                release_version=release_version,
                reason=FaqMissReason.CONTENT_INCONSISTENT,
                eligibility=AnswerEligibility.INELIGIBLE,
                reason_code="FAQ_ITEM_CACHE_NOT_FOUND",
            )
        else:
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                result = _miss(
                    intent_id=intent_id,
                    release_version=release_version,
                    reason=FaqMissReason.CONTENT_INCONSISTENT,
                    eligibility=AnswerEligibility.INELIGIBLE,
                    reason_code="FAQ_ITEM_CACHE_INVALID",
                )
                return {
                    "faq_turn_id": state["turn_id"],
                    "faq_resolution": result.model_dump(mode="json"),
                }
            if not isinstance(entry, dict):
                result = _miss(
                    intent_id=intent_id,
                    release_version=release_version,
                    reason=FaqMissReason.CONTENT_INCONSISTENT,
                    eligibility=AnswerEligibility.INELIGIBLE,
                    reason_code="FAQ_ITEM_CACHE_INVALID",
                )
                return {
                    "faq_turn_id": state["turn_id"],
                    "faq_resolution": result.model_dump(mode="json"),
                }
            if (
                entry.get("faq_item_id") != hint.faq_item_id
                or entry.get("faq_group_id") != hint.faq_group_id
                or entry.get("faq_version") != hint.faq_version
            ):
                result = _miss(
                    intent_id=intent_id,
                    release_version=release_version,
                    reason=FaqMissReason.CONTENT_INCONSISTENT,
                    eligibility=AnswerEligibility.INELIGIBLE,
                    reason_code="FAQ_ITEM_CACHE_MISMATCH",
                )
            else:
                result = _select_answer(
                    intent_id,
                    release_version,
                    entry,
                    state,
                    winning_variant_id=hint.winning_variant_id,
                )
    return {"faq_turn_id": state["turn_id"], "faq_resolution": result.model_dump(mode="json")}


async def _resolve_semantic_faq(
    state: dict[str, object],
    redis: Redis | None,
    intent_id: str,
    *,
    reranker: DashScopeTextReranker | None,
    rerank_min_score: float,
    rerank_min_margin: float,
) -> dict[str, object]:
    """比较 FAQ 语义候选，再复用 Redis 答案包和适用性检查。"""

    candidates = [dict(item) for item in state.get("semantic_faq_candidates", [])]
    release_version = "unavailable"
    grouped_candidates = tuple(
        FaqCandidate(
            faq_item_id=str(candidate["faq_item_id"]),
            winning_variant_id=str(candidate["winning_variant_id"]),
            score=_contract_score(float(candidate["score"])),
        )
        for candidate in candidates
    )
    top1 = _contract_score(float(candidates[0]["score"])) if candidates else None
    top2 = _contract_score(float(candidates[1]["score"])) if len(candidates) > 1 else None
    margin = top1 - top2 if top1 is not None and top2 is not None else None
    policy = dict(state.get("semantic_thresholds", {}))
    min_score = float(policy.get("faq_min_score", FAQ_SEMANTIC_MIN_SCORE))
    min_margin = float(policy.get("faq_margin", FAQ_SEMANTIC_MARGIN))
    if not candidates or top1 is None or top1 < min_score:
        result = _miss(
            intent_id=intent_id,
            release_version=release_version,
            reason=FaqMissReason.NO_CANDIDATE,
            eligibility=AnswerEligibility.INELIGIBLE,
            reason_code="FAQ_SEMANTIC_SCORE_BELOW_THRESHOLD",
        )
        return {"faq_turn_id": state["turn_id"], "faq_resolution": result.model_dump(mode="json")}

    if redis is None:
        result = _miss(
            intent_id=intent_id,
            release_version=release_version,
            reason=FaqMissReason.DEPENDENCY_UNAVAILABLE,
            eligibility=AnswerEligibility.INELIGIBLE,
            reason_code="FAQ_CACHE_NOT_CONFIGURED",
        )
        return {"faq_turn_id": state["turn_id"], "faq_resolution": result.model_dump(mode="json")}
    try:
        active = await redis.get(FAQ_ACTIVE_KEY)
    except RedisError:
        result = _miss(
            intent_id=intent_id,
            release_version=release_version,
            reason=FaqMissReason.DEPENDENCY_UNAVAILABLE,
            eligibility=AnswerEligibility.INELIGIBLE,
            reason_code="FAQ_CACHE_UNAVAILABLE",
        )
        return {"faq_turn_id": state["turn_id"], "faq_resolution": result.model_dump(mode="json")}
    if active is None:
        result = _miss(
            intent_id=intent_id,
            release_version=release_version,
            reason=FaqMissReason.DEPENDENCY_UNAVAILABLE,
            eligibility=AnswerEligibility.INELIGIBLE,
            reason_code="FAQ_ACTIVE_RELEASE_NOT_FOUND",
        )
        return {"faq_turn_id": state["turn_id"], "faq_resolution": result.model_dump(mode="json")}

    release_version = active.decode() if isinstance(active, bytes) else str(active)
    loaded: list[tuple[dict[str, object], dict[str, object]]] = []
    try:
        for candidate in candidates:
            raw = await redis.hget(
                faq_items_key(release_version),
                faq_item_field(str(candidate["faq_item_id"]), str(candidate["faq_version"])),
            )
            if raw is None:
                raise LookupError("FAQ_ITEM_CACHE_NOT_FOUND")
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as error:
                raise LookupError("FAQ_ITEM_CACHE_INVALID") from error
            if not isinstance(entry, dict):
                raise LookupError("FAQ_ITEM_CACHE_INVALID")
            if (
                entry.get("faq_item_id") != candidate["faq_item_id"]
                or entry.get("faq_group_id") != candidate["faq_group_id"]
                or entry.get("faq_version") != candidate["faq_version"]
                or not entry.get("answers")
            ):
                raise LookupError("FAQ_ITEM_CACHE_MISMATCH")
            loaded.append((candidate, entry))
    except RedisError:
        result = _miss(
            intent_id=intent_id,
            release_version=release_version,
            reason=FaqMissReason.DEPENDENCY_UNAVAILABLE,
            eligibility=AnswerEligibility.INELIGIBLE,
            reason_code="FAQ_CACHE_UNAVAILABLE",
        )
        return {"faq_turn_id": state["turn_id"], "faq_resolution": result.model_dump(mode="json")}
    except LookupError as error:
        result = _miss(
            intent_id=intent_id,
            release_version=release_version,
            reason=FaqMissReason.CONTENT_INCONSISTENT,
            eligibility=AnswerEligibility.INELIGIBLE,
            reason_code=str(error),
        )
        return {"faq_turn_id": state["turn_id"], "faq_resolution": result.model_dump(mode="json")}

    applicable = [
        (candidate, entry)
        for candidate, entry in loaded
        if len(
            [
                answer
                for answer in entry.get("answers", [])
                if _scope_matches(answer, state) and _within_validity(answer, datetime.now(UTC))
            ]
        )
        == 1
    ]
    selected_top_score = top1
    selected_second_score = top2
    selected_candidates = grouped_candidates
    selected_reason_code = "FAQ_SEMANTIC_MATCH"
    if len(applicable) == 1:
        selected_candidate, selected_entry = applicable[0]
    elif len(loaded) > 1 and margin is not None and margin < min_margin:
        choices = [
            (candidate, entry)
            for candidate, entry in applicable
            if float(candidate["score"]) >= min_score
            and top1 - float(candidate["score"]) < min_margin
        ][:3]
        rerank_reason = "FAQ_RERANK_UNAVAILABLE"
        if reranker is not None and len(choices) >= 2:
            documents = [
                str(entry["title"])
                + "\n"
                + "\n".join(
                    str(question["question"])
                    for question in entry.get("questions", [])
                    if isinstance(question, dict) and question.get("question")
                )
                + "\n"
                + "\n".join(str(answer["answer"]) for answer in entry.get("answers", []))
                for _candidate, entry in choices
            ]
            try:
                reranked = await reranker.rerank(
                    str(state["effective_query"]),
                    documents,
                    top_n=len(documents),
                )
            except (httpx.HTTPError, KeyError, TypeError, ValueError):
                reranked = ()
                rerank_reason = "FAQ_RERANK_REQUEST_FAILED"
            if len(reranked) >= 2:
                rerank_top = reranked[0]
                rerank_second = reranked[1]
                if rerank_top.score < rerank_min_score:
                    rerank_reason = "FAQ_RERANK_SCORE_BELOW_THRESHOLD"
                    selected_candidate = None
                elif rerank_top.score - rerank_second.score < rerank_min_margin:
                    rerank_reason = "FAQ_RERANK_MARGIN_TOO_SMALL"
                    selected_candidate = None
                else:
                    selected_candidate, selected_entry = choices[rerank_top.index]
                    selected_top_score = rerank_top.score
                    selected_second_score = rerank_second.score
                    selected_candidates = tuple(
                        FaqCandidate(
                            faq_item_id=str(choices[item.index][0]["faq_item_id"]),
                            winning_variant_id=str(choices[item.index][0]["winning_variant_id"]),
                            score=_contract_score(item.score),
                        )
                        for item in reranked
                    )
                    selected_reason_code = "FAQ_RERANK_MATCH"
            else:
                selected_candidate = None
        else:
            selected_candidate = None

        if selected_candidate is None:
            result = FaqResolutionResult(
                intent_id=intent_id,
                confidence_band=ConfidenceBand.MEDIUM,
                match_status=FaqMatchStatus.AMBIGUOUS,
                miss_reason=FaqMissReason.AMBIGUOUS,
                answer_eligibility=AnswerEligibility.INELIGIBLE,
                faq_version=str(loaded[0][1].get("faq_version", release_version)),
                index_version=release_version,
                top1_score=top1,
                top2_item_score=top2,
                score_margin=margin,
                candidates=grouped_candidates,
                reason_codes=("FAQ_SCORE_MARGIN_TOO_SMALL", rerank_reason),
            )
            # 仅将分数达标、与第一名接近的条目作为澄清选项，避免低分条目混进来。
            if len(choices) < 2:
                return {
                    "faq_turn_id": state["turn_id"],
                    "faq_resolution": result.model_dump(mode="json"),
                }
            options = tuple(
                ClarificationOption(
                    option_id=f"faq_{index}",
                    label=str(entry["title"]),
                    candidate_ref=str(candidate["faq_item_id"]),
                )
                for index, (candidate, entry) in enumerate(choices, start=1)
            )
            question = (
                "您具体想了解哪一项？"
                + "；".join(
                    f"{index}. {option.label}" for index, option in enumerate(options, start=1)
                )
                + "。请回复序号或对应问题。"
            )
            pending = PendingClarification(
                clarification_id=f"clar_faq_{state['turn_id']}",
                source_layer="FAQ",
                origin_turn_id=str(state["turn_id"]),
                candidate_refs=tuple(o.candidate_ref for o in options),
                question=question,
                options=options,
                resume_node="resolve_faq",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
            hints = {
                str(candidate["faq_item_id"]): FaqExactHint(
                    intent_id=intent_id,
                    faq_group_id=str(candidate["faq_group_id"]),
                    faq_item_id=str(candidate["faq_item_id"]),
                    faq_version=str(candidate["faq_version"]),
                    winning_variant_id=str(candidate["winning_variant_id"]),
                    release_version=release_version,
                    locale="zh-CN",
                    query_digest=faq_question_digest(str(state["effective_query"])),
                ).model_dump(mode="json")
                for candidate, _entry in choices
            }
            return {
                "faq_turn_id": state["turn_id"],
                "faq_resolution": result.model_dump(mode="json"),
                "pending_clarification": pending.model_dump(mode="json"),
                "clarification_faq_hints": hints,
                "clarification_origin_query": state["effective_query"],
            }
    elif loaded:
        selected_candidate, selected_entry = loaded[0]
    else:
        result = _miss(
            intent_id=intent_id,
            release_version=release_version,
            reason=FaqMissReason.CONTENT_INCONSISTENT,
            eligibility=AnswerEligibility.INELIGIBLE,
            reason_code="FAQ_ITEM_CACHE_NOT_FOUND",
        )
        return {"faq_turn_id": state["turn_id"], "faq_resolution": result.model_dump(mode="json")}

    result = _select_answer(
        intent_id,
        release_version,
        selected_entry,
        state,
        winning_variant_id=str(selected_candidate["winning_variant_id"]),
        top1_score=_contract_score(float(selected_top_score)),
        top2_item_score=(
            _contract_score(float(selected_second_score))
            if selected_second_score is not None
            else None
        ),
        candidates=selected_candidates,
        reason_code=selected_reason_code,
    )
    return {"faq_turn_id": state["turn_id"], "faq_resolution": result.model_dump(mode="json")}


def _select_answer(
    intent_id: str,
    release_version: str,
    entry: dict[str, object],
    state: dict[str, object],
    *,
    winning_variant_id: str,
    top1_score: float = 1.0,
    top2_item_score: float | None = None,
    candidates: tuple[FaqCandidate, ...] | None = None,
    reason_code: str = "FAQ_EXACT_MATCH",
) -> FaqResolutionResult:
    """从同一条目的答案版本中选出当前范围和时间唯一适用的一条。"""

    answers = [dict(item) for item in entry.get("answers", [])]
    scope_matches = [answer for answer in answers if _scope_matches(answer, state)]
    now = datetime.now(UTC)
    eligible = [answer for answer in scope_matches if _within_validity(answer, now)]
    if not scope_matches:
        return _miss(
            intent_id=intent_id,
            release_version=release_version,
            reason=FaqMissReason.SCOPE_MISMATCH,
            eligibility=AnswerEligibility.INELIGIBLE,
            reason_code="FAQ_ANSWER_SCOPE_MISMATCH",
        )
    if not eligible:
        return _miss(
            intent_id=intent_id,
            release_version=release_version,
            reason=FaqMissReason.ANSWER_EXPIRED,
            eligibility=AnswerEligibility.EXPIRED,
            reason_code="FAQ_ANSWER_NOT_EFFECTIVE",
        )
    if len(eligible) != 1:
        return FaqResolutionResult(
            intent_id=intent_id,
            confidence_band=ConfidenceBand.MEDIUM,
            match_status=FaqMatchStatus.AMBIGUOUS,
            miss_reason=FaqMissReason.AMBIGUOUS,
            answer_eligibility=AnswerEligibility.INELIGIBLE,
            faq_version=str(entry["faq_version"]),
            index_version=release_version,
            reason_codes=("MULTIPLE_ELIGIBLE_FAQ_ANSWERS",),
        )
    answer = eligible[0]
    item_id = str(entry["faq_item_id"])
    return FaqResolutionResult(
        intent_id=intent_id,
        faq_item_id=item_id,
        faq_title=str(entry["title"]),
        winning_variant_id=winning_variant_id,
        top1_score=top1_score,
        top2_item_score=top2_item_score,
        score_margin=(top1_score - top2_item_score if top2_item_score is not None else None),
        confidence_band=ConfidenceBand.HIGH,
        match_status=FaqMatchStatus.MATCHED,
        answer_eligibility=AnswerEligibility.ELIGIBLE,
        answer_id=str(answer["answer_id"]),
        answer=str(answer["answer"]),
        answer_source_ref=str(answer["source_ref"]),
        answer_content_hash=str(answer["content_hash"]),
        faq_version=str(entry["faq_version"]),
        index_version=release_version,
        candidates=candidates
        or (
            FaqCandidate(
                faq_item_id=item_id,
                winning_variant_id=winning_variant_id,
                score=top1_score,
            ),
        ),
        reason_codes=(reason_code,),
    )
