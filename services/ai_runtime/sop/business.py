"""SOP 外部业务客户端的通用扩展边界。"""

from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

import httpx

from shared.contracts.business import CustomerServiceContext
from shared.contracts.common import ApiResponse
from shared.contracts.sop import (
    BusinessOperationRequest,
    BusinessOperationResult,
    BusinessToolResult,
)


class SopBusinessClient:
    """只接受代码白名单中的固定端点，不执行模型提供的 URL。"""

    def __init__(self, base_url: str, token: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    async def execute(
        self,
        name: str,
        arguments: dict[str, str],
        state: dict,
    ) -> BusinessToolResult:
        """读取通用客户服务上下文；其他工具必须由部署方显式实现。"""

        started = perf_counter()
        customer_id = str(state.get("business_customer_id") or "UNAVAILABLE")
        status, data, error = "UNAVAILABLE", None, "TOOL_NOT_PUBLISHED"
        if name == "GET_CUSTOMER_CONTEXT" and not arguments and customer_id != "UNAVAILABLE":
            try:
                async with httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=self.timeout_seconds,
                ) as client:
                    response = await client.get(
                        "/internal/v1/customers/" + quote(customer_id, safe="") + "/context",
                        headers={
                            "X-Internal-Token": self.token,
                            "X-Request-ID": str(state["request_id"]),
                            "X-Trace-ID": str(state["trace_id"]),
                        },
                    )
                    response.raise_for_status()
                context = ApiResponse[CustomerServiceContext].model_validate(response.json()).data
                if context.customer.customer_id != customer_id:
                    raise ValueError("业务响应客户范围不一致")
                status, data, error = "SUCCEEDED", context.model_dump(mode="json"), None
            except httpx.TimeoutException:
                status, error = "TIMEOUT", "BUSINESS_TOOL_TIMEOUT"
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                error = "BUSINESS_TOOL_INVALID_OR_UNAVAILABLE"
        progress = state.get("active_sop_execution") or {}
        identity = f"{state['run_id']}:{name}:{progress.get('sop_execution_id')}"
        return BusinessToolResult(
            tool_call_id="tool_" + uuid5(NAMESPACE_URL, identity).hex,
            tool_name=name,
            conversation_id=state["conversation_id"],
            turn_id=state["turn_id"],
            run_id=state["run_id"],
            trace_id=state["trace_id"],
            sop_execution_id=str(progress.get("sop_execution_id") or "UNAVAILABLE"),
            customer_id=customer_id,
            arguments=arguments,
            status=status,
            data=data,
            error_code=error,
            source_ref="customer-data:" + name,
            observed_at=datetime.now(UTC),
            latency_ms=max(0, round((perf_counter() - started) * 1000)),
        )

    async def perform_operation(
        self,
        request: BusinessOperationRequest,
        state: dict,
    ) -> BusinessOperationResult:
        """公开默认实现不发布有副作用操作。"""

        return BusinessOperationResult(
            operation_id=request.operation_id,
            customer_id=str(state.get("business_customer_id") or "UNAVAILABLE"),
            subject_id=request.subject_id,
            action=request.action,
            status="REJECTED",
            error_code="OPERATION_NOT_PUBLISHED",
        )
