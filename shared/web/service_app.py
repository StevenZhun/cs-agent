"""本地服务共用的 FastAPI 应用工厂和健康检查。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from fastapi import FastAPI, Request, Response, status

from shared.contracts.health import (
    DependencyHealth,
    DependencyState,
    HealthState,
    ServiceHealth,
)
from shared.observability import emit_structured_event

Probe = Callable[["TcpDependency"], Awaitable[DependencyHealth]]
Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


class SuccessfulHealthAccessFilter(logging.Filter):
    """隐藏成功健康探测；故障状态和普通业务访问仍保留。"""

    _health_paths = frozenset({"/health/live", "/health/ready"})

    def filter(self, record: logging.LogRecord) -> bool:
        """Uvicorn access 参数格式变化时保守放行，避免误删业务日志。"""

        if not isinstance(record.args, tuple) or len(record.args) < 5:
            return True
        path = str(record.args[2]).partition("?")[0]
        try:
            status_code = int(record.args[4])
        except (TypeError, ValueError):
            return True
        return not (path in self._health_paths and 200 <= status_code < 400)


def install_health_access_filter() -> None:
    """给 Uvicorn access logger 安装一次健康探测过滤器。"""

    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, SuccessfulHealthAccessFilter) for item in access_logger.filters):
        access_logger.addFilter(SuccessfulHealthAccessFilter())


@dataclass(frozen=True)
class TcpDependency:
    """一个必须可连通的 TCP 地址；健康检查结果不暴露凭据。"""

    name: str
    host: str
    port: int
    timeout_seconds: float = 0.5


async def probe_tcp(dependency: TcpDependency) -> DependencyHealth:
    """在短超时时间内建立并立即关闭 TCP 连接。"""

    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(dependency.host, dependency.port),
            timeout=dependency.timeout_seconds,
        )
    except (TimeoutError, OSError):
        return DependencyHealth(name=dependency.name, state=DependencyState.DOWN)
    writer.close()
    await writer.wait_closed()
    return DependencyHealth(name=dependency.name, state=DependencyState.UP)


def create_service_app(
    *,
    service: str,
    title: str,
    version: str,
    environment: str,
    dependencies: Sequence[TcpDependency] = (),
    probe: Probe = probe_tcp,
    lifespan: Lifespan | None = None,
) -> FastAPI:
    """创建统一提供存活检查和就绪检查语义的服务应用。"""

    install_health_access_filter()
    app = FastAPI(title=title, version=version, lifespan=lifespan)

    @app.middleware("http")
    async def structured_request_log(request: Request, call_next):
        """记录服务、Trace、路由模板、状态和耗时，永不读取正文或查询参数。"""

        started = time.perf_counter()
        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            route = request.scope.get("route")
            route_template = getattr(route, "path", request.url.path)
            status_code = response.status_code if response is not None else 500
            if route_template not in {"/health/live", "/health/ready"} or status_code >= 400:
                emit_structured_event(
                    "http_request_completed",
                    service=service,
                    request_id=getattr(request.state, "request_id", None)
                    or request.headers.get("X-Request-ID"),
                    trace_id=getattr(request.state, "trace_id", None)
                    or request.headers.get("X-Trace-ID"),
                    method=request.method,
                    route=route_template,
                    status_code=status_code,
                    latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
                )

    @app.get("/health/live", response_model=ServiceHealth, tags=["system"])
    async def liveness() -> ServiceHealth:
        """返回进程本身是否仍能响应请求。"""

        return ServiceHealth(
            service=service,
            version=version,
            environment=environment,
            state=HealthState.UP,
        )

    @app.get("/health/ready", response_model=ServiceHealth, tags=["system"])
    async def readiness(response: Response) -> ServiceHealth:
        """并行检查依赖服务；任一依赖不可用就返回 503。"""

        checks = tuple(await asyncio.gather(*(probe(item) for item in dependencies)))
        ready = all(check.state is DependencyState.UP for check in checks)
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ServiceHealth(
            service=service,
            version=version,
            environment=environment,
            state=HealthState.READY if ready else HealthState.NOT_READY,
            dependencies=checks,
        )

    return app
