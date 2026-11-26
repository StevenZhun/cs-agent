"""校验并导入已经冻结的 RAG 知识包。"""

import argparse
import asyncio
import hashlib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from transformers import AutoTokenizer

from services.content.rag.models import RagDocumentModel
from services.content.rag.publication import approve_documents, prepare_release, save_draft
from shared.contracts.rag import RagDocumentBundle
from shared.web.settings import ServiceSettings

ROOT = Path(__file__).resolve().parents[2]

def verify_bundle(bundle: RagDocumentBundle) -> None:
    """入库前核对原 PDF 和计数，拒绝改过文件或伪造 Token 数的解析包。"""
    if (
        hashlib.sha256((ROOT / bundle.source.source_file).read_bytes()).hexdigest()
        != bundle.source_sha256
    ):
        raise ValueError("原 PDF 已变化，需重新解析并建立新版本")
    tokenizer = AutoTokenizer.from_pretrained(bundle.policy.tokenizer_name, local_files_only=True)
    for document in (*bundle.parents, *bundle.chunks):
        actual = len(
            tokenizer(document.page_content, add_special_tokens=True, truncation=False)["input_ids"]
        )
        if actual != document.token_count:
            raise ValueError("解析包 Token 数与实际 tokenizer 不一致")


async def database_action(args) -> None:
    """按显式动作导入、批准、冻结或下线知识版本。"""
    settings = ServiceSettings()
    engine = create_async_engine(settings.database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            if args.action == "import":
                paths = sorted(args.output.glob("*.json"))
                if not paths:
                    raise ValueError("未找到冻结知识包，请检查 --output 指向的目录")
                changed = 0
                for path in paths:
                    bundle = RagDocumentBundle.model_validate_json(path.read_text(encoding="utf-8"))
                    verify_bundle(bundle)
                    changed += await save_draft(session, bundle, args.source_ref)
                await session.commit()
                print(
                    f"导入检查 {len(paths)} 个版本：新增或修改 {changed}，"
                    f"同内容保留 {len(paths) - changed}。导入本身不执行批准或上线。"
                )
            elif args.action == "approve":
                if not args.approved_by or not args.review_note:
                    raise ValueError("approve 必须提供 --approved-by 和 --review-note")
                rows = list(
                    (
                        await session.scalars(
                            select(RagDocumentModel).where(
                                RagDocumentModel.source_ref == args.source_ref,
                                RagDocumentModel.lifecycle_status == "DRAFT",
                            )
                        )
                    ).all()
                )
                for row in rows:
                    verify_bundle(RagDocumentBundle.model_validate(row.bundle))
                count = await approve_documents(
                    session, args.source_ref, args.approved_by, args.review_note
                )
                await session.commit()
                print(f"已显式批准 {count} 个知识版本；尚未建立或激活索引。")
            elif args.action == "prepare":
                result = await prepare_release(session, args.source_ref, args.release)
                await session.commit()
                print(
                    f"清单 {result['release_version']} 已冻结为 READY，"
                    f"索引状态 {result['index_status']}。"
                )
            elif args.action == "retire":
                if not args.knowledge_id or not args.version:
                    raise ValueError("retire 需要 --knowledge-id 和 --version")
                row = await session.get(
                    RagDocumentModel, (args.knowledge_id, args.version), with_for_update=True
                )
                if (
                    row is None
                    or row.source_ref != args.source_ref
                    or row.lifecycle_status == "DRAFT"
                ):
                    raise ValueError("没有对应作者源的已批准知识版本；不把草稿标为正式下线")
                row.lifecycle_status, row.enabled = "RETIRED", False
                await session.commit()
                print("已下线指定知识版本；历史快照不删除。")
            else:
                rows = (
                    await session.scalars(
                        select(RagDocumentModel)
                        .where(RagDocumentModel.source_ref == args.source_ref)
                        .order_by(RagDocumentModel.knowledge_id, RagDocumentModel.version)
                    )
                ).all()
                for row in rows:
                    print(row.knowledge_id, row.version, row.lifecycle_status, row.enabled)
    finally:
        await engine.dispose()


def main() -> None:
    """每一步独立执行；导入不暗中批准，批准不暗中建索引或上线。"""
    parser = argparse.ArgumentParser(description="RAG 知识内容与发布准备")
    parser.add_argument("action", choices=("import", "show", "approve", "prepare", "retire"))
    parser.add_argument(
        "--output", type=Path, default=ROOT / "data/bootstrap/rag-release-v1"
    )
    parser.add_argument("--source-ref", default="bootstrap:rag:generic-v1")
    parser.add_argument("--release", default="rag-release-v1")
    parser.add_argument("--approved-by")
    parser.add_argument("--review-note")
    parser.add_argument("--knowledge-id")
    parser.add_argument("--version")
    args = parser.parse_args()
    asyncio.run(database_action(args))


if __name__ == "__main__":
    main()
