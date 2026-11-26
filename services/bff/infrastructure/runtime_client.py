"""BFF 专用的 AI Runtime HTTP 调用客户端。"""

from __future__ import annotations

from urllib.parse import quote

import httpx
from pydantic import ValidationError

from shared.contracts.runtime import (
    ExecuteGraphTurnRequest,
    ExecuteGraphTurnResponse,
    GraphTerminalSnapshot,
    InspectGraphTurnResponse,
)


class RuntimeExecutionUncertain(Exception):
    """调用方没有拿到可用结果，但远程图可能已经继续执行。"""


class RuntimeExecutionRejected(Exception):
    """Runtime 明确拒绝了请求，并保留 HTTP 状态码供上层决定是否重试。"""

    def __init__(self, message: str, *, status_code: int) -> None:
        """记录拒绝原因；401 是配置问题，409 可能只是旧 checkpoint。"""

        super().__init__(message)
        self.status_code = status_code


class AiRuntimeClient:
    """只携带已落库的 Turn/Run 编号，并使用服务间凭据调用 Runtime。"""

    def __init__(
        self,
        base_url: str,
        internal_token: str,
        *,
        execute_timeout_seconds: float,
        inspect_timeout_seconds: float,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._internal_token = internal_token
        self._execute_timeout_seconds = execute_timeout_seconds
        self._inspect_timeout_seconds = inspect_timeout_seconds

    async def execute(self, request: ExecuteGraphTurnRequest) -> ExecuteGraphTurnResponse:
        """请求 Runtime 执行图，并核对返回结果是否仍属于同一个 Run。"""

        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._execute_timeout_seconds
            ) as client:
                response = await client.post(
                    "/internal/v1/turns/execute",
                    json=request.model_dump(mode="json"),
                    headers={
                        "X-Internal-Token": self._internal_token,
                        "X-Request-ID": request.request_id,
                        "X-Trace-ID": request.trace_id,
                    },
                )
                if response.status_code in (401, 409):
                    raise RuntimeExecutionRejected(
                        "AI Runtime rejected the persisted Turn",
                        status_code=response.status_code,
                    )
                response.raise_for_status()
                result = ExecuteGraphTurnResponse.model_validate(response.json())
                if (
                    result.conversation_id != request.conversation_id
                    or result.thread_id != request.thread_id
                    or result.turn_id != request.turn_id
                    or result.run_id != request.run_id
                ):
                    raise RuntimeExecutionUncertain("AI Runtime returned mismatched identities")
                return result
        except RuntimeExecutionRejected:
            raise
        except (httpx.HTTPError, ValueError, ValidationError) as error:
            raise RuntimeExecutionUncertain(
                "AI Runtime result is unknown; inspect persisted graph state before retry"
            ) from error

    async def inspect_sop(
        self, conversation_id: str, thread_id: str
    ) -> GraphTerminalSnapshot | None:
        """仅获取当前 SOP 快照，不用已完成 Turn 再次执行图。"""
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._inspect_timeout_seconds
            ) as client:
                response = await client.get(
                    f"/internal/v1/conversations/{quote(conversation_id, safe='')}/sop",
                    params={"thread_id": thread_id},
                    headers={"X-Internal-Token": self._internal_token},
                )
                response.raise_for_status()
                payload = response.json()
                return GraphTerminalSnapshot.model_validate(payload) if payload else None
        except (httpx.HTTPError, ValueError) as error:
            raise RuntimeExecutionUncertain("流程进度暂时无法读取") from error

    async def inspect(self, request: ExecuteGraphTurnRequest) -> InspectGraphTurnResponse:
        """询问 Runtime 的 checkpoint 状态；这里绝不重新执行图。"""

        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._inspect_timeout_seconds
            ) as client:
                response = await client.post(
                    "/internal/v1/turns/inspect",
                    json=request.model_dump(mode="json"),
                    headers={
                        "X-Internal-Token": self._internal_token,
                        "X-Request-ID": request.request_id,
                        "X-Trace-ID": request.trace_id,
                    },
                )
                if response.status_code in (401, 409):
                    raise RuntimeExecutionRejected(
                        "AI Runtime rejected the persisted Turn",
                        status_code=response.status_code,
                    )
                response.raise_for_status()
                result = InspectGraphTurnResponse.model_validate(response.json())
                if (
                    result.conversation_id != request.conversation_id
                    or result.thread_id != request.thread_id
                    or result.turn_id != request.turn_id
                    or result.run_id != request.run_id
                ):
                    raise RuntimeExecutionUncertain("AI Runtime returned mismatched identities")
                return result
        except RuntimeExecutionRejected:
            raise
        except (httpx.HTTPError, ValueError, ValidationError) as error:
            raise RuntimeExecutionUncertain(
                "AI Runtime inspection is unavailable; keep the Run unresolved"
            ) from error
