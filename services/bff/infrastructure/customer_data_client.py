"""BFF 调用内部客户资料服务的 HTTP 适配器。"""

from __future__ import annotations

import httpx

from services.bff.errors import AppError
from shared.contracts.business import CustomerProfile, CustomerServiceContext
from shared.contracts.common import ApiResponse


class CustomerDataClient:
    """完成对象授权后，只读取当前坐席可见的脱敏客户资料。"""

    def __init__(self, base_url: str, *, timeout_seconds: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def list_customers(
        self, customer_ids: tuple[str, ...], request_id: str
    ) -> tuple[CustomerProfile, ...]:
        if not customer_ids:
            return ()
        payload = await self._get(
            "/api/v2/customers",
            request_id=request_id,
            params=[("customer_ids", customer_id) for customer_id in customer_ids],
        )
        return ApiResponse[tuple[CustomerProfile, ...]].model_validate(payload).data

    async def get_customer_context(
        self, customer_id: str, request_id: str
    ) -> CustomerServiceContext:
        payload = await self._get(f"/api/v2/customers/{customer_id}/context", request_id=request_id)
        return ApiResponse[CustomerServiceContext].model_validate(payload).data

    async def _get(
        self,
        path: str,
        *,
        request_id: str,
        params: list[tuple[str, str]] | None = None,
    ) -> dict[str, object]:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout_seconds
            ) as client:
                response = await client.get(
                    path, params=params, headers={"X-Request-ID": request_id}
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                raise AppError(
                    code="BUSINESS_OBJECT_NOT_FOUND", message="业务对象不存在", status_code=404
                ) from error
            raise AppError(
                code="BUSINESS_SERVICE_UNAVAILABLE",
                message="业务服务暂时不可用",
                status_code=503,
                retryable=True,
            ) from error
        except (httpx.RequestError, ValueError) as error:
            raise AppError(
                code="BUSINESS_SERVICE_UNAVAILABLE",
                message="业务服务暂时不可用",
                status_code=503,
                retryable=True,
            ) from error
