"""维护控制命令和未接能力边界的已发布规则。

PostgreSQL 保存可审核历史，Redis 只保存 Runtime 读取的不可变发布快照。
这些规则只是仓库随附的初始化基线；正式环境应通过审核发布流程维护。
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.ai_runtime.query.context import normalize_query
from services.content.models import IntentRuleModel
from services.content.publication import approve_intent_source, publish_intents
from shared.web.settings import ServiceSettings

SOURCE_REF = "bootstrap:routing-rules:v2"
RULE_VERSION = "routing-rule-v2"
RELEASE_VERSION = "routing-release-v2"
PREVIOUS_SOURCE_REF = "bootstrap:routing-rules:v1"

# 规则编号、类型、短语、匹配方式、优先级、能力、控制动作、否定短语。
ROUTING_RULES: tuple[
    tuple[str, str, str, str, int, str | None, str | None, tuple[str, ...]], ...
] = (
    ("human-bare", "CONTROL", "人工", "EXACT", 120, None, "HUMAN", ()),
    (
        "human-transfer",
        "CONTROL",
        "转人工",
        "CONTAINS",
        120,
        None,
        "HUMAN",
        ("不用转", "不要转", "不需要转", "无需转", "先别转", "不想转"),
    ),
    ("human-service", "CONTROL", "人工客服", "EXACT", 120, None, "HUMAN", ()),
    ("human-request", "CONTROL", "我要人工", "EXACT", 120, None, "HUMAN", ()),
    (
        "human-contact",
        "CONTROL",
        "联系人工坐席",
        "CONTAINS",
        120,
        None,
        "HUMAN",
        ("不用联系", "不要联系", "不需要联系", "先别联系", "不想联系"),
    ),
    (
        "human-find",
        "CONTROL",
        "找人工客服",
        "CONTAINS",
        120,
        None,
        "HUMAN",
        ("不用找", "不要找", "不需要找", "先别找", "不想找"),
    ),
    ("cancel-consultation", "CONTROL", "取消咨询", "EXACT", 110, None, "CANCEL", ()),
    ("weather", "CAPABILITY", "天气", "CONTAINS", 100, "WEATHER", None, ()),
    ("rain", "CAPABILITY", "下雨", "CONTAINS", 100, "WEATHER", None, ()),
    ("snow", "CAPABILITY", "下雪", "CONTAINS", 100, "WEATHER", None, ()),
    ("temperature", "CAPABILITY", "气温", "CONTAINS", 100, "WEATHER", None, ()),
    ("temperature-degree", "CAPABILITY", "几度", "CONTAINS", 100, "WEATHER", None, ()),
    ("navigation", "CAPABILITY", "导航", "CONTAINS", 100, "NAVIGATION", None, ()),
    (
        "route-how",
        "CAPABILITY",
        "怎么走",
        "CONTAINS",
        100,
        "NAVIGATION",
        None,
        ("流程",),
    ),
    ("self-driving-route", "CAPABILITY", "自驾路线", "CONTAINS", 100, "NAVIGATION", None, ()),
    ("driving-route", "CAPABILITY", "驾车路线", "CONTAINS", 100, "NAVIGATION", None, ()),
    (
        "stock-market",
        "CAPABILITY",
        "股票会涨",
        "CONTAINS",
        100,
        "INVESTMENT_MARKET",
        None,
        (),
    ),
    (
        "stock-recommendation",
        "CAPABILITY",
        "推荐股票",
        "CONTAINS",
        100,
        "INVESTMENT_MARKET",
        None,
        (),
    ),
    (
        "stock-selection",
        "CAPABILITY",
        "买哪只股票",
        "CONTAINS",
        100,
        "INVESTMENT_MARKET",
        None,
        (),
    ),
    (
        "stock-forecast",
        "CAPABILITY",
        "预测股票",
        "CONTAINS",
        100,
        "INVESTMENT_MARKET",
        None,
        (),
    ),
    (
        "stock-profit",
        "CAPABILITY",
        "炒股能赚",
        "CONTAINS",
        100,
        "INVESTMENT_MARKET",
        None,
        (),
    ),
    (
        "investment-forecast",
        "CAPABILITY",
        "投资预测",
        "CONTAINS",
        100,
        "INVESTMENT_MARKET",
        None,
        (),
    ),
    ("service-policy-domain", "DOMAIN", "服务规则", "CONTAINS", 50, None, None, ()),
)


async def seed(session_factory: async_sessionmaker) -> None:
    """写入待审核规则；重复执行不会覆盖人工已经修改的同版本记录。"""

    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        for rule_id, kind, phrase, mode, priority, capability, action, negatives in ROUTING_RULES:
            await session.execute(
                insert(IntentRuleModel)
                .values(
                    rule_id=rule_id,
                    version=RULE_VERSION,
                    rule_kind=kind,
                    intent_id=None,
                    intent_version=None,
                    pattern=normalize_query(phrase),
                    match_mode=mode,
                    negative_patterns=list(negatives),
                    priority=priority,
                    product_codes=[],
                    channels=[],
                    regions=[],
                    required_capability=capability,
                    control_action=action,
                    lifecycle_status="DRAFT",
                    source_ref=SOURCE_REF,
                    created_at=now,
                )
                .on_conflict_do_nothing()
            )


async def main() -> None:
    """执行种子、批准或发布动作。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("seed", "approve", "publish"))
    parser.add_argument("--release", default=RELEASE_VERSION)
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="没有待批准规则时保持成功，供可重复执行的环境初始化使用",
    )
    args = parser.parse_args()
    settings = ServiceSettings()
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(settings.redis_url)
    try:
        if args.action == "seed":
            await seed(session_factory)
            print("已写入待审核控制与能力规则；尚未进入 Runtime。")
        elif args.action == "approve":
            async with session_factory() as session:
                pending_count = await session.scalar(
                    select(func.count())
                    .select_from(IntentRuleModel)
                    .where(
                        IntentRuleModel.source_ref == SOURCE_REF,
                        IntentRuleModel.lifecycle_status.in_(("DRAFT", "IN_REVIEW")),
                    )
                )
                if pending_count:
                    await approve_intent_source(
                        session,
                        source_ref=SOURCE_REF,
                        approved_by="repository_bootstrap",
                    )
                    print("已批准控制与能力规则；尚未生成 Redis 在线副本。")
                elif args.allow_empty:
                    print("控制与能力规则已经批准或发布，无需重复批准。")
                else:
                    raise ValueError("没有待批准的控制与能力规则")
        else:
            async with session_factory() as session:
                manifest = await publish_intents(
                    session,
                    redis,
                    release_version=args.release,
                    source_ref=SOURCE_REF,
                    approved_by="repository_bootstrap",
                )
                # 新活动快照已经成功写入 Redis 后，再逻辑淘汰旧数据库规则。
                # 旧 release 与 Redis 不可变副本继续保留，便于审计历史决策。
                await session.execute(
                    update(IntentRuleModel)
                    .where(
                        IntentRuleModel.source_ref == PREVIOUS_SOURCE_REF,
                        IntentRuleModel.lifecycle_status == "PUBLISHED",
                    )
                    .values(lifecycle_status="RETIRED")
                )
                await session.commit()
            print(f"已发布规则版本 {manifest.release_version} 并切换活动指针。")
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
