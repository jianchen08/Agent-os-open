#!/usr/bin/env python3
"""打包产物 external MCP 受管生成目录剥离（afterPack 钩子调用，清单生成之前）。

external MCP 受管目录（``mcp_<清洗名>/`` 且带 ``.mcp-managed`` 标记）是内核
discover 每轮从 config/mcp.json **派生**的生成物（ADR 2026-09-03-mcp-one-line
-install），非源码资产。dev 侧内核运行过即落进仓内插件树（未跟踪），extraResources
整目录照搬会把它打进安装包——装机 tier1 随包带一份派生副本，与用户根副本构成
同 id 双源（2026-11-20 mcp_windows_mcp 启动复活链实证：升级带回陈旧副本 →
内核每轮按单副本收敛清掉 → WARN 噪声 + 升级面抖动）。

做法：删除 plugins/shared 下全部带 ``.mcp-managed`` 标记的插件目录。手写
``mcp_*`` 插件（无标记）不动。删除的是 staged 产物内的拷贝，不触碰仓内源树。

用法：
  python scripts/strip_packaged_external_mcp.py <resources根> [--dry-run]

退出码：0 = 剥离完成或本无受管目录；1 = 剥离失败（IO 错误）或资源根缺失。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

SHARED_REL = Path("plugins") / "shared"
MANAGED_MARKER = ".mcp-managed"


def strip_managed_external_mcp_dirs(
    root: Path, dry_run: bool
) -> tuple[list[str], list[str]]:
    """剥离 resources/plugins/shared 下全部受管 external MCP 目录。

    返回 (已剥离, 失败)。
    """
    shared = root / SHARED_REL
    if not shared.is_dir():
        return [], []
    removed: list[str] = []
    failures: list[str] = []
    for entry in sorted(shared.iterdir()):
        if not entry.is_dir() or not entry.name.startswith("mcp_"):
            continue
        if not (entry / MANAGED_MARKER).exists():
            continue  # 手写 mcp_ 插件不动
        label = str(entry.relative_to(root))
        if dry_run:
            removed.append(label)
            continue
        try:
            shutil.rmtree(entry)
            removed.append(label)
        except OSError as exc:
            failures.append(f"{label}: {exc}")
    return removed, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包产物 external MCP 受管目录剥离")
    parser.add_argument("resources", help="打包资源根（安装包 resources 目录）")
    parser.add_argument("--dry-run", action="store_true", help="只报告不删除")
    args = parser.parse_args(argv)

    root = Path(args.resources)
    if not root.is_dir():
        print(f"FAIL 资源根不存在：{root}", file=sys.stderr)
        return 1

    removed, failures = strip_managed_external_mcp_dirs(root, args.dry_run)
    action = "将剥离" if args.dry_run else "已剥离"
    for label in removed:
        print(f"{action}: {label}")
    for failure in failures:
        print(f"剥离失败: {failure}", file=sys.stderr)
    if not removed and not failures:
        print("无需处理：产物内无 external MCP 受管目录")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
