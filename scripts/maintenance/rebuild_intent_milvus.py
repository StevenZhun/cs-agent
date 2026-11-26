"""只重建 intent 相似问 Milvus Collection，不动 FAQ 侧 Collection。

适用于只变更意图相似语料、FAQ 问法索引无需重建的发布操作。若 FAQ 问法也有
变更，应改用 ``semantic_index.py build`` 同时构建两类 Collection。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from time import perf_counter

from pymilvus import MilvusClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.content.models import IntentDefinitionModel, IntentSimilarityModel
from services.content.semantic_index import collection_names
from shared.semantic_embedding import DEFAULT_EMBEDDING_PROFILE, QwenEmbeddingModel
from shared.web.settings import ServiceSettings


async def _load_intent_similarity(
    session_factory: async_sessionmaker,
) -> list[tuple[IntentSimilarityModel, IntentDefinitionModel]]:
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(IntentSimilarityModel, IntentDefinitionModel)
                .join(
                    IntentDefinitionModel,
                    (IntentDefinitionModel.intent_id == IntentSimilarityModel.intent_id)
                    & (IntentDefinitionModel.version == IntentSimilarityModel.intent_version),
                )
                .where(
                    IntentSimilarityModel.lifecycle_status.in_(("APPROVED", "PUBLISHED")),
                    IntentDefinitionModel.lifecycle_status == "PUBLISHED",
                    IntentDefinitionModel.enabled.is_(True),
                )
                .order_by(IntentSimilarityModel.similarity_id)
            )
        ).all()
    if not rows:
        raise ValueError("没有可用的意图相似问,请先执行 seed+approve")
    return rows


async def rebuild_intent_only(index_version: str) -> None:
    settings = ServiceSettings()
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        rows = await _load_intent_similarity(session_factory)
        print(f"已加载 {len(rows)} 条意图相似问")

        started = perf_counter()
        model = QwenEmbeddingModel()
        print(f"模型加载 {round((perf_counter() - started) * 1000, 1)} ms")

        texts = [sim.normalized_text for sim, _ in rows]
        started = perf_counter()
        vectors = model.encode_documents(texts, show_progress=True)
        print(f"向量编码 {round((perf_counter() - started) * 1000, 1)} ms")

        entities = [
            {
                "record_id": sim.similarity_id,
                "vector": vec,
                "intent_id": sim.intent_id,
                "intent_version": sim.intent_version,
                "text": sim.normalized_text,
                "locale": sim.locale,
                "source_ref": sim.source_ref,
                "embedding_profile_id": DEFAULT_EMBEDDING_PROFILE.profile_id,
                "index_version": index_version,
            }
            for (sim, _), vec in zip(rows, vectors, strict=True)
        ]

        names = collection_names(index_version)
        client = MilvusClient(uri=settings.milvus_uri)
        try:
            # 只重建意图 Collection,不动 FAQ Collection
            from services.content.semantic_index import _recreate_intent_collection

            _recreate_intent_collection(client, names.intent, DEFAULT_EMBEDDING_PROFILE)
            if entities:
                client.insert(collection_name=names.intent, data=entities)
            client.flush(collection_name=names.intent)
            stats = client.get_collection_stats(names.intent)
            print(
                json.dumps(
                    {"intent_collection": names.intent, "row_count": stats["row_count"]},
                    ensure_ascii=False,
                )
            )
        finally:
            client.close()
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="只重建 Milvus 意图 Collection")
    parser.add_argument("--index-version", default="semantic-v2")
    args = parser.parse_args()
    asyncio.run(rebuild_intent_only(args.index_version))


if __name__ == "__main__":
    main()
