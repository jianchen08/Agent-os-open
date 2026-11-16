#!/usr/bin/env python3
# @feature: FP-0.2.〇 项目类型 CI 包
"""tracked 总字节预算（base/block）：仓库跟踪内容总量超预算即红，列大头清单。

预算可用 run argv 声明式覆盖（--budget-bytes N），缺省 200 MiB。与单文件上限
分工：本门管总量慢漂移（素材堆积），oversized_files 管单文件突刺。
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from _common import PROJECT_ROOT, to_posix

DEFAULT_BUDGET_BYTES = 200 * 1024 * 1024


def tracked_sizes() -> list[tuple[str, int]]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=str(PROJECT_ROOT), capture_output=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    sizes: list[tuple[str, int]] = []
    for rel in out.stdout.decode("utf-8", "replace").split("\0"):
        if not rel:
            continue
        try:
            sizes.append((rel, (PROJECT_ROOT / rel).stat().st_size))
        except OSError:
            continue
    return sizes


def main() -> int:
    parser = argparse.ArgumentParser(description="tracked 总字节预算")
    parser.add_argument("--budget-bytes", type=int, default=DEFAULT_BUDGET_BYTES)
    args = parser.parse_args()
    sizes = tracked_sizes()
    total = sum(s for _, s in sizes)
    if total <= args.budget_bytes:
        return 0
    print(f"tracked 总量 {total} 字节 > 预算 {args.budget_bytes}（超 {(total - args.budget_bytes)} 字节）")
    for rel, size in sorted(sizes, key=lambda kv: -kv[1])[:5]:
        print(f"  大头: {to_posix(rel)} = {size} 字节")
    print("处置：大产物走版本化发行位/外部存储，不进 git。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
