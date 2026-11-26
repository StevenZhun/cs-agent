"""BFF HTTP 入口：应用写入、授权读取和可补发事件。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from services.bff.conversations import ConversationService
from services.bff.diagnostics import DiagnosticsService
from services.bff.engagement import (
    ConfirmWrapUpRequest,
    EditWrapUpRequest,
    EngagementService,
    RecordInteractionRequest,
    SubmitFeedbackRequest,
)
from services.bff.errors import AppError, install_error_handlers
from services.bff.infrastructure.customer_data_client import CustomerDataClient
from services.bff.infrastructure.database import create_bff_engine, create_session_factory
from services.bff.infrastructure.events import (
    agent_event_page,
    event_page,
    latest_event_sequence,
)
from services.bff.infrastructure.history import SqlAlchemyChatHistoryRepository
from services.bff.infrastructure.idempotency import PostgresIdempotencyExecutor
from services.bff.infrastructure.models import ConversationHiddenModel, ConversationModel
from services.bff.infrastructure.repositories import (
    SqlAlchemyConversationRepository,
    SqlAlchemyIdentityRepository,
    SqlAlchemyObjectAuthorizer,
)
from services.bff.infrastructure.runtime_client import AiRuntimeClient
from services.bff.messages import CustomerMessageService
from services.bff.rag_review import RagReviewService
from services.bff.request_context import install_request_context
from services.bff.security import (
    AuthPrincipal,
    BffSecuritySettings,
    JwtTokenService,
    LocalAuthenticationService,
    LoginRequest,
    LoginResponse,
    Role,
    ScryptPasswordService,
    authorize_customer,
    authorize_roles,
    get_current_principal,
)
from services.bff.sop_assistance import SopAssistanceService

# Register the referenced customer table in shared SQLAlchemy metadata. BFF reads
# customer-owned records through the service boundary, not through this import.
from services.customer_data.infrastructure import models as customer_data_models  # noqa: F401
from services.quality.governance import CandidateGovernanceService
from shared.contracts.business import CustomerProfile, CustomerServiceContext
from shared.contracts.common import ApiResponse, ResponseMeta
from shared.contracts.conversation import (
    ConversationMessage,
    ConversationSummary,
    CreateConversationRequest,
    CreateConversationResponse,
    CustomerConversationWorkload,
    CustomerTurnResult,
    ModelSelectionCatalog,
    ModelSelectionOption,
    RealtimeEventEnvelope,
    RenameConversationRequest,
    SendMessageRequest,
    TurnTokenUsage,
    UpdateConversationPinRequest,
)
from shared.contracts.enums import (
    ConversationModelProfile,
    DecisionMode,
    SampleCandidateStatus,
    TurnStatus,
)
from shared.contracts.feedback import RecommendationFeedback, RecommendationInteractionEvent
from shared.contracts.observability import TraceDetail, TraceOperationsSummary
from shared.contracts.quality import (
    QualityCandidateDetail,
    QualityCandidateReleaseLink,
    QualityOperationsSummary,
    QualitySampleCandidate,
    RegisterCandidateReleaseRequest,
    ReviewQualityCandidateRequest,
)
from shared.contracts.rag import RagReviewRequest, RagReviewResult
from shared.contracts.recommendation import RecommendationResult
from shared.contracts.runtime import ConversationWrapUp
from shared.contracts.sop import SopAssistanceReply, SopAssistanceView, SopReviewRequest
from shared.web.service_app import create_service_app
from shared.web.settings import ServiceSettings

settings = ServiceSettings()


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """进程退出时关闭 SOP 配置缓存和数据库连接池，不遗留后台连接。"""
    try:
        yield
    finally:
        await application.state.sop_redis.aclose()
        await application.state.database_engine.dispose()


app = create_service_app(
    service="bff",
    title="Agent Assist BFF",
    version=settings.service_version,
    environment=settings.app_env,
    lifespan=lifespan,
)
install_request_context(app)
install_error_handlers(app)

app.state.database_engine = create_bff_engine(settings.database_url)
session_factory = create_session_factory(app.state.database_engine)
app.state.token_service = JwtTokenService(BffSecuritySettings())
app.state.password_service = ScryptPasswordService()
app.state.object_authorizer = SqlAlchemyObjectAuthorizer(session_factory)
app.state.customer_data_client = CustomerDataClient(settings.customer_data_url)
app.state.authentication_service = LocalAuthenticationService(
    SqlAlchemyIdentityRepository(session_factory),
    app.state.password_service,
    app.state.token_service,
)
app.state.conversation_repository = SqlAlchemyConversationRepository(session_factory)
app.state.conversation_service = ConversationService(
    app.state.conversation_repository,
    app.state.object_authorizer,
    PostgresIdempotencyExecutor(session_factory),
)
app.state.chat_history_repository = SqlAlchemyChatHistoryRepository(
    session_factory,
    quality_async_enabled=settings.quality_async_enabled,
    quality_max_attempts=settings.quality_max_attempts,
)
app.state.ai_runtime_client = AiRuntimeClient(
    settings.ai_runtime_url,
    settings.ai_runtime_internal_token.get_secret_value(),
    execute_timeout_seconds=settings.ai_runtime_execute_timeout_seconds,
    inspect_timeout_seconds=settings.ai_runtime_inspect_timeout_seconds,
)
app.state.sop_redis = Redis.from_url(settings.redis_url)
app.state.sop_assistance_service = SopAssistanceService(
    session_factory, app.state.object_authorizer, app.state.ai_runtime_client, app.state.sop_redis
)
app.state.rag_review_service = RagReviewService(
    session_factory,
    app.state.object_authorizer,
    app.state.ai_runtime_client,
    settings.rag_review_ttl_minutes,
)
app.state.customer_message_service = CustomerMessageService(
    app.state.chat_history_repository,
    app.state.object_authorizer,
    PostgresIdempotencyExecutor(session_factory),
    app.state.ai_runtime_client,
    app.state.sop_redis,
    settings.rag_review_ttl_minutes,
)
app.state.engagement_service = EngagementService(
    session_factory,
    app.state.object_authorizer,
    PostgresIdempotencyExecutor(session_factory),
)
app.state.diagnostics_service = DiagnosticsService(
    session_factory,
    app.state.object_authorizer,
)
app.state.candidate_governance_service = CandidateGovernanceService(session_factory)


def response_meta(request: Request) -> ResponseMeta:
    """从请求上下文生成返回给前端的请求元信息。"""

    return ResponseMeta(
        request_id=request.state.request_id,
        timestamp=datetime.now(UTC),
    )


def get_customer_message_service() -> CustomerMessageService:
    """Expose the dynamically stored service with a type the IDE can follow."""

    return cast(CustomerMessageService, app.state.customer_message_service)


@app.get(
    "/api/v2/model-options",
    response_model=ApiResponse[ModelSelectionCatalog],
    tags=["models"],
)
async def get_model_options(
    request: Request,
    _principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> ApiResponse[ModelSelectionCatalog]:
    """返回受控模型目录；客户只能选择已公开的模式，不能提交供应商参数。"""

    jev_available = bool(
        settings.typesafe_api_key and settings.typesafe_api_key.get_secret_value().strip()
    )
    catalog = ModelSelectionCatalog(
        conversation_models=(
            ModelSelectionOption(
                value=ConversationModelProfile.DEFAULT.value,
                label=settings.llm_model,
                description="用于需要语言生成的改写、分类和草稿环节。",
            ),
        ),
        decision_models=(
            ModelSelectionOption(
                value=DecisionMode.LLM.value,
                label="LLM 意图决策",
                description="在全局意图候选判断中形成实际业务路由",
            ),
            ModelSelectionOption(
                value=DecisionMode.LLM_WITH_JEV_COMPARE.value,
                label="Jev 旁路对比",
                description="仅比较全局意图候选判断，实际业务路由仍由 LLM 决定",
                enabled=jev_available,
            ),
        ),
        default_conversation_model=ConversationModelProfile.DEFAULT,
        default_decision_mode=DecisionMode.LLM,
    )
    return ApiResponse(data=catalog, meta=response_meta(request))


@app.get(
    "/api/v2/diagnostics/traces/{trace_id}",
    response_model=ApiResponse[TraceDetail],
    tags=["diagnostics"],
)
async def get_trace_detail(
    trace_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> ApiResponse[TraceDetail]:
    """坐席只能查看自己有权访问会话的无正文 Trace。"""

    authorize_roles(principal, {Role.AGENT, Role.ADMIN})
    detail = await app.state.diagnostics_service.trace_detail(principal, trace_id)
    return ApiResponse(data=detail, meta=response_meta(request))


@app.get(
    "/api/v2/diagnostics/operations",
    response_model=ApiResponse[TraceOperationsSummary],
    tags=["diagnostics"],
)
async def get_trace_operations(
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
    window_hours: Annotated[int, Query(ge=1, le=24 * 31)] = 24,
) -> ApiResponse[TraceOperationsSummary]:
    """管理员读取跨客户聚合耗时；返回中不含会话 ID 和正文。"""

    authorize_roles(principal, {Role.ADMIN})
    summary = await app.state.diagnostics_service.trace_summary(window_hours)
    return ApiResponse(data=summary, meta=response_meta(request))


@app.get(
    "/api/v2/diagnostics/quality",
    response_model=ApiResponse[QualityOperationsSummary],
    tags=["diagnostics"],
)
async def get_quality_operations(
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
    window_hours: Annotated[int, Query(ge=1, le=24 * 31)] = 24,
) -> ApiResponse[QualityOperationsSummary]:
    """管理员读取异步质检积压、失败、分歧和候选数量。"""

    authorize_roles(principal, {Role.ADMIN})
    summary = await app.state.diagnostics_service.quality_summary(window_hours)
    return ApiResponse(data=summary, meta=response_meta(request))


@app.get(
    "/api/v2/diagnostics/quality/candidates",
    response_model=ApiResponse[tuple[QualitySampleCandidate, ...]],
    tags=["diagnostics"],
)
async def list_quality_candidates(
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
    candidate_status: Annotated[SampleCandidateStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ApiResponse[tuple[QualitySampleCandidate, ...]]:
    """管理员读取不含客户正文的治理候选列表。"""

    authorize_roles(principal, {Role.ADMIN})
    candidates = await app.state.candidate_governance_service.list_candidates(
        status=candidate_status,
        limit=limit,
    )
    return ApiResponse(data=candidates, meta=response_meta(request))


@app.get(
    "/api/v2/diagnostics/quality/candidates/{candidate_id}",
    response_model=ApiResponse[QualityCandidateDetail],
    tags=["diagnostics"],
)
async def get_quality_candidate(
    candidate_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> ApiResponse[QualityCandidateDetail]:
    """管理员读取候选、来源证据、审核和发布关联。"""

    authorize_roles(principal, {Role.ADMIN})
    detail = await app.state.candidate_governance_service.detail(candidate_id)
    return ApiResponse(data=detail, meta=response_meta(request))


@app.post(
    "/api/v2/diagnostics/quality/candidates/{candidate_id}/reviews",
    response_model=ApiResponse[QualityCandidateDetail],
    tags=["diagnostics"],
)
async def review_quality_candidate(
    candidate_id: str,
    payload: ReviewQualityCandidateRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> ApiResponse[QualityCandidateDetail]:
    """管理员按候选版本审核，出现新证据时拒绝覆盖。"""

    authorize_roles(principal, {Role.ADMIN})
    detail = await app.state.candidate_governance_service.review(
        candidate_id,
        payload,
        principal.subject_id,
    )
    return ApiResponse(data=detail, meta=response_meta(request))


@app.post(
    "/api/v2/diagnostics/quality/candidates/{candidate_id}/release-links",
    response_model=ApiResponse[QualityCandidateReleaseLink],
    tags=["diagnostics"],
)
async def register_quality_candidate_release(
    candidate_id: str,
    payload: RegisterCandidateReleaseRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> ApiResponse[QualityCandidateReleaseLink]:
    """登记人工发布结果；接口本身不会修改或发布线上内容。"""

    authorize_roles(principal, {Role.ADMIN})
    link = await app.state.candidate_governance_service.register_release(
        candidate_id,
        payload,
        principal.subject_id,
    )
    return ApiResponse(data=link, meta=response_meta(request))


@app.post("/api/v2/auth/login", response_model=LoginResponse, tags=["session"])
async def login(credentials: LoginRequest) -> LoginResponse:
    """Exchange a local application account for a short-lived bearer token."""

    return await app.state.authentication_service.login(credentials)


@app.get("/api/v2/session", response_model=AuthPrincipal, tags=["session"])
async def get_session(
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> AuthPrincipal:
    """Return the authenticated application principal without business customer data."""

    return principal


@app.get(
    "/api/v2/customers",
    response_model=ApiResponse[tuple[CustomerProfile, ...]],
    tags=["customers"],
)
async def list_accessible_customers(
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> ApiResponse[tuple[CustomerProfile, ...]]:
    """Read a customer's profile only for current subject bindings."""

    customer_ids = await app.state.object_authorizer.accessible_customer_ids(principal.subject_id)
    customers = await app.state.customer_data_client.list_customers(
        customer_ids, request.state.request_id
    )
    return ApiResponse(data=customers, meta=response_meta(request))


