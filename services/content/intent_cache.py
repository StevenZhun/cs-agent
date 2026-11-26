"""已发布意图规则在 Redis 中使用的稳定键名。"""

INTENT_ACTIVE_KEY = "content:intent:active"
INTENT_RELEASE_PREFIX = "content:intent:release:"


def intent_release_key(release_version: str) -> str:
    """返回包含意图目录和规则快照的不可变发布键。"""

    return f"{INTENT_RELEASE_PREFIX}{release_version}"
