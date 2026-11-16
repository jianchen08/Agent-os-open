"""git hooks 幂等安装器（方案 AI辅助编程体系落地AgentOS_20261022 §3.6 步骤 5 件 1）。

core.hooksPath 是 git config（.git/config，不入库）——受管工作区还原后钩子
失联，一条命令重装：``python scripts/install_hooks.py``（幂等，重复执行无害）。
钩子本体版本化入库 ``scripts/githooks/``（可评审源码；旧面 scripts/hooks/
已摘除，见 ADR 2026-11-04-e038-hook-installer-and-old-surface-removal）。

用法：
    python scripts/install_hooks.py                 # 本仓
    python scripts/install_hooks.py --repo <path>   # 指定仓库（脚手架/测试注入）
"""

from __future__ import annotations

import argparse
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS_REL = Path("scripts") / "githooks"
REQUIRED_HOOK = "pre-commit"
OPTIONAL_HOOKS = ("commit-msg",)


def install(repo: Path) -> int:
    """校验钩子在位 → 补可执行位 → 设置 core.hooksPath。幂等。"""
    hooks_dir = repo / HOOKS_REL
    required = hooks_dir / REQUIRED_HOOK
    if not required.is_file():
        print(f"install_hooks: 钩子文件缺失: {required}", file=sys.stderr)
        return 1

    # 可执行位：Windows/挂载盘文件系统可能不支持 chmod——尽力而为不阻断
    # （git for Windows 按 shebang 照常执行 bash 钩子）。
    for name in (REQUIRED_HOOK, *OPTIONAL_HOOKS):
        hook = hooks_dir / name
        if hook.is_file():
            try:
                hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except OSError:
                pass

    proc = subprocess.run(
        ["git", "-C", str(repo), "config", "core.hooksPath", HOOKS_REL.as_posix()],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(
            f"install_hooks: git config 失败: {proc.stderr.strip()}",
            file=sys.stderr,
        )
        return 1

    verify = subprocess.run(
        ["git", "-C", str(repo), "config", "core.hooksPath"],
        capture_output=True,
        text=True,
        check=False,
    )
    print(f"install_hooks: core.hooksPath = {verify.stdout.strip()}（幂等安装完成；受管工作区还原后重跑本命令即可）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="git hooks 幂等安装（core.hooksPath → scripts/githooks）")
    parser.add_argument(
        "--repo",
        default=str(ROOT),
        help="目标仓库根（默认本仓；脚手架/测试可注入）",
    )
    args = parser.parse_args(argv)
    return install(Path(args.repo).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
