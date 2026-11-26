"""SOP 草稿配置、审核检查和 PostgreSQL 到 Redis 的不可变发布。"""

import json
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.content.models import IntentDefinitionModel, SopDefinitionModel, SopReleaseModel
from shared.sop_config import SOP_ACTIVE_KEY, SopConfig, sop_content_hash, sop_release_key


async def save_draft(session: AsyncSession, config: SopConfig, source: str) -> None:
    """新增或修改草稿；批准后的版本必须另建新版本，不原地覆盖。"""

    now = datetime.now(UTC)
    row = await session.get(SopDefinitionModel, (config.sop_id, config.version))
    if row is not None and row.lifecycle_status != "DRAFT":
        if row.config != config.model_dump(mode="json"):
            raise ValueError("该版本已批准或发布，修改时请建立新版本")
        return
    intent = await session.get(IntentDefinitionModel, (config.intent_id, config.version))
    if intent is not None and (intent.route_target != "SCENE" or intent.intent_class != "ACTION"):
        raise ValueError("SOP 只能绑定 ACTION 类的 SCENE 意图")
    if intent is None:
        session.add(
            IntentDefinitionModel(
                intent_id=config.intent_id,
                version=config.version,
                name=config.intent_name,
                description=config.name,
                intent_class="ACTION",
                route_target="SCENE",
                request_kind="ACTION",
                lifecycle_status="DRAFT",
                enabled=True,
                source_ref=source,
                created_at=now,
            )
        )
        await session.flush()
    if row is None:
        row = SopDefinitionModel(
            sop_id=config.sop_id,
            version=config.version,
            intent_id=config.intent_id,
            intent_version=config.version,
            lifecycle_status="DRAFT",
            source_ref=source,
            created_at=now,
        )
        session.add(row)
    row.config = config.model_dump(mode="json")
    row.intent_id = config.intent_id
    row.intent_version = config.version
    await session.flush()


async def approve_sops(session: AsyncSession, source: str, approved_by: str) -> int:
    """校验草稿结构后记录批准人；命令操作不等同于生产环境人工审核。"""

    rows = list(
        (
            await session.scalars(
                select(SopDefinitionModel)
                .where(
                    SopDefinitionModel.source_ref == source,
                    SopDefinitionModel.lifecycle_status == "DRAFT",
                )
                .with_for_update()
            )
        ).all()
    )
    for row in rows:
        SopConfig.model_validate(row.config)
        row.lifecycle_status = "APPROVED"
        row.approved_by = approved_by
        row.approved_at = datetime.now(UTC)
        intent = await session.get(IntentDefinitionModel, (row.intent_id, row.intent_version))
        if intent.route_target != "SCENE" or intent.intent_class != "ACTION":
            raise ValueError("SOP 只能绑定 ACTION 类的场景动作")
        intent.lifecycle_status = "APPROVED"
        intent.approved_by = approved_by
        intent.approved_at = row.approved_at
    return len(rows)


