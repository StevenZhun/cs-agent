"""FAQ 精确查询副本在 Redis 中使用的稳定键名。"""

from __future__ import annotations

import hashlib

FAQ_ACTIVE_KEY = "content:faq:active"
FAQ_RELEASE_PREFIX = "content:faq:release"


def faq_release_meta_key(release_version: str) -> str:
    """返回一批 FAQ 副本的发布信息键。"""

    return f"{FAQ_RELEASE_PREFIX}:{release_version}:meta"


def faq_intent_groups_key(release_version: str) -> str:
    """返回全局意图到 FAQ 分组的 Redis Hash 键。"""

    return f"{FAQ_RELEASE_PREFIX}:{release_version}:intent-groups"


def faq_intent_catalog_key(release_version: str) -> str:
    """返回全局意图编号到分类、路由目标、请求形态和名称的 Redis Hash 键。"""

    return f"{FAQ_RELEASE_PREFIX}:{release_version}:intent-catalog"


def faq_question_intents_key(release_version: str) -> str:
    """返回标准问题哈希到意图及 FAQ 条目线索的 Redis Hash 键。"""

    return f"{FAQ_RELEASE_PREFIX}:{release_version}:question-intents"


def faq_items_key(release_version: str) -> str:
    """返回 FAQ 条目版本到完整答案包的 Redis Hash 键。"""

    return f"{FAQ_RELEASE_PREFIX}:{release_version}:items"


def faq_item_field(faq_item_id: str, faq_version: str) -> str:
    """返回包含条目稳定标识和内容版本的 Redis 字段。"""

    return f"{faq_item_id}:{faq_version}"


def faq_group_entries_key(release_version: str, faq_group_id: str, locale: str) -> str:
    """返回旧版分组精确问题键，仅用于重建缓存时删除旧副本。"""

    return f"{FAQ_RELEASE_PREFIX}:{release_version}:group:{faq_group_id}:{locale}"


def faq_question_digest(normalized_question: str) -> str:
    """把规范化问题转成固定长度键，避免长问题直接出现在 Redis 键中。"""

    return hashlib.sha256(normalized_question.encode("utf-8")).hexdigest()


def faq_question_intent_field(locale: str, normalized_question: str) -> str:
    """返回带语言标识的标准问题字段，避免不同语言意外覆盖。"""

    return f"{locale}:{faq_question_digest(normalized_question)}"
