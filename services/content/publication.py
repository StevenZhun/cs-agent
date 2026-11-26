"""把已审核的数据库规则复制到 Redis，供运行时快速读取。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from services.ai_runtime.query.context import normalize_query
from services.content.faq_cache import (
    FAQ_ACTIVE_KEY,
    faq_group_entries_key,
    faq_intent_catalog_key,
    faq_intent_groups_key,
    faq_item_field,
    faq_items_key,
    faq_question_digest,
    faq_question_intents_key,
    faq_release_meta_key,
)
from services.content.intent_cache import INTENT_ACTIVE_KEY, intent_release_key
from services.content.models import (
    FaqAnswerModel,
    FaqGroupModel,
    FaqItemModel,
    FaqQuestionModel,
    FaqReleaseModel,
    IntentDefinitionModel,
    IntentReleaseModel,
    IntentRuleModel,
)
from shared.contracts.content import (
    ContentAssetVersion,
    ContentConflictCheck,
    ContentPublicationManifest,
    ProjectionBuild,
)
from shared.contracts.enums import (
    ContentAssetType,
    ContentConflictStatus,
    ContentLifecycleStatus,
    ProjectionTarget,
    PublicationStatus,
)
from shared.contracts.faq import FaqExactHint


def _overlap(left: list[str], right: list[str]) -> bool:
    """空数组代表不限，因此与任何作用域都有交集。"""

    return not left or not right or bool(set(left) & set(right))


def check_intent_conflicts(
    intents: list[IntentDefinitionModel],
    rules: list[IntentRuleModel],
    *,
    allowed_statuses: frozenset[str] = frozenset({"APPROVED", "PUBLISHED"}),
) -> None:
    """拒绝同一动作的双路由、悬空规则和同范围相互矛盾的规则。"""

    by_id: dict[str, IntentDefinitionModel] = {}
    for intent in intents:
        if intent.intent_id in by_id:
            raise ValueError(f"同一发布版本包含多个 {intent.intent_id} 意图版本")
        if intent.lifecycle_status not in allowed_statuses or not intent.enabled:
            raise ValueError(f"意图 {intent.intent_id} 尚未批准或已停用")
        if intent.intent_class == "STRATEGY" and intent.route_target != "FAQ":
            raise ValueError(f"策略意图 {intent.intent_id} 只能进入 FAQ 策略内容链路")
        if intent.request_kind == "HIGH_RISK" and intent.intent_class != "STRATEGY":
            raise ValueError(f"高风险意图 {intent.intent_id} 必须标记为 STRATEGY")
        by_id[intent.intent_id] = intent
    if not rules:
        raise ValueError("发布版本至少需要一条规则")
    if any(rule.rule_kind == "INTENT" for rule in rules) and not intents:
        raise ValueError("发布 INTENT 规则时必须同时包含其意图定义")

    for rule in rules:
        if rule.lifecycle_status not in allowed_statuses:
            raise ValueError(f"规则 {rule.rule_id} 尚未批准")
        if normalize_query(rule.pattern) != rule.pattern or not rule.pattern:
            raise ValueError(f"规则 {rule.rule_id} 必须使用非空规范化短语")
        if rule.rule_kind == "INTENT":
            intent = by_id.get(rule.intent_id or "")
            if intent is None or intent.version != rule.intent_version:
                raise ValueError(f"规则 {rule.rule_id} 引用了清单外的意图版本")
        if rule.rule_kind == "CAPABILITY" and not rule.required_capability:
            raise ValueError(f"能力规则 {rule.rule_id} 缺少能力代码")
        if rule.rule_kind == "CONTROL" and rule.control_action not in {"CANCEL", "HUMAN"}:
            raise ValueError(f"控制规则 {rule.rule_id} 缺少合法控制动作")
        if rule.rule_kind not in {"INTENT", "CONTROL", "CAPABILITY", "DOMAIN"}:
            raise ValueError(f"规则 {rule.rule_id} 的类型未定义")

    for index, left in enumerate(rules):
        for right in rules[index + 1 :]:
            if left.match_mode != right.match_mode or left.pattern != right.pattern:
                continue
            if not all(
                _overlap(getattr(left, field), getattr(right, field))
                for field in ("product_codes", "channels", "regions")
            ):
                continue
            # 同一句、同作用域即使优先级不同，也不允许靠偶然排序掩盖两个业务定义。
            raise ValueError(f"规则 {left.rule_id} 与 {right.rule_id} 在同范围冲突")


async def approve_intent_source(
    session: AsyncSession, *, source_ref: str, approved_by: str
) -> None:
    """校验并批准一批意图规则；批准只改变生命周期，不生成在线副本。"""

    intents = list(
        (
            await session.scalars(
                select(IntentDefinitionModel).where(IntentDefinitionModel.source_ref == source_ref)
            )
        ).all()
    )
    rules = list(
        (
            await session.scalars(
                select(IntentRuleModel).where(IntentRuleModel.source_ref == source_ref)
            )
        ).all()
    )
    check_intent_conflicts(
        intents,
        rules,
        allowed_statuses=frozenset({"DRAFT", "IN_REVIEW", "APPROVED"}),
    )
    if not approved_by.strip():
        raise ValueError("意图规则批准人不能为空")
    now = datetime.now(UTC)
    for item in [*intents, *rules]:
        item.lifecycle_status = "APPROVED"
        item.approved_by = approved_by
        item.approved_at = now
        if isinstance(item, IntentDefinitionModel):
            item.enabled = True
    await session.commit()


def check_faq_conflicts(
    intents: list[IntentDefinitionModel],
    groups: list[FaqGroupModel],
    items: list[FaqItemModel],
    questions: list[FaqQuestionModel],
    answers: list[FaqAnswerModel],
) -> None:
    """发布 FAQ 前复用的静态检查，必须严格通过，否则不能发布；草稿也可提前演练但不能在线直答。"""

    intent_by_ref = {(item.intent_id, item.version): item for item in intents}
    group_by_ref = {(item.faq_group_id, item.version): item for item in groups}
    item_by_ref = {(item.faq_item_id, item.version): item for item in items}
    question_counts = {item_ref: 0 for item_ref in item_by_ref}
    exact_question_counts = {item_ref: 0 for item_ref in item_by_ref}
    answer_counts = {item_ref: 0 for item_ref in item_by_ref}

    # 检查 FAQ 分组是否绑定允许进入 FAQ 的全局意图。
    for group in groups:
        intent = intent_by_ref.get((group.intent_id, group.intent_version))
        if intent is None or intent.route_target != "FAQ":
            raise ValueError(f"FAQ 分组 {group.faq_group_id} 必须绑定 FAQ 全局意图")

    # 检查 FAQ 条目是否绑定 FAQ 分组
    for item in items:
        if (item.faq_group_id, item.group_version) not in group_by_ref:
            raise ValueError(f"FAQ 条目 {item.faq_item_id} 未绑定清单中的分组")

    # 检查 FAQ 问法是否绑定 FAQ 条目，并检查规范化文本是否一致
    exact_keys: dict[tuple[str, str], str] = {}
    for question in questions:
        item = item_by_ref.get((question.faq_item_id, question.item_version))
        if item is None:
            raise ValueError(f"FAQ 问法 {question.variant_id} 未绑定清单中的条目")
        question_counts[(question.faq_item_id, question.item_version)] += 1
        if normalize_query(question.question) != question.normalized_question:
            raise ValueError(f"FAQ 问法 {question.variant_id} 的规范化文本不一致")
        if question.exact_match_enabled:
            exact_question_counts[(question.faq_item_id, question.item_version)] += 1
            # 全局路由要先根据标准问题找到动作意图，所以同一语言下的同一句话
            # 不能同时属于两个 FAQ 条目，否则系统无法确定先进入哪个分组。
            key = (question.locale, question.normalized_question)
            previous = exact_keys.setdefault(key, question.faq_item_id)
            if previous != question.faq_item_id:
                raise ValueError(f"FAQ 精确键 {question.normalized_question} 指向不同条目")

    # 检查 FAQ 答案是否绑定 FAQ 条目，并检查有效期是否倒置
    for answer in answers:
        if (answer.faq_item_id, answer.item_version) not in item_by_ref:
            raise ValueError(f"FAQ 答案 {answer.answer_id} 未绑定清单中的条目")
        answer_counts[(answer.faq_item_id, answer.item_version)] += 1
        if (
            answer.effective_from
            and answer.effective_to
            and answer.effective_to < answer.effective_from
        ):
            raise ValueError(f"FAQ 答案 {answer.answer_id} 有效期倒置")

    # 检查 FAQ 答案是否存在时间重叠
    for index, left in enumerate(answers):
        for right in answers[index + 1 :]:
            if (left.faq_item_id, left.item_version) != (right.faq_item_id, right.item_version):
                continue
            if not all(
                _overlap(getattr(left, field), getattr(right, field))
                for field in ("product_codes", "channels", "regions")
            ):
                continue
            if (
                left.effective_to is None
                or right.effective_from is None
                or left.effective_to >= right.effective_from
            ) and (
                right.effective_to is None
                or left.effective_from is None
                or right.effective_to >= left.effective_from
            ):
                raise ValueError(
                    f"FAQ 答案 {left.answer_id} 与 {right.answer_id} 适用范围和时间重叠"
                )
    for item_ref, item in item_by_ref.items():
        if question_counts[item_ref] == 0:
            raise ValueError(f"FAQ 条目 {item.faq_item_id} 没有任何问题表述")
        if exact_question_counts[item_ref] == 0:
            raise ValueError(f"FAQ 条目 {item.faq_item_id} 没有可用于 Redis 的精确问题")
        if answer_counts[item_ref] == 0:
            raise ValueError(f"FAQ 条目 {item.faq_item_id} 没有固定答案")


def _asset(
    asset_type: ContentAssetType, asset_id: str, version: str, payload: dict[str, object]
) -> ContentAssetVersion:
    """计算内容指纹，用于识别同一版本的数据是否发生变化。"""

    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return ContentAssetVersion(
        asset_type=asset_type,
        asset_id=asset_id,
        version=version,
        lifecycle_status=ContentLifecycleStatus.APPROVED,
        content_hash=hashlib.sha256(encoded).hexdigest(),
    )


async def publish_intents(
    session: AsyncSession,
    redis: Redis,
    *,
    release_version: str,
    source_ref: str,
    approved_by: str,
) -> ContentPublicationManifest:
    """把一批已审核规则复制到 Redis，并选择它作为当前使用版本。

    PostgreSQL 长期保存规则及历史，Redis 只是读取更快的副本。如果数据库记录
    成功但 Redis 切换失败，运行时仍使用上一版规则，可以用同一版本号重新执行。
    当前主链路不调用本函数；它用于受控的动态规则发布。
    """

    existing = await session.get(IntentReleaseModel, release_version)
    if existing is not None:
        # 再次选择旧版本时，只使用 Redis 中已经存在的完整规则副本。
        if not await redis.exists(intent_release_key(release_version)):
            raise ValueError("该版本的 Redis 规则副本已丢失，请先从 PostgreSQL 重建")
        await redis.set(INTENT_ACTIVE_KEY, release_version)
        return ContentPublicationManifest.model_validate(existing.manifest)

    intents = list(
        (
            await session.scalars(
                select(IntentDefinitionModel).where(IntentDefinitionModel.source_ref == source_ref)
            )
        ).all()
    )
    rules = list(
        (
            await session.scalars(
                select(IntentRuleModel).where(IntentRuleModel.source_ref == source_ref)
            )
        ).all()
    )
    check_intent_conflicts(intents, rules)
    # FAQ 此时仍为草稿：这里只提前发现错误，不把答案复制到线上查询缓存。
    groups = list(
        (
            await session.scalars(
                select(FaqGroupModel).where(
                    FaqGroupModel.source_ref == source_ref,
                    FaqGroupModel.lifecycle_status != "RETIRED",
                )
            )
        ).all()
    )
    items = list(
        (
            await session.scalars(
                select(FaqItemModel).where(
                    FaqItemModel.source_ref == source_ref,
                    FaqItemModel.lifecycle_status != "RETIRED",
                )
            )
        ).all()
    )
    if groups or items:
        item_refs = [(item.faq_item_id, item.version) for item in items]
        questions = list(
            (
                await session.scalars(
                    select(FaqQuestionModel).where(
                        tuple_(FaqQuestionModel.faq_item_id, FaqQuestionModel.item_version).in_(
                            item_refs
                        )
                    )
                )
            ).all()
        )
        answers = list(
            (
                await session.scalars(
                    select(FaqAnswerModel).where(
                        tuple_(FaqAnswerModel.faq_item_id, FaqAnswerModel.item_version).in_(
                            item_refs
                        )
                    )
                )
            ).all()
        )
        check_faq_conflicts(intents, groups, items, questions, answers)

    catalog = [
        {
            "intent_id": i.intent_id,
            "version": i.version,
            "intent_class": i.intent_class,
            "route_target": i.route_target,
            "request_kind": i.request_kind,
            "enabled": i.enabled,
            "source_ref": i.source_ref,
        }
        for i in intents
    ]
    rule_data = [
        {
            "rule_id": r.rule_id,
            "version": r.version,
            "rule_kind": r.rule_kind,
            "intent_id": r.intent_id,
            "intent_version": r.intent_version,
            "pattern": r.pattern,
            "match_mode": r.match_mode,
            "negative_patterns": r.negative_patterns,
            "priority": r.priority,
            "product_codes": r.product_codes,
            "channels": r.channels,
            "regions": r.regions,
            "required_capability": r.required_capability,
            "control_action": r.control_action,
            "source_ref": r.source_ref,
        }
        for r in rules
    ]
    now = datetime.now(UTC)
    assets = tuple(
        [
            _asset(ContentAssetType.INTENT_DEFINITION, i.intent_id, i.version, data)
            for i, data in zip(intents, catalog, strict=True)
        ]
        + [
            _asset(ContentAssetType.INTENT_RULE, r.rule_id, r.version, data)
            for r, data in zip(rules, rule_data, strict=True)
        ]
    )
    manifest = ContentPublicationManifest(
        publication_id=f"intent-{release_version}",
        release_version=release_version,
        source_revision=source_ref,
        status=PublicationStatus.PUBLISHED,
        assets=assets,
        conflict_check=ContentConflictCheck(
            status=ContentConflictStatus.PASSED,
            checker_version="content-conflict-v1",
            checked_at=now,
        ),
        projection_builds=tuple(
            [
                ProjectionBuild(
                    target=ProjectionTarget.REDIS_INTENT_CATALOG,
                    projection_version=release_version,
                    asset_refs=tuple(
                        a.asset_ref
                        for a in assets
                        if a.asset_type is ContentAssetType.INTENT_DEFINITION
                    ),
                )
            ]
            if intents
            else []
        )
        + (
            ProjectionBuild(
                target=ProjectionTarget.REDIS_INTENT_RULES,
                projection_version=release_version,
                asset_refs=tuple(
                    a.asset_ref for a in assets if a.asset_type is ContentAssetType.INTENT_RULE
                ),
            ),
        ),
        approved_by=approved_by,
        approved_at=now,
        published_at=now,
    )
    payload = json.dumps(
        {"release_version": release_version, "catalog": catalog, "rules": rule_data},
        ensure_ascii=False,
        sort_keys=True,
    )
    release_key = intent_release_key(release_version)
    prior_payload = await redis.get(release_key)
    if prior_payload is not None and prior_payload.decode() != payload:
        raise ValueError("Redis 中已有同名但内容不同的规则副本，不可覆盖")
    if prior_payload is None and not await redis.set(release_key, payload, nx=True):
        raise ValueError("另一个发布者已占用同名版本，请重新检查内容")
    session.add(
        IntentReleaseModel(
            release_version=release_version,
            manifest=manifest.model_dump(mode="json"),
            published_at=now,
        )
    )
    for item in [*intents, *rules]:
        item.lifecycle_status = "PUBLISHED"
        item.release_version = release_version
    await session.commit()
    await redis.set(INTENT_ACTIVE_KEY, release_version)
    return manifest


async def activate_release(session: AsyncSession, redis: Redis, release_version: str) -> None:
    """选择一批已有规则供进阶方案使用；不删除其他历史版本。"""

    if await session.get(IntentReleaseModel, release_version) is None:
        raise ValueError("PostgreSQL 中不存在该发布版本")
    if not await redis.exists(intent_release_key(release_version)):
        raise ValueError("Redis 中不存在该版本的规则副本")
    await redis.set(INTENT_ACTIVE_KEY, release_version)


async def approve_faq_source(session: AsyncSession, *, source_ref: str, approved_by: str) -> None:
    """显式批准一批已经人工核对的 FAQ，不在种子脚本中自动批准。"""

    intents, groups, items, questions, answers = await _load_faq_source(session, source_ref)
    check_faq_conflicts(intents, groups, items, questions, answers)
    if not approved_by.strip():
        raise ValueError("FAQ 批准人不能为空")
    for intent in intents:
        if intent.lifecycle_status != "PUBLISHED":
            intent.lifecycle_status = "APPROVED"
        intent.enabled = True
        if intent.approved_at is None:
            intent.approved_by = approved_by
            intent.approved_at = datetime.now(UTC)
    for item in [*groups, *items]:
        if item.lifecycle_status != "PUBLISHED":
            item.lifecycle_status = "APPROVED"
    await session.commit()


async def publish_faq(
    session: AsyncSession,
    redis: Redis,
    *,
    release_version: str,
    source_ref: str,
    approved_by: str,
) -> ContentPublicationManifest:
    """把已批准 FAQ 复制到 Redis，并原子切换当前查询版本。

    PostgreSQL 始终保存完整内容和发布记录。Redis 只保存运行时需要的
    “标准问题到意图”“意图到分组”和“分组内问题到答案包”三个查询结构，
    丢失后可从 PostgreSQL 重建。
    """

    existing = await session.get(FaqReleaseModel, release_version)
    if existing is not None and existing.source_ref != source_ref:
        raise ValueError("同一个 FAQ 发布版本不能指向另一批内容")

    intents, groups, items, questions, answers = await _load_faq_source(session, source_ref)
    if not intents or not groups or not items:
        raise ValueError("FAQ 发布内容不能为空")
    if any(
        item.lifecycle_status not in {"APPROVED", "PUBLISHED"}
        for item in [*intents, *groups, *items]
    ) or any(not intent.enabled for intent in intents):
        raise ValueError("FAQ 意图、分组和条目必须先审核通过")
    check_faq_conflicts(intents, groups, items, questions, answers)

    manifest, exact_routes, item_entries = _build_faq_release(
        release_version=release_version,
        source_ref=source_ref,
        approved_by=approved_by,
        intents=intents,
        groups=groups,
        items=items,
        questions=questions,
        answers=answers,
    )
    if existing is not None:
        stored = ContentPublicationManifest.model_validate(existing.manifest)
        # 数据库没有承诺返回顺序；同一批资产只要标识和内容一致，就仍是同一发布版。
        # 若直接比较元组，完全相同的资产仅因查询顺序变化也会被误判成内容被篡改。
        stored_assets = {asset.asset_ref: asset for asset in stored.assets}
        current_assets = {asset.asset_ref: asset for asset in manifest.assets}
        if stored_assets != current_assets or stored.source_revision != manifest.source_revision:
            raise ValueError("PostgreSQL 中的同名 FAQ 发布版本与当前内容不同")
        await _write_faq_cache(redis, stored, intents, groups, exact_routes, item_entries)
        for intent in intents:
            intent.lifecycle_status = "PUBLISHED"
            intent.release_version = release_version
        for item in [*groups, *items]:
            item.lifecycle_status = "PUBLISHED"
        await session.commit()
        await redis.set(FAQ_ACTIVE_KEY, release_version)
        return stored

    if await redis.exists(faq_release_meta_key(release_version)):
        raise ValueError("Redis 中已有未登记的同名 FAQ 副本，请先核对后处理")
    await _write_faq_cache(redis, manifest, intents, groups, exact_routes, item_entries)
    session.add(
        FaqReleaseModel(
            release_version=release_version,
            source_ref=source_ref,
            manifest=manifest.model_dump(mode="json"),
            published_at=manifest.published_at,
        )
    )
    for intent in intents:
        intent.lifecycle_status = "PUBLISHED"
        intent.release_version = release_version
    for item in [*groups, *items]:
        item.lifecycle_status = "PUBLISHED"
    await session.commit()
    # 最后切换活动指针。即使前面失败，运行时也会继续读取上一版。
    await redis.set(FAQ_ACTIVE_KEY, release_version)
    return manifest


async def activate_faq_release(session: AsyncSession, redis: Redis, release_version: str) -> None:
    """切换到已经登记且 Redis 副本完整存在的 FAQ 版本。"""

    if await session.get(FaqReleaseModel, release_version) is None:
        raise ValueError("PostgreSQL 中不存在该 FAQ 发布版本")
    if not await redis.exists(faq_release_meta_key(release_version)):
        raise ValueError("Redis 中缺少该 FAQ 副本，请先运行重建发布命令")
    await redis.set(FAQ_ACTIVE_KEY, release_version)


async def _load_faq_source(
    session: AsyncSession, source_ref: str
) -> tuple[
    list[IntentDefinitionModel],
    list[FaqGroupModel],
    list[FaqItemModel],
    list[FaqQuestionModel],
    list[FaqAnswerModel],
]:
    """一次读出同一来源的 FAQ 四表内容，避免发布时混入其他版本。"""

    intents = list(
        (
            await session.scalars(
                select(IntentDefinitionModel).where(IntentDefinitionModel.source_ref == source_ref)
            )
        ).all()
    )
    groups = list(
        (
            await session.scalars(
                select(FaqGroupModel).where(
                    FaqGroupModel.source_ref == source_ref,
                    FaqGroupModel.lifecycle_status != "RETIRED",
                )
            )
        ).all()
    )
    items = list(
        (
            await session.scalars(
                select(FaqItemModel).where(
                    FaqItemModel.source_ref == source_ref,
                    FaqItemModel.lifecycle_status != "RETIRED",
                )
            )
        ).all()
    )
    item_refs = [(item.faq_item_id, item.version) for item in items]
    if not item_refs:
        return intents, groups, items, [], []
    questions = list(
        (
            await session.scalars(
                select(FaqQuestionModel).where(
                    tuple_(FaqQuestionModel.faq_item_id, FaqQuestionModel.item_version).in_(
                        item_refs
                    )
                )
            )
        ).all()
    )
    answers = list(
        (
            await session.scalars(
                select(FaqAnswerModel).where(
                    tuple_(FaqAnswerModel.faq_item_id, FaqAnswerModel.item_version).in_(item_refs)
                )
            )
        ).all()
    )
    return intents, groups, items, questions, answers


def _build_faq_release(
    *,
    release_version: str,
    source_ref: str,
    approved_by: str,
    intents: list[IntentDefinitionModel],
    groups: list[FaqGroupModel],
    items: list[FaqItemModel],
    questions: list[FaqQuestionModel],
    answers: list[FaqAnswerModel],
) -> tuple[
    ContentPublicationManifest,
    dict[tuple[str, str], str],
    dict[tuple[str, str], str],
]:
    """构造发布清单、标准问题路由线索和按条目读取的答案包。"""

    answers_by_item: dict[tuple[str, str], list[FaqAnswerModel]] = {}
    for answer in answers:
        answers_by_item.setdefault((answer.faq_item_id, answer.item_version), []).append(answer)
    questions_by_item: dict[tuple[str, str], list[FaqQuestionModel]] = {}
    for question in questions:
        questions_by_item.setdefault((question.faq_item_id, question.item_version), []).append(
            question
        )
    intent_by_group_ref = {(group.faq_group_id, group.version): group.intent_id for group in groups}
    assets: list[ContentAssetVersion] = []
    for group in groups:
        assets.append(
            _asset(
                ContentAssetType.FAQ_GROUP,
                group.faq_group_id,
                group.version,
                {
                    "intent_id": group.intent_id,
                    "intent_version": group.intent_version,
                    "name": group.name,
                },
            )
        )
    exact_routes: dict[tuple[str, str], str] = {}
    item_entries: dict[tuple[str, str], str] = {}
    for item in items:
        item_questions = questions_by_item.get((item.faq_item_id, item.version), [])
        item_answers = answers_by_item.get((item.faq_item_id, item.version), [])
        answer_data = [
            {
                "answer_id": answer.answer_id,
                "answer": answer.answer,
                "product_codes": answer.product_codes,
                "channels": answer.channels,
                "regions": answer.regions,
                "effective_from": (
                    answer.effective_from.isoformat() if answer.effective_from else None
                ),
                "effective_to": answer.effective_to.isoformat() if answer.effective_to else None,
                "source_ref": answer.source_ref,
                "content_hash": hashlib.sha256(
                    json.dumps(
                        {
                            "answer": answer.answer,
                            "product_codes": answer.product_codes,
                            "channels": answer.channels,
                            "regions": answer.regions,
                            "effective_from": (
                                answer.effective_from.isoformat() if answer.effective_from else None
                            ),
                            "effective_to": (
                                answer.effective_to.isoformat() if answer.effective_to else None
                            ),
                            "source_ref": answer.source_ref,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest(),
            }
            for answer in item_answers
        ]
        item_payload = {
            "faq_item_id": item.faq_item_id,
            "faq_group_id": item.faq_group_id,
            "faq_version": item.version,
            "title": item.title,
            "questions": [
                {
                    "variant_id": question.variant_id,
                    "question": question.question,
                    "locale": question.locale,
                    "exact_match_enabled": question.exact_match_enabled,
                }
                for question in item_questions
            ],
            "answers": answer_data,
        }
        item_hash = hashlib.sha256(
            json.dumps(item_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        item_payload["content_hash"] = item_hash
        item_entries[(item.faq_item_id, item.version)] = json.dumps(
            item_payload, ensure_ascii=False, sort_keys=True
        )
        assets.append(
            _asset(
                ContentAssetType.FAQ_ITEM,
                item.faq_item_id,
                item.version,
                item_payload,
            )
        )
        for question in item_questions:
            if not question.exact_match_enabled:
                continue
            digest = faq_question_digest(question.normalized_question)
            exact_routes[(question.locale, digest)] = FaqExactHint(
                intent_id=intent_by_group_ref[(item.faq_group_id, item.group_version)],
                faq_group_id=item.faq_group_id,
                faq_item_id=item.faq_item_id,
                faq_version=item.version,
                winning_variant_id=question.variant_id,
                release_version=release_version,
                locale=question.locale,
                query_digest=digest,
            ).model_dump_json()

    now = datetime.now(UTC)
    item_refs = tuple(
        asset.asset_ref for asset in assets if asset.asset_type is ContentAssetType.FAQ_ITEM
    )
    manifest = ContentPublicationManifest(
        publication_id=f"faq-{release_version}",
        release_version=release_version,
        source_revision=source_ref,
        status=PublicationStatus.PUBLISHED,
        assets=tuple(assets),
        conflict_check=ContentConflictCheck(
            status=ContentConflictStatus.PASSED,
            checker_version="faq-exact-v1",
            checked_at=now,
        ),
        projection_builds=(
            ProjectionBuild(
                target=ProjectionTarget.REDIS_FAQ_EXACT,
                projection_version=release_version,
                asset_refs=item_refs,
            ),
        ),
        approved_by=approved_by,
        approved_at=now,
        published_at=now,
    )
    return manifest, exact_routes, item_entries


async def _write_faq_cache(
    redis: Redis,
    manifest: ContentPublicationManifest,
    intents: list[IntentDefinitionModel],
    groups: list[FaqGroupModel],
    exact_routes: dict[tuple[str, str], str],
    item_entries: dict[tuple[str, str], str],
) -> None:
    """用一次 Redis 事务写完意图目录、标准问题路由线索和按条目查询的答案包。"""

    release_version = manifest.release_version
    pipeline = redis.pipeline(transaction=True)
    pipeline.delete(faq_intent_catalog_key(release_version))
    pipeline.delete(faq_intent_groups_key(release_version))
    pipeline.delete(faq_question_intents_key(release_version))
    pipeline.delete(faq_items_key(release_version))
    intent_by_group = {group.faq_group_id: group.intent_id for group in groups}
    coverage_by_intent: dict[str, list[str]] = {}
    for raw_item in item_entries.values():
        item_payload = json.loads(raw_item)
        intent_id = intent_by_group.get(str(item_payload["faq_group_id"]))
        if intent_id is not None:
            coverage_by_intent.setdefault(intent_id, []).append(str(item_payload["title"]))
    # 意图目录是 Runtime 路由的唯一动作清单来源；Python 不再维护一份副本。
    for intent in intents:
        pipeline.hset(
            faq_intent_catalog_key(release_version),
            intent.intent_id,
            json.dumps(
                {
                    "intent_id": intent.intent_id,
                    "version": intent.version,
                    "name": intent.name,
                    "description": intent.description,
                    "coverage_summary": "；".join(coverage_by_intent.get(intent.intent_id, [])),
                    "intent_class": intent.intent_class,
                    "route_target": intent.route_target,
                    "request_kind": intent.request_kind,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    for group in groups:
        pipeline.hset(faq_intent_groups_key(release_version), group.intent_id, group.faq_group_id)
    # 当前结构不再按“分组 + 同一问题哈希”二次匹配；删除本版本的旧查询副本。
    legacy_group_keys = {
        faq_group_entries_key(release_version, group.faq_group_id, locale)
        for group in groups
        for locale, _digest in exact_routes
    }
    for key in legacy_group_keys:
        pipeline.delete(key)
    for (locale, digest), hint in exact_routes.items():
        pipeline.hset(
            faq_question_intents_key(release_version),
            f"{locale}:{digest}",
            hint,
        )
    for (faq_item_id, faq_version), payload in item_entries.items():
        pipeline.hset(
            faq_items_key(release_version),
            faq_item_field(faq_item_id, faq_version),
            payload,
        )
    pipeline.set(
        faq_release_meta_key(release_version),
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
    )
    await pipeline.execute()
