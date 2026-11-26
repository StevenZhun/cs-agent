"""按依赖顺序完成数据库迁移和全部运行基线初始化。"""

from __future__ import annotations

import sys

from scripts.bootstrap.execution import run_commands
from scripts.bootstrap.plan import initialization_sections
from scripts.bootstrap.readiness import is_initialized
from shared.web.settings import ServiceSettings


def main() -> None:
    """建立数据库结构并发布内容与查询投影，不写入预置客户数据。"""

    settings = ServiceSettings()
    run_commands(
        "数据库结构",
        ((sys.executable, "-m", "alembic", "upgrade", "head"),),
    )
    if is_initialized(settings):
        print("\n数据库结构、活动内容和语义索引已经完成初始化。", flush=True)
        return

    for section in initialization_sections(
        python=sys.executable,
        semantic_index_version=settings.semantic_index_version,
    ):
        run_commands(section.label, section.commands)
    print("\n数据库结构、内容版本和查询索引初始化完成。", flush=True)


if __name__ == "__main__":
    main()
