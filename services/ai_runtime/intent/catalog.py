"""从已发布的 FAQ 投影读取全局意图目录；Runtime 不在代码中维护意图清单。"""

from __future__ import annotations

import json

from redis.asyncio import Redis
from redis.exceptions import RedisError

from services.content.faq_cache import FAQ_ACTIVE_KEY, faq_intent_catalog_key
from services.content.intent_cache import INTENT_ACTIVE_KEY, intent_release_key
from shared.contracts.routing import IntentRule

CATALOG_FIELDS = (
    "intent_id",
    "version",
    "name",
    "description",
    "coverage_summary",
    "intent_class",
    "route_target",
    "request_kind",
)


async def load_intent_catalog(redis: Redis | None) -> dict[str, dict[str, str]]:
    """读取当前活动 FAQ 发布版本携带的意图目录；读不到时返回空目录，不用内置清单兜底。"""

    if redis is None or not hasattr(redis, "hgetall"):
        return {}
    try:
        active = await redis.get(FAQ_ACTIVE_KEY)
        if active is None:
            return {}
        release_version = active.decode() if isinstance(active, bytes) else str(active)
        raw = await redis.hgetall(faq_intent_catalog_key(release_version))
    except RedisError:
        return {}
    catalog: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        intent_id = key.decode() if isinstance(key, bytes) else str(key)
        try:
            payload = json.loads(value)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            continue
        if not isinstance(payload, dict) or any(field not in payload for field in CATALOG_FIELDS):
            continue
        if payload["intent_id"] != intent_id:
            continue
        catalog[intent_id] = {field: str(payload[field]) for field in CATALOG_FIELDS}
    return catalog


async def load_catalog_node(state: dict, redis: Redis | None = None) -> dict:
    """目录变化时才更新图状态，避免每轮重复快照同一不可变发布内容。"""

    release_version, rules = await load_intent_rules(redis)
    catalog = await load_intent_catalog(redis)
    updates = {}
    if state.get("intent_catalog") != catalog:
        updates["intent_catalog"] = catalog
    if state.get("intent_rule_release") != release_version:
        updates["intent_rule_release"] = release_version
    if state.get("intent_rules") != rules:
        updates["intent_rules"] = rules
    return updates


async def load_intent_rules(redis: Redis | None) -> tuple[str | None, list[dict[str, object]]]:
    """读取活动规则快照；缺失、损坏或版本错配时不使用 Python 词表兜底。"""

    if redis is None or not hasattr(redis, "get"):
        return None, []
    try:
        active = await redis.get(INTENT_ACTIVE_KEY)
        if active is None:
            return None, []
        release_version = active.decode() if isinstance(active, bytes) else str(active)
        raw = await redis.get(intent_release_key(release_version))
    except RedisError:
        return None, []
    if raw is None:
        return None, []
    try:
        payload = json.loads(raw)
        if payload.get("release_version") != release_version or not isinstance(
            payload.get("rules"), list
        ):
            return None, []
        rules = [
            {
                **IntentRule.model_validate(item).model_dump(mode="json"),
                "release_version": release_version,
            }
            for item in payload["rules"]
        ]
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        return None, []
    return release_version, rules
