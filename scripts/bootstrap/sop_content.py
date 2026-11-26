"""SOP 配置管理：草稿导入、查看、删除、结构校验批准和发布。"""

import argparse
import asyncio
import json
from pathlib import Path

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.bootstrap.sop_data import SOP_DEFINITIONS
from services.content.models import SopDefinitionModel
from services.content.sop_publication import approve_sops, publish_sops, save_draft
from shared.sop_config import SopConfig
from shared.web.settings import ServiceSettings


async def main() -> None:
    """按显式命令处理指定内容，默认来源只包含本地 SOP 草稿。"""

    parser = argparse.ArgumentParser(description="SOP 配置与发布")
    parser.add_argument("action", choices=("seed", "save", "show", "delete", "approve", "publish"))
    parser.add_argument("--source", default="bootstrap:sop:v1")
    parser.add_argument("--release-version", default="sop-release-v1")
    parser.add_argument("--approved-by", default="repository_bootstrap")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--sop-id")
    parser.add_argument("--version", default="sop-v1")
    args = parser.parse_args()
    settings = ServiceSettings()
    engine = create_async_engine(settings.database_url)
    redis = Redis.from_url(settings.redis_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            if args.action in {"seed", "save"}:
                if args.action == "save" and args.file is None:
                    parser.error("save 需要 --file 指定完整 JSON 配置")
                definitions = (
                    SOP_DEFINITIONS
                    if args.action == "seed"
                    else (SopConfig.model_validate_json(args.file.read_text()),)
                )
                if args.action == "seed" and args.version != "sop-v1":
                    parser.error("内置配置仅支持 sop-v1；自定义版本使用 save")
                for definition in definitions:
                    await save_draft(session, definition, args.source)
                await session.commit()
                print(f"已保存或保留 {len(definitions)} 条 SOP 配置。")
            elif args.action == "approve":
                count = await approve_sops(session, args.source, args.approved_by)
                await session.commit()
                print(f"已校验并批准 {count} 条 SOP。")
            elif args.action == "publish":
                result = await publish_sops(session, redis, args.source, args.release_version)
                print(
                    f"已选择发布版本 {result['release_version']}，"
                    f"包含 {len(result['definitions'])} 条 SOP。"
                )
            else:
                if not args.sop_id:
                    parser.error("show/delete 需要 --sop-id")
                row = await session.get(SopDefinitionModel, (args.sop_id, args.version))
                if row is None:
                    parser.error("没有对应版本")
                if args.action == "show":
                    print(json.dumps(row.config, ensure_ascii=False, indent=2))
                else:
                    if row.lifecycle_status != "DRAFT":
                        raise ValueError("只能删除草稿；已批准或发布内容需保留历史")
                    await session.delete(row)
                    await session.commit()
                    print("已删除指定草稿。")
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
