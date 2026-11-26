"""构建相似意图与 FAQ 的 Milvus 索引。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from time import perf_counter

from pymilvus import MilvusClient
from sqlalchemy import select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.content.models import (
    FaqGroupModel,
    FaqItemModel,
    FaqQuestionModel,
    FaqReleaseModel,
    IntentDefinitionModel,
    IntentSimilarityModel,
    SemanticIndexBuildModel,
)
from services.content.semantic_index import (
    HNSW_EF_CONSTRUCTION,
    HNSW_EF_SEARCH,
    HNSW_M,
    VECTOR_INDEX_TYPE,
    collection_names,
    rebuild_collections,
)
from shared.semantic_embedding import DEFAULT_EMBEDDING_PROFILE, QwenEmbeddingModel
from shared.web.settings import ServiceSettings

DEFAULT_INDEX_VERSION = "semantic-v3"
FAQ_SOURCE = "bootstrap:faq:v1"


def _content_hash(records: list[tuple[str, str]]) -> str:
    """按稳定顺序计算内容指纹，便于确认同一索引版本是否真的可复现。"""

    payload = "\n".join(f"{record_id}\t{text}" for record_id, text in sorted(records))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _load_content(session_factory: async_sessionmaker) -> tuple[list[object], list[object]]:
    """读取已批准语料和当前发布版本的 FAQ 问法。"""

    async with session_factory() as session:
        intent_rows = (
            await session.execute(
                select(IntentSimilarityModel, IntentDefinitionModel)
                .join(
                    IntentDefinitionModel,
                    (
                        IntentDefinitionModel.intent_id == IntentSimilarityModel.intent_id
                    )
                    & (
                        IntentDefinitionModel.version == IntentSimilarityModel.intent_version
                    ),
                )
                .where(
                    IntentSimilarityModel.lifecycle_status.in_(("APPROVED", "PUBLISHED")),
                    IntentDefinitionModel.lifecycle_status == "PUBLISHED",
                    IntentDefinitionModel.enabled.is_(True),
                )
                .order_by(IntentSimilarityModel.similarity_id)
            )
        ).all()
        faq_rows = (
            await session.execute(
                select(FaqQuestionModel, FaqItemModel, FaqGroupModel)
                .join(
                    FaqItemModel,
                    (FaqItemModel.faq_item_id == FaqQuestionModel.faq_item_id)
                    & (FaqItemModel.version == FaqQuestionModel.item_version),
                )
                .join(
                    FaqGroupModel,
                    (FaqGroupModel.faq_group_id == FaqItemModel.faq_group_id)
                    & (FaqGroupModel.version == FaqItemModel.group_version),
                )
                .where(
                    FaqItemModel.source_ref == FAQ_SOURCE,
                    FaqItemModel.lifecycle_status == "PUBLISHED",
                    FaqGroupModel.lifecycle_status == "PUBLISHED",
                )
                .order_by(FaqQuestionModel.variant_id)
            )
        ).all()
    if not intent_rows:
        raise ValueError("没有已批准的相似意图语料，请先执行 intent_content approve")
    if not faq_rows:
        raise ValueError("没有已发布的 FAQ 问法，请先完成 FAQ 发布")
    return intent_rows, faq_rows


async def _record_build(
    session_factory: async_sessionmaker,
    *,
    index_version: str,
    intent_collection: str,
    faq_collection: str,
    manifest: dict[str, object],
    similarity_keys: list[tuple[str, str]],
) -> None:
    """只在两个 Collection 均写入成功后记录构建结果并更新语料状态。"""

    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        await session.execute(
            insert(SemanticIndexBuildModel)
            .values(
                index_version=index_version,
                embedding_profile_id=DEFAULT_EMBEDDING_PROFILE.profile_id,
                intent_collection=intent_collection,
                faq_collection=faq_collection,
                manifest=manifest,
                built_at=now,
            )
            .on_conflict_do_update(
                index_elements=[SemanticIndexBuildModel.index_version],
                set_={
                    "embedding_profile_id": DEFAULT_EMBEDDING_PROFILE.profile_id,
                    "intent_collection": intent_collection,
                    "faq_collection": faq_collection,
                    "manifest": manifest,
                    "built_at": now,
                },
            )
        )
        await session.execute(
            update(IntentSimilarityModel)
            .where(
                tuple_(
                    IntentSimilarityModel.similarity_id,
                    IntentSimilarityModel.version,
                ).in_(similarity_keys),
                IntentSimilarityModel.lifecycle_status == "APPROVED",
            )
            .values(lifecycle_status="PUBLISHED")
        )


async def build(index_version: str) -> None:
    """读取权威内容、生成归一化向量，并重建两个离线 Milvus Collection。"""

    settings = ServiceSettings()
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        intent_rows, faq_rows = await _load_content(session_factory)
        started = perf_counter()
        model = QwenEmbeddingModel()
        model_load_ms = round((perf_counter() - started) * 1000, 1)

        intent_records = [
            (similarity.similarity_id, similarity.normalized_text)
            for similarity, _intent in intent_rows
        ]
        faq_records = [
            (question.variant_id, question.normalized_question)
            for question, _item, _group in faq_rows
        ]
        model.validate_corpus(intent_records)
        model.validate_corpus(faq_records)

        started = perf_counter()
        intent_vectors = model.encode_documents(
            [text for _record_id, text in intent_records], show_progress=True
        )
        intent_encode_ms = round((perf_counter() - started) * 1000, 1)
        started = perf_counter()
        faq_vectors = model.encode_documents(
            [text for _record_id, text in faq_records], show_progress=True
        )
        faq_encode_ms = round((perf_counter() - started) * 1000, 1)

        names = collection_names(index_version)
        intent_entities = [
            {
                "record_id": similarity.similarity_id,
                "vector": vector,
                "intent_id": similarity.intent_id,
                "intent_version": similarity.intent_version,
                "text": similarity.normalized_text,
                "locale": similarity.locale,
                "source_ref": similarity.source_ref,
                "embedding_profile_id": DEFAULT_EMBEDDING_PROFILE.profile_id,
                "index_version": index_version,
            }
            for (similarity, _intent), vector in zip(intent_rows, intent_vectors, strict=True)
        ]
        faq_entities = [
            {
                "record_id": question.variant_id,
                "vector": vector,
                "faq_item_id": question.faq_item_id,
                "item_version": question.item_version,
                "faq_group_id": item.faq_group_id,
                "intent_id": group.intent_id,
                "question": question.normalized_question,
                "locale": question.locale,
                "source_ref": item.source_ref,
                "embedding_profile_id": DEFAULT_EMBEDDING_PROFILE.profile_id,
                "index_version": index_version,
            }
            for (question, item, group), vector in zip(faq_rows, faq_vectors, strict=True)
        ]
        client = MilvusClient(uri=settings.milvus_uri)
        try:
            started = perf_counter()
            counts = rebuild_collections(
                client,
                names=names,
                profile=DEFAULT_EMBEDDING_PROFILE,
                intent_rows=intent_entities,
                faq_rows=faq_entities,
            )
            milvus_build_ms = round((perf_counter() - started) * 1000, 1)
        finally:
            client.close()
        if counts != {"intent": len(intent_entities), "faq": len(faq_entities)}:
            raise RuntimeError(f"Milvus 写入数量不一致：{counts}")

        faq_release = None
        async with session_factory() as session:
            faq_release = await session.scalar(
                select(FaqReleaseModel.release_version)
                .where(FaqReleaseModel.source_ref == FAQ_SOURCE)
                .order_by(FaqReleaseModel.published_at.desc())
                .limit(1)
            )
        manifest: dict[str, object] = {
            "profile": asdict(DEFAULT_EMBEDDING_PROFILE),
            "index_type": VECTOR_INDEX_TYPE,
            "index_params": {
                "M": HNSW_M,
                "efConstruction": HNSW_EF_CONSTRUCTION,
                "ef": HNSW_EF_SEARCH,
            },
            "metric_type": "IP",
            "normalized_vectors": True,
            "intent_count": counts["intent"],
            "faq_count": counts["faq"],
            "intent_content_hash": _content_hash(intent_records),
            "faq_content_hash": _content_hash(faq_records),
            "faq_release_version": faq_release,
            "model_max_tokens": model.model_max_tokens,
            "model_load_ms": model_load_ms,
            "intent_encode_ms": intent_encode_ms,
            "faq_encode_ms": faq_encode_ms,
            "milvus_build_ms": milvus_build_ms,
        }
        await _record_build(
            session_factory,
            index_version=index_version,
            intent_collection=names.intent,
            faq_collection=names.faq,
            manifest=manifest,
            similarity_keys=[
                (similarity.similarity_id, similarity.version)
                for similarity, _intent in intent_rows
            ],
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    finally:
        await engine.dispose()


def main() -> None:
    """根据已发布内容重建指定版本的语义索引。"""

    parser = argparse.ArgumentParser(description="构建语义索引")
    parser.add_argument("action", choices=("build",))
    parser.add_argument("--index-version", default=DEFAULT_INDEX_VERSION)
    args = parser.parse_args()
    asyncio.run(build(args.index_version))


if __name__ == "__main__":
    main()
