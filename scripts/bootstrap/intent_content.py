"""写入并审核通用客服意图的相似表达。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.ai_runtime.query.context import normalize_query
from services.content.models import IntentSimilarityModel
from shared.web.settings import ServiceSettings

SIMILARITY_VERSION = "intent-sim-v1"
SIMILARITY_SOURCE = "bootstrap:intent-similarity:faq:v1"
INTENT_VERSION = "faq-v1"

INTENT_SIMILARITIES: dict[str, tuple[str, ...]] = {
    "ACCOUNT_ACCESS_GUIDANCE": (
        "账号登录不了怎么办",
        "忘记登录密码了",
        "账号为什么被锁定",
    ),
    "SERVICE_CHANNEL_GUIDANCE": (
        "怎么联系人工客服",
        "客服什么时候在线",
        "在哪里查看处理进度",
    ),
    "PRIVACY_SECURITY_GUIDANCE": (
        "有人向我要验证码",
        "收到陌生登录链接怎么办",
        "怎样保护账号安全",
    ),
    "COMPLAINT_GUIDANCE": (
        "我要投诉服务问题",
        "怎么提交反馈",
        "投诉多久能处理",
    ),
    "GENERAL_SERVICE_GUIDANCE": (
        "我想了解服务说明",
        "如何确认页面内容是最新的",
        "同一个问题可以继续补充吗",
    ),
}


async def seed(session_factory: async_sessionmaker) -> None:
    """幂等写入待审核语料，不覆盖已经人工调整的记录。"""

    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        for intent_id, texts in INTENT_SIMILARITIES.items():
            prefix = intent_id.lower().replace("_guidance", "")
            for number, value in enumerate(texts, start=1):
                await session.execute(
                    insert(IntentSimilarityModel)
                    .values(
                        similarity_id=f"{prefix}_sim_{number:02d}",
                        version=SIMILARITY_VERSION,
                        intent_id=intent_id,
                        intent_version=INTENT_VERSION,
                        text=value,
                        normalized_text=normalize_query(value),
                        locale="zh-CN",
                        lifecycle_status="DRAFT",
                        source_ref=SIMILARITY_SOURCE,
                        created_at=now,
                    )
                    .on_conflict_do_nothing()
                )


async def approve(session_factory: async_sessionmaker, approved_by: str) -> None:
    """批准本次冷启动语料，供离线索引构建读取。"""

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
        print(f"已批准 {result.rowcount} 条相似意图语料。")


async def main() -> None:
    parser = argparse.ArgumentParser(description="相似意图语料管理")
    parser.add_argument("action", choices=("seed", "approve"))
    parser.add_argument("--approved-by", default="repository_bootstrap")
    args = parser.parse_args()
    engine = create_async_engine(ServiceSettings().database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if args.action == "seed":
            await seed(session_factory)
            print("已写入通用客服相似意图语料，当前仍为 DRAFT。")
        else:
            await approve(session_factory, args.approved_by)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
