"""独立异步质量服务：在线回复完成后再做深度复核。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

from fastapi import FastAPI

from services.bff.infrastructure.database import create_bff_engine, create_session_factory
from services.quality.repository import QualityRepository
from services.quality.reviewer import ShadowQualityReviewer
from services.quality.service import AsyncQualityProcessor
from shared.observability import emit_structured_event
from shared.web.service_app import create_service_app
from shared.web.settings import ServiceSettings

settings = ServiceSettings()


async def quality_loop(processor: AsyncQualityProcessor) -> None:
    """持续领取短任务；无任务时让出事件循环，不进行固定频率数据库写入。"""

    while True:
        try:
            processed = await processor.process_one()
        except Exception as error:  # Worker 边界必须保持存活并留下无正文原因码。
            emit_structured_event(
                "quality_worker_iteration_failed",
                error_code=type(error).__name__.upper(),
            )
            processed = False
        if not processed:
            await asyncio.sleep(settings.quality_poll_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = create_bff_engine(settings.database_url)
    repository = QualityRepository(create_session_factory(engine))
    reviewer = (
        ShadowQualityReviewer(
            provider=settings.llm_provider,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key.get_secret_value(),
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
            context_window_tokens=settings.llm_context_window_tokens,
        )
        if settings.quality_shadow_enabled
        else None
    )
    processor = AsyncQualityProcessor(
        repository,
        reviewer,
        worker_id=f"quality-{uuid4().hex[:12]}",
    )
    task = asyncio.create_task(quality_loop(processor)) if settings.quality_async_enabled else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await engine.dispose()


app = create_service_app(
    service="quality",
    title="Agent Assist Async Quality",
    version=settings.service_version,
    environment=settings.app_env,
    dependencies=(settings.postgres_dependency(),),
    lifespan=lifespan,
)
