"""构建和激活 RAG 双路 Milvus 索引；输入只来自冻结的 READY 清单。

  build     读取 content.rag_release 中的清单，编码全部子块并重建候选 Collection；
            不改变线上活动指针
  activate  对已建候选 Collection 核对行数，再标为 PUBLISHED 并切换活动指针
  show      打印活动指针与 Collection 行数，不做任何修改

构建用的是清单快照里的子块，不是当前 rag_document 表；清单冻结后知识正文再改
也不会悄悄进入索引，必须重新 prepare 一个新发布号。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from pymilvus import MilvusClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.content.rag.index import (
    HNSW_EF_CONSTRUCTION,
    HNSW_EF_SEARCH,
    HNSW_M,
    TEXT_ANALYZER_PARAMS,
    VECTOR_INDEX_TYPE,
    build_rag_collection,
    chunk_records,
    rag_collection_name,
)
from services.content.rag.models import RagReleaseModel
from services.content.rag.publication import load_rag_release, payload_hash, publish_release
from shared.contracts.rag import RagDocumentBundle
from shared.semantic_embedding import DEFAULT_EMBEDDING_PROFILE, QwenEmbeddingModel
from shared.web.settings import ServiceSettings

ROOT = Path(__file__).resolve().parents[2]


def candidate_pointer(release: RagReleaseModel, collection_name: str, row_count: int) -> dict:
    """生成可供发布前检查的候选快照，不写 Redis 活动指针。"""

    return {
        "release_version": release.release_version,
        "collection_name": collection_name,
        "embedding_profile_id": DEFAULT_EMBEDDING_PROFILE.profile_id,
        "content_hash": release.content_hash,
        "chunk_count": row_count,
        "candidate_status": "BUILT_NOT_ACTIVE",
    }


async def build(release_version: str, output: Path) -> None:
    settings = ServiceSettings()
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(settings.redis_url)
    try:
        async with session_factory() as session:
            release = await session.get(RagReleaseModel, release_version)
            if release is None:
                raise ValueError(f"没有 {release_version}，请先运行 rag_content prepare")
            if release.lifecycle_status == "RETIRED":
                raise ValueError("已下线的发布版本不能构建索引")
            if release.lifecycle_status != "READY":
                raise ValueError("只有 READY 发布版本可以构建候选索引")
            if payload_hash(release.manifest) != release.content_hash:
                raise ValueError("清单内容与冻结哈希不一致，拒绝构建")
            manifest = release.manifest

        records = []
        for item in manifest["documents"]:
            bundle = RagDocumentBundle.model_validate(item["bundle"])
            records.extend(chunk_records(bundle, item["content_hash"]))
        if len(records) != manifest["chunk_count"]:
            raise ValueError("清单子块数与展开结果不一致")
        print(f"清单 {release_version}：{len(manifest['documents'])} 份知识，{len(records)} 个子块")

        started = perf_counter()
        model = QwenEmbeddingModel(DEFAULT_EMBEDDING_PROFILE)
        load_ms = round((perf_counter() - started) * 1000, 1)
        # 子块按清单策略已限制在 512 Token，这里再用在线模型核对一次，不静默截断。
        model.validate_corpus([(r.chunk_id, r.text) for r in records])
        started = perf_counter()
        vectors = model.encode_documents([r.text for r in records], show_progress=True)
        encode_ms = round((perf_counter() - started) * 1000, 1)

        collection = rag_collection_name(release_version)
        client = MilvusClient(uri=settings.milvus_uri)
        try:
            started = perf_counter()
            row_count = build_rag_collection(
                client,
                collection_name=collection,
                release_version=release_version,
                profile=DEFAULT_EMBEDDING_PROFILE,
                records=records,
                vectors=vectors,
            )
            milvus_ms = round((perf_counter() - started) * 1000, 1)
        finally:
            client.close()

        pointer = candidate_pointer(release, collection, row_count)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(pointer, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    **pointer,
                    "index_type": VECTOR_INDEX_TYPE,
                    "index_params": {
                        "M": HNSW_M,
                        "efConstruction": HNSW_EF_CONSTRUCTION,
                        "ef": HNSW_EF_SEARCH,
                    },
                    "sparse": {"function": "BM25", "analyzer": TEXT_ANALYZER_PARAMS},
                    "model_load_ms": load_ms,
                    "encode_ms": encode_ms,
                    "milvus_build_ms": milvus_ms,
                    "built_at": datetime.now(UTC).isoformat(),
                    "candidate_pointer": str(output),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        await redis.aclose()
        await engine.dispose()


async def activate(release_version: str) -> None:
    """候选 Collection 通过内容与检索检查后原子切换活动发布。"""

    settings = ServiceSettings()
    engine = create_async_engine(settings.database_url)
    redis = Redis.from_url(settings.redis_url)
    collection = rag_collection_name(release_version)
    client = MilvusClient(uri=settings.milvus_uri)
    try:
        if not client.has_collection(collection):
            raise ValueError("候选 Collection 不存在，请先执行 build")
        row_count = int(client.get_collection_stats(collection)["row_count"])
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            pointer = await publish_release(
                session,
                redis,
                release_version=release_version,
                collection_name=collection,
                embedding_profile_id=DEFAULT_EMBEDDING_PROFILE.profile_id,
                row_count=row_count,
            )
        print(json.dumps(pointer, ensure_ascii=False, indent=2))
    finally:
        client.close()
        await redis.aclose()
        await engine.dispose()


async def show() -> None:
    settings = ServiceSettings()
    redis = Redis.from_url(settings.redis_url)
    try:
        pointer = await load_rag_release(redis)
        if not pointer:
            print("没有活动的 RAG 发布版本")
            return
        client = MilvusClient(uri=settings.milvus_uri)
        try:
            exists = client.has_collection(pointer["collection_name"])
            rows = (
                int(client.get_collection_stats(pointer["collection_name"])["row_count"])
                if exists
                else None
            )
        finally:
            client.close()
        print(
            json.dumps(
                {**pointer, "collection_exists": exists, "rows": rows}, ensure_ascii=False, indent=2
            )
        )
    finally:
        await redis.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="RAG 双路索引构建与激活")
    parser.add_argument("action", choices=("build", "activate", "show"))
    parser.add_argument("--release", default="rag-release-v1")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / f".runtime/rag/{args.release}-candidate.json"
    if args.action == "build":
        asyncio.run(build(args.release, output))
    elif args.action == "activate":
        asyncio.run(activate(args.release))
    else:
        asyncio.run(show())


if __name__ == "__main__":
    main()
