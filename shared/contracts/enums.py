"""Contract v2 跨服务契约共同使用的有限状态与来源枚举。"""

from enum import StrEnum


class ConversationStatus(StrEnum):
    """业务会话生命周期状态。"""

    ACTIVE = "ACTIVE"
    WRAP_UP = "WRAP_UP"
    FINISHED = "FINISHED"


class MessageRole(StrEnum):
    """业务会话中的消息发送角色。"""

    CUSTOMER = "CUSTOMER"
    AGENT = "AGENT"
    ASSISTANT = "ASSISTANT"
    SYSTEM = "SYSTEM"
    TOOL = "TOOL"


class SlotStatus(StrEnum):
    """业务槽位是模型候选值还是已核实事实。"""

    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"


class DataSource(StrEnum):
    """接口字段或推理结果的来源系统。"""

    USER = "USER"
    APP = "APP"
    AI_RUNTIME = "AI_RUNTIME"
    CUSTOMER_DATA = "CUSTOMER_DATA"
    TICKET_SYSTEM = "TICKET_SYSTEM"
    CONTENT = "CONTENT"
    CUSTOM_RAG = "CUSTOM_RAG"
    MODEL_INFERENCE = "MODEL_INFERENCE"
    LONG_TERM_MEMORY = "LONG_TERM_MEMORY"


class QueryResolutionStatus(StrEnum):
    """本轮 Query 是否需要且能否完成语义消解。"""

    NOT_REQUIRED = "NOT_REQUIRED"
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    INSUFFICIENT = "INSUFFICIENT"


class HistoryRecallMode(StrEnum):
    """上下文构建层允许执行的历史读取方式。"""

    NOT_REQUIRED = "NOT_REQUIRED"
    EXACT_REF = "EXACT_REF"
    TURN_RANGE = "TURN_RANGE"
    CONVERSATION_SEARCH = "CONVERSATION_SEARCH"
    CLARIFY = "CLARIFY"


class DialogueAct(StrEnum):
    """当前客户消息相对上一条客户可见回复所承担的会话作用。"""

    NEW_REQUEST = "NEW_REQUEST"
    CORRECTION = "CORRECTION"
    CLARIFICATION_REPLY = "CLARIFICATION_REPLY"
    ACK = "ACK"
    COMPLAINT = "COMPLAINT"


class FactSourceType(StrEnum):
    """已解析上下文字段的来源类型。"""

    USER_CONFIRMED = "USER_CONFIRMED"
    BUSINESS_FACT = "BUSINESS_FACT"
    SESSION_CONFIRMED = "SESSION_CONFIRMED"
    LONG_TERM_MEMORY = "LONG_TERM_MEMORY"
    MODEL_INFERENCE = "MODEL_INFERENCE"


class FactAuthority(StrEnum):
    """已解析上下文字段的权威等级。"""

    AUTHORITATIVE = "AUTHORITATIVE"
    CONFIRMED = "CONFIRMED"
    PREFERENCE = "PREFERENCE"
    CANDIDATE = "CANDIDATE"


class IntentDecisionStatus(StrEnum):
    """全局意图识别状态。"""

    MATCHED = "MATCHED"
    UNKNOWN = "UNKNOWN"
    AMBIGUOUS = "AMBIGUOUS"
    MULTI_INTENT = "MULTI_INTENT"


class IntentRouteTarget(StrEnum):
    """已发布全局意图唯一绑定的目标模块。"""

    SCENE = "SCENE"
    FAQ = "FAQ"


class IntentClass(StrEnum):
    """意图在内容治理中的职责，不等同于本轮请求形态。"""

    ACTION = "ACTION"
    STRATEGY = "STRATEGY"


class IntentMatchSource(StrEnum):
    """全局意图的命中来源。"""

    KEYWORD = "KEYWORD"
    SIMILAR_QUESTION = "SIMILAR_QUESTION"
    MODEL = "MODEL"
    ACTIVE_SOP = "ACTIVE_SOP"
    CONTEXT = "CONTEXT"


class RequestScope(StrEnum):
    """请求是否属于当前业务域。"""

    IN_SCOPE = "IN_SCOPE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    UNSURE = "UNSURE"


class RequestKind(StrEnum):
    """请求的业务形态。"""

    KNOWLEDGE = "KNOWLEDGE"
    ACTION = "ACTION"
    CONTROL = "CONTROL"
    SOCIAL = "SOCIAL"
    EXTERNAL_CAPABILITY = "EXTERNAL_CAPABILITY"
    HIGH_RISK = "HIGH_RISK"
    UNSURE = "UNSURE"


class ScopeMatchSource(StrEnum):
    """请求范围判断的来源。"""

    RULE = "RULE"
    INTENT_METADATA = "INTENT_METADATA"
    MODEL = "MODEL"


