"""RAG 草稿、显式审核、不可变清单和下线；不提前切换在线索引。"""

import hashlib
import json
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.content.models import PolicyParameterModel
from services.content.rag.models import RagDocumentModel, RagReleaseModel
from shared.contracts.rag import RagDocumentBundle


def payload_hash(payload: dict) -> str:
    """对完整发布输入计算指纹；来源、适用范围或分块变化都需新快照。"""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


async def save_draft(session: AsyncSession, bundle: RagDocumentBundle, source_ref: str) -> bool:
    """草稿可修改；同内容重试无副作用，非草稿版本不能原地覆盖。"""
    source = bundle.source
    payload = bundle.model_dump(mode="json")
    fingerprint = payload_hash(payload)
    row = await session.get(RagDocumentModel, (source.knowledge_id, source.version))
    if row is not None and row.source_ref != source_ref:
        raise ValueError("已有知识版本属于其他作者源")
    if row is not None and row.content_hash == fingerprint:
        return False
    if row is not None and row.lifecycle_status != "DRAFT":
        raise ValueError("知识版本已批准或发布，修改需建立新版本")
    if row is None:
        row = RagDocumentModel(
            knowledge_id=source.knowledge_id,
            version=source.version,
            source_ref=source_ref,
            lifecycle_status="DRAFT",
            enabled=True,
            created_at=datetime.now(UTC),
        )
        session.add(row)
    row.source_document_id = source.source_document_id
    row.bundle = payload
    row.content_hash = fingerprint
    row.product_code, row.channel, row.region = source.product_code, source.channel, source.region
    row.effective_at, row.expires_at = source.effective_at, source.expires_at
    await session.flush()
    return True


def validate_document(row: RagDocumentModel) -> RagDocumentBundle:
    """清单构建前再次验证权威正文，拒绝手工改坏的哈希或重复字段。"""
    bundle = RagDocumentBundle.model_validate(row.bundle)
    source = bundle.source
    if payload_hash(row.bundle) != row.content_hash:
        raise ValueError("知识包哈希不一致")
    if (row.knowledge_id, row.version, row.source_document_id) != (
        source.knowledge_id,
        source.version,
        source.source_document_id,
    ) or (row.product_code, row.channel, row.region, row.effective_at, row.expires_at) != (
        source.product_code,
        source.channel,
        source.region,
        source.effective_at,
        source.expires_at,
    ):
        raise ValueError("知识正文与数据库标识或适用范围不一致")
    return bundle


def build_manifest(rows: list[RagDocumentModel], release_version: str, source_ref: str) -> dict:
    """冻结已批准内容；不伪造向量数量、索引名称或检索效果。

    结构冲突可自动阻断，政策意义是否冲突仍需审核者显式记录核查结论。
    同批同知识只能有一个版本，避免由查询顺序偷偷决定采用哪个版本。
    """
    if not rows or len({row.knowledge_id for row in rows}) != len(rows):
        raise ValueError("清单不能为空，同一知识只能选择一个版本")
    documents = []
    policy_parameter_refs: set[str] = set()
    for row in sorted(rows, key=lambda item: item.knowledge_id):
        bundle = validate_document(row)
        if row.source_ref != source_ref or not row.enabled:
            raise ValueError("知识作者源不一致或已禁用")
        if row.lifecycle_status not in {"APPROVED", "PUBLISHED"} or not all(
            (row.approved_by, row.approved_at, row.review_note)
        ):
            raise ValueError("清单只能包含有完整审核记录的已批准知识")
        if row.expires_at and row.expires_at < date.today():
            raise ValueError("清单不能包含已过期知识")
        documents.append(
            {
                "bundle": bundle.model_dump(mode="json"),
                "content_hash": row.content_hash,
                "approved_by": row.approved_by,
                "approved_at": row.approved_at.isoformat(),
                "review_note": row.review_note,
            }
        )
        policy_parameter_refs.update(bundle.source.policy_parameter_refs)
    return {
        "release_version": release_version,
        "source_ref": source_ref,
        "documents": documents,
        "index_status": "NOT_BUILT",
        "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
        "dimension": 1024,
        "retrieval_paths": ["DENSE", "BM25"],
        "policy_parameter_refs": sorted(policy_parameter_refs),
        "parent_count": sum(len(item["bundle"]["parents"]) for item in documents),
        "chunk_count": sum(len(item["bundle"]["chunks"]) for item in documents),
    }


def validate_policy_parameter_rows(
    references: list[str], rows: list[PolicyParameterModel | None]
) -> None:
    """RAG 发布只能绑定存在、已批准且仍有效的精确口径版本。"""

    if len(references) != len(rows):
        raise ValueError("口径参数引用与查询结果数量不一致")
    today = date.today()
    for reference, row in zip(references, rows, strict=True):
        if row is None:
            raise ValueError(f"RAG 引用了不存在的口径参数 {reference}")
        actual_ref = f"{row.parameter_key}:{row.version}"
        if actual_ref != reference:
            raise ValueError(f"口径参数引用错位：期望 {reference}，实际 {actual_ref}")
        if row.lifecycle_status not in {"APPROVED", "PUBLISHED"}:
            raise ValueError(f"口径参数 {reference} 尚未批准")
        if row.effective_at > today or (row.retired_at is not None and row.retired_at < today):
            raise ValueError(f"口径参数 {reference} 当前不在有效期内")


