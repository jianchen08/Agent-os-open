#!/usr/bin/env python3
"""打包后 editable 安装重定位（afterPack 钩子调用，出包前最后一道改写）。

问题（2026-10-29 第三犯 / 2026-11-10 复核 / 2026-11-12 第四犯）：仓内 venv 被
整目录照搬进安装包，``__editable__*.pth`` 里写的是**绝对路径**——形态有二：
开发机路径（第三犯），或**打包位/worktree 绝对路径**（第四犯：旧版把越界路径
重挂为 ``root.joinpath(...)`` 的打包位绝对路径，打包机上原地存在、原地校验
全绿，NSIS 装机挪位/打包树清理后即死链——装机侧表现为全部合宿宿主 spawn 即
``ModuleNotFoundError: agentos_plugin_sdk``，crash loop）。

做法（第四犯后重立）：``.pth`` 的绝对路径行一律改写为**相对 site-packages 的
相对路径行**（CPython ``site.addpackage`` 以 .pth 所在目录解析相对行，产物整体
挪位后依然成立）。先锚定包内目标：包外路径按「``plugins/`` / ``prereqs/`` 之后
的部分」重挂，包内路径取原值；目标不存在则记为未解决，由布局门禁 fail-closed 拦下。

载体口径收窄：只改写 plain ``.pth`` 路径行（uv editable 的实际形态）；finder
模块 ``*_finder.py`` 里的绝对字面量**没有可挪位的改写形式**（MAPPING 值在
import 期按字面解析），一律记为未解决 fail-closed——改用 plain .pth 形态或
装后重建 venv。

注：``pyvenv.cfg`` 的 home **不属本脚本职责**——随包 venv 的 home 无防挪位
形态（绝对=装机挪位死；相对=CWD 依赖，bash venv 全量 exe 实测启动即死；uv
trampoline 另烘创建机基座路径），唯一正解=随包不带 venv（strip_packaged_venvs
出包剥离）+ 装后 boot autoprovision 安装位原地自建。

用法：
  python scripts/relocate_packaged_editable.py <resources根> [--dry-run]

退出码：0 = 全部绝对路径已相对化；1 = 存在无法重定位的路径。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# 可重定位锚点：仓根布局里的顶层段（包内同名路径即正确目标）
_RELOCATE_ANCHORS = ("plugins", "prereqs")
# site-packages 扫描深度上限（resources/<5 层>/site-packages 已覆盖全部形态）
_SCAN_MAX_DEPTH = 8
_EDITABLE_GLOBS = ("__editable__*.pth", "__editable__*_finder.py")


def is_within(path: Path, root: Path) -> bool:
    """path 是否落在 root 内（解析后按父链判；非法路径按越界处理）。"""
    try:
        resolved_path = path.resolve()
        resolved_root = root.resolve()
        return resolved_path == resolved_root or resolved_root in resolved_path.parents
    except OSError:
        return False


def relocate_target(target: Path, root: Path) -> Path | None:
    """包外绝对路径 → 包内同名绝对路径（锚点之后的部分；目标不存在返回 None）。

    返回的是包内绝对路径，仅作中转值——最终写盘形态由调用方转为相对路径行。
    """
    if is_within(target, root):
        return None
    parts = target.parts
    for anchor in _RELOCATE_ANCHORS:
        if anchor not in parts:
            continue
        candidate = root.joinpath(*parts[parts.index(anchor) :])
        return candidate if candidate.exists() else None
    return None


def iter_site_packages(root: Path) -> list[Path]:
    """resources 下的 site-packages 目录（定深扫描，剪 node_modules/.git）。"""
    found: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > _SCAN_MAX_DEPTH:
            continue
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            if entry.name == "site-packages":
                found.append(entry)
                continue
            if entry.name in {"node_modules", ".git", "__pycache__"}:
                continue
            stack.append((entry, depth + 1))
    return sorted(found)


def relocate_file(path: Path, root: Path, dry_run: bool) -> tuple[list[str], list[str]]:
    """处理单个 editable 载体，返回 (已重定位, 未解决)。

    plain ``.pth``：绝对路径行 → 相对 site-packages 的相对路径行（保留行序与
    非路径行原样）；包内目标锚定不到/不存在 → 未解决。finder 模块：绝对字面量
    无可挪位改写形式，一律未解决（fail-closed）。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [f"{path}: 读取失败 {exc}"]
    if path.suffix != ".pth":
        return [], [f"UNRESOLVED {literal} (in {path.name})" for literal in _absolute_literals(text)]
    moved: list[str] = []
    unresolved: list[str] = []
    rewritten: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        candidate = Path(line) if line and not line.startswith(("#", "import ")) else None
        if candidate is None or not candidate.is_absolute():
            rewritten.append(raw)
            continue
        final = candidate if is_within(candidate, root) else relocate_target(candidate, root)
        if final is None or not final.exists():
            unresolved.append(f"UNRESOLVED {candidate} (in {path.name})")
            rewritten.append(raw)
            continue
        relative = os.path.relpath(final, path.parent)
        rewritten.append(relative)
        moved.append(f"{candidate} -> {relative} ({path.name})")
    if moved and not dry_run:
        newline = "\r\n" if "\r\n" in text else "\n"
        trailing = newline if text.endswith("\n") else ""
        path.write_text(newline.join(rewritten) + trailing, encoding="utf-8")
    return moved, unresolved


def _absolute_literals(text: str) -> list[Path]:
    """finder 源码里的绝对路径字面量（去转义后判绝对性，保守取并集）。"""
    found: list[Path] = []
    for match in re.finditer(r"""["']([A-Za-z]:[\\/][^"']*|/[^"']*)["']""", text):
        raw = match.group(1).replace("\\\\", "\\")
        candidate = Path(raw)
        if candidate.is_absolute() and candidate not in found:
            found.append(candidate)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包后 editable 安装重定位")
    parser.add_argument("resources", help="打包资源根（安装包 resources 目录）")
    parser.add_argument("--dry-run", action="store_true", help="只报告不写盘")
    args = parser.parse_args(argv)

    root = Path(args.resources)
    if not root.is_dir():
        print(f"FAIL 资源根不存在：{root}", file=sys.stderr)
        return 1

    relocated: list[str] = []
    unresolved: list[str] = []
    for site_packages in iter_site_packages(root):
        for pattern in _EDITABLE_GLOBS:
            for carrier in sorted(site_packages.glob(pattern)):
                moved, stuck = relocate_file(carrier, root, args.dry_run)
                relocated.extend(moved)
                unresolved.extend(stuck)

    action = "将重定位" if args.dry_run else "已重定位"
    for entry in relocated:
        print(f"{action}: {entry}")
    for entry in unresolved:
        print(f"未解决: {entry}", file=sys.stderr)
    if not relocated and not unresolved:
        print("无需处理：包内 editable 指向全部合法")
    return 1 if unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