class ResponsePath(StrEnum):
    """一次推荐最终选择的交付路径。"""

    SOP = "SOP"
    FAQ = "FAQ"
    RAG = "RAG"
    CLARIFICATION = "CLARIFICATION"
    SAFE_FALLBACK = "SAFE_FALLBACK"
    HUMAN = "HUMAN"


class ClarificationSourceLayer(StrEnum):
    """产生澄清要求的处理层。"""

    QUERY = "QUERY"
    INTENT = "INTENT"
    FAQ = "FAQ"
    SOP = "SOP"
    RAG = "RAG"
    BUSINESS_OBJECT = "BUSINESS_OBJECT"


class ConfidenceBand(StrEnum):
    """经过离线校准后的离散置信档位。"""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class FaqMatchStatus(StrEnum):
    """FAQ 条目解析结果。"""

    MATCHED = "MATCHED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_MATCHED = "NOT_MATCHED"


class FaqMissReason(StrEnum):
    """FAQ 没有安全直答时的原因。"""

    NO_CANDIDATE = "NO_CANDIDATE"
    AMBIGUOUS = "AMBIGUOUS"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    ANSWER_EXPIRED = "ANSWER_EXPIRED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    CONTENT_INCONSISTENT = "CONTENT_INCONSISTENT"


class AnswerEligibility(StrEnum):
    """FAQ 固定答案对当前请求的适用性。"""

    ELIGIBLE = "ELIGIBLE"
    REQUIRES_BUSINESS_FACT = "REQUIRES_BUSINESS_FACT"
    INELIGIBLE = "INELIGIBLE"
    EXPIRED = "EXPIRED"


class SopExecutionStatus(StrEnum):
    """SOP 执行实例向外暴露的状态。"""

    ACTIVE = "ACTIVE"
    WAITING_CLARIFICATION = "WAITING_CLARIFICATION"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUSPENDED = "SUSPENDED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class ApprovalStatus(StrEnum):
    """有副作用动作的人工确认状态。"""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class RetrievalAssessmentStatus(StrEnum):
    """检索证据是否足以支持回答。"""

    SUFFICIENT = "SUFFICIENT"
    INSUFFICIENT = "INSUFFICIENT"
    CONFLICTING = "CONFLICTING"
    STALE = "STALE"


class RetrievalRecommendedAction(StrEnum):
    """证据评估后的建议动作。"""

    ANSWER = "ANSWER"
    CLARIFY = "CLARIFY"
    SAFE_FALLBACK = "SAFE_FALLBACK"
    HUMAN = "HUMAN"


class DecisionProfile(StrEnum):
    """影响或旁路评估一次决策的 Provider Profile。"""

    BASELINE = "BASELINE"
    JEV = "JEV"


class ConversationModelProfile(StrEnum):
    """客户可选的对话生成配置；具体供应商和模型由服务端目录解释。"""

    DEFAULT = "DEFAULT"


class DecisionMode(StrEnum):
    """客户请求使用的决策方式；对比模式不改变生产路由结果。"""

    LLM = "LLM"
    LLM_WITH_JEV_COMPARE = "LLM_WITH_JEV_COMPARE"


class DecisionTask(StrEnum):
    """Decision Provider 处理的原子任务类型。"""

    QUERY = "QUERY"
    ROUTING = "ROUTING"
    FAQ = "FAQ"
    SOP_INPUT = "SOP_INPUT"
    RETRIEVAL = "RETRIEVAL"
    QUALITY = "QUALITY"
    MODEL_ROUTING = "MODEL_ROUTING"


class ModelCallStatus(StrEnum):
    """一次模型调用的结束状态。"""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"


class TurnStatus(StrEnum):
    """一次业务 Turn 的处理状态。"""

    RUNNING = "RUNNING"
    INTERRUPTED = "INTERRUPTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RunStatus(StrEnum):
    """一次 Graph Run 的执行状态。"""

    RUNNING = "RUNNING"
    INTERRUPTED = "INTERRUPTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class WrapUpStatus(StrEnum):
    """会话结束小结的确认状态。"""

    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"


class AnswerSourceType(StrEnum):
    """推荐话术的一项可追溯来源。"""

    FAQ = "FAQ"
    RAG = "RAG"
    BUSINESS_FACT = "BUSINESS_FACT"
    SOP = "SOP"
    SAFE_FALLBACK = "SAFE_FALLBACK"


class QualitySeverity(StrEnum):
    """单项质量发现的风险等级。"""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class QualityAction(StrEnum):
    """同步质量策略产生的放行动作。"""

    PASS = "PASS"
    BLOCK = "BLOCK"
    REWRITE = "REWRITE"
    CLARIFY = "CLARIFY"
    HANDOFF = "HANDOFF"


