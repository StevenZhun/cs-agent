"""把已审核的 FAQ 相似问按分组投影为动作意图相似问。

FAQ 与 SCENE 意图语料需要覆盖相近的口语表达风格，否则知识类问题可能在意图层被
更口语化的 SCENE 语料吸引，形成不必要的歧义，无法进入 FAQ 条目解析。

本脚本不调用模型：每个 FAQ 意图下，从其每条已发布条目各取 1 条非标准问的相似问
（已经过 faq_variants 复核），写入 content.intent_similarity 作为该意图的语料。
这样 FAQ 意图的语料风格与 SCENE 侧对齐，且语料本身就是审核过的内容。

执行：
  python -m scripts.bootstrap.faq_intent_projection seed
  python -m scripts.bootstrap.faq_intent_projection approve
  python -m scripts.maintenance.rebuild_intent_milvus          # 只重建意图 Collection
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.content.models import (
    FaqGroupModel,
    FaqItemModel,
    FaqQuestionModel,
    IntentDefinitionModel,
    IntentSimilarityModel,
)
from shared.web.settings import ServiceSettings

FAQ_SOURCE = "bootstrap:faq:v1"
SIMILARITY_VERSION = "intent-sim-v1"
SIMILARITY_SOURCE = "bootstrap:intent-similarity:from-faq:v1"
PER_ITEM = 1


async def seed(session_factory: async_sessionmaker) -> None:
    now = datetime.now(UTC)
    written = 0
    async with session_factory() as session, session.begin():
        intents = (
            (
                await session.execute(
                    select(IntentDefinitionModel).where(
                        IntentDefinitionModel.source_ref == FAQ_SOURCE,
                        IntentDefinitionModel.route_target == "FAQ",
                        IntentDefinitionModel.request_kind == "KNOWLEDGE",
                        IntentDefinitionModel.lifecycle_status.in_(("APPROVED", "PUBLISHED")),
                    )
                )
            )
            .scalars()
            .all()
        )
        for intent in intents:
            groups = (
                (
                    await session.execute(
                        select(FaqGroupModel).where(
                            FaqGroupModel.intent_id == intent.intent_id,
                            FaqGroupModel.intent_version == intent.version,
                        )
                    )
                )
                .scalars()
                .all()
            )
            number = 0
            for group in groups:
                items = (
                    (
                        await session.execute(
                            select(FaqItemModel)
                            .where(
                                FaqItemModel.faq_group_id == group.faq_group_id,
                                FaqItemModel.group_version == group.version,
                                FaqItemModel.lifecycle_status.in_(("APPROVED", "PUBLISHED")),
                            )
                            .order_by(FaqItemModel.faq_item_id)
                        )
                    )
                    .scalars()
                    .all()
                )
                for item in items:
                    variants = (
                        (
                            await session.execute(
                                select(FaqQuestionModel)
                                .where(
                                    FaqQuestionModel.faq_item_id == item.faq_item_id,
                                    FaqQuestionModel.item_version == item.version,
                                    FaqQuestionModel.exact_match_enabled.is_(False),
                                )
                                .order_by(FaqQuestionModel.variant_id)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for question in variants[:PER_ITEM]:
                        number += 1
                        result = await session.execute(
                            insert(IntentSimilarityModel)
                            .values(
                                similarity_id=f"faqproj_{intent.intent_id.lower()}_{number:02d}",
                                version=SIMILARITY_VERSION,
                                intent_id=intent.intent_id,
                                intent_version=intent.version,
                                text=question.question,
                                normalized_text=question.normalized_question,
                                locale=question.locale,
                                lifecycle_status="DRAFT",
                                source_ref=SIMILARITY_SOURCE,
                                created_at=now,
                            )
                            .on_conflict_do_nothing()
                            .returning(IntentSimilarityModel.similarity_id)
                        )
                        if result.scalar_one_or_none() is not None:
                            written += 1
    print(f"已从 FAQ 相似问投影 {written} 条意图语料（DRAFT）。")


async def approve(session_factory: async_sessionmaker, approved_by: str) -> None:
    async with session_factory() as session, session.begin():
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
        print(f"已批准 {result.rowcount} 条由 FAQ 投影的意图语料。")


async def main() -> None:
    parser = argparse.ArgumentParser(description="从已审核 FAQ 相似问投影意图语料")
    parser.add_argument("action", choices=("seed", "approve"))
    parser.add_argument("--approved-by", default="repository_bootstrap")
    args = parser.parse_args()
    engine = create_async_engine(ServiceSettings().database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if args.action == "seed":
            await seed(session_factory)
        else:
            await approve(session_factory, args.approved_by)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