async def approve_documents(
    session: AsyncSession, source_ref: str, approved_by: str, review_note: str
) -> int:
    """显式批准指定来源的草稿；命令调用者对业务内容核查说明负责。"""
    if not approved_by.strip() or not review_note.strip():
        raise ValueError("批准人和核查说明都必须填写")
    rows = list(
        (
            await session.scalars(
                select(RagDocumentModel)
                .where(
                    RagDocumentModel.source_ref == source_ref,
                    RagDocumentModel.lifecycle_status == "DRAFT",
                )
                .with_for_update()
            )
        ).all()
    )
    for row in rows:
        validate_document(row)
        if not row.enabled or (row.expires_at and row.expires_at < date.today()):
            raise ValueError("禁用或过期草稿不能批准")
        row.lifecycle_status = "APPROVED"
        row.approved_by, row.review_note = approved_by, review_note
        row.approved_at = datetime.now(UTC)
    return len(rows)


async def prepare_release(session: AsyncSession, source_ref: str, release_version: str) -> dict:
    """冻结内容准备清单；索引未建立时保持 READY，不改变线上活动版本。"""
    existing = await session.get(RagReleaseModel, release_version)
    if existing is not None:
        if (
            existing.manifest["source_ref"] != source_ref
            or payload_hash(existing.manifest) != existing.content_hash
        ):
            raise ValueError("同名清单来源不同或内容损坏")
        return existing.manifest
    rows = list(
        (
            await session.scalars(
                select(RagDocumentModel)
                .where(
                    RagDocumentModel.source_ref == source_ref,
                    RagDocumentModel.lifecycle_status.in_(("APPROVED", "PUBLISHED")),
                )
                .with_for_update()
            )
        ).all()
    )
    manifest = build_manifest(rows, release_version, source_ref)
    parameter_rows = []
    for reference in manifest["policy_parameter_refs"]:
        parameter_key, version = reference.split(":", maxsplit=1)
        parameter_rows.append(
            await session.get(
                PolicyParameterModel,
                (parameter_key, version),
                with_for_update=True,
            )
        )
    validate_policy_parameter_rows(manifest["policy_parameter_refs"], parameter_rows)
    session.add(
        RagReleaseModel(
            release_version=release_version,
            manifest=manifest,
            content_hash=payload_hash(manifest),
            lifecycle_status="READY",
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    return manifest


RAG_ACTIVE_KEY = "content:rag:active"
RAG_RELEASE_PREFIX = "content:rag:release:"


def rag_release_key(release_version: str) -> str:
    """Runtime 读取某个 RAG 发布版本的索引名称和内容指纹时使用的 Redis 键。"""

    return f"{RAG_RELEASE_PREFIX}{release_version}"


async def publish_release(
    session: AsyncSession,
    redis,
    *,
    release_version: str,
    collection_name: str,
    embedding_profile_id: str,
    row_count: int,
) -> dict:
    """索引真正写完后，把 READY 清单标为 PUBLISHED 并切换 Redis 活动指针。

    这里不重新计算清单内容；Milvus 行数必须等于清单子块数，否则拒绝激活，
    避免半份索引被客户请求读到。清单 `content_hash` 保持冻结时的值。
    """

    release = await session.get(RagReleaseModel, release_version, with_for_update=True)
    if release is None:
        raise ValueError("PostgreSQL 中不存在该 RAG 发布版本")
    if release.lifecycle_status == "RETIRED":
        raise ValueError("已下线的 RAG 发布版本不能重新激活")
    manifest = dict(release.manifest)
    if payload_hash(manifest) != release.content_hash:
        raise ValueError("清单内容与冻结哈希不一致")
    if manifest["chunk_count"] != row_count:
        raise ValueError(f"Milvus 行数 {row_count} 与清单子块数 {manifest['chunk_count']} 不一致")
    now = datetime.now(UTC)
    pointer = {
        "release_version": release_version,
        "collection_name": collection_name,
        "embedding_profile_id": embedding_profile_id,
        "content_hash": release.content_hash,
        "chunk_count": row_count,
        "published_at": now.isoformat(),
    }
    manifest.update(
        {
            "index_status": "BUILT",
            "collection_name": collection_name,
            "embedding_profile_id": embedding_profile_id,
            "published_at": now.isoformat(),
        }
    )
    # 清单正文的指纹在 READY 时冻结；索引状态属于投影结果，另存以免改写冻结哈希。
    release.manifest = manifest
    release.lifecycle_status = "PUBLISHED"
    for item in manifest["documents"]:
        source = item["bundle"]["source"]
        row = await session.get(RagDocumentModel, (source["knowledge_id"], source["version"]))
        if row is not None and row.lifecycle_status == "APPROVED":
            row.lifecycle_status = "PUBLISHED"
    await session.commit()
    encoded = json.dumps(pointer, ensure_ascii=False, sort_keys=True)
    key = rag_release_key(release_version)
    cached = await redis.get(key)
    if cached is not None and cached.decode() != encoded:
        raise ValueError("Redis 已有同名不同内容的 RAG 发布版本，不能覆盖")
    if cached is None and not await redis.set(key, encoded, nx=True):
        raise ValueError("有其他发布者正在写同一版本，请重试")
    await redis.set(RAG_ACTIVE_KEY, release_version)
    return pointer


async def load_rag_release(redis, version: str | None = None) -> dict:
    """读取活动或指定 RAG 发布指针；缺失时返回空，不用内置索引名兜底。"""

    if redis is None:
        return {}
    if version is None:
        active = await redis.get(RAG_ACTIVE_KEY)
        if active is None:
            return {}
        version = active.decode() if isinstance(active, bytes) else str(active)
    raw = await redis.get(rag_release_key(version))
    if raw is None:
        return {}
    pointer = json.loads(raw)
    required = {"release_version", "collection_name", "embedding_profile_id", "content_hash"}
    if not isinstance(pointer, dict) or not required <= set(pointer):
        raise ValueError("RAG 发布指针缺少必要字段")
    if pointer["release_version"] != version:
        raise ValueError("RAG 发布指针版本与键不一致")
    return pointer
