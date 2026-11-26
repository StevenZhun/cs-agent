"""把已审核的 FAQ 相似问资产写入权威内容库。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.ai_runtime.query.context import normalize_query
from services.content.models import FaqQuestionModel
from shared.web.settings import ServiceSettings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSET_PATH = PROJECT_ROOT / "data/knowledge/faq_variants.json"


async def seed() -> None:
    """幂等写入已批准的相似问；未批准条目保持跳过。"""

    data = json.loads(ASSET_PATH.read_text(encoding="utf-8"))
    settings = ServiceSettings()
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    written = skipped = 0
    try:
        async with session_factory() as session, session.begin():
            for item_id, row in sorted(data.items()):
                if row.get("review_status") != "APPROVED":
                    skipped += 1
                    continue
                existing = list(
                    (
                        await session.scalars(
                            select(FaqQuestionModel.variant_id).where(
                                FaqQuestionModel.faq_item_id == item_id,
                                FaqQuestionModel.item_version == row["item_version"],
                            )
                        )
                    ).all()
                )
                next_index = len(existing) + 1
                for text in row["variants"]:
                    result = await session.execute(
                        insert(FaqQuestionModel)
                        .values(
                            variant_id=f"{item_id}_q{next_index}",
                            faq_item_id=item_id,
                            item_version=row["item_version"],
                            question=text,
                            normalized_question=normalize_query(text),
                            exact_match_enabled=False,
                            locale="zh-CN",
                        )
                        .on_conflict_do_nothing()
                        .returning(FaqQuestionModel.variant_id)
                    )
                    if result.scalar_one_or_none() is not None:
                        written += 1
                    next_index += 1
    finally:
        await engine.dispose()
    print(f"写入 {written} 条 FAQ 相似问；跳过 {skipped} 个未批准条目。")


if __name__ == "__main__":
    asyncio.run(seed())
