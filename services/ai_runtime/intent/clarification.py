"""解析用户对有限澄清选项的明确选择，不用模型猜测“第一项”指哪件事。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError

from services.content.faq_cache import FAQ_ACTIVE_KEY
from shared.contracts.enums import (
    IntentDecisionStatus,
    IntentMatchSource,
    IntentRouteTarget,
    RequestKind,
    RequestScope,
    ResponsePath,
    ScopeMatchSource,
)
from shared.contracts.faq import FaqExactHint
from shared.contracts.routing import IntentDecision, PendingClarification, RoutingDecision


@dataclass(frozen=True)
class ClarificationChoiceResolution:
    """一次有限选择解析出的既有路由或需重新路由的完整 Query。"""

    routing_decision: RoutingDecision | None = None
    faq_hint: FaqExactHint | None = None
    resolved_query: str | None = None


async def resolve_clarification_choice(
    query: str,
    state: dict[str, object],
    catalog: dict[str, dict[str, str]],
    redis: Redis | None,
) -> ClarificationChoiceResolution | None:
    """在未过期选项中匹配序号或完整名称；FAQ 版本变化后不能复用旧条目线索。

    未明确选择时返回 None，让原路由处理客户补充的新问题。仅在保存的
    候选集合内选择，不能根据客户给出的任意意图编号或条目编号绕过校验。
    """

    raw = state.get("pending_clarification")
    if not raw:
        return None
    pending = PendingClarification.model_validate(raw)
    if pending.expires_at <= datetime.now(UTC):
        return None
    selected = None
    for index, option in enumerate(pending.options, start=1):
        chinese_index = "一二三"[index - 1] if index <= 3 else str(index)
        if query.rstrip("。!?") in {
            str(index),
            f"第{index}项",
            f"第{chinese_index}项",
            option.label,
        }:
            selected = option
            break
    if selected is None:
        return None
    if pending.source_layer.value == "QUERY":
        resolutions = state.get("clarification_query_resolutions", {})
        if not isinstance(resolutions, dict):
            return None
        resolved_query = resolutions.get(selected.candidate_ref)
        if not isinstance(resolved_query, str) or not resolved_query.strip():
            return None
        return ClarificationChoiceResolution(resolved_query=resolved_query)
    hint = None
    if pending.source_layer.value == "FAQ":
        raw_hint = state.get("clarification_faq_hints", {}).get(selected.candidate_ref)
        if not raw_hint or redis is None:
            return None
        hint = FaqExactHint.model_validate(raw_hint)
        try:
            active = await redis.get(FAQ_ACTIVE_KEY)
        except RedisError:
            return None
        if active is None or active.decode() != hint.release_version:
            return None
        intent_id = hint.intent_id
    else:
        intent_id = selected.candidate_ref
    definition = catalog.get(intent_id)
    if definition is None:
        return None
    target = IntentRouteTarget(definition["route_target"])
    return ClarificationChoiceResolution(
        routing_decision=RoutingDecision(
            intent_decision=IntentDecision(
                status=IntentDecisionStatus.MATCHED,
                intent_id=intent_id,
                route_target=target,
                match_source=IntentMatchSource.KEYWORD,
                catalog_version=hint.release_version if hint else "python-rules-v2",
                matched_source_ref=f"clarification:{pending.clarification_id}",
                reason_codes=("USER_CLARIFICATION_SELECTION",),
            ),
            request_scope=RequestScope.IN_SCOPE,
            request_kind=RequestKind(definition["request_kind"]),
            scope_match_source=ScopeMatchSource.INTENT_METADATA,
            response_path=ResponsePath.FAQ if target is IntentRouteTarget.FAQ else ResponsePath.SOP,
        ),
        faq_hint=hint,
    )
