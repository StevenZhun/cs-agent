"""同步输入、工具和输出护栏共用的原因码与确定性检查。"""

from __future__ import annotations

import re

from shared.contracts.enums import QualityAction, QualitySeverity
from shared.contracts.quality import QualityDecision, QualityFinding

GUARDRAIL_POLICY_VERSION = "synchronous-guardrails-v1"

INPUT_PROMPT_INJECTION = "INPUT_PROMPT_INJECTION"
INPUT_PROTECTED_SECRET = "INPUT_PROTECTED_SECRET"
TOOL_NOT_PUBLISHED = "TOOL_NOT_PUBLISHED"
TOOL_CUSTOMER_SCOPE_MISSING = "TOOL_CUSTOMER_SCOPE_MISSING"
TOOL_ARGUMENTS_INVALID = "TOOL_ARGUMENTS_INVALID"
TOOL_APPROVAL_REQUIRED = "TOOL_APPROVAL_REQUIRED"
OUTPUT_PROTECTED_SECRET = "OUTPUT_PROTECTED_SECRET"
OUTPUT_UNAUTHORIZED_PROMISE = "OUTPUT_UNAUTHORIZED_PROMISE"

_PROMPT_INJECTION_PATTERNS = (
    re.compile(r"忽略(?:以上|之前|系统|所有).{0,12}(?:指令|规则|提示词)", re.IGNORECASE),
    re.compile(r"(?:显示|泄露|输出|复述).{0,10}(?:系统提示词|system\s*prompt)", re.IGNORECASE),
    re.compile(r"(?:越狱|jailbreak|开发者模式)", re.IGNORECASE),
)
_PROTECTED_SECRET_PATTERNS = (
    re.compile(r"(?<!\d)\d{13,19}(?!\d)"),
    re.compile(
        r"(?:短信验证码|验证码|安全码|访问令牌|登录密码|支付密码|身份号码)"
        r"\s*(?:是|为|:|：)?\s*[A-Za-z0-9]{3,24}",
        re.IGNORECASE,
    ),
)
_UNAUTHORIZED_PROMISE_PATTERNS = (
    re.compile(r"(?:保证|确保).{0,10}(?:通过|完成|退款|成功|获批)"),
    re.compile(r"(?:百分之百|100%)\s*(?:通过|完成|退款|成功|获批)"),
)


def redact_protected_secrets(content: str) -> str:
    """模型异步复核前移除普通聊天链不应携带的完整秘密信息。"""

    redacted = content
    for pattern in _PROTECTED_SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _decision(
    *,
    decision_id: str,
    action: QualityAction,
    code: str | None = None,
    message: str | None = None,
) -> QualityDecision:
    findings = (
        (
            QualityFinding(
                finding_id=f"{decision_id}:{code}",
                code=code,
                severity=QualitySeverity.ERROR,
                message=message or code,
                source="DETERMINISTIC_GUARDRAIL",
                source_version=GUARDRAIL_POLICY_VERSION,
            ),
        )
        if code
        else ()
    )
    return QualityDecision(
        decision_id=decision_id,
        action=action,
        findings=findings,
        policy_version=GUARDRAIL_POLICY_VERSION,
        reason_codes=((code,) if code else ("SYNCHRONOUS_GUARDRAILS_PASSED",)),
    )


def evaluate_customer_input(content: str, *, decision_id: str) -> QualityDecision:
    """只阻断明确注入和受保护秘密；普通业务长文本交给后续路由。"""

    if any(pattern.search(content) for pattern in _PROTECTED_SECRET_PATTERNS):
        return _decision(
            decision_id=decision_id,
            action=QualityAction.BLOCK,
            code=INPUT_PROTECTED_SECRET,
            message="客户消息包含不应进入普通聊天链路的完整秘密信息。",
        )
    if any(pattern.search(content) for pattern in _PROMPT_INJECTION_PATTERNS):
        return _decision(
            decision_id=decision_id,
            action=QualityAction.BLOCK,
            code=INPUT_PROMPT_INJECTION,
            message="客户消息试图覆盖系统指令或索取系统提示词。",
        )
    return _decision(decision_id=decision_id, action=QualityAction.PASS)


def evaluate_output_content(content: str, *, decision_id: str) -> QualityDecision:
    """在路径来源校验之外，阻止秘密信息和未经授权的确定性承诺。"""

    if any(pattern.search(content) for pattern in _PROTECTED_SECRET_PATTERNS):
        return _decision(
            decision_id=decision_id,
            action=QualityAction.BLOCK,
            code=OUTPUT_PROTECTED_SECRET,
            message="候选回复包含不允许展示的完整秘密信息。",
        )
    unauthorized_promise = False
    for pattern in _UNAUTHORIZED_PROMISE_PATTERNS:
        for match in pattern.finditer(content):
            prefix = content[max(0, match.start() - 4) : match.start()]
            if any(negation in prefix for negation in ("不", "不能", "无法", "不一定")):
                continue
            unauthorized_promise = True
            break
        if unauthorized_promise:
            break
    if unauthorized_promise:
        return _decision(
            decision_id=decision_id,
            action=QualityAction.BLOCK,
            code=OUTPUT_UNAUTHORIZED_PROMISE,
            message="候选回复包含未经权威业务结果支持的确定性承诺。",
        )
    return _decision(decision_id=decision_id, action=QualityAction.PASS)


class ToolGuardrailViolation(ValueError):
    """工具调用未通过发布配置、客户范围或副作用授权检查。"""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def validate_read_only_tool_call(
    *,
    tool_name: str,
    arguments: dict[str, str],
    state: dict,
) -> None:
    """根据当前已发布 SOP 的工具清单和固定参数契约放行只读调用。"""

    definition = state.get("sop_definition")
    published_tools = (
        {str(item) for item in definition.get("tools", ())}
        if isinstance(definition, dict)
        else set()
    )
    if tool_name not in published_tools:
        raise ToolGuardrailViolation(TOOL_NOT_PUBLISHED)
    if not state.get("business_customer_id") or not state.get("active_sop_execution"):
        raise ToolGuardrailViolation(TOOL_CUSTOMER_SCOPE_MISSING)
    expected_arguments = {"GET_CUSTOMER_CONTEXT": {frozenset()}}
    if frozenset(arguments) not in expected_arguments.get(tool_name, set()):
        raise ToolGuardrailViolation(TOOL_ARGUMENTS_INVALID)
