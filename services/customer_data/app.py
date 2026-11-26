"""通用客户资料 HTTP 服务入口。"""

from datetime import UTC, datetime
from hmac import compare_digest
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, Header, HTTPException, Query, Response, status

from services.customer_data.infrastructure.database import (
    create_business_engine,
    create_session_factory,
)
from services.customer_data.infrastructure.repositories import SqlAlchemyCustomerDataRepository
from shared.contracts.business import CustomerProfile, CustomerServiceContext
from shared.contracts.common import ApiResponse, ResponseMeta
from shared.web.service_app import create_service_app
from shared.web.settings import ServiceSettings

settings = ServiceSettings()

app = create_service_app(
    service="customer-data",
    title="Agent Assist Customer Data",
    version=settings.service_version,
    environment=settings.app_env,
    dependencies=(settings.postgres_dependency(),),
)

app.state.database_engine = create_business_engine(settings.database_url)
app.state.customer_repository = SqlAlchemyCustomerDataRepository(
    create_session_factory(app.state.database_engine)
)


def response_meta(request_id: str | None) -> tuple[str, ResponseMeta]:
    """BFF 未转发请求编号时，在服务内部补生成一个编号。"""

    resolved_id = request_id or f"req_{uuid4().hex}"
    return resolved_id, ResponseMeta(request_id=resolved_id, timestamp=datetime.now(UTC))


def require_service_token(x_internal_token: Annotated[str | None, Header()] = None) -> None:
    """只读工具接口仅供内部服务调用，浏览器不能绕过 BFF 访问客户事实。"""
    if x_internal_token is None or not compare_digest(
        x_internal_token, settings.ai_runtime_internal_token.get_secret_value()
    ):
        raise HTTPException(status_code=401, detail="内部服务凭据无效")


@app.get(
    "/api/v2/customers",
    response_model=ApiResponse[tuple[CustomerProfile, ...]],
    tags=["customers"],
)
async def list_customers(
    response: Response,
    customer_ids: tuple[str, ...] = Query(default=()),
    x_request_id: Annotated[str | None, Header()] = None,
) -> ApiResponse[tuple[CustomerProfile, ...]]:
    """只返回 BFF 明确请求且已经通过授权的客户资料。"""

    repository: SqlAlchemyCustomerDataRepository = app.state.customer_repository
    customers = await repository.list_customers(customer_ids)
    resolved_id, meta = response_meta(x_request_id)
    response.headers["X-Request-ID"] = resolved_id
    return ApiResponse(data=customers, meta=meta)


@app.get(
    "/api/v2/customers/{customer_id}",
    response_model=ApiResponse[CustomerProfile],
    tags=["customers"],
)
async def get_customer(
    customer_id: str,
    response: Response,
    x_request_id: Annotated[str | None, Header()] = None,
) -> ApiResponse[CustomerProfile]:
    """返回已经脱敏的客户业务资料。"""

    repository: SqlAlchemyCustomerDataRepository = app.state.customer_repository
    customer = await repository.get_customer(customer_id)
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="客户不存在")
    resolved_id, meta = response_meta(x_request_id)
    response.headers["X-Request-ID"] = resolved_id
    return ApiResponse(data=customer, meta=meta)


@app.get(
    "/api/v2/customers/{customer_id}/context",
    response_model=ApiResponse[CustomerServiceContext],
    tags=["customers"],
)
@app.get(
    "/internal/v1/customers/{customer_id}/context",
    response_model=ApiResponse[CustomerServiceContext],
    dependencies=[Depends(require_service_token)],
)
async def get_customer_context(
    customer_id: str,
    response: Response,
    x_request_id: Annotated[str | None, Header()] = None,
) -> ApiResponse[CustomerServiceContext]:
    """为已授权的 BFF 聚合客户资料和通用服务记录。"""

    repository: SqlAlchemyCustomerDataRepository = app.state.customer_repository
    context = await repository.get_customer_context(customer_id)
    if context is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="客户不存在")
    resolved_id, meta = response_meta(x_request_id)
    response.headers["X-Request-ID"] = resolved_id
    return ApiResponse(data=context, meta=meta)
