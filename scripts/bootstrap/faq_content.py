"""建立、审核并发布仓库随附的 FAQ 内容基线。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.bootstrap.faq_data import FAQ_GROUPS
from services.ai_runtime.query.context import normalize_query
from services.content.models import (
    FaqAnswerModel,
    FaqGroupModel,
    FaqItemModel,
    FaqQuestionModel,
    IntentDefinitionModel,
)
from services.content.publication import approve_faq_source, publish_faq
from shared.web.settings import ServiceSettings

FAQ_VERSION = "faq-v1"
FAQ_SOURCE = "bootstrap:faq:v1"
FAQ_RELEASE = "faq-release-v1"
BOOTSTRAP_ACTOR = "repository_bootstrap"


async def seed_faq_drafts(session_factory: async_sessionmaker) -> None:
    """幂等写入 FAQ 草稿，不覆盖数据库中已经人工编辑的同编号内容。"""

    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        for group_id, intent_id, group_name, entries in FAQ_GROUPS:
            await session.execute(
                insert(IntentDefinitionModel)
                .values(
                    intent_id=intent_id,
                    version=FAQ_VERSION,
                    name=group_name,
                    description=f"客户咨询{group_name}，组内再匹配具体 FAQ 条目。",
                    intent_class="ACTION",
                    route_target="FAQ",
                    request_kind="KNOWLEDGE",
                    lifecycle_status="DRAFT",
                    enabled=False,
                    source_ref=FAQ_SOURCE,
                    created_at=now,
                )
                .on_conflict_do_nothing()
            )
            await session.execute(
                insert(FaqGroupModel)
                .values(
                    faq_group_id=group_id,
                    version=FAQ_VERSION,
                    intent_id=intent_id,
                    intent_version=FAQ_VERSION,
                    name=group_name,
                    lifecycle_status="DRAFT",
                    source_ref=FAQ_SOURCE,
                    created_at=now,
                )
                .on_conflict_do_nothing()
            )
            for number, (question, answer, _complexity) in enumerate(entries, start=1):
                item_id = f"{group_id}_{number:02d}"
                await session.execute(
                    insert(FaqItemModel)
                    .values(
                        faq_item_id=item_id,
                        version=FAQ_VERSION,
                        faq_group_id=group_id,
                        group_version=FAQ_VERSION,
                        title=question,
                        lifecycle_status="DRAFT",
                        source_ref=FAQ_SOURCE,
                        created_at=now,
                    )
                    .on_conflict_do_nothing()
                )
                await session.execute(
                    insert(FaqQuestionModel)
                    .values(
                        variant_id=f"{item_id}_q1",
                        faq_item_id=item_id,
                        item_version=FAQ_VERSION,
                        question=question,
                        normalized_question=normalize_query(question),
                        exact_match_enabled=True,
                        locale="zh-CN",
                    )
                    .on_conflict_do_nothing()
                )
                await session.execute(
                    insert(FaqAnswerModel)
                    .values(
                        answer_id=f"{item_id}_a1",
                        faq_item_id=item_id,
                        item_version=FAQ_VERSION,
                        answer=answer,
                        product_codes=[],
                        channels=[],
                        regions=[],
                        effective_from=None,
                        effective_to=None,
                        source_ref=FAQ_SOURCE,
                    )
                    .on_conflict_do_nothing()
                )


async def main() -> None:
    """执行相互独立的写草稿、批准或发布动作。"""

    parser = argparse.ArgumentParser(description="FAQ 内容初始化")
    parser.add_argument("action", choices=("seed", "approve", "publish"))
    parser.add_argument("--release", default=FAQ_RELEASE)
    args = parser.parse_args()
    settings = ServiceSettings()
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(settings.redis_url)
    try:
        if args.action == "seed":
            await seed_faq_drafts(session_factory)
            print("FAQ 草稿已写入；尚未批准或发布。")
        else:
            async with session_factory() as session:
                if args.action == "approve":
                    await approve_faq_source(
                        session,
                        source_ref=FAQ_SOURCE,
                        approved_by=BOOTSTRAP_ACTOR,
                    )
                    print("FAQ 内容已批准；尚未生成 Redis 在线副本。")
                else:
                    manifest = await publish_faq(
                        session,
                        redis,
                        release_version=args.release,
                        source_ref=FAQ_SOURCE,
                        approved_by=BOOTSTRAP_ACTOR,
                    )
                    print(f"FAQ 版本 {manifest.release_version} 已发布并激活。")
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
