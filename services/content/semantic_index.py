"""把 PostgreSQL 中已审核的语义内容构建成可重建的 Milvus 索引。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pymilvus import DataType, MilvusClient

from shared.semantic_embedding import EmbeddingProfile

# 显式固定索引参数，便于课堂复现和不同索引方案的公平对比。
VECTOR_INDEX_TYPE = "HNSW"
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 200
HNSW_EF_SEARCH = 64


@dataclass(frozen=True)
class SemanticCollectionNames:
    """一次索引构建生成的两个逻辑隔离 Collection 名称。"""

    intent: str
    faq: str


def collection_names(index_version: str) -> SemanticCollectionNames:
    """把可读版本号转换成 Milvus 可接受且稳定的 Collection 名称。"""

    suffix = re.sub(r"[^a-z0-9_]", "_", index_version.lower())
    return SemanticCollectionNames(
        intent=f"intent_similarities_{suffix}",
        faq=f"faq_question_variants_{suffix}",
    )


def rebuild_collections(
    client: MilvusClient,
    *,
    names: SemanticCollectionNames,
    profile: EmbeddingProfile,
    intent_rows: list[dict[str, object]],
    faq_rows: list[dict[str, object]],
) -> dict[str, int]:
    """重建两个离线 Collection；相同版本重复执行会得到相同内容。"""

    _recreate_intent_collection(client, names.intent, profile)
    _recreate_faq_collection(client, names.faq, profile)
    if intent_rows:
        client.insert(collection_name=names.intent, data=intent_rows)
    if faq_rows:
        client.insert(collection_name=names.faq, data=faq_rows)
    client.flush(collection_name=names.intent)
    client.flush(collection_name=names.faq)
    return {
        "intent": int(client.get_collection_stats(names.intent)["row_count"]),
        "faq": int(client.get_collection_stats(names.faq)["row_count"]),
    }


def _recreate_intent_collection(
    client: MilvusClient, collection_name: str, profile: EmbeddingProfile
) -> None:
    """建立字段明确、使用 IP 相似度的相似意图语料 Collection。"""

    if client.has_collection(collection_name):
        client.drop_collection(collection_name)
    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("record_id", DataType.VARCHAR, is_primary=True, max_length=128)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=profile.dimension)
    schema.add_field("intent_id", DataType.VARCHAR, max_length=64)
    schema.add_field("intent_version", DataType.VARCHAR, max_length=32)
    schema.add_field("text", DataType.VARCHAR, max_length=4096)
    schema.add_field("locale", DataType.VARCHAR, max_length=16)
    schema.add_field("source_ref", DataType.VARCHAR, max_length=255)
    schema.add_field("embedding_profile_id", DataType.VARCHAR, max_length=64)
    schema.add_field("index_version", DataType.VARCHAR, max_length=64)
    _create_indexed_collection(client, collection_name, schema)


def _recreate_faq_collection(
    client: MilvusClient, collection_name: str, profile: EmbeddingProfile
) -> None:
    """建立保留 FAQ 条目、分组与全局意图关系的问法 Collection。"""

    if client.has_collection(collection_name):
        client.drop_collection(collection_name)
    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("record_id", DataType.VARCHAR, is_primary=True, max_length=128)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=profile.dimension)
    schema.add_field("faq_item_id", DataType.VARCHAR, max_length=64)
    schema.add_field("item_version", DataType.VARCHAR, max_length=32)
    schema.add_field("faq_group_id", DataType.VARCHAR, max_length=64)
    schema.add_field("intent_id", DataType.VARCHAR, max_length=64)
    schema.add_field("question", DataType.VARCHAR, max_length=4096)
    schema.add_field("locale", DataType.VARCHAR, max_length=16)
    schema.add_field("source_ref", DataType.VARCHAR, max_length=255)
    schema.add_field("embedding_profile_id", DataType.VARCHAR, max_length=64)
    schema.add_field("index_version", DataType.VARCHAR, max_length=64)
    _create_indexed_collection(client, collection_name, schema)


def _create_indexed_collection(client: MilvusClient, collection_name: str, schema: object) -> None:
    """给显式 Schema 添加统一的 HNSW/IP 向量索引并立即加载。"""

    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="vector",
        index_type=VECTOR_INDEX_TYPE,
        metric_type="IP",
        params={"M": HNSW_M, "efConstruction": HNSW_EF_CONSTRUCTION},
    )
    client.create_collection(
        collection_name=collection_name,
        schema=schema,
        index_params=index_params,
        consistency_level="Strong",
    )
