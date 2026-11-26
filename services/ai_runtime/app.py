"""AI Runtime 内部服务入口：执行消息轮次与只读查看流程进度。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hmac import compare_digest
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Query, status

from services.ai_runtime.health import RuntimeDependencyProbe
from services.ai_runtime.persistence.checkpoint import open_graph_runtime
from services.ai_runtime.turns import RuntimeTurnError, RuntimeTurnProcessor
from shared.contracts.runtime import (
    ExecuteGraphTurnRequest,
    ExecuteGraphTurnResponse,
    GraphTerminalSnapshot,
    InspectGraphTurnResponse,
)
from shared.web.service_app import create_service_app
from shared.web.settings import ServiceSettings

settings = ServiceSettings()
configured_internal_token = settings.ai_runtime_internal_token.get_secret_value()
runtime_dependency_probe = RuntimeDependencyProbe(settings)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """在进程整个生命周期内复用同一个已初始化的 checkpoint 保存器。"""

    if (
        settings.app_env != "local"
        and configured_internal_token == "local-internal-token-change-before-use"
    ):
        raise RuntimeError("Set a non-default AI_RUNTIME_INTERNAL_TOKEN outside local mode")
    try:
        async with open_graph_runtime(settings) as runtime:
            app.state.graph_runtime = runtime
            app.state.turn_processor = RuntimeTurnProcessor(runtime, settings.database_url)
            yield
    finally:
        await runtime_dependency_probe.close()


app = create_service_app(
    service="ai-runtime",
    title="Agent Assist AI Runtime",
    version=settings.service_version,
    environment=settings.app_env,
    dependencies=(
        settings.postgres_dependency(),
        settings.redis_dependency(),
        settings.milvus_dependency(),
    ),
    probe=runtime_dependency_probe,
    lifespan=lifespan,
)


def require_internal_token(internal_token: str | None) -> None:
    """仅允许持有内部凭据的 BFF 调用，客户浏览器不能直接进入图服务。"""
    if internal_token is None or not compare_digest(internal_token, configured_internal_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


@app.get(
    "/internal/v1/conversations/{conversation_id}/sop", response_model=GraphTerminalSnapshot | None
)
async def inspect_current_sop(
    conversation_id: str,
    thread_id: Annotated[str, Query(min_length=1, max_length=64)],
    internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
) -> GraphTerminalSnapshot | None:
    """向已经完成对象授权的 BFF 返回流程进度，不提供审批或外部系统写入操作。"""
    require_internal_token(internal_token)
    try:
        return await app.state.turn_processor.inspect_sop(conversation_id, thread_id)
    except RuntimeTurnError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post(
    "/internal/v1/turns/execute",
    response_model=ExecuteGraphTurnResponse,
    tags=["internal"],
)
async def execute_graph_turn(
    payload: ExecuteGraphTurnRequest,
    internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
) -> ExecuteGraphTurnResponse:
    """核对已落库轮次后执行主图，返回 FAQ、澄清、SOP 或安全回复终态。"""

    require_internal_token(internal_token)
    try:
        return await app.state.turn_processor.execute(payload)
    except RuntimeTurnError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@app.post(
    "/internal/v1/turns/inspect",
    response_model=InspectGraphTurnResponse,
    tags=["internal"],
)
async def inspect_graph_turn(
    payload: ExecuteGraphTurnRequest,
    internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
) -> InspectGraphTurnResponse:
    """只检查已保存的准备结果或终态，不重新执行图节点。"""

    require_internal_token(internal_token)
    try:
        return await app.state.turn_processor.inspect(payload)
    except RuntimeTurnError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
