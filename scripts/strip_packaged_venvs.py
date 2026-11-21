#!/usr/bin/env python3
"""打包产物随包 venv 剥离（afterPack 钩子调用，relocate 之前）。

随包 venv 无法防挪位（2026-11-12 实证三形态全灭）：
- ``pyvenv.cfg`` home 绝对（打包位/worktree/开发机）→ NSIS 挪位安装后即死链；
- home 相对 → CWD 依赖（getpath 按 cwd 解析，异 cwd 启动即 ``No Python at``）；
- uv trampoline ``python.exe`` 另烘创建机基座路径（cfg 修复也不生效）。

现役设计（与布局门禁 HOST_VENV_MISSING 载荷阶段豁免同源）：resources 不带
venv，装后由内核 boot autoprovision（``AGENTOS_PLUGIN_VENV_AUTOPROVISION=1``，
共享合宿宿主优先）按包内 prereqs/python + wheelhouse 在安装位原地自建——
基座指针与 editable 天然指向安装树。

做法：删除 plugins/shared 下全部 ``.venv`` 目录（_host 共享 venv 与平铺独立
插件 venv；junction/symlink 形态摘点不穿透——rmtree 穿 junction 会误删目标树）。
删除的是 staged 产物内的拷贝，不触碰仓内源 venv。

用法：
  python scripts/strip_packaged_venvs.py <resources根> [--dry-run]

退出码：0 = 剥离完成或本无 venv；1 = 剥离失败（IO 错误）或资源根缺失。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

SHARED_REL = Path("plugins") / "shared"


def _is_link_dir(path: Path) -> bool:
    """junction/symlink 判定（junction 只能摘点，禁止 rmtree 穿透）。"""
    if path.is_symlink():
        return True
    isjunction = getattr(os.path, "isjunction", None)  # Python 3.12+；低版本退无判
    return bool(isjunction and isjunction(path))


def strip_venvs(root: Path, dry_run: bool) -> tuple[list[str], list[str]]:
    """剥离 resources/plugins/shared 下全部 .venv，返回 (已剥离, 失败)。"""
    shared = root / SHARED_REL
    if not shared.is_dir():
        return [], []
    removed: list[str] = []
    failures: list[str] = []
    for venv in sorted(shared.glob("*/.venv")):
        if not venv.exists() and not venv.is_symlink():
            continue
        label = str(venv.relative_to(root))
        if dry_run:
            removed.append(label)
            continue
        try:
            if _is_link_dir(venv):
                venv.rmdir()  # 只摘 reparse 点，绝不穿透
            else:
                shutil.rmtree(venv)
            removed.append(label)
        except OSError as exc:
            failures.append(f"{label}: {exc}")
    return removed, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包产物随包 venv 剥离")
    parser.add_argument("resources", help="打包资源根（安装包 resources 目录）")
    parser.add_argument("--dry-run", action="store_true", help="只报告不删除")
    args = parser.parse_args(argv)

    root = Path(args.resources)
    if not root.is_dir():
        print(f"FAIL 资源根不存在：{root}", file=sys.stderr)
        return 1

    removed, failures = strip_venvs(root, args.dry_run)
    action = "将剥离" if args.dry_run else "已剥离"
    for label in removed:
        print(f"{action}: {label}")
    for failure in failures:
        print(f"剥离失败: {failure}", file=sys.stderr)
    if not removed and not failures:
        print("无需处理：产物内无随包 .venv")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
