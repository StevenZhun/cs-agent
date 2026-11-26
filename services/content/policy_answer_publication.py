"""高风险策略答案的审核、不可变发布和活动版本切换。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.content.models import (
    IntentDefinitionModel,
    PolicyAnswerModel,
    PolicyAnswerReleaseModel,
)
from services.content.policy_answers import (
    POLICY_ANSWER_ACTIVE_KEY,
    policy_answer_content_hash,
    policy_answer_release_content_hash,
    policy_answer_release_key,
    validate_policy_answer_release,
)


async def approve_policy_answers(
    session: AsyncSession,
    *,
    source_ref: str,
    approved_by: str,
    allow_empty: bool = False,
) -> int:
    """批准一批草稿；只有已发布的高风险策略意图可以绑定策略答复。

    内容审核入口默认要求确有待批准数据，防止操作人员误以为完成了审核。
    可重复执行的环境初始化可以显式允许空批次，此时已发布内容保持不变。
    """

    if not approved_by.strip():
        raise ValueError("策略答案批准人不能为空")
    rows = list(
        (
            await session.scalars(
                select(PolicyAnswerModel)
                .where(
                    PolicyAnswerModel.source_ref == source_ref,
                    PolicyAnswerModel.lifecycle_status.in_(("DRAFT", "IN_REVIEW")),
                )
                .with_for_update()
            )
        ).all()
    )
    if not rows and allow_empty:
        return 0
    if not rows:
        raise ValueError("没有待批准的策略答案")
    seen_intents: set[str] = set()
    now = datetime.now(UTC)
    for row in rows:
        if row.intent_id in seen_intents:
            raise ValueError(f"同一批次包含多个 {row.intent_id} 策略答案版本")
        seen_intents.add(row.intent_id)
        await _validate_policy_answer(session, row)
        row.lifecycle_status = "APPROVED"
        row.approved_by = approved_by
        row.approved_at = now
    return len(rows)


async def publish_policy_answers(
    session: AsyncSession,
    redis: Redis,
    *,
    source_ref: str,
    release_version: str,
) -> dict[str, object]:
    """保存 PostgreSQL 权威快照后写 Redis，完整成功后才切换活动指针。"""

    existing = await session.get(PolicyAnswerReleaseModel, release_version)
    if existing is not None:
        if existing.source_ref != source_ref:
            raise ValueError("同一策略答案发布版本不能指向另一批内容")
        payload = validate_policy_answer_release(
            existing.payload,
            expected_release_version=release_version,
        )
    else:
        rows = list(
            (
                await session.scalars(
                    select(PolicyAnswerModel)
                    .where(
                        PolicyAnswerModel.source_ref == source_ref,
                        PolicyAnswerModel.lifecycle_status.in_(("APPROVED", "PUBLISHED")),
                    )
                    .with_for_update()
                )
            ).all()
        )
        if not rows:
            raise ValueError("没有已批准的策略答案")
        answers: dict[str, object] = {}
        for row in rows:
            if row.intent_id in answers:
                raise ValueError(f"同一发布版本只能包含一个 {row.intent_id} 策略答案")
            await _validate_policy_answer(session, row)
            if not row.approved_by or row.approved_at is None:
                raise ValueError(f"策略答案 {row.intent_id}:{row.version} 缺少批准记录")
            answers[row.intent_id] = {
                "intent_id": row.intent_id,
                "intent_version": row.intent_version,
                "policy_answer_version": row.version,
                "answer_text": row.answer_text,
                "answer_content_hash": row.answer_content_hash,
                "source_ref": row.source_ref,
            }
        now = datetime.now(UTC)
        payload = {
            "release_version": release_version,
            "answers": answers,
            "content_hash": policy_answer_release_content_hash(answers),
            "approved_by": sorted({str(row.approved_by) for row in rows}),
            "published_at": now.isoformat(),
        }
        validate_policy_answer_release(payload, expected_release_version=release_version)
        session.add(
            PolicyAnswerReleaseModel(
                release_version=release_version,
                source_ref=source_ref,
                payload=payload,
                published_at=now,
            )
        )
    await _align_policy_answer_lifecycle(session, payload)
    await session.commit()

    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    key = policy_answer_release_key(release_version)
    cached = await redis.get(key)
    if cached is not None and cached.decode() != encoded:
        raise ValueError("Redis 已有同名不同内容的策略答案发布版本")
    if cached is None and not await redis.set(key, encoded, nx=True):
        raise ValueError("有其他发布者正在写同一策略答案版本，请重试")
    await redis.set(POLICY_ANSWER_ACTIVE_KEY, release_version)
    return payload


async def activate_policy_answer_release(
    session: AsyncSession,
    redis: Redis,
    release_version: str,
) -> None:
    """重新激活已登记且 Redis 完整存在的策略答案发布版本。"""

    release = await session.get(PolicyAnswerReleaseModel, release_version)
    if release is None:
        raise ValueError("PostgreSQL 中不存在该策略答案发布版本")
    if not await redis.exists(policy_answer_release_key(release_version)):
        raise ValueError("Redis 中缺少该策略答案发布快照")
    payload = validate_policy_answer_release(
        release.payload,
        expected_release_version=release_version,
    )
    await _align_policy_answer_lifecycle(session, payload)
    await session.commit()
    await redis.set(POLICY_ANSWER_ACTIVE_KEY, release_version)


async def _validate_policy_answer(session: AsyncSession, row: PolicyAnswerModel) -> None:
    """核对正文哈希及绑定意图，防止普通 FAQ 或动作意图误用安全出口。"""

    if row.answer_content_hash != policy_answer_content_hash(row.answer_text):
        raise ValueError(f"策略答案 {row.intent_id}:{row.version} 正文哈希不一致")
    intent = await session.get(IntentDefinitionModel, (row.intent_id, row.intent_version))
    if (
        intent is None
        or intent.intent_class != "STRATEGY"
        or intent.request_kind != "HIGH_RISK"
        or not intent.enabled
        or intent.lifecycle_status not in {"APPROVED", "PUBLISHED"}
    ):
        raise ValueError(f"策略答案 {row.intent_id}:{row.version} 未绑定可用高风险策略意图")


async def _align_policy_answer_lifecycle(
    session: AsyncSession,
    payload: dict[str, object],
) -> None:
    """让活动答案保持 PUBLISHED，并将同意图的其他已发布版本逻辑淘汰。"""

    active = {
        (str(intent_id), str(answer["policy_answer_version"]))
        for intent_id, answer in payload["answers"].items()
    }
    managed_intent_ids = {intent_id for intent_id, _version in active}
    rows = list(
        (
            await session.scalars(
                select(PolicyAnswerModel)
                .where(
                    PolicyAnswerModel.intent_id.in_(managed_intent_ids),
                    PolicyAnswerModel.lifecycle_status.in_(("APPROVED", "PUBLISHED", "RETIRED")),
                )
                .with_for_update()
            )
        ).all()
    )
    for row in rows:
        is_active = (row.intent_id, row.version) in active
        row.lifecycle_status = "PUBLISHED" if is_active else "RETIRED"
        row.release_version = str(payload["release_version"]) if is_active else row.release_version
