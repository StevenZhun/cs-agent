"""把已冻结的 RAG 发布清单构建成 Milvus 双路（稠密 + BM25）可重建索引。

PostgreSQL `content.rag_release` 的清单是权威快照；本模块只把清单里的子块
投影成一个 Milvus Collection。Collection 丢失后可用同一清单重建，构建结果
写回清单 `index_status`，不改动知识正文或审核记录。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pymilvus import DataType, Function, FunctionType, MilvusClient

from shared.contracts.rag import RagDocumentBundle
from shared.semantic_embedding import EmbeddingProfile

VECTOR_INDEX_TYPE = "HNSW"
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 200
HNSW_EF_SEARCH = 64
# 中文分析器负责分词；BM25 由 Milvus 在插入时从 text 字段计算稀疏向量，
# 构建脚本不自己维护词表，也不把第三方 pickle 词典带进进程。
TEXT_ANALYZER_PARAMS = {"type": "chinese"}


def rag_collection_name(release_version: str) -> str:
    """把发布号转换成 Milvus 可接受且稳定的 Collection 名。"""

    suffix = re.sub(r"[^a-z0-9_]", "_", release_version.lower())
    return f"rag_chunks_{suffix}"


@dataclass(frozen=True)
class RagChunkRecord:
    """写入 Milvus 的一条子块；字段来自权威清单，不在构建时重新切块。"""

    chunk_id: str
    knowledge_id: str
    version: str
    parent_doc_id: str
    source_document_id: str
    title: str
    scene_category: str
    section_title: str
    page: int
    block_kinds: str
    product_code: str
    channel: str
    region: str
    effective_at: str
    expires_at: str
    content_hash: str
    text: str
    token_count: int


def chunk_records(bundle: RagDocumentBundle, content_hash: str) -> list[RagChunkRecord]:
    """按清单里的父子关系展开子块，附上过滤和溯源字段。"""

    parents = {parent.parent_doc_id: parent for parent in bundle.parents}
    blocks = {block.block_id: block for block in bundle.blocks}
    source = bundle.source
    records = []
    for chunk in bundle.chunks:
        parent = parents[chunk.parent_doc_id]
        kinds = sorted({blocks[block_id].kind for block_id in chunk.block_ids})
        records.append(
            RagChunkRecord(
                chunk_id=chunk.chunk_id,
                knowledge_id=source.knowledge_id,
                version=source.version,
                parent_doc_id=chunk.parent_doc_id,
                source_document_id=source.source_document_id,
                title=source.title,
                scene_category=source.scene_category,
                section_title=parent.section_title,
                page=parent.page,
                block_kinds=",".join(kinds),
                product_code=source.product_code or "",
                channel=source.channel or "",
                region=source.region or "",
                effective_at=source.effective_at.isoformat() if source.effective_at else "",
                expires_at=source.expires_at.isoformat() if source.expires_at else "",
                content_hash=content_hash,
                text=chunk.page_content,
                token_count=chunk.token_count,
            )
        )
    return records


def recreate_rag_collection(
    client: MilvusClient, collection_name: str, profile: EmbeddingProfile
) -> None:
    """建立稠密 + BM25 双路 Schema；同名 Collection 先删除再按清单重建。"""

    if client.has_collection(collection_name):
        client.drop_collection(collection_name)
    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=128)
    schema.add_field("dense", DataType.FLOAT_VECTOR, dim=profile.dimension)
    schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
    schema.add_field(
        "text",
        DataType.VARCHAR,
        max_length=8192,
        enable_analyzer=True,
        analyzer_params=TEXT_ANALYZER_PARAMS,
    )
    for name, length in (
        ("knowledge_id", 64),
        ("version", 32),
        ("parent_doc_id", 64),
        ("source_document_id", 64),
        ("title", 256),
        ("scene_category", 64),
        ("section_title", 256),
        ("block_kinds", 32),
        ("product_code", 64),
        ("channel", 64),
        ("region", 64),
        ("effective_at", 16),
        ("expires_at", 16),
        ("content_hash", 64),
        ("embedding_profile_id", 64),
        ("release_version", 64),
    ):
        schema.add_field(name, DataType.VARCHAR, max_length=length)
    schema.add_field("page", DataType.INT64)
    schema.add_field("token_count", DataType.INT64)
    schema.add_function(
        Function(
            name="bm25_text",
            function_type=FunctionType.BM25,
            input_field_names=["text"],
            output_field_names=["sparse"],
        )
    )
    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="dense",
        index_type=VECTOR_INDEX_TYPE,
        metric_type="IP",
        params={"M": HNSW_M, "efConstruction": HNSW_EF_CONSTRUCTION},
    )
    index_params.add_index(
        field_name="sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25"
    )
    client.create_collection(
        collection_name=collection_name,
        schema=schema,
        index_params=index_params,
        consistency_level="Strong",
    )


def build_rag_collection(
    client: MilvusClient,
    *,
    collection_name: str,
    release_version: str,
    profile: EmbeddingProfile,
    records: list[RagChunkRecord],
    vectors: list[list[float]],
) -> int:
    """重建并写入全部子块；返回 Milvus 实际行数供调用方核对。"""

    if len(records) != len(vectors):
        raise ValueError("子块数量与向量数量不一致")
    recreate_rag_collection(client, collection_name, profile)
    rows = [
        {
            "chunk_id": record.chunk_id,
            "dense": vector,
            "text": record.text,
            "knowledge_id": record.knowledge_id,
            "version": record.version,
            "parent_doc_id": record.parent_doc_id,
            "source_document_id": record.source_document_id,
            "title": record.title,
            "scene_category": record.scene_category,
            "section_title": record.section_title,
            "block_kinds": record.block_kinds,
            "product_code": record.product_code,
            "channel": record.channel,
            "region": record.region,
            "effective_at": record.effective_at,
            "expires_at": record.expires_at,
            "content_hash": record.content_hash,
            "embedding_profile_id": profile.profile_id,
            "release_version": release_version,
            "page": record.page,
            "token_count": record.token_count,
        }
        for record, vector in zip(records, vectors, strict=True)
    ]
    if rows:
        client.insert(collection_name=collection_name, data=rows)
    client.flush(collection_name=collection_name)
    return int(client.get_collection_stats(collection_name)["row_count"])
