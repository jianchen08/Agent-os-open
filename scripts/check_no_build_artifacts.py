#!/usr/bin/env python3
"""构建产物禁入提交机械闸（ADR 2026-10-29-build-artifact-commit-guard）。

背景（2026-10-28 f50f0b1f1 事故）：worktree 创建前的 auto-save 把
``kernel/target-cov-g2/``（llvm-cov 产物，1955 文件 / 5.0 GB / 最大 146 MB）
整体提交入库——两个远端 100 MB 单文件硬限直接拒 push，CI 从此无法转绿。
同类事故 2026-09-01 已发生一次（target-rel-alt 等 1394 件，当时靠补
.gitignore 收口）。.gitignore 是第一层（防再犯）；本闸是第二层（漏配、
``add -f``、auto-save 盲扫都拦得住）：**已跟踪面**出现产物路径或超限大
文件即红。

口径：
- 路径禁入清单 ``PATH_DENY_PATTERNS``：cargo 构建目录、node_modules、
  移动壳生成物（www/APK）、交付产物（release 位安装器/云包）、二进制
  副产物（.pdb/.rlib/.blockmap）——真源 = .gitignore 产物段 +
  ADR 2026-10-16-build-artifacts-out-of-git；
- 大小硬限 ``MAX_TRACKED_BYTES``：锚 GitHub 单文件硬限 100 MiB，取 95 MiB
  留裕量（仓内正常文件最大 < 20 MiB，实测无假阳性）；
- 扫描面：默认 HEAD 树；``--range A..B`` 额外扫区间内**新增对象**（防
  「提交后又删」绕过 HEAD 面——CI push 事件传 GITHUB_EVENT_BEFORE）。
  模式匹配用 fnmatch（``*`` 可跨 ``/``，嵌套路径一条模式即覆盖）。

用法：
    python scripts/check_no_build_artifacts.py [--repo <仓库根>] [--range A..B]
退出码：0 = 干净；1 = 存在产物入库 / 超限大文件。
"""

from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 单文件大小硬限（字节）。GitHub = 100 MiB；95 留打包/单位换算裕量。
MAX_TRACKED_BYTES = 95 * 1024 * 1024

#: 产物路径禁入模式（fnmatch，* 跨 /；加新模式须同步 ADR 归档节）。
PATH_DENY_PATTERNS: tuple[str, ...] = (
    # cargo 构建目录：标准 target/ 已由 .gitignore 锚定，备用目录（target-cov-g2、
    # target-rel-alt、target_release_check…）是两次事故的实际形态——按前缀整族收
    "kernel/target*/*",
    # JS 依赖树（任何层级；dsh_adapter 等嵌套 node_modules 曾拖垮遍历）
    "node_modules/*",
    "*/node_modules/*",
    # 移动壳生成物：www 由 syncweb 生成、APK 走 release/<版本>/ 发行位
    "app-mobile/www/*",
    "app-mobile/*.apk",
    # 交付产物：安装器/云包只走发行位，git 只承载源码与配方
    "release/*.exe",
    "release/*.apk",
    "release/*.tar.gz",
    "release/win-unpacked/*",
    "pack_linux.tar.gz",
    # 二进制构建副产物：源码仓不应出现
    "*.pdb",
    "*.rlib",
    "*.blockmap",
)

_MAX_LISTED = 30


def match_denied(path: str) -> str | None:
    """返回命中的禁入模式；未命中返回 None。"""
    for pattern in PATH_DENY_PATTERNS:
        if fnmatch.fnmatch(path, pattern):
            return pattern
    return None


def head_entries(repo: Path) -> list[tuple[str, int]]:
    """HEAD 树的 (路径, 字节) 清单。"""
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "-l", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    rows: list[tuple[str, int]] = []
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) >= 4 and parts[1] == "blob":
            rows.append((path.strip(), int(parts[3])))
    return rows


def range_entries(repo: Path, range_: str) -> list[tuple[str, int]]:
    """diff 区间（A..B）内出现过的 (路径, 字节) 清单——含「提交后又删」的对象。

    经 ``rev-list --objects`` 取对象名，``cat-file --batch-check`` 批量取
    类型与大小；只收 blob。
    """
    rev = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--objects", range_],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    feed = [line for line in rev.splitlines() if line.strip()]
    batch = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "cat-file",
            "--batch-check=%(objectname) %(objecttype) %(objectsize) %(rest)",
        ],
        input="\n".join(feed),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    rows: list[tuple[str, int]] = []
    for line in batch.splitlines():
        parts = line.split(" ", 3)
        if len(parts) == 4 and parts[1] == "blob":
            rows.append((parts[3].strip(), int(parts[2])))
    return rows


def find_violations(entries: Sequence[tuple[str, int]]) -> list[str]:
    """禁入 / 超限违规清单（按路径去重保序）。"""
    out: list[str] = []
    seen: set[str] = set()
    for path, size in sorted(entries):
        if not path or path in seen:
            continue
        seen.add(path)
        pattern = match_denied(path)
        if pattern:
            out.append(f"{path}（禁入模式 {pattern}）")
        elif size > MAX_TRACKED_BYTES:
            out.append(f"{path}（{size / 1048576:.1f} MiB > {MAX_TRACKED_BYTES / 1048576:.0f} MiB 上限）")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建产物禁入提交机械闸")
    parser.add_argument("--repo", default=str(ROOT), help="仓库根（缺省 = 脚本所在仓）")
    parser.add_argument("--range", help="额外扫描区间（A..B）内出现过的对象")
    args = parser.parse_args(argv)

    repo = Path(args.repo)
    entries: list[tuple[str, int]] = head_entries(repo)
    scanned = "HEAD 树"
    if args.range:
        entries = entries + range_entries(repo, args.range)
        scanned += f" + 区间 {args.range} 对象"

    bad = find_violations(entries)
    print(f"[artifact-guard] 扫描面：{scanned}；条目 {len(entries)}")
    if bad:
        print(f"❌ 构建产物入库 / 超限大文件 {len(bad)} 项（前 {_MAX_LISTED}）：")
        for item in bad[:_MAX_LISTED]:
            print("  -", item)
        if len(bad) > _MAX_LISTED:
            print(f"  … 共 {len(bad)} 项")
        print(
            "修复：git rm -r --cached <路径> 并补 .gitignore；"
            "历史剥离见 ADR docs/decisions/2026-10-29-build-artifact-commit-guard.md。"
        )
        return 1
    print("✅ 无构建产物入库，无超限大文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
