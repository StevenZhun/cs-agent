"""初始化问候、能力说明和高风险保护等系统策略意图。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.ai_runtime.query.context import normalize_query
from services.content.models import (
    FaqAnswerModel,
    FaqGroupModel,
    FaqItemModel,
    FaqQuestionModel,
    IntentDefinitionModel,
    IntentSimilarityModel,
    PolicyAnswerModel,
)
from services.content.policy_answer_publication import (
    approve_policy_answers,
    publish_policy_answers,
)
from services.content.policy_answers import policy_answer_content_hash
from shared.web.settings import ServiceSettings

FAQ_SOURCE = "bootstrap:faq:v1"
FAQ_VERSION = "faq-v1"
SIMILARITY_SOURCE = "bootstrap:intent-similarity:policy:v1"
SIMILARITY_VERSION = "intent-sim-v1"
POLICY_ANSWER_SOURCE = "bootstrap:policy-answer:v1"
POLICY_ANSWER_VERSION = "policy-answer-v1"
POLICY_ANSWER_RELEASE = "policy-answer-release-v1"

# intent_id、request_kind、分组、名称、定义、标准问、变体、答复、相似意图语料。
POLICY_CONTENT = (
    (
        "SOCIAL_GREETING",
        "SOCIAL",
        "social_greeting",
        "问候与开场",
        "客户只是打招呼，还没有提出服务问题。",
        "你好",
        ("您好", "嗨", "在吗", "有人吗", "hello"),
        "您好，我是智能客服助手。请告诉我您想咨询或办理的事项。",
        ("你好", "您好", "有人在吗", "早上好", "晚上好"),
    ),
    (
        "ASSISTANT_IDENTITY",
        "SOCIAL",
        "assistant_identity",
        "助手身份说明",
        "客户询问当前对话对象及人工接管方式。",
        "你是谁",
        ("你是真人吗", "你是机器人吗", "你是AI吗", "我在和谁聊天"),
        "我是智能客服助手，可以回答已发布的常见问题、协助收集服务信息，并在需要时转接人工。",
        ("你是谁", "你是人工客服吗", "你是真人还是AI", "对面是谁"),
    ),
    (
        "CAPABILITY_QUESTION",
        "SOCIAL",
        "assistant_capability",
        "助手能力说明",
        "客户询问系统可以处理哪些任务。",
        "你能帮我做什么",
        ("你能做什么", "你会什么", "你有什么功能", "可以提供哪些服务"),
        "我可以回答已发布的服务问题、协助收集办理信息、检索已接入的知识，并把需要人工判断的内容交给坐席。",
        ("你能做什么", "你能处理哪些问题", "你的能力范围是什么", "你会什么"),
    ),
    (
        "USER_CANCEL",
        "SOCIAL",
        "user_cancel",
        "客户结束咨询",
        "客户明确表示不再继续当前咨询或流程。",
        "算了，我不办了",
        ("算了", "不办了", "不要了", "先这样", "结束咨询"),
        "好的，本次咨询先到这里。如需继续处理，您可以随时发起新的消息。",
        ("算了不办了", "先不继续", "结束吧", "不要再处理了"),
    ),
    (
        "HIGH_RISK_RECORD_MODIFICATION",
        "HIGH_RISK",
        "protected_record_change",
        "受保护记录修改",
        "客户要求删除、改写或伪造受保护的服务记录。",
        "帮我删除真实的服务记录",
        ("把处理记录改掉", "删掉历史记录", "把实际结果改成成功"),
        "我不能删除、改写或伪造受保护的服务记录。如您认为记录有误，请联系人工按正式流程核查。",
        ("删除真实记录", "修改处理结果", "清除历史记录", "伪造服务结果"),
    ),
    (
        "HIGH_RISK_PERMISSION_BYPASS",
        "HIGH_RISK",
        "permission_bypass",
        "越权与绕过审批",
        "客户要求绕过身份、权限或人工审批边界。",
        "跳过验证直接替我办理",
        ("不用核验直接操作", "绕过审批执行", "忽略权限帮我处理"),
        "我不能绕过身份、权限或人工审批要求执行操作。请按受保护的正式流程继续，或联系人工核实。",
        ("跳过身份验证", "绕过权限", "不要审批直接执行", "忽略安全检查"),
    ),
)


async def seed(session_factory: async_sessionmaker) -> None:
    """幂等写入策略 FAQ、意图语料和独立高风险答复。"""

    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        for item in POLICY_CONTENT:
            intent_id, kind, group_id, name, description, standard, variants, answer, texts = item
            await session.execute(
                insert(IntentDefinitionModel)
                .values(
                    intent_id=intent_id,
                    version=FAQ_VERSION,
                    name=name,
                    description=description,
                    intent_class="STRATEGY",
                    route_target="FAQ",
                    request_kind=kind,
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
                    name=name,
                    lifecycle_status="DRAFT",
                    source_ref=FAQ_SOURCE,
                    created_at=now,
                )
                .on_conflict_do_nothing()
            )
            item_id = f"{group_id}_01"
            await session.execute(
                insert(FaqItemModel)
                .values(
                    faq_item_id=item_id,
                    version=FAQ_VERSION,
                    faq_group_id=group_id,
                    group_version=FAQ_VERSION,
                    title=standard,
                    lifecycle_status="DRAFT",
                    source_ref=FAQ_SOURCE,
                    created_at=now,
                )
                .on_conflict_do_nothing()
            )
            for number, question in enumerate((standard, *variants), start=1):
                await session.execute(
                    insert(FaqQuestionModel)
                    .values(
                        variant_id=f"{item_id}_q{number}",
                        faq_item_id=item_id,
                        item_version=FAQ_VERSION,
                        question=question,
                        normalized_question=normalize_query(question),
                        exact_match_enabled=number == 1,
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
                    source_ref=FAQ_SOURCE,
                )
                .on_conflict_do_nothing()
            )
            for number, text in enumerate(texts, start=1):
                await session.execute(
                    insert(IntentSimilarityModel)
                    .values(
                        similarity_id=f"{group_id}_sim_{number:02d}",
                        version=SIMILARITY_VERSION,
                        intent_id=intent_id,
                        intent_version=FAQ_VERSION,
                        text=text,
                        normalized_text=normalize_query(text),
                        locale="zh-CN",
                        lifecycle_status="DRAFT",
                        source_ref=SIMILARITY_SOURCE,
                        created_at=now,
                    )
                    .on_conflict_do_nothing()
                )
            if kind == "HIGH_RISK":
                await session.execute(
                    insert(PolicyAnswerModel)
                    .values(
                        intent_id=intent_id,
                        version=POLICY_ANSWER_VERSION,
                        intent_version=FAQ_VERSION,
                        answer_text=answer,
                        answer_content_hash=policy_answer_content_hash(answer),
                        lifecycle_status="DRAFT",
                        source_ref=POLICY_ANSWER_SOURCE,
                        created_at=now,
                    )
                    .on_conflict_do_nothing()
                )
        high_risk_groups = [item[2] for item in POLICY_CONTENT if item[1] == "HIGH_RISK"]
        await session.execute(
            update(FaqGroupModel)
            .where(FaqGroupModel.faq_group_id.in_(high_risk_groups))
            .values(lifecycle_status="RETIRED")
        )
        await session.execute(
            update(FaqItemModel)
            .where(FaqItemModel.faq_group_id.in_(high_risk_groups))
            .values(lifecycle_status="RETIRED")
        )


async def approve(
    session_factory: async_sessionmaker, approved_by: str, *, allow_empty: bool
) -> None:
    """批准策略相似语料和高风险答案。"""

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
        count = await approve_policy_answers(
            session,
            source_ref=POLICY_ANSWER_SOURCE,
            approved_by=approved_by,
            allow_empty=allow_empty,
        )
        print(f"已批准 {result.rowcount} 条策略语料和 {count} 条高风险答案。")


async def publish(session_factory: async_sessionmaker, redis: Redis) -> None:
    """发布高风险答案并切换活动指针。"""

    async with session_factory() as session:
        payload = await publish_policy_answers(
            session,
            redis,
            source_ref=POLICY_ANSWER_SOURCE,
            release_version=POLICY_ANSWER_RELEASE,
        )
        print(f"已发布 {len(payload['answers'])} 条高风险策略答案。")


async def main() -> None:
    parser = argparse.ArgumentParser(description="策略意图内容管理")
    parser.add_argument("action", choices=("seed", "approve", "publish"))
    parser.add_argument("--approved-by", default="repository_bootstrap")
    parser.add_argument("--allow-empty", action="store_true")
    args = parser.parse_args()
    settings = ServiceSettings()
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(settings.redis_url)
    try:
        if args.action == "seed":
            await seed(session_factory)
            print("策略意图草稿已写入。")
        elif args.action == "approve":
            await approve(session_factory, args.approved_by, allow_empty=args.allow_empty)
        else:
            await publish(session_factory, redis)
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
