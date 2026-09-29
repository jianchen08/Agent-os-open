#!/usr/bin/env python3
"""打包前脏树检查（A2 止血，goreleaser dirty-check 范式）。

设计：docs/working/打包发版循环问题与方案_20260929.md §5.2-A。electron-builder
的 files/extraResources 全部相对 cwd（工作树）解析——并行会话 WIP 是否打进包
纯看时机，无任何闸（R299 实证带过未验证 WIP；受管还原又曾把 WIP 抹掉致包内容
漂移）。本脚本在 electron:build 链首对**打包相关路径**做 `git status --porcelain`
检查，非空即拒，与 goreleaser 的 dirty-worktree 拒绝同范式：

- 检查面（= electron-builder 实际摄入面）：package.json、electron/、
  frontend/src、frontend/public、plugins/shared、config/、kernel/；
  忽略构建产物（dist/dist-electron/release 等已被 .gitignore 排除，porcelain
  天然不报）；
- `--allow-dirty` 逃生口：放行但打 [unverified] 大字警示（与仓内 [skip-diff-cov]
  逃生口先例同风格）——测试包可打，正式语义的包不可打；产物不得作为发版包出货。

用法：
  python scripts/check_clean_tree.py [--allow-dirty] [--repo <仓库根>]
退出码（与 sanitize 门禁一致的非零即红）：0 = 干净（或 allow-dirty 放行）；
1 = 打包相关路径存在未提交改动；2 = 环境不可判（git 不可用/非仓库）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_REPO_DEFAULT = Path(__file__).resolve().parents[1]

#: 打包相关路径（electron-builder files/extraResources 的仓内摄入面；与
#: 方案文档 §5.2-A 同源）。kernel/ 含内核源码面（A1 参照系），一并纳入。
PACK_PATHS = (
    "package.json",
    "electron/",
    "frontend/src",
    "frontend/public",
    "plugins/shared",
    "config/",
    "kernel/",
)

_MAX_LISTED = 30


def _git_status(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--", *PACK_PATHS],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _warn_banner() -> None:
    bar = "=" * 68
    print(bar)
    print("[unverified] 脏树打包放行（--allow-dirty）：产物含未提交改动，")
    print("[unverified] 未通过源纯净度校验——仅供本地测试，")
    print("[unverified] 禁止作为正式发版包出货（goreleaser snapshot 同义）。")
    print(bar)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="打包前脏树检查（A2 止血闸）")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="逃生口：放行脏树但产物带 unverified 语义（测试包专用）")
    parser.add_argument("--repo", type=Path, default=_REPO_DEFAULT, help="仓库根（默认脚本所在仓）")
    args = parser.parse_args()

    repo = args.repo.resolve()
    status = _git_status(repo)
    if status.returncode != 0:
        print(f"[clean-tree] 环境不可判（git 不可用或非 git 仓库）：{status.stderr.strip()}")
        return 2

    dirty = [ln.rstrip() for ln in status.stdout.splitlines() if ln.strip()]
    if not dirty:
        print(f"[clean-tree] PASS 打包相关路径干净（{len(PACK_PATHS)} 个 pathspec 零未提交改动）")
        return 0

    print(f"[clean-tree] 打包相关路径存在 {len(dirty)} 项未提交改动：")
    for line in dirty[:_MAX_LISTED]:
        print(f"             {line}")
    if len(dirty) > _MAX_LISTED:
        print(f"             ……（共 {len(dirty)} 项）")

    if args.allow_dirty:
        _warn_banner()
        return 0

    print("[clean-tree] FAIL 打包摄入面不纯净——WIP 是否入包纯看时机（A2 事故面）。")
    print("             修复动作：提交或 stash 打包相关改动后重打包；确需带 WIP 出测试包")
    print("             用 --allow-dirty（产物 unverified，禁止作为发版包出货）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
