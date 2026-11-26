"""为部署初始化入口提供失败即停的命令执行能力。"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Sequence

Command = Sequence[str]


def run_commands(title: str, commands: Iterable[Command]) -> None:
    """按既定顺序执行一组命令，任一步失败就停止后续初始化。"""

    print(f"\n{'=' * 12} {title} {'=' * 12}", flush=True)
    for command in commands:
        print(f"\n[初始化] {' '.join(command)}", flush=True)
        subprocess.run(command, check=True)  # noqa: S603