@app.get(
    "/api/v2/agent/customer-workloads",
    response_model=ApiResponse[tuple[CustomerConversationWorkload, ...]],
    tags=["agent"],
)
async def list_customer_workloads(
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> ApiResponse[tuple[CustomerConversationWorkload, ...]]:
    """一次读取全部授权客户的会话计数，不按客户循环请求，不受单客户最近 50 条限制。"""
    authorize_roles(principal, {Role.AGENT, Role.ADMIN})
    customer_ids = await app.state.object_authorizer.accessible_customer_ids(principal.subject_id)
    items = await app.state.conversation_repository.list_for_customer(
        principal.subject_id, customer_ids, limit=None
    )
    counts = {cid: [0, 0] for cid in customer_ids}
    for item in items:
        if item.status.value != "FINISHED":
            counts[item.customer_id][0] += 1
            if item.status.value == "WRAP_UP" or item.needs_manual_review:
                counts[item.customer_id][1] += 1
    return ApiResponse(
        data=tuple(
            CustomerConversationWorkload(
                customer_id=cid, open_count=count[0], waiting_count=count[1]
            )
            for cid, count in counts.items()
        ),
        meta=response_meta(request),
    )


@app.delete(
    "/api/v2/conversations/{conversation_id}",
    response_model=ApiResponse[bool],
    tags=["conversations"],
)
async def hide_customer_conversation(
    conversation_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> ApiResponse[bool]:
    """客户软删除自己的已结束咨询；重试不会重复记录，也不清除消息或业务审计。"""
    authorize_roles(principal, {Role.CUSTOMER})
    await app.state.conversation_service.get(principal, conversation_id)
    async with session_factory.begin() as session:
        conversation = await session.scalar(
            select(ConversationModel)
            .where(ConversationModel.conversation_id == conversation_id)
            .with_for_update()
        )
        if conversation is None or conversation.status != "FINISHED":
            raise AppError(
                code="CONVERSATION_NOT_FINISHED",
                message="请先结束咨询，再删除列表记录",
                status_code=409,
            )
        await session.execute(
            insert(ConversationHiddenModel)
            .values(subject_id=principal.subject_id, conversation_id=conversation_id)
            .on_conflict_do_nothing()
        )
    return ApiResponse(data=True, meta=response_meta(request))


@app.get(
    "/api/v2/customers/{customer_id}/context",
    response_model=ApiResponse[CustomerServiceContext],
    tags=["customers"],
)
async def get_customer_context(
    customer_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> ApiResponse[CustomerServiceContext]:
    """完成对象授权后，向客户资料服务读取当前上下文。"""

    await authorize_customer(principal, customer_id, app.state.object_authorizer)
    context = await app.state.customer_data_client.get_customer_context(
        customer_id, request.state.request_id
    )
    return ApiResponse(data=context, meta=response_meta(request))


@app.get(
    "/api/v2/customers/{customer_id}/conversations",
    response_model=ApiResponse[tuple[ConversationSummary, ...]],
    tags=["customers"],
)
async def list_customer_conversations(
    customer_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> ApiResponse[tuple[ConversationSummary, ...]]:
    """返回当前主体有权查看的客户最近咨询列表。"""

    data = await app.state.conversation_service.list_for_customer(principal, customer_id)
    return ApiResponse(data=data, meta=response_meta(request))


@app.post(
    "/api/v2/conversations",
    response_model=ApiResponse[CreateConversationResponse],
    status_code=status.HTTP_201_CREATED,
    tags=["conversations"],
)
async def create_conversation(
    payload: CreateConversationRequest,
    request: Request,
    response: Response,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        ),
    ],
) -> ApiResponse[CreateConversationResponse]:
    """创建一条已授权的业务会话，并同时保存对应的图线程映射。"""

    created, replayed = await app.state.conversation_service.create(
        principal,
        payload,
        idempotency_key,
    )
    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    return ApiResponse(data=created, meta=response_meta(request))


@app.get(
    "/api/v2/conversations/{conversation_id}",
    response_model=ApiResponse[CreateConversationResponse],
    tags=["conversations"],
)
async def get_conversation(
    conversation_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> ApiResponse[CreateConversationResponse]:
    """Return the minimal state snapshot only after object authorization."""

    conversation = await app.state.conversation_service.get(principal, conversation_id)
    return ApiResponse(data=conversation, meta=response_meta(request))


@app.patch(
    "/api/v2/conversations/{conversation_id}/title",
    response_model=ApiResponse[ConversationSummary],
    tags=["conversations"],
)
async def rename_conversation_title(
    conversation_id: str,
    payload: RenameConversationRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> ApiResponse[ConversationSummary]:
    """保存客户或坐席为自己有权访问的会话设置的新标题。"""

    renamed = await app.state.conversation_service.rename_title(
        principal, conversation_id, payload.title
    )
    return ApiResponse(data=renamed, meta=response_meta(request))


@app.patch(
    "/api/v2/conversations/{conversation_id}/pin",
    response_model=ApiResponse[ConversationSummary],
    tags=["conversations"],
)
async def update_conversation_pin(
    conversation_id: str,
    payload: UpdateConversationPinRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> ApiResponse[ConversationSummary]:
    """设置当前登录主体自己的会话置顶状态。"""

    summary = await app.state.conversation_service.set_pinned(
        principal,
        conversation_id,
        payload.pinned,
    )
    return ApiResponse(data=summary, meta=response_meta(request))


@app.post(
    "/api/v2/conversations/{conversation_id}/finish",
    response_model=ApiResponse[CreateConversationResponse],
    tags=["conversations"],
)
async def finish_customer_conversation(
    conversation_id: str,
    request: Request,
    response: Response,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        ),
    ],
) -> ApiResponse[CreateConversationResponse]:
    """客户主动结束当前咨询；有消息的会话随后由坐席完成小结。"""

    closed, replayed = await app.state.conversation_service.request_finish(
        principal,
        conversation_id,
        idempotency_key=idempotency_key,
    )
    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    return ApiResponse(data=closed, meta=response_meta(request))


@app.get(
    "/api/v2/conversations/{conversation_id}/messages",
    response_model=ApiResponse[tuple[ConversationMessage, ...]],
    tags=["conversations"],
)
async def get_conversation_messages(
    conversation_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> ApiResponse[tuple[ConversationMessage, ...]]:
    """Authorize the conversation before reading a bounded page of visible history."""

    await app.state.conversation_service.get(principal, conversation_id)
    messages = await app.state.chat_history_repository.page(
        conversation_id, limit=limit, before=before
    )
    return ApiResponse(data=messages, meta=response_meta(request))


@app.post(
    "/api/v2/conversations/{conversation_id}/messages",
    response_model=ApiResponse[CustomerTurnResult],
    status_code=status.HTTP_202_ACCEPTED,
    tags=["conversations"],
)
async def send_customer_message(
    conversation_id: str,
    payload: SendMessageRequest,
    request: Request,
    response: Response,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
    message_service: Annotated[CustomerMessageService, Depends(get_customer_message_service)],
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        ),
    ],
) -> ApiResponse[CustomerTurnResult]:
    """Accept one customer message and release only a durably saved safe reply."""

    authorize_roles(principal, {Role.CUSTOMER})
    if payload.decision_mode is DecisionMode.LLM_WITH_JEV_COMPARE and not (
        settings.typesafe_api_key and settings.typesafe_api_key.get_secret_value().strip()
    ):
        raise AppError(
            code="DECISION_MODE_UNAVAILABLE",
            message="Jev 对比模式当前不可用",
            status_code=409,
        )
    result, replayed = await message_service.send(
        principal,
        conversation_id,
        payload,
        idempotency_key=idempotency_key,
        request_id=request.state.request_id,
        trace_id=request.state.trace_id,
    )
    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    if result.status is TurnStatus.COMPLETED:
        response.status_code = status.HTTP_200_OK
    return ApiResponse(data=result, meta=response_meta(request))


@app.get(
    "/api/v2/conversations/{conversation_id}/turns/{turn_id}",
    response_model=ApiResponse[CustomerTurnResult],
    tags=["conversations"],
)
async def get_customer_turn(
    conversation_id: str,
    turn_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
    message_service: Annotated[CustomerMessageService, Depends(get_customer_message_service)],
) -> ApiResponse[CustomerTurnResult]:
    """Read the application-owned result, including an unresolved accepted turn."""

    result = await message_service.get_result(principal, conversation_id, turn_id)
    return ApiResponse(data=result, meta=response_meta(request))


@app.get(
    "/api/v2/conversations/{conversation_id}/token-usage",
    response_model=ApiResponse[TurnTokenUsage | None],
    tags=["conversations"],
)
async def get_conversation_token_usage(
    conversation_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
    message_service: Annotated[CustomerMessageService, Depends(get_customer_message_service)],
) -> ApiResponse[TurnTokenUsage | None]:
    """读取会话当前上下文周期截至最近一轮的累计 Token 用量。"""

    result = await message_service.get_token_usage(principal, conversation_id)
    return ApiResponse(data=result, meta=response_meta(request))


@app.post(
    "/api/v2/conversations/{conversation_id}/recommendations/{recommendation_id}/interactions",
    response_model=ApiResponse[RecommendationInteractionEvent],
    status_code=status.HTTP_201_CREATED,
    tags=["recommendations"],
)
async def record_recommendation_interaction(
    conversation_id: str,
    recommendation_id: str,
    payload: RecordInteractionRequest,
    request: Request,
    response: Response,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> ApiResponse[RecommendationInteractionEvent]:
    """坐席记录查看或复制等客观交互，不因此认定推荐已被采用。"""

    result, replayed = await app.state.engagement_service.record_interaction(
        principal,
        conversation_id,
        recommendation_id,
        payload,
        key=idempotency_key,
        request_id=request.state.request_id,
    )
    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    return ApiResponse(data=result, meta=response_meta(request))


@app.get(
    "/api/v2/conversations/{conversation_id}/recommendations",
    response_model=ApiResponse[tuple[RecommendationResult, ...]],
    tags=["recommendations"],
)
async def list_recommendations(
    conversation_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> ApiResponse[tuple[RecommendationResult, ...]]:
    """坐席读取当前会话中已放行的推荐和可追溯来源。"""

    data = await app.state.engagement_service.list_recommendations(principal, conversation_id)
    return ApiResponse(data=data, meta=response_meta(request))


@app.post(
    "/api/v2/conversations/{conversation_id}/rag/review",
    response_model=ApiResponse[RagReviewResult],
    tags=["recommendations"],
)
async def review_rag_draft(
    conversation_id: str,
    payload: RagReviewRequest,
    request: Request,
    response: Response,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> ApiResponse[RagReviewResult]:
    """坐席采用、编辑后发送或拒绝一份仍处于真实图停点的 RAG 草稿。"""

    result, replayed = await app.state.rag_review_service.review(
        principal,
        conversation_id,
        payload,
        key=idempotency_key,
        request_id=request.state.request_id,
        trace_id=request.state.trace_id,
    )
    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    return ApiResponse(data=result, meta=response_meta(request))


@app.get(
    "/api/v2/conversations/{conversation_id}/recommendations/{recommendation_id}/interactions",
    response_model=ApiResponse[tuple[RecommendationInteractionEvent, ...]],
    tags=["recommendations"],
)
async def list_recommendation_interactions(
    conversation_id: str,
    recommendation_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> ApiResponse[tuple[RecommendationInteractionEvent, ...]]:
    """坐席读取这条推荐的交互记录，不推断是否采用。"""

    data = await app.state.engagement_service.list_interactions(
        principal, conversation_id, recommendation_id
    )
    return ApiResponse(data=data, meta=response_meta(request))


@app.post(
    "/api/v2/conversations/{conversation_id}/recommendations/{recommendation_id}/feedback",
    response_model=ApiResponse[RecommendationFeedback],
    status_code=status.HTTP_201_CREATED,
    tags=["recommendations"],
)
async def submit_recommendation_feedback(
    conversation_id: str,
    recommendation_id: str,
    payload: SubmitFeedbackRequest,
    request: Request,
    response: Response,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> ApiResponse[RecommendationFeedback]:
    """坐席明确评价推荐的采用和帮助程度。"""

    result, replayed = await app.state.engagement_service.submit_feedback(
        principal,
        conversation_id,
        recommendation_id,
        payload,
        key=idempotency_key,
        request_id=request.state.request_id,
    )
    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    return ApiResponse(data=result, meta=response_meta(request))


@app.get(
    "/api/v2/conversations/{conversation_id}/recommendations/{recommendation_id}/feedback",
    response_model=ApiResponse[tuple[RecommendationFeedback, ...]],
    tags=["recommendations"],
)
async def list_recommendation_feedback(
    conversation_id: str,
    recommendation_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> ApiResponse[tuple[RecommendationFeedback, ...]]:
    """坐席读取明确提交的推荐反馈。"""

    data = await app.state.engagement_service.list_feedback(
        principal, conversation_id, recommendation_id
    )
    return ApiResponse(data=data, meta=response_meta(request))


@app.get(
    "/api/v2/conversations/{conversation_id}/wrap-up",
    response_model=ApiResponse[ConversationWrapUp],
    tags=["conversations"],
)
async def get_conversation_wrap_up(
    conversation_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> ApiResponse[ConversationWrapUp]:
    """只向有权处理该会话的坐席展示结束小结当前版。"""

    result = await app.state.engagement_service.get_wrap_up(principal, conversation_id)
    return ApiResponse(data=result, meta=response_meta(request))


@app.post(
    "/api/v2/conversations/{conversation_id}/wrap-up",
    response_model=ApiResponse[ConversationWrapUp],
    status_code=status.HTTP_201_CREATED,
    tags=["conversations"],
)
async def create_conversation_wrap_up(
    conversation_id: str,
    request: Request,
    response: Response,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> ApiResponse[ConversationWrapUp]:
    """从已落库消息生成待坐席确认的小结草稿。"""

    result, replayed = await app.state.engagement_service.create_wrap_up(
        principal,
        conversation_id,
        key=idempotency_key,
    )
    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    return ApiResponse(data=result, meta=response_meta(request))


@app.put(
    "/api/v2/conversations/{conversation_id}/wrap-up",
    response_model=ApiResponse[ConversationWrapUp],
    tags=["conversations"],
)
async def edit_conversation_wrap_up(
    conversation_id: str,
    payload: EditWrapUpRequest,
    request: Request,
    response: Response,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> ApiResponse[ConversationWrapUp]:
    """坐席编辑草稿，按版本拒绝覆盖较新的修改。"""

    result, replayed = await app.state.engagement_service.edit_wrap_up(
        principal,
        conversation_id,
        payload,
        key=idempotency_key,
    )
    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    return ApiResponse(data=result, meta=response_meta(request))


@app.post(
    "/api/v2/conversations/{conversation_id}/wrap-up/confirm",
    response_model=ApiResponse[ConversationWrapUp],
    tags=["conversations"],
)
async def confirm_conversation_wrap_up(
    conversation_id: str,
    payload: ConfirmWrapUpRequest,
    request: Request,
    response: Response,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> ApiResponse[ConversationWrapUp]:
    """坐席确认当前草稿，与会话结束状态同事务提交。"""

    result, replayed = await app.state.engagement_service.confirm_wrap_up(
        principal,
        conversation_id,
        payload,
        key=idempotency_key,
    )
    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    return ApiResponse(data=result, meta=response_meta(request))


@app.get(
    "/api/v2/conversations/{conversation_id}/events/history",
    response_model=ApiResponse[tuple[RealtimeEventEnvelope, ...]],
    tags=["conversations"],
)
async def get_conversation_events(
    conversation_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
    after_sequence: Annotated[int, Query(ge=0)] = 0,
) -> ApiResponse[tuple[RealtimeEventEnvelope, ...]]:
    """按序号读取至多一页持久事件，便于诊断和断线补发。"""

    await app.state.conversation_service.get(principal, conversation_id)
    events = await event_page(session_factory, conversation_id, after_sequence)
    return ApiResponse(data=events, meta=response_meta(request))


@app.get("/api/v2/conversations/{conversation_id}/events", tags=["conversations"])
async def stream_conversation_events(
    conversation_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """授权后按持久事件序号补发；消息正文仍须从历史接口读取。"""

    await app.state.conversation_service.get(principal, conversation_id)
    cursor = after_sequence
    if last_event_id:
        if not last_event_id.startswith("evt_") or not last_event_id[4:].isdigit():
            raise AppError(code="INVALID_EVENT_CURSOR", message="事件续接编号无效", status_code=400)
        cursor = max(cursor, int(last_event_id[4:]))

    async def stream():
        """每秒检查一次新事件；空闲时发心跳并重新核对对象授权。"""

        current = cursor
        idle_seconds = 0
        yield "retry: 2000\n\n"
        while not await request.is_disconnected():
            events = await event_page(session_factory, conversation_id, current)
            if events:
                for event in events:
                    current = event.sequence
                    yield (
                        f"id: {event.event_id}\nevent: conversation\n"
                        f"data: {event.model_dump_json()}\n\n"
                    )
                idle_seconds = 0
                continue
            await asyncio.sleep(1)
            idle_seconds += 1
            if idle_seconds >= 15:
                if not await app.state.object_authorizer.can_access_conversation(
                    principal.subject_id, conversation_id
                ):
                    return
                yield ": keep-alive\n\n"
                idle_seconds = 0

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get(
    "/api/v2/conversations/{conversation_id}/sop",
    response_model=ApiResponse[SopAssistanceView | None],
    tags=["sop"],
)
async def get_sop_assistance(
    conversation_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> ApiResponse[SopAssistanceView | None]:
    """坐席读取流程暂停原因和已收集资料，不能据此解除身份核验停点。"""
    data = await app.state.sop_assistance_service.get(principal, conversation_id)
    return ApiResponse(data=data, meta=response_meta(request))


@app.post(
    "/api/v2/conversations/{conversation_id}/sop/replies",
    response_model=ApiResponse[ConversationMessage],
    status_code=201,
    tags=["sop"],
)
async def send_sop_assistance_reply(
    conversation_id: str,
    payload: SopAssistanceReply,
    request: Request,
    response: Response,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        ),
    ],
) -> ApiResponse[ConversationMessage]:
    """保存人工处理意见并通知客户，不直接执行受保护的业务操作。"""
    data, replayed = await app.state.sop_assistance_service.reply(
        principal,
        conversation_id,
        payload,
        key=idempotency_key,
        request_id=request.state.request_id,
        trace_id=request.state.trace_id,
    )
    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    return ApiResponse(data=data, meta=response_meta(request))


@app.post(
    "/api/v2/conversations/{conversation_id}/sop/review",
    response_model=ApiResponse[CustomerTurnResult],
    tags=["sop"],
)
async def process_sop_review(
    conversation_id: str,
    payload: SopReviewRequest,
    request: Request,
    response: Response,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
):
    """坐席明确批准、退回更正、拒绝或查询原操作结果，客户端没有此权限。"""
    data, replayed = await app.state.sop_assistance_service.review(
        principal,
        conversation_id,
        payload,
        key=idempotency_key,
        request_id=request.state.request_id,
        trace_id=request.state.trace_id,
        messages=app.state.customer_message_service,
    )
    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    return ApiResponse(data=data, meta=response_meta(request))


@app.get("/api/v2/agent/events", tags=["agent"])
async def stream_agent_events(
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """给坐席补发其可服务客户的全部会话事件，但不替坐席切换中央窗口。"""

    authorize_roles(principal, {Role.AGENT, Role.ADMIN})
    cursor = after_sequence
    if last_event_id:
        if not last_event_id.startswith("evt_") or not last_event_id[4:].isdigit():
            raise AppError(code="INVALID_EVENT_CURSOR", message="事件续接编号无效", status_code=400)
        cursor = max(cursor, int(last_event_id[4:]))
    elif cursor == 0:
        # 首次打开坐席页只监听之后的新消息，不把历史事件误当成未读提醒。
        cursor = await latest_event_sequence(session_factory)

    async def stream():
        """按全局事件序号轮询；每次查询都会重新应用当前客户授权。"""

        current = cursor
        idle_seconds = 0
        yield "retry: 2000\n\n"
        while not await request.is_disconnected():
            events = await agent_event_page(session_factory, principal.subject_id, current)
            if events:
                for event in events:
                    current = event.sequence
                    yield (
                        f"id: {event.event_id}\nevent: agent\ndata: {event.model_dump_json()}\n\n"
                    )
                idle_seconds = 0
                continue
            await asyncio.sleep(1)
            idle_seconds += 1
            if idle_seconds >= 15:
                yield ": keep-alive\n\n"
                idle_seconds = 0

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
