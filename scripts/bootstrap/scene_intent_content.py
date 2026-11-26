"""维护通用服务请求场景的相似意图语料。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.ai_runtime.query.context import normalize_query
from services.content.models import IntentDefinitionModel, IntentSimilarityModel
from shared.web.settings import ServiceSettings

SIMILARITY_VERSION = "intent-sim-v1"
SIMILARITY_SOURCE = "bootstrap:intent-similarity:scene:v1"

SCENE_INTENT_SIMILARITIES: dict[str, tuple[str, ...]] = {
    "SERVICE_REQUEST_CREATE": (
        "我要提交一个服务请求",
        "帮我登记这个问题",
        "我需要你们处理一件事",
        "帮我创建一个处理单",
        "我要反馈并跟进这个问题",
        "请把这个问题交给人工处理",
        "我想正式提交这个问题",
        "请记录我的诉求",
    ),
}


async def seed(session_factory: async_sessionmaker) -> None:
    """写入待审核语料；重复执行不会覆盖人工调整。"""

    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        rows = (
            (
                await session.execute(
                    select(IntentDefinitionModel).where(
                        IntentDefinitionModel.intent_id.in_(SCENE_INTENT_SIMILARITIES),
                        IntentDefinitionModel.intent_class == "ACTION",
                        IntentDefinitionModel.route_target == "SCENE",
                        IntentDefinitionModel.lifecycle_status == "PUBLISHED",
                        IntentDefinitionModel.enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        active_versions = {row.intent_id: row.version for row in rows}
        missing = set(SCENE_INTENT_SIMILARITIES) - set(active_versions)
        if missing:
            raise ValueError(f"以下场景意图没有唯一已发布版本：{sorted(missing)}")
        for intent_id, texts in SCENE_INTENT_SIMILARITIES.items():
            for number, text in enumerate(texts, start=1):
                await session.execute(
                    insert(IntentSimilarityModel)
                    .values(
                        similarity_id=f"scene_{intent_id.lower()}_{number:02d}",
                        version=SIMILARITY_VERSION,
                        intent_id=intent_id,
                        intent_version=active_versions[intent_id],
                        text=text,
                        normalized_text=normalize_query(text),
                        locale="zh-CN",
                        lifecycle_status="DRAFT",
                        source_ref=SIMILARITY_SOURCE,
                        created_at=now,
                    )
                    .on_conflict_do_nothing()
                )


async def approve(session_factory: async_sessionmaker, approved_by: str) -> None:
    """核对条数并批准当前语料。"""

    async with session_factory() as session, session.begin():
        expected = sum(len(texts) for texts in SCENE_INTENT_SIMILARITIES.values())
        actual = await session.scalar(
            select(func.count())
            .select_from(IntentSimilarityModel)
            .where(IntentSimilarityModel.source_ref == SIMILARITY_SOURCE)
        )
        if actual != expected:
            raise ValueError(f"场景意图语料不完整：期望 {expected} 条，实际 {actual} 条")
        result = await session.execute(
            update(IntentSimilarityModel)
            .where(
                IntentSimilarityModel.source_ref == SIMILARITY_SOURCE,
                IntentSimilarityModel.lifecycle_status.in_(("DRAFT", "IN_REVIEW")),
            )
            .values(
                lifecycle_status="APPROVED",
                approved_by=approved_by,
                approved_at=datetime.now(UTC),
            )
        )
        print(f"已批准 {result.rowcount} 条场景意图语料。")


async def main() -> None:
    """执行场景意图语料写入或审核。"""

    parser = argparse.ArgumentParser(description="场景意图语料管理")
    parser.add_argument("action", choices=("seed", "approve"))
    parser.add_argument("--approved-by", default="repository_bootstrap")
    args = parser.parse_args()
    engine = create_async_engine(ServiceSettings().database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if args.action == "seed":
            await seed(session_factory)
            print("场景意图语料已写入，当前状态为 DRAFT。")
        else:
            await approve(session_factory, args.approved_by)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
