"""高风险策略答案的 Redis 发布快照与一致性校验。"""

from __future__ import annotations

import hashlib
import json

from redis.asyncio import Redis

POLICY_ANSWER_ACTIVE_KEY = "content:policy-answer:active"
POLICY_ANSWER_RELEASE_PREFIX = "content:policy-answer:release"


def policy_answer_release_key(release_version: str) -> str:
    """返回一个不可变策略答案发布快照的 Redis 键。"""

    return f"{POLICY_ANSWER_RELEASE_PREFIX}:{release_version}"


def policy_answer_content_hash(answer_text: str) -> str:
    """计算客户可见策略正文的稳定 SHA-256。"""

    return hashlib.sha256(answer_text.encode("utf-8")).hexdigest()


def policy_answer_release_content_hash(answers: dict[str, object]) -> str:
    """计算整批意图到策略答案映射的稳定 SHA-256。"""

    encoded = json.dumps(answers, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_policy_answer_release(
    payload: object,
    *,
    expected_release_version: str,
) -> dict[str, object]:
    """拒绝版本错配、字段缺失或正文被篡改的发布快照。"""

    if not isinstance(payload, dict) or payload.get("release_version") != expected_release_version:
        raise ValueError("策略答案发布版本不一致")
    answers = payload.get("answers")
    if not isinstance(answers, dict) or not answers:
        raise ValueError("策略答案发布快照为空")
    if payload.get("content_hash") != policy_answer_release_content_hash(answers):
        raise ValueError("策略答案发布快照内容哈希不一致")
    for intent_id, raw_answer in answers.items():
        if not isinstance(raw_answer, dict) or raw_answer.get("intent_id") != intent_id:
            raise ValueError("策略答案意图索引不一致")
        answer_text = raw_answer.get("answer_text")
        answer_version = raw_answer.get("policy_answer_version")
        if not isinstance(answer_text, str) or not answer_text.strip() or not answer_version:
            raise ValueError("策略答案正文或版本缺失")
        if raw_answer.get("answer_content_hash") != policy_answer_content_hash(answer_text):
            raise ValueError("策略答案正文内容哈希不一致")
    return payload


async def load_policy_answer_release(
    redis: Redis | None,
    release_version: str | None = None,
) -> dict[str, object] | None:
    """读取并校验活动或指定发布快照；缺少缓存时不使用代码文案兜底。"""

    if redis is None or not hasattr(redis, "get"):
        return None
    version = release_version
    if version is None:
        active = await redis.get(POLICY_ANSWER_ACTIVE_KEY)
        if active is None:
            return None
        version = active.decode() if isinstance(active, bytes) else str(active)
    raw = await redis.get(policy_answer_release_key(version))
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as error:
        raise ValueError("策略答案发布快照无法解析") from error
    return validate_policy_answer_release(payload, expected_release_version=version)


async def load_policy_answer(
    redis: Redis | None,
    intent_id: str,
    *,
    release_version: str | None = None,
) -> dict[str, object] | None:
    """按 canonical intent_id 读取唯一策略答案，不进行 FAQ 或向量检索。"""

    release = await load_policy_answer_release(redis, release_version)
    if release is None:
        return None
    answer = release["answers"].get(intent_id)
    if not isinstance(answer, dict):
        return None
    return {**answer, "release_version": release["release_version"]}
