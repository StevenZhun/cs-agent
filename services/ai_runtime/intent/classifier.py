"""使用 OpenAI 兼容模型对语义路径无法确定的请求做结构化分类兜底。"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx

from prompts.intent import (
    business_fact_review_messages,
    clarification_review_messages,
    intent_classification_system_prompt,
    intent_classification_user_prompt,
    routing_repair_messages,
)
from services.ai_runtime.model_request import (
    ModelContextWindowExceeded,
    validate_chat_request_budget,
)
from shared.contracts.enums import (
    IntentDecisionStatus,
    IntentMatchSource,
    IntentRouteTarget,
    ModelCallStatus,
    RequestKind,
    RequestScope,
    ResponsePath,
    ScopeMatchSource,
)
from shared.contracts.routing import (
    IntentCandidate,
    IntentDecision,
    QueryClarificationOption,
    QueryClarificationProposal,
    RoutingDecision,
)
from shared.contracts.runtime import ModelCallRecord

HIGH_RISK_MIN_CONFIDENCE = 0.80


class LlmIntentClassifier:
    """调用 OpenAI 兼容接口，只返回全局意图和请求范围，不生成业务答案。"""

    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        fast_retry_timeout_seconds: float = 3.0,
        context_window_tokens: int,
    ) -> None:
        """保存模型连接配置；空地址或空密钥表示本地未配置分类模型。"""

        self._provider = provider
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._fast_retry_timeout_seconds = min(
            fast_retry_timeout_seconds,
            timeout_seconds,
        )
        self._context_window_tokens = context_window_tokens

    @property
    def model(self) -> str:
        """返回当前分类模型名称，供路由追踪记录使用。"""

        return self._model

    async def classify(
        self,
        query: str,
        *,
        state: dict[str, object],
        catalog: dict[str, dict[str, str]],
        candidate_intents: tuple[IntentCandidate, ...] = (),
    ) -> tuple[RoutingDecision, tuple[ModelCallRecord, ...]]:
        """结合目录和可选语义候选请求 JSON 决策，失败时保守退出。"""

        started = time.perf_counter()
        call_id = f"mc_{uuid5(NAMESPACE_URL, str(state['run_id']) + ':MODEL_ROUTING').hex}"
        if not self._base_url or not self._api_key:
            decision, record = self._failure(
                state,
                call_id,
                ModelCallStatus.FAILED,
                "MODEL_CONFIG_MISSING",
                started,
            )
            return decision, (record,)

        system_prompt = intent_classification_system_prompt(catalog)
        payload = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": intent_classification_user_prompt(
                        query,
                        candidate_intents,
                        catalog,
                    ),
                },
            ],
        }
        if self._provider.lower() == "deepseek":
            # 全局路由只需按已发布目录返回受约束 JSON，不生成业务答案。关闭思考模式
            # 可以减少长推理和结构漂移；复杂歧义另由窄职责复核调用处理。
            payload["thinking"] = {"type": "disabled"}
        try:
            body = await self._request(payload, timeout_seconds=self._timeout_seconds)
            fast_retry = False
        except ModelContextWindowExceeded as error:
            decision, record = self._failure(
                state,
                call_id,
                ModelCallStatus.FAILED,
                "MODEL_CONTEXT_WINDOW_EXCEEDED",
                started,
                usage={"estimated_input_tokens": error.estimated_input_tokens},
            )
            return decision, (record,)
        except httpx.TimeoutException:
            if self._provider.lower() != "deepseek":
                decision, record = self._failure(
                    state,
                    call_id,
                    ModelCallStatus.TIMEOUT,
                    "MODEL_TIMEOUT",
                    started,
                )
                return decision, (record,)
            fast_payload = dict(payload)
            fast_payload["thinking"] = {"type": "disabled"}
            try:
                body = await self._request(
                    fast_payload,
                    timeout_seconds=self._fast_retry_timeout_seconds,
                )
                fast_retry = True
            except httpx.TimeoutException:
                decision, record = self._failure(
                    state,
                    call_id,
                    ModelCallStatus.TIMEOUT,
                    "MODEL_TIMEOUT",
                    started,
                )
                return decision, (record,)
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
                decision, record = self._failure(
                    state,
                    call_id,
                    ModelCallStatus.FAILED,
                    "MODEL_OUTPUT_INVALID",
                    started,
                )
                return decision, (record,)
        output: dict[str, Any] | None = None
        try:
            content = body["choices"][0]["message"]["content"]
            output = self._parse_json(content)
            decision = self._to_decision(output, catalog, query=query)
            if fast_retry:
                decision = decision.model_copy(
                    update={
                        "scope_reason_codes": (
                            *decision.scope_reason_codes,
                            "MODEL_FAST_RETRY",
                        )
                    }
                )
            record = self._record(
                state=state,
                call_id=call_id,
                purpose="MODEL_ROUTING",
                status=ModelCallStatus.COMPLETED,
                started=started,
                usage=body.get("usage") or {},
                provider_request_id=body.get("id"),
            )
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            decision, invalid_record = self._failure(
                state,
                call_id,
                ModelCallStatus.FAILED,
                "MODEL_OUTPUT_INVALID",
                started,
                usage=body.get("usage") or {},
                provider_request_id=body.get("id"),
            )
            recovery_records = [invalid_record]
            if (
                output is not None
                and output.get("requires_business_fact") is True
                and output.get("business_object_complete") is not True
                and output.get("query_clarification") is None
            ):
                boundary_decision, boundary_record = await self._review_incomplete_business_fact(
                    query,
                    state=state,
                    routing_output=output,
                )
                recovery_records.append(boundary_record)
                if boundary_decision is not None:
                    return boundary_decision, tuple(recovery_records)

            repaired, repair_record = await self._repair_invalid_output(
                query,
                state=state,
                catalog=catalog,
                candidate_intents=candidate_intents,
                invalid_content=body.get("choices", [{}])[0].get("message", {}).get("content"),
            )
            recovery_records.append(repair_record)
            if repaired is not None:
                return repaired, tuple(recovery_records)
            return decision, tuple(recovery_records)

        reviewed, review_record = await self._review_query_clarification(
            query,
            decision=decision,
            state=state,
        )
        if review_record is None:
            return reviewed, (record,)
        return reviewed, (record, review_record)

    async def _review_incomplete_business_fact(
        self,
        query: str,
        *,
        state: dict[str, object],
        routing_output: dict[str, Any],
    ) -> tuple[RoutingDecision | None, ModelCallRecord]:
        """复核缺少澄清方案的业务事实判断，避免把通用知识题误判为个人查询。"""

        purpose = "MODEL_QUERY_CLARIFICATION_REVIEW"
        started = time.perf_counter()
        call_id = self._call_id(state, purpose)
        payload = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": business_fact_review_messages(query, routing_output),
        }
        if self._provider.lower() == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        body: dict[str, Any] | None = None
        try:
            body = await self._request(
                payload,
                timeout_seconds=self._fast_retry_timeout_seconds,
            )
            output = self._parse_json(body["choices"][0]["message"]["content"])
            required = output.get("clarification_required")
            answerable = output.get("general_knowledge_answerable")
            personal_fact = output.get("personal_fact_requested")
            if not all(isinstance(value, bool) for value in (required, answerable, personal_fact)):
                raise ValueError("business fact review requires boolean fields")
        except (
            httpx.HTTPError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return None, self._record(
                state=state,
                call_id=call_id,
                purpose=purpose,
                status=ModelCallStatus.FAILED,
                started=started,
                usage=(body or {}).get("usage") or {},
                provider_request_id=(body or {}).get("id"),
                error_code="MODEL_CLARIFICATION_REVIEW_FAILED",
            )

        record = self._record(
            state=state,
            call_id=call_id,
            purpose=purpose,
            status=ModelCallStatus.COMPLETED,
            started=started,
            usage=body.get("usage") or {},
            provider_request_id=body.get("id"),
        )
        if required or not answerable:
            return None, record
        return (
            RoutingDecision(
                intent_decision=IntentDecision(
                    status=IntentDecisionStatus.UNKNOWN,
                    catalog_version="model-catalog-v1",
                    provider_model=self._model,
                    reason_codes=("MODEL_CLARIFICATION_REJECTED",),
                ),
                request_scope=RequestScope.IN_SCOPE,
                request_kind=RequestKind.KNOWLEDGE,
                scope_match_source=ScopeMatchSource.MODEL,
                scope_reason_codes=(
                    "MODEL_CLASSIFIER",
                    "MODEL_CLARIFICATION_REJECTED",
                ),
                response_path=ResponsePath.SAFE_FALLBACK,
            ),
            record,
        )

    async def _repair_invalid_output(
        self,
        query: str,
        *,
        state: dict[str, object],
        catalog: dict[str, dict[str, str]],
        candidate_intents: tuple[IntentCandidate, ...],
        invalid_content: object,
    ) -> tuple[RoutingDecision | None, ModelCallRecord]:
        """对已返回但不符合契约的分类结果做一次有界修复。"""

        purpose = "MODEL_ROUTING_REPAIR"
        started = time.perf_counter()
        call_id = self._call_id(state, purpose)
        payload = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": routing_repair_messages(
                query,
                catalog,
                candidate_intents,
                invalid_content,
            ),
        }
        if self._provider.lower() == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        body: dict[str, Any] | None = None
        try:
            body = await self._request(
                payload,
                timeout_seconds=self._fast_retry_timeout_seconds,
            )
            output = self._parse_json(body["choices"][0]["message"]["content"])
            decision = self._to_decision(output, catalog, query=query)
        except (
            httpx.HTTPError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return None, self._record(
                state=state,
                call_id=call_id,
                purpose=purpose,
                status=ModelCallStatus.FAILED,
                started=started,
                usage=(body or {}).get("usage") or {},
                provider_request_id=(body or {}).get("id"),
                error_code="MODEL_REPAIR_FAILED",
            )
        decision = self._append_reason_code(decision, "MODEL_OUTPUT_REPAIRED")
        return decision, self._record(
            state=state,
            call_id=call_id,
            purpose=purpose,
            status=ModelCallStatus.COMPLETED,
            started=started,
            usage=body.get("usage") or {},
            provider_request_id=body.get("id"),
        )

    async def _review_query_clarification(
        self,
        query: str,
        *,
        decision: RoutingDecision,
        state: dict[str, object],
    ) -> tuple[RoutingDecision, ModelCallRecord | None]:
        """复核 Query 澄清是否真是回答前置条件，避免把可回答知识题退回客户。"""

        proposal = decision.query_clarification
        if proposal is None:
            return decision, None
        purpose = "MODEL_QUERY_CLARIFICATION_REVIEW"
        started = time.perf_counter()
        call_id = self._call_id(state, purpose)
        payload = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": clarification_review_messages(
                query,
                proposal,
                decision.intent_decision.reason_codes,
            ),
        }
        if self._provider.lower() == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        try:
            body = await self._request(
                payload,
                timeout_seconds=self._fast_retry_timeout_seconds,
            )
            output = self._parse_json(body["choices"][0]["message"]["content"])
            required = output.get("clarification_required")
            answerable = output.get("general_knowledge_answerable")
            personal_fact = output.get("personal_fact_requested")
            if not all(isinstance(value, bool) for value in (required, answerable, personal_fact)):
                raise ValueError("clarification review requires boolean fields")
        except (
            httpx.HTTPError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return decision, self._record(
                state=state,
                call_id=call_id,
                purpose=purpose,
                status=ModelCallStatus.FAILED,
                started=started,
                usage={},
                error_code="MODEL_CLARIFICATION_REVIEW_FAILED",
            )

        record = self._record(
            state=state,
            call_id=call_id,
            purpose=purpose,
            status=ModelCallStatus.COMPLETED,
            started=started,
            usage=body.get("usage") or {},
            provider_request_id=body.get("id"),
        )
        if required or not answerable:
            return self._append_reason_code(
                decision,
                "MODEL_CLARIFICATION_CONFIRMED",
            ), record
        return (
            RoutingDecision(
                intent_decision=IntentDecision(
                    status=IntentDecisionStatus.UNKNOWN,
                    catalog_version=decision.intent_decision.catalog_version,
                    provider_model=self._model,
                    reason_codes=("MODEL_CLARIFICATION_REJECTED",),
                ),
                request_scope=RequestScope.IN_SCOPE,
                request_kind=RequestKind.KNOWLEDGE,
                scope_match_source=ScopeMatchSource.MODEL,
                scope_reason_codes=(
                    "MODEL_CLASSIFIER",
                    "MODEL_CLARIFICATION_REJECTED",
                ),
                response_path=ResponsePath.SAFE_FALLBACK,
            ),
            record,
        )

    @staticmethod
    def _append_reason_code(decision: RoutingDecision, reason_code: str) -> RoutingDecision:
        """在不改变既有决策语义的前提下记录模型复核结果。"""

        intent = decision.intent_decision.model_copy(
            update={
                "reason_codes": (*decision.intent_decision.reason_codes, reason_code),
            }
        )
        return decision.model_copy(
            update={
                "intent_decision": intent,
                "scope_reason_codes": (*decision.scope_reason_codes, reason_code),
            }
        )

    @staticmethod
    def _call_id(state: dict[str, object], purpose: str) -> str:
        """为同一 Run 内不同模型用途生成稳定且互不冲突的调用编号。"""

        return f"mc_{uuid5(NAMESPACE_URL, str(state['run_id']) + ':' + purpose).hex}"

    async def _request(
        self,
        payload: dict[str, object],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """执行一次兼容接口请求，并返回 JSON 响应体。"""

        estimated_input_tokens = validate_chat_request_budget(
            payload,
            context_window_tokens=self._context_window_tokens,
        )
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            response.raise_for_status()
            body = response.json()
        if isinstance(body, dict):
            usage = body.get("usage")
            if not isinstance(usage, dict):
                usage = {}
                body["usage"] = usage
            usage["estimated_input_tokens"] = estimated_input_tokens
        return body

    def _system_prompt(self, catalog: dict[str, dict[str, str]]) -> str:
        """返回集中维护的全局意图分类提示词。"""

        return intent_classification_system_prompt(catalog)

    @staticmethod
    def _user_prompt(
        query: str,
        candidate_intents: tuple[IntentCandidate, ...],
        catalog: dict[str, dict[str, str]],
    ) -> str:
        """返回集中维护的全局意图分类输入。"""

        return intent_classification_user_prompt(query, candidate_intents, catalog)

    @staticmethod
    def _parse_json(content: Any) -> dict[str, Any]:
        """去掉模型偶尔包裹的代码围栏，再解析 JSON。"""

        if not isinstance(content, str):
            raise ValueError("model content must be text")
        text = content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
        if not isinstance(result, dict):
            raise ValueError("model output must be an object")
        return result

    def _to_decision(
        self,
        output: dict[str, Any],
        catalog: dict[str, dict[str, str]],
        *,
        query: str = "",
    ) -> RoutingDecision:
        """校验模型字段，只允许已登记的全局意图进入后续图节点。"""

        status = IntentDecisionStatus(str(output.get("status", "UNKNOWN")))
        scope = RequestScope(str(output.get("request_scope", "UNSURE")))
        kind = RequestKind(str(output.get("request_kind", "UNSURE")))
        candidates = tuple(
            IntentCandidate(
                intent_id=str(item["intent_id"]),
                route_target=IntentRouteTarget(catalog[str(item["intent_id"])]["route_target"]),
                score=max(0.0, min(1.0, float(item.get("score", 0)))),
                source_ref="llm-classifier",
            )
            for item in output.get("candidate_intents", [])[:5]
            if isinstance(item, dict) and str(item.get("intent_id")) in catalog
        )
        selected_item = catalog.get(str(output.get("intent_id") or ""))
        scene_candidate_count = sum(
            1
            for candidate in candidates
            if catalog[candidate.intent_id]["route_target"] == IntentRouteTarget.SCENE.value
            and catalog[candidate.intent_id]["request_kind"] == RequestKind.ACTION.value
        )
        scene_collects_business_object = (
            status is IntentDecisionStatus.MATCHED
            and output.get("execution_requested") is True
            and selected_item is not None
            and selected_item["route_target"] == IntentRouteTarget.SCENE.value
            and selected_item["request_kind"] == RequestKind.ACTION.value
        )
        multiple_scenes_collect_business_objects = (
            status is IntentDecisionStatus.MULTI_INTENT
            and output.get("execution_requested") is True
            and scene_candidate_count >= 2
        )
        if (
            output.get("requires_business_fact") is True
            and output.get("business_object_complete") is not True
            and output.get("general_knowledge_answerable") is True
            and not scene_collects_business_object
            and not multiple_scenes_collect_business_objects
        ):
            return RoutingDecision(
                intent_decision=IntentDecision(
                    status=IntentDecisionStatus.UNKNOWN,
                    catalog_version="model-catalog-v1",
                    provider_model=self._model,
                    reason_codes=("GENERAL_KNOWLEDGE_BEFORE_PERSONAL_FACT",),
                ),
                request_scope=RequestScope.IN_SCOPE,
                request_kind=RequestKind.KNOWLEDGE,
                scope_match_source=ScopeMatchSource.MODEL,
                scope_reason_codes=(
                    "MODEL_CLASSIFIER",
                    "GENERAL_KNOWLEDGE_BEFORE_PERSONAL_FACT",
                ),
                response_path=ResponsePath.SAFE_FALLBACK,
            )
        if (
            output.get("requires_business_fact") is True
            and output.get("business_object_complete") is not True
            and not scene_collects_business_object
            and not multiple_scenes_collect_business_objects
        ):
            query_clarification = self._query_clarification(output, query)
            if query_clarification is None:
                raise ValueError("personal business fact needs object clarification")
            return RoutingDecision(
                intent_decision=IntentDecision(
                    status=IntentDecisionStatus.AMBIGUOUS,
                    catalog_version="model-catalog-v1",
                    candidates=(),
                    provider_model=self._model,
                    reason_codes=("BUSINESS_OBJECT_REQUIRED",),
                ),
                request_scope=RequestScope.IN_SCOPE,
                request_kind=RequestKind.ACTION,
                scope_match_source=ScopeMatchSource.MODEL,
                scope_reason_codes=("MODEL_CLASSIFIER", "BUSINESS_OBJECT_REQUIRED"),
                response_path=ResponsePath.CLARIFICATION,
                query_clarification=query_clarification,
            )
        primary_intent_id = str(output.get("primary_intent_id") or "")
        candidate_ids = {candidate.intent_id for candidate in candidates}
        primary = catalog.get(primary_intent_id)
        if (
            output.get("execution_requested") is True
            and output.get("primary_action_can_start") is True
            and primary_intent_id in candidate_ids
            and primary is not None
            and primary["route_target"] == IntentRouteTarget.SCENE.value
            and primary["request_kind"] == RequestKind.ACTION.value
        ):
            return RoutingDecision(
                intent_decision=IntentDecision(
                    status=IntentDecisionStatus.MATCHED,
                    intent_id=primary_intent_id,
                    route_target=IntentRouteTarget.SCENE,
                    match_source=IntentMatchSource.MODEL,
                    confidence=max(
                        (
                            candidate.score
                            for candidate in candidates
                            if candidate.intent_id == primary_intent_id
                        ),
                        default=0.0,
                    ),
                    catalog_version="model-catalog-v1",
                    matched_source_ref="llm-classifier:primary-protective-action",
                    provider_model=self._model,
                    candidates=candidates,
                    reason_codes=("MODEL_PRIMARY_PROTECTIVE_ACTION",),
                ),
                request_scope=RequestScope.IN_SCOPE,
                request_kind=RequestKind.ACTION,
                scope_match_source=ScopeMatchSource.MODEL,
                scope_reason_codes=(
                    "MODEL_CLASSIFIER",
                    "MODEL_PRIMARY_PROTECTIVE_ACTION",
                ),
                response_path=ResponsePath.SAFE_FALLBACK,
            )
        if status is IntentDecisionStatus.MATCHED:
            intent_id = str(output.get("intent_id", ""))
            if intent_id not in catalog:
                raise ValueError("unknown matched intent")
            item = catalog[intent_id]
            confidence = max(0.0, min(1.0, float(output.get("confidence", 0))))
            catalog_kind = RequestKind(item["request_kind"])
            if (
                item["route_target"] == IntentRouteTarget.SCENE.value
                and output.get("execution_requested") is False
            ):
                return RoutingDecision(
                    intent_decision=IntentDecision(
                        status=IntentDecisionStatus.UNKNOWN,
                        catalog_version="model-catalog-v1",
                        provider_model=self._model,
                        candidates=candidates,
                        reason_codes=("SCENE_ACTION_NOT_REQUESTED",),
                    ),
                    request_scope=RequestScope.IN_SCOPE,
                    request_kind=RequestKind.KNOWLEDGE,
                    scope_match_source=ScopeMatchSource.MODEL,
                    scope_reason_codes=("MODEL_CLASSIFIER", "SCENE_ACTION_NOT_REQUESTED"),
                    response_path=ResponsePath.SAFE_FALLBACK,
                )
            if (
                catalog_kind is not RequestKind.HIGH_RISK
                and kind is not RequestKind.UNSURE
                and kind is not catalog_kind
            ):
                return RoutingDecision(
                    intent_decision=IntentDecision(
                        status=IntentDecisionStatus.UNKNOWN,
                        catalog_version="model-catalog-v1",
                        provider_model=self._model,
                        candidates=candidates,
                        reason_codes=("INTENT_REQUEST_KIND_CONFLICT",),
                    ),
                    request_scope=scope,
                    request_kind=kind,
                    scope_match_source=ScopeMatchSource.MODEL,
                    scope_reason_codes=("MODEL_CLASSIFIER", "INTENT_REQUEST_KIND_CONFLICT"),
                    response_path=ResponsePath.SAFE_FALLBACK,
                )
            if (
                item["request_kind"] == RequestKind.HIGH_RISK.value
                and confidence < HIGH_RISK_MIN_CONFIDENCE
            ):
                return RoutingDecision(
                    intent_decision=IntentDecision(
                        status=IntentDecisionStatus.UNKNOWN,
                        catalog_version="model-catalog-v1",
                        provider_model=self._model,
                        reason_codes=("HIGH_RISK_MODEL_LOW_CONFIDENCE",),
                    ),
                    request_scope=RequestScope.IN_SCOPE,
                    request_kind=RequestKind.HIGH_RISK,
                    scope_match_source=ScopeMatchSource.MODEL,
                    scope_reason_codes=("HIGH_RISK_MODEL_LOW_CONFIDENCE",),
                    response_path=ResponsePath.SAFE_FALLBACK,
                )
            decision = IntentDecision(
                status=status,
                intent_id=intent_id,
                route_target=IntentRouteTarget(item["route_target"]),
                match_source=IntentMatchSource.MODEL,
                confidence=confidence,
                catalog_version="model-catalog-v1",
                matched_source_ref="llm-classifier",
                provider_model=self._model,
                candidates=candidates,
                reason_codes=(str(output.get("reason", "MODEL_MATCH")),),
            )
            return RoutingDecision(
                intent_decision=decision,
                request_scope=scope,
                request_kind=catalog_kind,
                scope_match_source=ScopeMatchSource.MODEL,
                scope_reason_codes=("MODEL_CLASSIFIER",),
                response_path=(
                    ResponsePath.FAQ
                    if item["route_target"] == IntentRouteTarget.FAQ.value
                    else ResponsePath.SAFE_FALLBACK
                ),
            )
        if status in {IntentDecisionStatus.AMBIGUOUS, IntentDecisionStatus.MULTI_INTENT}:
            query_clarification = (
                self._query_clarification(output, query)
                if status is IntentDecisionStatus.AMBIGUOUS
                else None
            )
            if query_clarification is None and len(candidates) < 2:
                raise ValueError("intent ambiguity needs at least two candidates")
            return RoutingDecision(
                intent_decision=IntentDecision(
                    status=status,
                    catalog_version="model-catalog-v1",
                    candidates=candidates,
                    provider_model=self._model,
                    reason_codes=(str(output.get("reason", "MODEL_AMBIGUOUS")),),
                ),
                request_scope=scope,
                request_kind=kind,
                scope_match_source=ScopeMatchSource.MODEL,
                scope_reason_codes=("MODEL_CLASSIFIER",),
                response_path=ResponsePath.CLARIFICATION,
                query_clarification=query_clarification,
            )
        return RoutingDecision(
            intent_decision=IntentDecision(
                status=IntentDecisionStatus.UNKNOWN,
                catalog_version="model-catalog-v1",
                provider_model=self._model,
                reason_codes=(str(output.get("reason", "MODEL_UNKNOWN")),),
            ),
            request_scope=scope,
            request_kind=kind,
            required_capability=(
                str(output["required_capability"]) if output.get("required_capability") else None
            ),
            scope_match_source=ScopeMatchSource.MODEL,
            scope_reason_codes=("MODEL_CLASSIFIER",),
            response_path=ResponsePath.SAFE_FALLBACK,
        )

    @staticmethod
    def _query_clarification(
        output: dict[str, Any], query: str
    ) -> QueryClarificationProposal | None:
        """校验有限澄清选项，由 Runtime 保留原问题并构造后续检索 Query。"""

        raw = output.get("query_clarification")
        if raw is None:
            return None
        if not isinstance(raw, dict) or not query.strip():
            raise ValueError("query clarification requires the original query")
        raw_options = raw.get("options")
        if not isinstance(raw_options, list) or not 2 <= len(raw_options) <= 3:
            raise ValueError("query clarification requires two or three options")
        original_query = query.strip()
        if not original_query.rstrip("。！？?!"):
            raise ValueError("query clarification requires meaningful original text")
        original_numbers = set(re.findall(r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?%?", query))
        options = []
        for index, item in enumerate(raw_options, start=1):
            if not isinstance(item, dict):
                raise ValueError("query clarification option must be an object")
            label = str(item.get("label", "")).strip()
            if not label:
                raise ValueError("query clarification option requires a label")
            label_numbers = set(re.findall(r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?%?", label))
            if label_numbers - original_numbers:
                raise ValueError("query clarification introduced unverified numeric facts")
            resolved_query = f"客户已确认的业务条件：{label}。原问题：{original_query}"
            if len(resolved_query) > 4000:
                raise ValueError("resolved query exceeds the input length limit")
            options.append(
                QueryClarificationOption(
                    option_id=f"query_{index}",
                    label=label,
                    resolved_query=resolved_query,
                )
            )
        return QueryClarificationProposal(
            question=str(raw.get("question", "")).strip(),
            options=tuple(options),
        )

    def _record(
        self,
        *,
        state: dict[str, object],
        call_id: str,
        purpose: str,
        status: ModelCallStatus,
        started: float,
        usage: dict[str, Any],
        provider_request_id: str | None = None,
        error_code: str | None = None,
    ) -> ModelCallRecord:
        """从接口 usage 字段建立一条可落库的模型调用记录。"""

        input_tokens = self._number(usage.get("prompt_tokens"))
        output_tokens = self._number(usage.get("completion_tokens"))
        total_tokens = self._number(usage.get("total_tokens"))
        return ModelCallRecord(
            call_id=call_id,
            run_id=str(state["run_id"]),
            turn_id=str(state["turn_id"]),
            trace_id=str(state["trace_id"]),
            purpose=purpose,
            provider=self._provider,
            model=self._model,
            provider_request_id=provider_request_id,
            status=status,
            estimated_input_tokens=self._number(usage.get("estimated_input_tokens")),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            context_window_tokens=self._context_window_tokens,
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            error_code=error_code,
            created_at=datetime.now(UTC),
        )

    def _failure(
        self,
        state: dict[str, object],
        call_id: str,
        status: ModelCallStatus,
        error_code: str,
        started: float,
        *,
        usage: dict[str, Any] | None = None,
        provider_request_id: str | None = None,
    ) -> tuple[RoutingDecision, ModelCallRecord]:
        """模型不可用时返回安全 UNKNOWN；客户只会看到统一固定回复。"""

        decision = RoutingDecision(
            intent_decision=IntentDecision(
                status=IntentDecisionStatus.UNKNOWN,
                catalog_version="model-catalog-v1",
                provider_model=self._model,
                reason_codes=(error_code,),
            ),
            request_scope=RequestScope.UNSURE,
            request_kind=RequestKind.UNSURE,
            scope_match_source=ScopeMatchSource.MODEL,
            scope_reason_codes=(error_code,),
            response_path=ResponsePath.SAFE_FALLBACK,
        )
        return decision, self._record(
            state=state,
            call_id=call_id,
            purpose="MODEL_ROUTING",
            status=status,
            started=started,
            usage=usage or {},
            provider_request_id=provider_request_id,
            error_code=error_code,
        )

    @staticmethod
    def _number(value: Any) -> int | None:
        """只接受非负整数 usage，供应商未返回时保留空值。"""

        return int(value) if isinstance(value, (int, float)) and value >= 0 else None
