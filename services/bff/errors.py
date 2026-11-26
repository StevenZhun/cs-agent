"""BFF 错误类型和 FastAPI 异常处理器。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from shared.contracts.common import ApiError


class AppError(Exception):
    """可以安全返回给客户端的应用错误。"""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int,
        retryable: bool = False,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.details = details or {}


def _request_id(request: Request) -> str:
    """读取请求上下文中的编号；上下文缺失时返回可追踪的占位值。"""

    return getattr(request.state, "request_id", "req_unavailable")


def _response(request: Request, error: AppError) -> JSONResponse:
    """把应用错误转换成统一的 JSON 错误信封。"""

    payload = ApiError(
        code=error.code,
        message=error.message,
        request_id=_request_id(request),
        retryable=error.retryable,
        details=error.details,
    )
    headers = {"WWW-Authenticate": "Bearer"} if error.status_code == 401 else None
    return JSONResponse(
        status_code=error.status_code,
        content=payload.model_dump(mode="json"),
        headers=headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    """让框架、参数校验和应用异常统一使用同一种错误信封。"""

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, error: AppError) -> JSONResponse:
        """处理业务代码主动抛出的安全应用错误。"""

        return _response(request, error)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        """把 FastAPI 参数校验错误转换成可读字段列表。"""

        fields: list[dict[str, Any]] = []
        for item in error.errors():
            fields.append(
                {
                    "location": [str(part) for part in item["loc"]],
                    "type": item["type"],
                    "message": item["msg"],
                }
            )
        return _response(
            request,
            AppError(
                code="REQUEST_VALIDATION_FAILED",
                message="请求参数校验失败",
                status_code=422,
                details={"fields": fields},
            ),
        )

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, error: HTTPException) -> JSONResponse:
        """把 HTTPException 转换成项目统一的错误格式。"""

        message = error.detail if isinstance(error.detail, str) else "请求处理失败"
        return _response(
            request,
            AppError(
                code=f"HTTP_{error.status_code}",
                message=message,
                status_code=error.status_code,
            ),
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, _error: Exception) -> JSONResponse:
        """隐藏未预期异常的内部细节，只返回可重试的通用错误。"""

        return _response(
            request,
            AppError(
                code="INTERNAL_ERROR",
                message="服务内部错误",
                status_code=500,
                retryable=True,
            ),
        )
