"""客户可见安全回复的单一文本来源。

Runtime 负责根据路由原因选择回复，BFF 负责再次核对允许发送的原因码和文本。
两层必须共享同一份清单，避免 Runtime 与应用层的安全策略发生漂移。
"""

SAFE_FALLBACK_REPLY = "抱歉，我暂时无法准确回答您的问题。您可以联系人工客服继续咨询。"
UNSUPPORTED_CAPABILITY_REPLY = (
    "抱歉，当前服务暂未接入这项外部能力，无法提供对应的实时查询、预测或规划结果。"
    "请使用相应的专业服务。"
)
HUMAN_HANDOFF_REPLY = "正在为您转接人工坐席，请稍候。"
RAG_REVIEW_PENDING_REPLY = "已检索到相关资料，正在由人工坐席核对后回复您。"
RAG_REVIEW_REJECTED_REPLY = (
    "当前检索草稿未通过人工核对，暂不能作为可靠答复发送。"
    "您可以继续补充问题，或联系人工客服进一步处理。"
)
RAG_EVIDENCE_CONFLICT_REPLY = (
    "检索到的相关资料暂时无法支持唯一、可靠的结论。已停止自动回答，您可以联系人工客服进一步核对。"
)
RAG_EVIDENCE_INSUFFICIENT_REPLY = (
    "现有资料暂时不足以支持可靠答复。您可以补充更具体的问题，或联系人工客服继续核对。"
)
RAG_GENERATION_FAILED_REPLY = (
    "已找到相关资料，但暂时无法形成可核验的答复。您可以联系人工客服继续核对。"
)
PROTECTED_SECRET_REPLY = (
    "为保护您的账户安全，请勿在普通咨询中发送完整身份号码、密码、安全码、访问令牌或短信验证码。"
    "如需核验，请通过官方受保护的身份验证渠道完成。"
)
PROMPT_INJECTION_REPLY = "该请求涉及系统内部指令，无法处理。您可以继续咨询具体服务问题。"

SAFE_FALLBACK_ALLOWLIST: dict[str, frozenset[str]] = {
    "NO_GROUNDED_ANSWER": frozenset(
        {
            SAFE_FALLBACK_REPLY,
            "请联系人工客服继续咨询。",
        }
    ),
    "RAG_EVIDENCE_CONFLICT": frozenset({RAG_EVIDENCE_CONFLICT_REPLY}),
    "RAG_EVIDENCE_INSUFFICIENT": frozenset({RAG_EVIDENCE_INSUFFICIENT_REPLY}),
    "RAG_GENERATION_FAILED": frozenset({RAG_GENERATION_FAILED_REPLY}),
    "UNSUPPORTED_CAPABILITY": frozenset({UNSUPPORTED_CAPABILITY_REPLY}),
    "INPUT_PROTECTED_SECRET": frozenset({PROTECTED_SECRET_REPLY}),
    "INPUT_PROMPT_INJECTION": frozenset({PROMPT_INJECTION_REPLY}),
}
