"""全局意图、能力边界和澄清契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from shared.contracts.common import ContractModel
from shared.contracts.enums import (
    ClarificationSourceLayer,
    IntentClass,
    IntentDecisionStatus,
    IntentMatchSource,
    IntentRouteTarget,
    RequestKind,
    RequestScope,
    ResponsePath,
    ScopeMatchSource,
)


class IntentDefinition(ContractModel):
    """发布后唯一绑定一个目标模块的全局意图定义。"""

    intent_id: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    intent_class: IntentClass
    route_target: IntentRouteTarget
    request_kind: RequestKind
    catalog_version: str = Field(min_length=1)
    enabled: bool = True


class IntentRule(ContractModel):
    """经审核发布的高精度关键词或控制规则。"""

    rule_id: str = Field(min_length=1)
    rule_kind: Literal["INTENT", "CONTROL", "CAPABILITY", "DOMAIN"]
    intent_id: str | None = None
    intent_version: str | None = None
    pattern: str = Field(min_length=1)
    match_mode: Literal["EXACT", "CONTAINS"]
    priority: int = Field(default=0, ge=0)
    negative_patterns: tuple[str, ...] = ()
    product_codes: tuple[str, ...] = ()
    channels: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    required_capability: str | None = None
    control_action: Literal["CANCEL", "HUMAN"] | None = None
    source_ref: str = Field(min_length=1)
    version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_binding(self) -> IntentRule:
        """控制、能力和意图规则必须携带各自唯一需要的字段。"""

        if self.rule_kind == "INTENT":
            if not self.intent_id or not self.intent_version:
                raise ValueError("INTENT rule requires intent_id and intent_version")
        elif self.intent_id is not None or self.intent_version is not None:
            raise ValueError("only INTENT rule may bind an intent")
        if (self.rule_kind == "CAPABILITY") != bool(self.required_capability):
            raise ValueError("only CAPABILITY rule requires required_capability")
        if (self.rule_kind == "CONTROL") != bool(self.control_action):
            raise ValueError("only CONTROL rule requires control_action")
        return self


class IntentSimilarity(ContractModel):
    """绑定全局意图、用于语义相似检索的已审核语料。"""

    similarity_id: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    locale: str = "zh-CN"
    version: str = Field(min_length=1)


class IntentCandidate(ContractModel):
    """相似问或 Decision Provider 返回的一个全局意图候选。"""

    intent_id: str = Field(min_length=1)
    route_target: IntentRouteTarget
    score: float = Field(ge=0, le=1)
    source_ref: str | None = None


class IntentDecision(ContractModel):
    """只表达全局意图的识别结果。"""

    status: IntentDecisionStatus
    intent_id: str | None = None
    route_target: IntentRouteTarget | None = None
    match_source: IntentMatchSource | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    catalog_version: str = Field(min_length=1)
    matched_source_ref: str | None = None
    provider_model: str | None = None
    candidates: tuple[IntentCandidate, ...] = ()
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_status_payload(self) -> IntentDecision:
        """防止 UNKNOWN 或歧义状态携带伪造的唯一命中。"""

        if self.status is IntentDecisionStatus.MATCHED:
            if not self.intent_id or not self.route_target or not self.match_source:
                raise ValueError("MATCHED requires intent_id, route_target and match_source")
        elif self.intent_id is not None or self.route_target is not None:
            raise ValueError("only MATCHED may carry intent_id and route_target")

        if self.status is IntentDecisionStatus.MULTI_INTENT and len(self.candidates) < 2:
            raise ValueError("MULTI_INTENT requires at least two candidates")
        if self.status is IntentDecisionStatus.AMBIGUOUS and len(self.candidates) == 1:
            raise ValueError("AMBIGUOUS requires zero or at least two candidates")
        return self


class QueryClarificationOption(ContractModel):
    """模型提出、经 Runtime 校验后可供客户选择的完整 Query。"""

    option_id: str = Field(min_length=1)
    label: str = Field(min_length=1, max_length=100)
    resolved_query: str = Field(min_length=1, max_length=4000)


class QueryClarificationProposal(ContractModel):
    """客户原文缺少业务语义条件时使用的有限澄清方案。"""

    question: str = Field(min_length=1, max_length=500)
    options: tuple[QueryClarificationOption, ...] = Field(min_length=2, max_length=3)

    @model_validator(mode="after")
    def validate_unique_options(self) -> QueryClarificationProposal:
        """展示名称和内部选项编号必须唯一，避免选择结果不确定。"""

        if len({item.option_id for item in self.options}) != len(self.options):
            raise ValueError("query clarification option_id must be unique")
        if len({item.label for item in self.options}) != len(self.options):
            raise ValueError("query clarification label must be unique")
        return self


class RoutingDecision(ContractModel):
    """动作意图、业务域、能力边界和最终响应路径的组合决策。"""

    intent_decision: IntentDecision
    request_scope: RequestScope
    request_kind: RequestKind
    required_capability: str | None = None
    scope_match_source: ScopeMatchSource
    scope_reason_codes: tuple[str, ...] = ()
    response_path: ResponsePath
    query_clarification: QueryClarificationProposal | None = None

    @model_validator(mode="after")
    def validate_response_path(self) -> RoutingDecision:
        """保证回答路径不与动作意图或范围决策矛盾。"""

        intent = self.intent_decision
        if self.response_path is ResponsePath.SOP and not (
            intent.status is IntentDecisionStatus.MATCHED
            and intent.route_target is IntentRouteTarget.SCENE
        ):
            raise ValueError("SOP requires a matched SCENE intent")
        if self.response_path is ResponsePath.FAQ and not (
            intent.status is IntentDecisionStatus.MATCHED
            and intent.route_target is IntentRouteTarget.FAQ
        ):
            raise ValueError("FAQ requires a matched FAQ intent")
        if self.response_path is ResponsePath.RAG and not (
            intent.status is IntentDecisionStatus.UNKNOWN
            and self.request_scope is RequestScope.IN_SCOPE
            and self.request_kind is RequestKind.KNOWLEDGE
        ):
            raise ValueError("RAG requires UNKNOWN + IN_SCOPE + KNOWLEDGE")
        if self.response_path is ResponsePath.HUMAN and not (
            intent.status is IntentDecisionStatus.UNKNOWN
            and self.request_scope is RequestScope.IN_SCOPE
            and self.request_kind is RequestKind.CONTROL
            and "CONTROL:HUMAN" in self.scope_reason_codes
        ):
            raise ValueError("HUMAN requires the published CONTROL:HUMAN decision")
        if self.query_clarification is not None and not (
            self.response_path is ResponsePath.CLARIFICATION
            and intent.status is IntentDecisionStatus.AMBIGUOUS
        ):
            raise ValueError("query clarification requires an ambiguous clarification path")
        if (
            intent.status is IntentDecisionStatus.AMBIGUOUS
            and len(intent.candidates) < 2
            and self.query_clarification is None
        ):
            raise ValueError(
                "AMBIGUOUS requires intent candidates or a query clarification"
            )
        if (
            self.request_scope is RequestScope.OUT_OF_SCOPE
            and self.request_kind is RequestKind.EXTERNAL_CAPABILITY
        ):
            if not self.required_capability:
                raise ValueError("external capability requests require required_capability")
            if self.response_path is not ResponsePath.SAFE_FALLBACK:
                raise ValueError("unsupported external capabilities must use SAFE_FALLBACK")
        return self


class ClarificationOption(ContractModel):
    """向用户展示并可在下一轮确定性解析的一个有限选项。"""

    option_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    candidate_ref: str = Field(min_length=1)


class PendingClarification(ContractModel):
    """可跨 HTTP 请求恢复的一次待澄清状态。"""

    clarification_id: str = Field(min_length=1)
    source_layer: ClarificationSourceLayer
    origin_turn_id: str = Field(min_length=1)
    candidate_refs: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    question: str = Field(min_length=1)
    options: tuple[ClarificationOption, ...] = ()
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=2, ge=1)
    resume_node: str = Field(min_length=1)
    expires_at: datetime

    @model_validator(mode="after")
    def validate_attempts_and_candidates(self) -> PendingClarification:
        """限制重试次数，并要求澄清确实有待消解信息。"""

        if self.attempt_count > self.max_attempts:
            raise ValueError("attempt_count must not exceed max_attempts")
        if not self.candidate_refs and not self.missing_fields:
            raise ValueError("clarification requires candidates or missing_fields")
        return self