async def publish_sops(
    session: AsyncSession, redis: Redis, source: str, release_version: str
) -> dict:
    """先保存完整发布快照，再建立 Redis 副本，最后切换活动指针。

    Redis 写失败时不会切换活动版本；用同一发布号重试时读取 PostgreSQL
    已保存的快照，不能读取后来改过的草稿。旧版本也可按相同命令重新激活。
    """

    existing = await session.get(SopReleaseModel, release_version)
    if existing is not None:
        payload = existing.payload
    else:
        rows = list(
            (
                await session.scalars(
                    select(SopDefinitionModel)
                    .where(
                        SopDefinitionModel.source_ref == source,
                        SopDefinitionModel.lifecycle_status.in_(("APPROVED", "PUBLISHED")),
                    )
                    .with_for_update()
                )
            ).all()
        )
        if not rows:
            raise ValueError("没有已批准 SOP")
        definitions = {}
        intent_ids = set()
        phrases = set()
        for row in rows:
            config = SopConfig.model_validate(row.config)
            if (
                (config.sop_id, config.version, config.intent_id)
                != (row.sop_id, row.version, row.intent_id)
                or not row.approved_by
                or not row.approved_at
            ):
                raise ValueError("配置内容与数据库标识或批准记录不一致")
            if config.sop_id in definitions or config.intent_id in intent_ids:
                raise ValueError("同批次 SOP 编号或动作绑定重复；请只选择一个活动版本")
            if any(phrase in phrases for phrase in config.entry_phrases):
                raise ValueError("不同动作的入口短语冲突")
            intent = await session.get(IntentDefinitionModel, (row.intent_id, row.intent_version))
            if (
                intent is None
                or intent.route_target != "SCENE"
                or intent.intent_class != "ACTION"
                or not intent.enabled
            ):
                raise ValueError("SOP 绑定了不存在、禁用或非场景意图")
            definitions[config.sop_id] = config.model_dump(mode="json")
            intent_ids.add(config.intent_id)
            phrases.update(config.entry_phrases)
            row.lifecycle_status = "PUBLISHED"
            intent.lifecycle_status = "PUBLISHED"
            intent.release_version = release_version
        now = datetime.now(UTC)
        payload = {
            "release_version": release_version,
            "definitions": definitions,
            "content_hash": sop_content_hash(definitions),
            "published_at": now.isoformat(),
            "approved_by": sorted({row.approved_by for row in rows}),
        }
        session.add(
            SopReleaseModel(release_version=release_version, payload=payload, published_at=now)
        )
    await _align_active_lifecycle(session, payload)
    # 先让权威库保存发布快照和逻辑淘汰结果。后续 Redis 失败时，可按发布号
    # 重试同一份内容；历史版本保留为 RETIRED，不做物理删除。
    await session.commit()
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    key = sop_release_key(release_version)
    cached = await redis.get(key)
    if cached is not None and cached.decode() != encoded:
        raise ValueError("Redis 已有同名不同内容的发布版本，不能覆盖")
    if cached is None and not await redis.set(key, encoded, nx=True):
        raise ValueError("有其他发布者正在写同一版本，请重试")
    # 完整副本已经存在才切换指针，不能先让客户请求读到半份配置。
    await redis.set(SOP_ACTIVE_KEY, release_version)
    return payload


async def _align_active_lifecycle(session: AsyncSession, payload: dict) -> None:
    """让权威库的 PUBLISHED/RETIRED 与将要激活的发布快照保持一致。"""

    active_sops = {
        (str(sop_id), str(config["version"]))
        for sop_id, config in payload["definitions"].items()
    }
    active_intents = {
        (str(config["intent_id"]), str(config["version"]))
        for config in payload["definitions"].values()
    }
    sop_rows = list(
        (
            await session.scalars(
                select(SopDefinitionModel)
                .where(SopDefinitionModel.lifecycle_status.in_(("PUBLISHED", "RETIRED")))
                .with_for_update()
            )
        ).all()
    )
    for row in sop_rows:
        row.lifecycle_status = (
            "PUBLISHED" if (row.sop_id, row.version) in active_sops else "RETIRED"
        )
    managed_intent_ids = {row.intent_id for row in sop_rows} | {
        intent_id for intent_id, _version in active_intents
    }
    intent_rows = list(
        (
            await session.scalars(
                select(IntentDefinitionModel)
                .where(
                    IntentDefinitionModel.route_target == "SCENE",
                    IntentDefinitionModel.intent_id.in_(managed_intent_ids),
                    IntentDefinitionModel.lifecycle_status.in_(("PUBLISHED", "RETIRED")),
                )
                .with_for_update()
            )
        ).all()
    )
    for row in intent_rows:
        row.lifecycle_status = (
            "PUBLISHED" if (row.intent_id, row.version) in active_intents else "RETIRED"
        )
        if row.lifecycle_status == "PUBLISHED":
            row.release_version = str(payload["release_version"])