class TraceSpanStatus(StrEnum):
    """一个可观测阶段是否正常完成。"""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AsyncQualityJobStatus(StrEnum):
    """异步质检任务的领取和完成状态。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ShadowComparisonStatus(StrEnum):
    """active 与 shadow 判断的对照结果。"""

    AGREED = "AGREED"
    DISAGREED = "DISAGREED"
    SHADOW_FAILED = "SHADOW_FAILED"


class SampleCandidateStatus(StrEnum):
    """运行样本候选必须经过人工复核才能进入后续资产。"""

    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class SampleCandidateType(StrEnum):
    """运行信号可形成的治理候选类型。"""

    INTENT_EXPRESSION = "INTENT_EXPRESSION"
    FAQ_GAP = "FAQ_GAP"
    RESPONSE_QUALITY = "RESPONSE_QUALITY"
    PROCESS_GAP = "PROCESS_GAP"
    SAFETY_REVIEW = "SAFETY_REVIEW"


class SampleCandidateConflictStatus(StrEnum):
    """同一问题模式的多次观测是否出现互相冲突的结果。"""

    CLEAR = "CLEAR"
    POTENTIAL_CONFLICT = "POTENTIAL_CONFLICT"


class SampleCandidateReviewDecision(StrEnum):
    """人工对治理候选作出的审核决定。"""

    APPROVE = "APPROVE"
    REJECT = "REJECT"


class SampleCandidateSourceType(StrEnum):
    """候选证据来自哪个已落库的业务事实。"""

    QUALITY_JOB = "QUALITY_JOB"
    RECOMMENDATION_FEEDBACK = "RECOMMENDATION_FEEDBACK"
    CONVERSATION_WRAP_UP = "CONVERSATION_WRAP_UP"


class InteractionAction(StrEnum):
    """坐席对推荐卡产生的客观交互。"""

    VIEWED = "VIEWED"
    COPIED = "COPIED"
    INSERTED = "INSERTED"
    DISMISSED = "DISMISSED"
    ESCALATED = "ESCALATED"


class AdoptionStatus(StrEnum):
    """坐席明确给出的推荐采用状态。"""

    ADOPTED = "ADOPTED"
    ADOPTED_AFTER_EDIT = "ADOPTED_AFTER_EDIT"
    NOT_ADOPTED = "NOT_ADOPTED"
    NOT_RATED = "NOT_RATED"


class Helpfulness(StrEnum):
    """坐席明确给出的推荐帮助程度。"""

    HELPFUL = "HELPFUL"
    PARTIALLY_HELPFUL = "PARTIALLY_HELPFUL"
    NOT_HELPFUL = "NOT_HELPFUL"
    NOT_RATED = "NOT_RATED"


class KnowledgeProjectionType(StrEnum):
    """源知识审核后允许发布到的目标。"""

    FAQ = "FAQ"
    SOP = "SOP"
    RAG = "RAG"


class ContentAssetType(StrEnum):
    """PostgreSQL content schema 中可版本化发布的权威资产类型。"""

    INTENT_DEFINITION = "INTENT_DEFINITION"
    INTENT_RULE = "INTENT_RULE"
    INTENT_SIMILARITY = "INTENT_SIMILARITY"
    FAQ_GROUP = "FAQ_GROUP"
    FAQ_ITEM = "FAQ_ITEM"
    POLICY_ANSWER = "POLICY_ANSWER"
    POLICY_PARAMETER = "POLICY_PARAMETER"
    SOP_DEFINITION = "SOP_DEFINITION"
    RAG_DOCUMENT = "RAG_DOCUMENT"


class ContentLifecycleStatus(StrEnum):
    """权威内容版本从编辑到下线的生命周期。"""

    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"


class PublicationStatus(StrEnum):
    """一次不可变内容发布清单的执行状态。"""

    READY = "READY"
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"
    FAILED = "FAILED"


class ProjectionTarget(StrEnum):
    """由权威内容构建、可以丢弃重建的在线投影。"""

    REDIS_INTENT_CATALOG = "REDIS_INTENT_CATALOG"
    REDIS_INTENT_RULES = "REDIS_INTENT_RULES"
    REDIS_FAQ_EXACT = "REDIS_FAQ_EXACT"
    REDIS_POLICY_ANSWERS = "REDIS_POLICY_ANSWERS"
    MILVUS_INTENT_SIMILARITIES = "MILVUS_INTENT_SIMILARITIES"
    MILVUS_FAQ_VARIANTS = "MILVUS_FAQ_VARIANTS"
    MILVUS_RAG_DOCUMENTS = "MILVUS_RAG_DOCUMENTS"


class ContentConflictSeverity(StrEnum):
    """内容冲突是否阻断发布。"""

    WARNING = "WARNING"
    BLOCKING = "BLOCKING"


class ContentConflictStatus(StrEnum):
    """一次发布前冲突检查的最终状态。"""

    PASSED = "PASSED"
    BLOCKED = "BLOCKED"


class TicketStatus(StrEnum):
    """工单从草稿到关闭的状态。"""

    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    PROCESSING = "PROCESSING"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class TicketPriority(StrEnum):
    """工单处理优先级。"""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
