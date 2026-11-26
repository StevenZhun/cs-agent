"""Query 派生视图、上下文引用与事实解析契约。"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import Field, JsonValue, model_validator

from shared.contracts.common import ContractModel, RawContractModel
from shared.contracts.enums import (
    DialogueAct,
    FactAuthority,
    FactSourceType,
    HistoryRecallMode,
    QueryResolutionStatus,
)


class QueryResolutionResult(ContractModel):
    """Query 是否需要以及能否被唯一补全的可审计结果。"""

    status: QueryResolutionStatus
    resolved_query: str | None = None
    used_context_refs: tuple[str, ...] = ()
    missing_context: tuple[str, ...] = ()
    clarification: str | None = None
    reason_codes: tuple[str, ...] = ()
    dialogue_act: DialogueAct = DialogueAct.NEW_REQUEST
    preserve_intent: bool | None = None
    preserved_intent_id: str | None = None
    excluded_intent_id: str | None = None
    excluded_faq_item_id: str | None = None
    correction_source_ref: str | None = None

    @model_validator(mode="after")
    def validate_resolution_state(self) -> QueryResolutionResult:
        """保证状态、改写文本和澄清信息具有一致语义。"""

        if self.status is QueryResolutionStatus.RESOLVED:
            if not self.resolved_query or not self.used_context_refs:
                raise ValueError("RESOLVED requires resolved_query and used_context_refs")
        elif self.resolved_query is not None:
            raise ValueError("only RESOLVED may carry resolved_query")

        if (
            self.status
            in {
                QueryResolutionStatus.AMBIGUOUS,
                QueryResolutionStatus.INSUFFICIENT,
            }
            and not self.clarification
        ):
            raise ValueError("AMBIGUOUS and INSUFFICIENT require clarification")
        if self.status is QueryResolutionStatus.INSUFFICIENT and not self.missing_context:
            raise ValueError("INSUFFICIENT requires missing_context")

        correction_fields = (
            self.preserve_intent,
            self.preserved_intent_id,
            self.excluded_intent_id,
            self.excluded_faq_item_id,
            self.correction_source_ref,
        )
        if self.dialogue_act is not DialogueAct.CORRECTION:
            if any(value is not None for value in correction_fields):
                raise ValueError("only CORRECTION may carry correction constraints")
            return self
        if self.status is not QueryResolutionStatus.RESOLVED or not self.correction_source_ref:
            raise ValueError("CORRECTION requires a resolved query and source reference")
        if self.preserve_intent is True:
            if not self.preserved_intent_id or not self.excluded_faq_item_id:
                raise ValueError("preserved correction requires intent and FAQ item")
            if self.excluded_intent_id is not None:
                raise ValueError("preserved correction cannot exclude its intent")
        elif self.preserve_intent is False:
            if self.preserved_intent_id is not None or self.excluded_faq_item_id is not None:
                raise ValueError("intent correction cannot pin FAQ resolution")
        elif any(
            value is not None
            for value in (
                self.preserved_intent_id,
                self.excluded_intent_id,
                self.excluded_faq_item_id,
            )
        ):
            raise ValueError("unverified correction cannot carry exclusion targets")
        return self


class QueryContext(RawContractModel):
    """保留原始输入并记录确定性派生视图的 Query 契约。"""

    raw_query: str = Field(min_length=1)
    normalized_query: str = Field(min_length=1)
    resolution: QueryResolutionResult
    effective_query: str = Field(min_length=1)
    query_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedding_profile_id: str | None = None

    @model_validator(mode="after")
    def validate_query_derivation(self) -> QueryContext:
        """保证 effective_query 只能来自规范化文本或唯一消解结果。"""

        if not self.raw_query.strip():
            raise ValueError("raw_query must contain non-whitespace content")
        expected = (
            self.resolution.resolved_query
            if self.resolution.status is QueryResolutionStatus.RESOLVED
            else self.normalized_query
        )
        if self.effective_query != expected:
            raise ValueError("effective_query does not match the resolution result")
        return self


class HistoryRecallDecision(ContractModel):
    """限定当前 Turn 是否以及如何回查同一业务会话的历史原文。"""

    mode: HistoryRecallMode
    conversation_id: str = Field(min_length=1, max_length=64)
    turn_id: str = Field(min_length=1, max_length=64)
    attempt: int = Field(ge=0, le=3)
    max_attempts: int = Field(default=1, ge=1, le=3)
    target_source_refs: tuple[str, ...] = Field(default=(), max_length=8)
    target_turn_ids: tuple[str, ...] = Field(default=(), max_length=8)
    search_terms: tuple[str, ...] = Field(default=(), max_length=3)
    max_results: int = Field(default=8, ge=1, le=20)
    clarification: str | None = Field(default=None, min_length=1, max_length=300)
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_recall_scope(self) -> HistoryRecallDecision:
        """限制选择器组合，并在达到次数上限后停止自动回查。"""

        selectors = bool(self.target_source_refs or self.target_turn_ids or self.search_terms)
        if self.mode is HistoryRecallMode.NOT_REQUIRED:
            if selectors or self.clarification is not None:
                raise ValueError("NOT_REQUIRED cannot carry recall selectors")
            return self
        if self.mode is HistoryRecallMode.CLARIFY:
            if selectors or not self.clarification:
                raise ValueError("CLARIFY requires only a clarification question")
            return self
        if self.attempt >= self.max_attempts:
            raise ValueError("automatic history recall attempt limit reached")
        if self.clarification is not None:
            raise ValueError("only CLARIFY may carry clarification")
        if self.mode is HistoryRecallMode.EXACT_REF:
            if not self.target_source_refs or self.target_turn_ids or self.search_terms:
                raise ValueError("EXACT_REF requires only target_source_refs")
            if any(
                re.fullmatch(r"(?:message|turn):[A-Za-z0-9_.-]{1,64}", reference) is None
                for reference in self.target_source_refs
            ):
                raise ValueError("EXACT_REF only accepts message or turn references")
        elif self.mode is HistoryRecallMode.TURN_RANGE:
            if not self.target_turn_ids or self.target_source_refs or self.search_terms:
                raise ValueError("TURN_RANGE requires only target_turn_ids")
            if any(not turn_id.strip() or len(turn_id) > 64 for turn_id in self.target_turn_ids):
                raise ValueError("history target turn IDs must be short non-empty text")
        elif self.mode is HistoryRecallMode.CONVERSATION_SEARCH:
            if not self.search_terms or self.target_source_refs or self.target_turn_ids:
                raise ValueError("CONVERSATION_SEARCH requires only search_terms")
            if any(not term.strip() or len(term) > 80 for term in self.search_terms):
                raise ValueError("history search terms must be short non-empty text")
        return self


class ProductContext(ContractModel):
    """路由所需的最小产品上下文和事实引用。"""

    product_id: str = Field(min_length=1)
    product_type: str = Field(min_length=1)
    fact_ref: str = Field(min_length=1)


class ActiveSopContext(ContractModel):
    """路由器可读取的 Active SOP 轻量引用。"""

    sop_execution_id: str = Field(min_length=1)
    sop_id: str = Field(min_length=1)
    sop_version: str = Field(min_length=1)
    current_step_id: str = Field(min_length=1)


class RoutingContext(ContractModel):
    """不污染向量文本的结构化已确认路由上下文。"""

    conversation_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    product_context: tuple[ProductContext, ...] = ()
    previous_intent_id: str | None = None
    active_sop: ActiveSopContext | None = None
    confirmed_context_refs: tuple[str, ...] = ()
    pending_action_refs: tuple[str, ...] = ()


class ResolvedContextFact(ContractModel):
    """带语义、来源、权威性与时效的一项已解析上下文字段。"""

    fact_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    value: JsonValue
    value_type: str = Field(min_length=1)
    source_type: FactSourceType
    source_ref: str = Field(min_length=1)
    authority: FactAuthority
    subject_ref: str = Field(min_length=1)
    as_of: datetime
    expires_at: datetime | None = None
    conflict_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_authority(self) -> ResolvedContextFact:
        """阻止业务事实与长期偏好被赋予错误的权威等级。"""

        if (
            self.source_type is FactSourceType.BUSINESS_FACT
            and self.authority is not FactAuthority.AUTHORITATIVE
        ):
            raise ValueError("BUSINESS_FACT must be AUTHORITATIVE")
        if (
            self.source_type is FactSourceType.LONG_TERM_MEMORY
            and self.authority is not FactAuthority.PREFERENCE
        ):
            raise ValueError("LONG_TERM_MEMORY may only be PREFERENCE")
        if (
            self.source_type is FactSourceType.MODEL_INFERENCE
            and self.authority is not FactAuthority.CANDIDATE
        ):
            raise ValueError("MODEL_INFERENCE may only be CANDIDATE")
        if self.expires_at is not None and self.expires_at < self.as_of:
            raise ValueError("expires_at must not be earlier than as_of")
        return self


class ResolvedContext(ContractModel):
    """当前 Turn 后续节点实际需要的最小事实集合。"""

    facts: tuple[ResolvedContextFact, ...] = ()

    @model_validator(mode="after")
    def validate_unique_fact_keys(self) -> ResolvedContext:
        """同一快照中一个语义键只能有一个当前值。"""

        keys = [fact.fact_key for fact in self.facts]
        if len(keys) != len(set(keys)):
            raise ValueError("ResolvedContext fact_key values must be unique")
        return self
