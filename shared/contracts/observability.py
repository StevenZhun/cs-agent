"""端到端 Trace 的无正文阶段记录与授权诊断视图。"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, JsonValue, model_validator

from shared.contracts.common import ContractModel
from shared.contracts.enums import TraceSpanStatus
from shared.contracts.runtime import DecisionTrace, ModelCallRecord

_FORBIDDEN_ATTRIBUTE_FRAGMENTS = (
    "content",
    "message",
    "prompt",
    "query",
    "answer",
    "secret",
    "token_value",
)


class TraceSpan(ContractModel):
    """一次 Run 中一个处理阶段的耗时和有限结果标签，不保存客户正文。"""

    span_id: str = Field(min_length=1, max_length=96)
    trace_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=64)
    thread_id: str = Field(min_length=1, max_length=64)
    turn_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    parent_span_id: str | None = Field(default=None, max_length=96)
    component: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=32)
    operation: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    status: TraceSpanStatus
    started_at: datetime
    finished_at: datetime
    latency_ms: int = Field(ge=0)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    error_code: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]*$",
        max_length=64,
    )

    @model_validator(mode="after")
    def validate_safe_attributes(self) -> TraceSpan:
        """Span 只允许诊断标签，禁止把消息、Prompt 或答案伪装成属性。"""

        if self.finished_at < self.started_at:
            raise ValueError("trace span cannot finish before it starts")
        lowered = tuple(key.lower() for key in self.attributes)
        if any(fragment in key for key in lowered for fragment in _FORBIDDEN_ATTRIBUTE_FRAGMENTS):
            raise ValueError("trace span attributes cannot contain raw text fields")
        if self.status is TraceSpanStatus.FAILED and self.error_code is None:
            raise ValueError("failed trace span requires error_code")
        if self.status is TraceSpanStatus.COMPLETED and self.error_code is not None:
            raise ValueError("completed trace span cannot carry error_code")
        return self


class ToolCallTrace(ContractModel):
    """业务工具的无参数、无结果诊断摘要。"""

    tool_call_id: str
    tool_name: str
    status: str
    latency_ms: int = Field(ge=0)
    error_code: str | None = None


class TraceDetail(ContractModel):
    """管理员或授权坐席按 trace_id 读取的诊断视图。"""

    trace_id: str
    conversation_id: str
    turn_id: str
    run_id: str
    terminal_response_path: str | None = None
    terminal_reason_code: str | None = None
    spans: tuple[TraceSpan, ...] = ()
    decision_traces: tuple[DecisionTrace, ...] = ()
    model_calls: tuple[ModelCallRecord, ...] = ()
    tool_calls: tuple[ToolCallTrace, ...] = ()


class TraceOperationsSummary(ContractModel):
    """一段时间内各处理阶段的数量和延迟分位数。"""

    window_hours: int = Field(ge=1, le=24 * 31)
    operation_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    p50_latency_ms: float | None = Field(default=None, ge=0)
    p95_latency_ms: float | None = Field(default=None, ge=0)
    by_component: dict[str, int] = Field(default_factory=dict)
