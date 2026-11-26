"""用代码中清晰可见的规则完成一次保守的全局路由。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from redis.asyncio import Redis
from redis.exceptions import RedisError

from services.ai_runtime.decision.jev import JevIntentDecisionAdapter
from services.ai_runtime.intent.clarification import resolve_clarification_choice
from services.ai_runtime.intent.classifier import LlmIntentClassifier
from services.ai_runtime.query.context import normalize_query, update_confirmed_topic
from services.ai_runtime.retrieval.semantic import SemanticSearch
from services.content.faq_cache import (
    FAQ_ACTIVE_KEY,
    faq_question_digest,
    faq_question_intent_field,
    faq_question_intents_key,
)
from shared.contracts.enums import (
    DecisionProfile,
    DecisionTask,
    IntentDecisionStatus,
    IntentMatchSource,
    IntentRouteTarget,
    ModelCallStatus,
    RequestKind,
    RequestScope,
    ResponsePath,
    ScopeMatchSource,
)
from shared.contracts.faq import FaqExactHint
from shared.contracts.routing import IntentCandidate, IntentDecision, RoutingDecision
from shared.contracts.runtime import DecisionShadowObservation, DecisionSignal, DecisionTrace

# Python 只保留第一道安全护栏。控制命令、能力边界、域内范围与业务入口
# 均从已审核发布的 Redis 规则快照读取，新增表达不需要修改 Runtime 代码。
ROUTING_RULE_VERSION = "python-guardrails-v1"


def _contract_score(value: float) -> float:
    """把 Milvus 的内积限制到契约允许的 0 到 1，负分只能视为低置信。"""

    return max(0.0, min(1.0, value))


ROUTING_RULES: tuple[dict[str, Any], ...] = (
    # 高风险规则只负责阻止未授权动作被错投到 FAQ 或 SOP，不负责生成业务答案。
    {
        "rule_id": "protected_record_change",
        "rule_kind": "PROTECTED_ACTION",
        "policy_intent_id": "HIGH_RISK_RECORD_MODIFICATION",
        "pattern": "删除处理记录",
        "pattern_variants": (
            "把历史记录删掉",
            "修改系统记录",
            "清除处理记录",
            "篡改处理结果",
        ),
        "match_mode": "CONTAINS",
        "negative_patterns": (),
        "priority": 130,
    },
    {
        "rule_id": "protected_permission_bypass",
        "rule_kind": "PROTECTED_ACTION",
        "policy_intent_id": "HIGH_RISK_PERMISSION_BYPASS",
        "pattern": "绕过权限检查",
        "pattern_variants": (
            "跳过权限验证",
            "不要做身份校验",
            "直接给我管理员权限",
            "绕过审批步骤",
        ),
        "match_mode": "CONTAINS",
        "negative_patterns": (),
        "priority": 130,
    },
)


def _fits_rule(rule: dict[str, Any], query: str, state: dict[str, object]) -> bool:
    """命中短语、否定词和已确认的适用范围；未知范围不猜测。"""

    patterns = (str(rule["pattern"]), *(str(item) for item in rule.get("pattern_variants", ())))
    # 用户输入中的常见中文标点不应让同一条已审核入口规则失效。
    compact_query = query.replace("，", "").replace(",", "").replace("。", "")
    compact_patterns = tuple(
        pattern.replace("，", "").replace(",", "").replace("。", "") for pattern in patterns
    )
    if rule["match_mode"] == "EXACT":
        if compact_query not in compact_patterns:
            return False
    elif rule["match_mode"] == "CONTAINS":
        if not any(pattern in compact_query for pattern in compact_patterns):
            return False
    else:
        raise ValueError(f"规则 {rule['rule_id']} 使用了未知匹配方式")
    if any(str(word) in query for word in rule.get("negative_patterns", ())):
        return False
    for field, state_key in (
        ("product_codes", "product_code"),
        ("channels", "channel"),
        ("regions", "region"),
    ):
        allowed = rule.get(field, ())
        if allowed and state.get(state_key) not in allowed:
            return False
    return True


def current_intent_catalog(state: dict) -> dict[str, dict[str, str]]:
    """合并已发布 FAQ 投影中的意图目录与已发布 SOP 动作；代码中没有内置清单。"""
    catalog = {key: dict(value) for key, value in state.get("intent_catalog", {}).items()}
    for config in state.get("sop_catalog", {}).values():
        intent_id = config["intent_id"]
        if intent_id in catalog and catalog[intent_id]["route_target"] != "SCENE":
            raise ValueError("SOP 动作与已有 FAQ 动作冲突")
        catalog[intent_id] = {
            "intent_id": intent_id,
            "version": config["version"],
            "intent_class": "ACTION",
            "route_target": "SCENE",
            "request_kind": "ACTION",
            "name": config["intent_name"],
            "description": config.get("name", config["intent_name"]),
            "coverage_summary": config.get("name", config["intent_name"]),
        }
    return catalog


def apply_policy_response_path(
    decision: RoutingDecision,
    catalog: dict[str, dict[str, str]],
) -> RoutingDecision:
    """让三种意图识别来源统一进入高风险策略答案出口。"""

    intent_id = decision.intent_decision.intent_id
    intent = catalog.get(str(intent_id), {}) if intent_id else {}
    is_policy_answer = (
        decision.intent_decision.status is IntentDecisionStatus.MATCHED
        and decision.request_kind is RequestKind.HIGH_RISK
        and intent.get("intent_class") == "STRATEGY"
        and intent.get("request_kind") == RequestKind.HIGH_RISK.value
    )
    if not is_policy_answer:
        return decision
    return decision.model_copy(update={"response_path": ResponsePath.SAFE_FALLBACK})


def is_policy_answer_decision(
    decision: RoutingDecision,
    catalog: dict[str, dict[str, str]],
) -> bool:
    """判断已序列化前的路由结果是否应读取已发布策略答案。"""

    intent_id = decision.intent_decision.intent_id
    intent = catalog.get(str(intent_id), {}) if intent_id else {}
    normalized = apply_policy_response_path(decision, catalog)
    return (
        intent.get("intent_class") == "STRATEGY"
        and intent.get("request_kind") == RequestKind.HIGH_RISK.value
        and normalized.response_path is ResponsePath.SAFE_FALLBACK
        and normalized.intent_decision.status is IntentDecisionStatus.MATCHED
        and normalized.request_kind is RequestKind.HIGH_RISK
        and intent_id is not None
    )


def decide_route(query: str, state: dict[str, object]) -> RoutingDecision:
    """匹配基础规则和已发布 SOP 入口；没把握时返回 UNKNOWN，不猜意图。"""

    published_rule_version = str(state.get("intent_rule_release") or "")
    version = published_rule_version or ROUTING_RULE_VERSION
    catalog = current_intent_catalog(state)
    rules = list(ROUTING_RULES)
    rules.extend(dict(rule) for rule in state.get("intent_rules", []))
    for definition in state.get("sop_catalog", {}).values():
        rules.extend(
            {
                "rule_id": f"sop:{definition['sop_id']}:{index}",
                "rule_kind": "INTENT",
                "intent_id": definition["intent_id"],
                "pattern": phrase,
                "match_mode": "CONTAINS",
                "priority": 90,
                "negative_patterns": definition["negative_phrases"],
            }
            for index, phrase in enumerate(definition["entry_phrases"])
        )
    unknown = IntentDecision(
        status=IntentDecisionStatus.UNKNOWN,
        catalog_version=version,
        reason_codes=("NO_MATCH",),
    )
    matches = [rule for rule in rules if _fits_rule(rule, query, state)]
    # 规则引用的动作必须存在于当前已发布目录；目录没有它时按未命中处理，不猜路由。
    matches = [
        rule
        for rule in matches
        if rule["rule_kind"] != "INTENT" or str(rule["intent_id"]) in catalog
    ]
    if not matches:
        return RoutingDecision(
            intent_decision=unknown,
            request_scope=RequestScope.UNSURE,
            request_kind=RequestKind.UNSURE,
            scope_match_source=ScopeMatchSource.RULE,
            scope_reason_codes=("NO_HIGH_PRECISION_RULE",),
            response_path=ResponsePath.SAFE_FALLBACK,
        )

    # 明确控制命令优先于业务动作和能力边界。客户已经要求人工或取消时，
    # Runtime 不应再猜测同一句中的业务主题，更不能继续调用向量或分类模型。
    control_rules = [item for item in matches if item["rule_kind"] == "CONTROL"]
    if control_rules:
        selected = max(control_rules, key=lambda item: item["priority"])
        human_requested = selected["control_action"] == "HUMAN"
        return RoutingDecision(
            intent_decision=unknown,
            request_scope=RequestScope.IN_SCOPE,
            request_kind=RequestKind.CONTROL,
            scope_match_source=ScopeMatchSource.RULE,
            scope_reason_codes=(f"CONTROL:{selected['control_action']}",),
            response_path=(ResponsePath.HUMAN if human_requested else ResponsePath.SAFE_FALLBACK),
        )

    # 命中两个不同动作时保留多意图状态，不靠优先级随意挑一个业务动作。
    intent_rules = [item for item in matches if item["rule_kind"] == "INTENT"]
    unique_intents = sorted({str(item["intent_id"]) for item in intent_rules})
    if len(unique_intents) > 1:
        candidates = tuple(
            IntentCandidate(
                intent_id=intent_id,
                route_target=IntentRouteTarget(catalog[intent_id]["route_target"]),
                score=1.0,
                source_ref=f"python-rule:{version}:{intent_id}",
            )
            for intent_id in unique_intents
        )
        # 显式并列连接词表示多个请求；没有并列线索时保留歧义状态，交给澄清流程。
        multiple_requests = any(
            word in query for word in ("同时", "另外", "还有", "并且", "，", ",")
        )
        status = (
            IntentDecisionStatus.MULTI_INTENT
            if multiple_requests
            else IntentDecisionStatus.AMBIGUOUS
        )
        return RoutingDecision(
            intent_decision=IntentDecision(
                status=status,
                catalog_version=version,
                candidates=candidates,
                reason_codes=("MULTIPLE_HIGH_PRECISION_ACTIONS",),
            ),
            request_scope=RequestScope.IN_SCOPE,
            request_kind=RequestKind.UNSURE,
            scope_match_source=ScopeMatchSource.RULE,
            scope_reason_codes=("MULTIPLE_HIGH_PRECISION_ACTIONS",),
            response_path=ResponsePath.SAFE_FALLBACK,
        )

    # 明确要求天气、导航等本系统未接的能力，即使句中出现机构名也不转成客服动作。
    capability_rules = [item for item in matches if item["rule_kind"] == "CAPABILITY"]
    if capability_rules and intent_rules:
        return RoutingDecision(
            intent_decision=unknown,
            request_scope=RequestScope.UNSURE,
            request_kind=RequestKind.UNSURE,
            scope_match_source=ScopeMatchSource.RULE,
            scope_reason_codes=("BUSINESS_AND_EXTERNAL_REQUEST_MIXED",),
            response_path=ResponsePath.SAFE_FALLBACK,
        )
    if capability_rules:
        selected = max(capability_rules, key=lambda item: item["priority"])
        return RoutingDecision(
            intent_decision=unknown,
            request_scope=RequestScope.OUT_OF_SCOPE,
            request_kind=RequestKind.EXTERNAL_CAPABILITY,
            required_capability=selected["required_capability"],
            scope_match_source=ScopeMatchSource.RULE,
            scope_reason_codes=(f"CAPABILITY_RULE:{selected['rule_id']}",),
            response_path=ResponsePath.SAFE_FALLBACK,
        )
    guard_rules = [
        item
        for item in matches
        if item["rule_kind"] in {"PROTECTED_ACTION", "CONTEXT_REQUIRED", "BUSINESS_FACT_REQUIRED"}
    ]
    if guard_rules:
        # 同一条消息可能同时包含多个需要核验的事实，例如“异地收费”与
        # “未入账”。回复先按最高优先级选择一种安全话术，但审计原因保留
        # 全部命中的规则，避免后续排查时丢掉其中一项风险。
        ordered_guards = sorted(
            guard_rules,
            key=lambda item: (-int(item["priority"]), str(item["rule_id"])),
        )
        policy_intents = {
            str(item["policy_intent_id"]) for item in ordered_guards if item.get("policy_intent_id")
        }
        # 单一高风险主题由 Python 规则快速识别，答案在后续统一策略出口按
        # intent_id 读取。多个主题同时出现时不能挑一个代替另一个。
        if len(ordered_guards) == 1 and len(policy_intents) == 1:
            policy_intent_id = next(iter(policy_intents))
            intent = catalog.get(policy_intent_id)
            if (
                intent is not None
                and intent["intent_class"] == "STRATEGY"
                and intent["route_target"] == IntentRouteTarget.FAQ.value
                and intent["request_kind"] == RequestKind.HIGH_RISK.value
            ):
                return RoutingDecision(
                    intent_decision=IntentDecision(
                        status=IntentDecisionStatus.MATCHED,
                        intent_id=policy_intent_id,
                        route_target=IntentRouteTarget.FAQ,
                        match_source=IntentMatchSource.KEYWORD,
                        confidence=1.0,
                        catalog_version=str(intent.get("version", version)),
                        matched_source_ref=(
                            f"policy-rule:{version}:{ordered_guards[0]['rule_id']}"
                        ),
                        reason_codes=("HIGH_RISK_POLICY_INTENT_RULE",),
                    ),
                    request_scope=RequestScope.IN_SCOPE,
                    request_kind=RequestKind.HIGH_RISK,
                    scope_match_source=ScopeMatchSource.INTENT_METADATA,
                    scope_reason_codes=(f"POLICY_INTENT_RULE:{ordered_guards[0]['rule_id']}",),
                    response_path=ResponsePath.SAFE_FALLBACK,
                )
        selected = ordered_guards[0]
        request_kind = (
            RequestKind.ACTION
            if selected["rule_kind"] == "PROTECTED_ACTION"
            else RequestKind.KNOWLEDGE
        )
        return RoutingDecision(
            intent_decision=unknown,
            request_scope=RequestScope.IN_SCOPE,
            request_kind=request_kind,
            scope_match_source=ScopeMatchSource.RULE,
            scope_reason_codes=tuple(f"GUARD:{item['rule_id']}" for item in ordered_guards),
            response_path=ResponsePath.SAFE_FALLBACK,
        )
    if intent_rules:
        selected = max(intent_rules, key=lambda item: item["priority"])
        intent = catalog[str(selected["intent_id"])]
        sop_rule = str(selected["rule_id"]).startswith("sop:")
        published_rule = bool(selected.get("release_version"))
        matched_version = (
            str(state["sop_catalog_release"])
            if sop_rule
            else str(selected.get("release_version") or version)
        )
        source_kind = (
            "published-sop"
            if sop_rule
            else "published-intent-rule"
            if published_rule
            else "python-guardrail"
        )
        return RoutingDecision(
            intent_decision=IntentDecision(
                status=IntentDecisionStatus.MATCHED,
                intent_id=intent["intent_id"],
                route_target=IntentRouteTarget(intent["route_target"]),
                match_source=IntentMatchSource.KEYWORD,
                confidence=1.0,
                catalog_version=matched_version,
                matched_source_ref=f"{source_kind}:{matched_version}:{selected['rule_id']}",
            ),
            request_scope=RequestScope.IN_SCOPE,
            request_kind=RequestKind(intent["request_kind"]),
            scope_match_source=ScopeMatchSource.INTENT_METADATA,
            scope_reason_codes=(
                "PUBLISHED_INTENT_RULE" if published_rule else "PYTHON_INTENT_RULE",
            ),
            response_path=(
                ResponsePath.FAQ if intent["route_target"] == "FAQ" else ResponsePath.SAFE_FALLBACK
            ),
        )
    domain_rules = [item for item in matches if item["rule_kind"] == "DOMAIN"]
    if domain_rules:
        selected = max(domain_rules, key=lambda item: item["priority"])
        return RoutingDecision(
            intent_decision=unknown,
            request_scope=RequestScope.IN_SCOPE,
            request_kind=RequestKind.KNOWLEDGE,
            scope_match_source=ScopeMatchSource.RULE,
            scope_reason_codes=(f"DOMAIN_RULE:{selected['rule_id']}",),
            response_path=ResponsePath.SAFE_FALLBACK,
        )
    raise ValueError("Python 路由规则中出现了尚未支持的类型")


async def _match_published_faq_question(
    query: str, redis: Redis | None, catalog: dict[str, dict[str, str]]
) -> tuple[RoutingDecision, FaqExactHint] | None:
    """用已发布标准问题同时取得全局意图和后续可复用的 FAQ 条目线索。

    路由节点只用其中的 intent_id 完成全局分流；FAQ Resolver 在确认进入 FAQ
    后再使用条目线索读取答案并检查适用性，不会再次匹配同一个问题哈希。
    """

    if redis is None:
        return None
    try:
        active = await redis.get(FAQ_ACTIVE_KEY)
        if active is None:
            return None
        release_version = active.decode()
        hint_raw = await redis.hget(
            faq_question_intents_key(release_version),
            faq_question_intent_field("zh-CN", query),
        )
    except RedisError:
        return None
    if hint_raw is None:
        return None
    try:
        hint = FaqExactHint.model_validate(json.loads(hint_raw))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    if hint.release_version != release_version or hint.query_digest != faq_question_digest(query):
        return None
    intent_id = hint.intent_id
    intent = catalog.get(intent_id)
    if intent is None or intent["route_target"] != "FAQ":
        return None
    return (
        RoutingDecision(
            intent_decision=IntentDecision(
                status=IntentDecisionStatus.MATCHED,
                intent_id=intent_id,
                route_target=IntentRouteTarget.FAQ,
                match_source=IntentMatchSource.KEYWORD,
                confidence=1.0,
                catalog_version=release_version,
                matched_source_ref=(
                    f"redis-faq-standard-question:{release_version}:{hint.query_digest}"
                ),
            ),
            request_scope=RequestScope.IN_SCOPE,
            request_kind=RequestKind(intent["request_kind"]),
            scope_match_source=ScopeMatchSource.INTENT_METADATA,
            scope_reason_codes=("PUBLISHED_FAQ_STANDARD_QUESTION",),
            response_path=ResponsePath.FAQ,
        ),
        hint,
    )


async def route_turn(
    state: dict[str, object],
    redis: Redis | None = None,
    semantic_search: SemanticSearch | None = None,
    classifier: LlmIntentClassifier | None = None,
    jev_intent_adapter: JevIntentDecisionAdapter | None = None,
    rag_enabled: bool = False,
) -> dict[str, object]:
    """为当前轮次匹配一次路由；同一轮重试时沿用第一次结果。

    先读取代码中少量且容易讲清楚的规则，再用 Redis 做 FAQ 标准问题精确匹配。
    两者都没有命中时，才调用语义检索；语义检索先判断全局意图，确认是
    FAQ 后才检索 FAQ 条目，避免两个 Collection 同时无边界竞争。
    """

    cached = state.get("routing_turn_id") == state["turn_id"] and bool(
        state.get("routing_decision")
    )
    # 总耗时用于判断整条路由是否变慢，分段耗时用于定位是模型、意图检索还是 FAQ 检索变慢。
    route_started = perf_counter()
    semantic_latency_ms: dict[str, int] = {}
    semantic_vector: list[float] | None = None
    model_calls = list(state.get("model_calls", []))
    clarification_selected = False
    query_clarification_selected = False
    retrieval_query = str(state["effective_query"])
    catalog = current_intent_catalog(state)
    query_resolution = dict(state.get("query_context", {}).get("resolution", {}))
    correction_active = query_resolution.get("dialogue_act") == "CORRECTION"
    preserved_intent_id = (
        str(query_resolution["preserved_intent_id"])
        if correction_active
        and query_resolution.get("preserve_intent") is True
        and query_resolution.get("preserved_intent_id")
        else None
    )
    excluded_intent_id = (
        str(query_resolution["excluded_intent_id"])
        if correction_active
        and query_resolution.get("preserve_intent") is False
        and query_resolution.get("excluded_intent_id")
        else None
    )
    excluded_faq_item_id = (
        str(query_resolution["excluded_faq_item_id"])
        if correction_active
        and query_resolution.get("preserve_intent") is True
        and query_resolution.get("excluded_faq_item_id")
        else None
    )
    faq_search_status = (
        str(state.get("semantic_faq_search_status") or "NOT_RUN") if cached else "NOT_RUN"
    )
    if not cached and state.get("sop_action") in {
        "CONTINUE",
        "CANCEL",
        "HUMAN",
        "LOOKUP_PENDING",
        "REPLAY",
        "SENSITIVE",
    }:
        # 当前是在补充或退出既有流程，复用其全局意图，不调用向量或分类模型。
        decision = RoutingDecision(
            intent_decision=IntentDecision(
                status=IntentDecisionStatus.MATCHED,
                intent_id=state["sop_definition"]["intent_id"],
                route_target=IntentRouteTarget.SCENE,
                match_source=IntentMatchSource.ACTIVE_SOP,
                catalog_version=state["sop_release_version"],
                matched_source_ref=str(state["active_sop_execution"]["sop_execution_id"]),
                reason_codes=("ACTIVE_SOP_CONTINUATION",),
            ),
            request_scope=RequestScope.IN_SCOPE,
            request_kind=RequestKind.ACTION,
            scope_match_source=ScopeMatchSource.INTENT_METADATA,
            response_path=ResponsePath.SOP,
        )
        hint = None
        semantic_candidates = []
        semantic_faq_candidates = []
    elif cached:
        decision = RoutingDecision.model_validate(state["routing_decision"])
        hint = (
            FaqExactHint.model_validate(state["faq_exact_hint"])
            if state.get("faq_exact_hint")
            else None
        )
        # 同一 Turn 的 checkpoint 可能已有路由结果但没有 FAQ 条目线索；
        # 此时只补查一次当前标准问题，不重新执行整套路由。
        if hint is None and str(decision.intent_decision.matched_source_ref or "").startswith(
            "redis-faq-standard-question:"
        ):
            recovered = await _match_published_faq_question(
                normalize_query(str(state["effective_query"])), redis, catalog
            )
            if recovered is not None and (
                recovered[0].intent_decision.intent_id == decision.intent_decision.intent_id
            ):
                hint = recovered[1]
        semantic_candidates = list(state.get("semantic_candidates", []))
        semantic_faq_candidates = list(state.get("semantic_faq_candidates", []))
    else:
        hint = None
        # 先准备两个候选列表。控制命令会在下面直接结束路由，不能因为
        # 它的 intent_id 为空就被误认为 UNKNOWN，再进入 FAQ、向量或模型。
        semantic_candidates = []
        semantic_faq_candidates = []
        query = normalize_query(str(state["effective_query"]))
        decision = decide_route(query, state)
        if (
            excluded_intent_id
            and decision.intent_decision.status is IntentDecisionStatus.MATCHED
            and decision.intent_decision.intent_id == excluded_intent_id
        ):
            decision = _correction_excluded_decision(
                catalog_version=decision.intent_decision.catalog_version
            )
        capability_decided = (
            decision.request_kind is RequestKind.EXTERNAL_CAPABILITY
            or "BUSINESS_AND_EXTERNAL_REQUEST_MIXED" in decision.scope_reason_codes
        )
        resumed = (
            await resolve_clarification_choice(query, state, catalog, redis)
            if decision.request_kind is not RequestKind.CONTROL and not capability_decided
            else None
        )
        if resumed is not None and resumed.resolved_query:
            query = normalize_query(resumed.resolved_query)
            retrieval_query = query
            clarification_selected = True
            query_clarification_selected = True
            decision = decide_route(query, state)
            capability_decided = (
                decision.request_kind is RequestKind.EXTERNAL_CAPABILITY
                or "BUSINESS_AND_EXTERNAL_REQUEST_MIXED" in decision.scope_reason_codes
            )
            resumed = None
        if decision.request_kind is RequestKind.CONTROL or capability_decided:
            # 控制命令和未接能力已经由已发布高精度规则确认。UNKNOWN 只表示
            # 没有业务 intent_id，不是继续调用 Embedding 或模型的信号。
            pass
        elif resumed is not None:
            decision = resumed.routing_decision or decision
            hint = resumed.faq_hint
            clarification_selected = True
            # “第一项”是选择指令，后续检索和 SOP 入口仍使用触发澄清的原始业务问题。
            retrieval_query = str(state.get("clarification_origin_query") or retrieval_query)
        elif preserved_intent_id and preserved_intent_id in catalog:
            target = catalog[preserved_intent_id]
            if target["route_target"] == "FAQ":
                decision = RoutingDecision(
                    intent_decision=IntentDecision(
                        status=IntentDecisionStatus.MATCHED,
                        intent_id=preserved_intent_id,
                        route_target=IntentRouteTarget.FAQ,
                        match_source=IntentMatchSource.CONTEXT,
                        confidence=1.0,
                        catalog_version=str(target["version"]),
                        matched_source_ref=str(
                            query_resolution.get("correction_source_ref") or "conversation-context"
                        ),
                        reason_codes=("CORRECTION_PRESERVED_INTENT",),
                    ),
                    request_scope=RequestScope.IN_SCOPE,
                    request_kind=RequestKind(str(target["request_kind"])),
                    scope_match_source=ScopeMatchSource.INTENT_METADATA,
                    scope_reason_codes=("CORRECTION_PRESERVED_INTENT",),
                    response_path=ResponsePath.FAQ,
                )
        elif (
            decision.intent_decision.status is IntentDecisionStatus.MATCHED
            and decision.intent_decision.route_target is IntentRouteTarget.FAQ
        ):
            # Python 策略规则只负责快速确定全局意图；若这句话本身也是已发布
            # FAQ 标准问，则补上同一条 Redis 条目线索，避免再走一次无必要的
            # FAQ 向量检索。不是标准问时，后面的 FAQ 语义检索仍会处理它。
            faq_match = await _match_published_faq_question(query, redis, catalog)
            if faq_match is not None and (
                faq_match[0].intent_decision.intent_id == decision.intent_decision.intent_id
            ):
                hint = faq_match[1]
        elif decision.intent_decision.status is IntentDecisionStatus.UNKNOWN:
            faq_match = await _match_published_faq_question(query, redis, catalog)
            if faq_match is not None and (
                not excluded_intent_id
                or faq_match[0].intent_decision.intent_id != excluded_intent_id
            ):
                decision, hint = faq_match
            elif semantic_search is not None and not any(
                code.startswith("GUARD:") for code in decision.scope_reason_codes
            ):
                (
                    decision,
                    semantic_candidates,
                    semantic_vector,
                    semantic_latency_ms,
                ) = await _semantic_route(
                    query,
                    state,
                    semantic_search,
                    catalog,
                    excluded_intent_ids=(
                        frozenset({excluded_intent_id}) if excluded_intent_id else frozenset()
                    ),
                )
            if (
                classifier is not None
                and semantic_vector is not None
                and decision.intent_decision.status
                in {
                    IntentDecisionStatus.MATCHED,
                    IntentDecisionStatus.UNKNOWN,
                    IntentDecisionStatus.AMBIGUOUS,
                }
                and "SEMANTIC_SEARCH_UNAVAILABLE" not in decision.intent_decision.reason_codes
                and not any(code.startswith("GUARD:") for code in decision.scope_reason_codes)
            ):
                semantic_decision = decision
                classified, classifier_records = await classifier.classify(
                    query,
                    state=state,
                    catalog=catalog,
                    candidate_intents=decision.intent_decision.candidates,
                )
                model_calls.extend(record.model_dump(mode="json") for record in classifier_records)
                classifier_succeeded = any(
                    record.status is ModelCallStatus.COMPLETED for record in classifier_records
                )
                if (
                    query_clarification_selected
                    and classified.query_clarification is not None
                    and classified.request_scope is RequestScope.IN_SCOPE
                    and classified.request_kind is RequestKind.KNOWLEDGE
                ):
                    # 客户已经完成一次 Query 条件确认后，不再用新的开放式维度连续追问。
                    # 后续交给 RAG 证据门和坐席审核兜底，避免模型形成澄清循环。
                    decision = RoutingDecision(
                        intent_decision=IntentDecision(
                            status=IntentDecisionStatus.UNKNOWN,
                            catalog_version=classified.intent_decision.catalog_version,
                            provider_model=classified.intent_decision.provider_model,
                            reason_codes=("QUERY_CLARIFICATION_LIMIT_REACHED",),
                        ),
                        request_scope=RequestScope.IN_SCOPE,
                        request_kind=RequestKind.KNOWLEDGE,
                        scope_match_source=ScopeMatchSource.MODEL,
                        scope_reason_codes=(
                            "MODEL_CLASSIFIER",
                            "QUERY_CLARIFICATION_LIMIT_REACHED",
                        ),
                        response_path=ResponsePath.SAFE_FALLBACK,
                    )
                elif (
                    semantic_decision.intent_decision.status is IntentDecisionStatus.AMBIGUOUS
                    and not classifier_succeeded
                ):
                    # 有限候选仍比模型故障产生的 UNKNOWN 更可解释；失败时保留澄清。
                    decision = semantic_decision
                elif (
                    excluded_intent_id
                    and classified.intent_decision.status is IntentDecisionStatus.MATCHED
                    and classified.intent_decision.intent_id == excluded_intent_id
                ):
                    decision = _correction_excluded_decision(
                        catalog_version=classified.intent_decision.catalog_version
                    )
                else:
                    decision = classified
    # 只有已经有实际流程定义的 Scene 才能进入 SOP；其他 Scene 继续安全退出。
    # 同时覆盖 Python 规则和模型分类得到的同一个全局意图。
    if (
        decision.intent_decision.status is IntentDecisionStatus.MATCHED
        and decision.intent_decision.intent_id
        in {item["intent_id"] for item in state.get("sop_catalog", {}).values()}
    ):
        decision = decision.model_copy(update={"response_path": ResponsePath.SOP})
    # 规则、Milvus 与分类器只负责识别 canonical intent_id。高风险策略
    # 一律在这里收敛，避免三个分支各自维护不同的 FAQ/兜底行为。
    decision = apply_policy_response_path(decision, catalog)
    if rag_enabled and rag_admission_allowed(decision, state):
        # RAG 准入只复用已经形成的 Query、护栏和路由结果，不追加一次模型判断。
        # 后续节点仍要检查检索证据；这里仅把允许进入检索的路径标出来。
        decision = decision.model_copy(update={"response_path": ResponsePath.RAG})
    semantic_thresholds = (
        {
            "intent_min_score": semantic_search.thresholds.intent_min_score,
            "intent_margin": semantic_search.thresholds.intent_margin,
            "faq_min_score": semantic_search.thresholds.faq_min_score,
            "faq_margin": semantic_search.thresholds.faq_margin,
        }
        if semantic_search is not None
        else dict(state.get("semantic_thresholds", {}))
    )
    if (
        decision.intent_decision.status is IntentDecisionStatus.MATCHED
        and decision.intent_decision.route_target is IntentRouteTarget.FAQ
        and decision.response_path is ResponsePath.FAQ
        and hint is None
        and not cached
    ):
        if semantic_search is None:
            faq_search_status = "UNAVAILABLE"
        else:
            faq_search_started = perf_counter()
            try:
                if semantic_vector is None:
                    semantic_faq_candidates = await semantic_search.search_faq(
                        normalize_query(retrieval_query),
                        decision.intent_decision.intent_id or "",
                    )
                else:
                    semantic_faq_candidates = await semantic_search.search_faq_vector(
                        semantic_vector,
                        decision.intent_decision.intent_id or "",
                    )
                if excluded_faq_item_id:
                    semantic_faq_candidates = [
                        item
                        for item in semantic_faq_candidates
                        if str(item.get("faq_item_id")) != excluded_faq_item_id
                    ]
                faq_search_status = "COMPLETED"
                semantic_latency_ms["faq_search_latency_ms"] = max(
                    0, round((perf_counter() - faq_search_started) * 1000)
                )
            except Exception:
                # Milvus、模型或模型输入异常时只保留已判定的全局意图。FAQ
                # Resolver 会把它记为依赖故障，而不是伪装成内容无候选。
                semantic_faq_candidates = []
                faq_search_status = "UNAVAILABLE"
                semantic_latency_ms["faq_search_latency_ms"] = max(
                    0, round((perf_counter() - faq_search_started) * 1000)
                )
    faq_standard_match = hint is not None
    active_sop = decision.intent_decision.match_source is IntentMatchSource.ACTIVE_SOP
    run_id = str(state["run_id"])
    published_rule_match = bool(state.get("intent_rule_release")) and (
        decision.request_kind in {RequestKind.CONTROL, RequestKind.EXTERNAL_CAPABILITY}
        or "BUSINESS_AND_EXTERNAL_REQUEST_MIXED" in decision.scope_reason_codes
        or "PUBLISHED_INTENT_RULE" in decision.scope_reason_codes
        or any(code.startswith("DOMAIN_RULE:") for code in decision.scope_reason_codes)
    )
    shadow_observation: DecisionShadowObservation | None = None
    requested_shadow_profile = state.get("shadow_decision_profile")
    if cached and state.get("routing_trace"):
        previous_trace = DecisionTrace.model_validate(state["routing_trace"])
        shadow_observation = previous_trace.shadow_observation
    elif requested_shadow_profile == DecisionProfile.JEV.value:
        candidates = decision.intent_decision.candidates
        if jev_intent_adapter is None:
            shadow_observation = DecisionShadowObservation(
                profile=DecisionProfile.JEV,
                provider="typesafe",
                provider_model="unconfigured",
                input_snapshot_ref=f"graph_state:{state['thread_id']}:{state['turn_id']}",
                candidate_set_version=decision.intent_decision.catalog_version,
                latency_ms=0,
                error_code="JEV_NOT_CONFIGURED",
            )
        else:
            evaluation = await jev_intent_adapter.evaluate(
                normalize_query(retrieval_query),
                candidates=candidates,
                catalog=catalog,
                candidate_set_version=decision.intent_decision.catalog_version,
            )
            shadow_observation = evaluation.to_shadow_observation()

    trace = DecisionTrace(
        decision_id=f"decision_{uuid5(NAMESPACE_URL, 'routing:' + run_id).hex}",
        decision_task=DecisionTask.ROUTING,
        active_profile=DecisionProfile.BASELINE,
        shadow_profile=(DecisionProfile.JEV if shadow_observation is not None else None),
        provider=(
            "faq-standard-question-redis"
            if faq_standard_match
            else "published-intent-rules-redis"
            if published_rule_match
            else "python-routing-guardrails"
        ),
        provider_model=(
            decision.intent_decision.catalog_version
            if faq_standard_match
            else str(state["intent_rule_release"])
            if published_rule_match
            else ROUTING_RULE_VERSION
        ),
        input_snapshot_ref=f"graph_state:{state['thread_id']}:{state['turn_id']}",
        candidate_set_version=decision.intent_decision.catalog_version,
        threshold_policy_version=(
            "semantic-threshold-v1"
            if semantic_candidates or semantic_faq_candidates
            else "high-precision-v1"
        ),
        signals=(
            DecisionSignal(
                signal_name="intent_status",
                value=decision.intent_decision.status.value,
                reason_codes=decision.intent_decision.reason_codes,
            ),
            DecisionSignal(
                signal_name="request_scope",
                value=decision.request_scope.value,
                reason_codes=decision.scope_reason_codes,
            ),
        ),
        final_decision_ref=f"routing:{state['turn_id']}",
        latency_ms=max(0, round((perf_counter() - route_started) * 1000)),
        latency_breakdown_ms=semantic_latency_ms,
        created_at=datetime.now(UTC),
        shadow_observation=shadow_observation,
    )
    if semantic_candidates or semantic_faq_candidates:
        trace = trace.model_copy(
            update={
                "provider": "qwen3-embedding-milvus",
                "provider_model": "qwen3-embedding-0.6b-1024-v1",
            }
        )
    classifier_calls = [
        call for call in model_calls if str(call["purpose"]).startswith("MODEL_ROUTING")
    ]
    if classifier_calls:
        # SOP 消息解释不冒充全局分类；它的实际用量和耗时另有调用明细。
        latest_call = classifier_calls[-1]
        trace = trace.model_copy(
            update={
                "provider": str(latest_call["provider"]),
                "provider_model": str(latest_call["model"]),
                "provider_request_id": latest_call.get("provider_request_id"),
                "latency_breakdown_ms": {
                    **trace.latency_breakdown_ms,
                    "classifier_latency_ms": sum(
                        int(call["latency_ms"]) for call in classifier_calls
                    ),
                },
            }
        )
    clarification_review_calls = [
        call for call in model_calls if call["purpose"] == "MODEL_QUERY_CLARIFICATION_REVIEW"
    ]
    if clarification_review_calls:
        trace = trace.model_copy(
            update={
                "latency_breakdown_ms": {
                    **trace.latency_breakdown_ms,
                    "clarification_review_latency_ms": sum(
                        int(call["latency_ms"]) for call in clarification_review_calls
                    ),
                }
            }
        )
    if active_sop or clarification_selected:
        trace = trace.model_copy(
            update={
                "provider": "active-sop" if active_sop else "clarification-selection",
                "provider_model": decision.intent_decision.catalog_version,
            }
        )
    updates = {
        "routing_turn_id": state["turn_id"],
        "routing_decision": decision.model_dump(mode="json"),
        "routing_trace": trace.model_dump(mode="json"),
        "faq_exact_hint": hint.model_dump(mode="json") if hint else None,
        "semantic_candidates": semantic_candidates,
        "semantic_faq_candidates": semantic_faq_candidates,
        "semantic_faq_search_status": faq_search_status,
        "semantic_thresholds": semantic_thresholds,
        "model_calls": model_calls,
        # 只沉淀唯一命中的 FAQ/SOP 主题，供下一轮 Query Resolution 使用。
        "confirmed_facts": update_confirmed_topic(
            list(state.get("confirmed_facts", [])),
            state=state,
            routing=decision.model_dump(mode="json"),
            catalog=catalog,
            faq_hint=hint.model_dump(mode="json") if hint else None,
        ),
    }
    if clarification_selected and state.get("clarification_origin_query"):
        # 澄清选择本身不是业务问题；把原问题恢复到本轮状态，避免 SOP/FAQ
        # 下游拿“1”“第一项”作为检索或槽位抽取输入。
        updates["normalized_query"] = retrieval_query
        updates["effective_query"] = retrieval_query
    if clarification_selected or not cached:
        # 已选择、明确换题、控制命令或重新路由后清理旧问题，避免误用陈旧选项。
        updates["pending_clarification"] = None
        updates["clarification_origin_query"] = ""
        updates["clarification_faq_hints"] = {}
        updates["clarification_query_resolutions"] = {}
    return updates


def rag_admission_allowed(decision: RoutingDecision, state: dict[str, object]) -> bool:
    """判断本轮是否满足 RAG 六项准入条件，不读取外部知识也不调用模型。"""

    resolution = state.get("query_context", {}).get("resolution", {})
    resolution_status = str(resolution.get("status", ""))
    return (
        resolution_status in {"NOT_REQUIRED", "RESOLVED"}
        and decision.intent_decision.status is IntentDecisionStatus.UNKNOWN
        and decision.request_scope is RequestScope.IN_SCOPE
        and decision.request_kind is RequestKind.KNOWLEDGE
        and not any(code.startswith("GUARD:") for code in decision.scope_reason_codes)
        and not state.get("input_guardrail_blocked", False)
    )


def faq_rag_fallback_allowed(decision: RoutingDecision, state: dict[str, object]) -> bool:
    """FAQ 真正无候选时重新执行知识检索安全门，不绕过策略或适用性检查。"""

    resolution = state.get("query_context", {}).get("resolution", {})
    resolution_status = str(resolution.get("status", ""))
    intent_id = decision.intent_decision.intent_id
    intent = current_intent_catalog(state).get(str(intent_id), {}) if intent_id else {}
    policy_reason = any(
        code.startswith("GUARD:")
        or code.startswith("POLICY_INTENT_RULE:")
        or code == "HIGH_RISK_POLICY_INTENT_RULE"
        for code in decision.scope_reason_codes
    )
    return (
        resolution_status in {"NOT_REQUIRED", "RESOLVED"}
        and decision.intent_decision.status is IntentDecisionStatus.MATCHED
        and decision.intent_decision.route_target is IntentRouteTarget.FAQ
        and decision.request_scope is RequestScope.IN_SCOPE
        and decision.request_kind is RequestKind.KNOWLEDGE
        and intent.get("intent_class") != "STRATEGY"
        and not policy_reason
        and not state.get("input_guardrail_blocked", False)
    )


async def _semantic_route(
    query: str,
    state: dict[str, object],
    semantic_search: SemanticSearch,
    catalog: dict[str, dict[str, str]] | None = None,
    excluded_intent_ids: frozenset[str] = frozenset(),
) -> tuple[
    RoutingDecision,
    list[dict[str, object]],
    list[float] | None,
    dict[str, int],
]:
    """把意图向量候选转换成 MATCHED、AMBIGUOUS 或 UNKNOWN。"""

    latency_ms: dict[str, int] = {}
    catalog = catalog or current_intent_catalog(state)
    try:
        embedding_started = perf_counter()
        vector = await semantic_search.encode_query(query)
    except Exception:
        latency_ms["embedding_latency_ms"] = max(
            0, round((perf_counter() - embedding_started) * 1000)
        )
        return (
            RoutingDecision(
                intent_decision=IntentDecision(
                    status=IntentDecisionStatus.UNKNOWN,
                    catalog_version="semantic-unavailable",
                    reason_codes=("SEMANTIC_SEARCH_UNAVAILABLE",),
                ),
                request_scope=RequestScope.UNSURE,
                request_kind=RequestKind.UNSURE,
                scope_match_source=ScopeMatchSource.RULE,
                scope_reason_codes=("SEMANTIC_SEARCH_UNAVAILABLE",),
                response_path=ResponsePath.SAFE_FALLBACK,
            ),
            [],
            None,
            latency_ms,
        )
    latency_ms["embedding_latency_ms"] = max(0, round((perf_counter() - embedding_started) * 1000))
    try:
        intent_search_started = perf_counter()
        candidates = [
            item
            for item in await semantic_search.search_intents_vector(vector)
            if str(item.get("intent_id")) not in excluded_intent_ids
        ]
    except Exception:
        latency_ms["intent_search_latency_ms"] = max(
            0, round((perf_counter() - intent_search_started) * 1000)
        )
        return (
            RoutingDecision(
                intent_decision=IntentDecision(
                    status=IntentDecisionStatus.UNKNOWN,
                    catalog_version="semantic-unavailable",
                    reason_codes=("SEMANTIC_SEARCH_UNAVAILABLE",),
                ),
                request_scope=RequestScope.UNSURE,
                request_kind=RequestKind.UNSURE,
                scope_match_source=ScopeMatchSource.RULE,
                scope_reason_codes=("SEMANTIC_SEARCH_UNAVAILABLE",),
                response_path=ResponsePath.SAFE_FALLBACK,
            ),
            [],
            vector,
            latency_ms,
        )
    latency_ms["intent_search_latency_ms"] = max(
        0, round((perf_counter() - intent_search_started) * 1000)
    )
    if not candidates:
        decision = (
            _correction_excluded_decision(catalog_version=semantic_search.index_version)
            if excluded_intent_ids
            else decide_route(query, state)
        )
        return decision, [], vector, latency_ms
    threshold = semantic_search.thresholds
    top_score = float(candidates[0]["score"])
    second_score = float(candidates[1]["score"]) if len(candidates) > 1 else None
    if top_score < threshold.intent_min_score:
        status = IntentDecisionStatus.UNKNOWN
        reason_codes = ("SEMANTIC_SCORE_BELOW_THRESHOLD",)
    elif second_score is not None and top_score - second_score < threshold.intent_margin:
        status = IntentDecisionStatus.AMBIGUOUS
        reason_codes = ("SEMANTIC_INTENT_MARGIN_TOO_SMALL",)
    else:
        status = IntentDecisionStatus.MATCHED
        reason_codes = ("SEMANTIC_INTENT_MATCH",)
    intent_candidates = tuple(
        IntentCandidate(
            intent_id=str(item["intent_id"]),
            route_target=IntentRouteTarget(catalog[str(item["intent_id"])]["route_target"]),
            score=_contract_score(float(item["score"])),
            source_ref=f"milvus-intent:{item.get('source_ref', '')}",
        )
        for item in candidates[:5]
        if str(item["intent_id"]) in catalog
    )
    if status is IntentDecisionStatus.AMBIGUOUS and len(intent_candidates) < 2:
        status = IntentDecisionStatus.UNKNOWN
        reason_codes = ("SEMANTIC_CANDIDATES_INCOMPLETE",)
    if status is IntentDecisionStatus.MATCHED and intent_candidates:
        selected = intent_candidates[0]
        target = catalog[selected.intent_id]
        return (
            RoutingDecision(
                intent_decision=IntentDecision(
                    status=status,
                    intent_id=selected.intent_id,
                    route_target=selected.route_target,
                    match_source=IntentMatchSource.SIMILAR_QUESTION,
                    confidence=selected.score,
                    catalog_version=semantic_search.index_version,
                    matched_source_ref=selected.source_ref,
                    candidates=intent_candidates,
                    reason_codes=reason_codes,
                ),
                request_scope=RequestScope.IN_SCOPE,
                request_kind=RequestKind(target["request_kind"]),
                scope_match_source=ScopeMatchSource.INTENT_METADATA,
                scope_reason_codes=("SEMANTIC_INTENT_METADATA",),
                response_path=(
                    ResponsePath.FAQ
                    if selected.route_target is IntentRouteTarget.FAQ
                    else ResponsePath.SAFE_FALLBACK
                ),
            ),
            candidates,
            vector,
            latency_ms,
        )
    return (
        RoutingDecision(
            intent_decision=IntentDecision(
                status=status,
                catalog_version=semantic_search.index_version,
                candidates=intent_candidates,
                reason_codes=reason_codes,
            ),
            request_scope=RequestScope.IN_SCOPE,
            request_kind=RequestKind.UNSURE,
            scope_match_source=ScopeMatchSource.INTENT_METADATA,
            scope_reason_codes=reason_codes,
            response_path=(
                ResponsePath.CLARIFICATION
                if status is IntentDecisionStatus.AMBIGUOUS and len(intent_candidates) >= 2
                else ResponsePath.SAFE_FALLBACK
            ),
        ),
        candidates,
        vector,
        latency_ms,
    )


def _correction_excluded_decision(*, catalog_version: str) -> RoutingDecision:
    """上一轮已被客户否定时，不允许精确、语义或分类器再次选回同一意图。"""

    return RoutingDecision(
        intent_decision=IntentDecision(
            status=IntentDecisionStatus.UNKNOWN,
            catalog_version=catalog_version,
            reason_codes=("CORRECTION_EXCLUDED_PREVIOUS_INTENT",),
        ),
        request_scope=RequestScope.IN_SCOPE,
        request_kind=RequestKind.KNOWLEDGE,
        scope_match_source=ScopeMatchSource.INTENT_METADATA,
        scope_reason_codes=("CORRECTION_EXCLUDED_PREVIOUS_INTENT",),
        response_path=ResponsePath.SAFE_FALLBACK,
    )
