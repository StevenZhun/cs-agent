"""核对权威内容、活动指针和语义索引是否形成可用基线。"""

from __future__ import annotations

from contextlib import closing

import psycopg
from pymilvus import MilvusClient
from redis import Redis

from scripts.bootstrap.plan import (
    FAQ_RELEASE_VERSION,
    INTENT_RELEASE_VERSION,
    POLICY_ANSWER_RELEASE_VERSION,
    SOP_RELEASE_VERSION,
)
from services.content.faq_cache import FAQ_ACTIVE_KEY
from services.content.intent_cache import INTENT_ACTIVE_KEY
from services.content.policy_answers import POLICY_ANSWER_ACTIVE_KEY
from services.content.semantic_index import collection_names
from shared.sop_config import SOP_ACTIVE_KEY
from shared.web.settings import ServiceSettings


def is_initialized(settings: ServiceSettings) -> bool:
    """确认四类发布内容和意图、FAQ 两个向量索引可以复用。"""

    database_url = settings.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        with psycopg.connect(database_url) as connection:
            row = connection.execute(
                """
                SELECT
                    EXISTS (SELECT 1 FROM content.faq_release WHERE release_version = %s),
                    EXISTS (SELECT 1 FROM content.intent_release WHERE release_version = %s),
                    EXISTS (
                        SELECT 1 FROM content.policy_answer_release WHERE release_version = %s
                    ),
                    EXISTS (SELECT 1 FROM content.sop_release WHERE release_version = %s),
                    (SELECT manifest->>'intent_count'
                       FROM content.semantic_index_build
                      WHERE index_version = %s),
                    (SELECT manifest->>'faq_count'
                       FROM content.semantic_index_build
                      WHERE index_version = %s)
                """,
                (
                    FAQ_RELEASE_VERSION,
                    INTENT_RELEASE_VERSION,
                    POLICY_ANSWER_RELEASE_VERSION,
                    SOP_RELEASE_VERSION,
                    settings.semantic_index_version,
                    settings.semantic_index_version,
                ),
            ).fetchone()
        if row is None or not all(row[:4]) or any(value is None for value in row[4:]):
            return False

        expected_pointers = {
            FAQ_ACTIVE_KEY: FAQ_RELEASE_VERSION,
            INTENT_ACTIVE_KEY: INTENT_RELEASE_VERSION,
            POLICY_ANSWER_ACTIVE_KEY: POLICY_ANSWER_RELEASE_VERSION,
            SOP_ACTIVE_KEY: SOP_RELEASE_VERSION,
        }
        with closing(Redis.from_url(settings.redis_url, decode_responses=True)) as redis:
            if any(redis.get(key) != value for key, value in expected_pointers.items()):
                return False

        names = collection_names(settings.semantic_index_version)
        client = MilvusClient(uri=settings.milvus_uri)
        try:
            if not client.has_collection(names.intent) or not client.has_collection(names.faq):
                return False
            intent_count = int(client.get_collection_stats(names.intent)["row_count"])
            faq_count = int(client.get_collection_stats(names.faq)["row_count"])
        finally:
            client.close()
        return intent_count == int(row[4]) and faq_count == int(row[5])
    except Exception as exc:  # noqa: BLE001
        print(f"[初始化] 既有环境就绪核验未通过，将执行完整初始化：{exc}", flush=True)
        return False
