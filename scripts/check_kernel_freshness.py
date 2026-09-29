#!/usr/bin/env python3
"""打包前置内核新鲜度校验（A1 根治阶段一判据）。

设计：docs/working/打包发版循环问题与方案_20260929.md §5.1。electron-builder
从 kernel/target/release/agentos-kernel.exe 照搬内核为 extraResources，对缺失
源文件仅 warn、对陈旧零校验——本脚本把两道退化判据 fail-closed 化，挂在
electron:build 链首（sanitize 门禁同款位置），消灭「人肉 mtime 对比 +
二进制字符串 grep」兜底：

1. 内核侧 dirty 拒绝：`git status --porcelain -- kernel/` 非空 → fail——HEAD
   参照系不可靠（受管还原环境下脏树本身即漂移源），先提交再打包；
2. 因果时间序：exe mtime 早于 `git log -1 --format=%ct -- kernel/`（内核侧
   最后提交时刻）→ exe 必然陈旧 → fail，动作 = `cd kernel && cargo build --release`。

局限（如实声明）：mtime 只证「晚于」不证「含」——提交后未重编但之后跑过
cargo build 也会判新鲜。彻底根治为阶段二 build.rs 指纹注入（挂 ADR，方案
文档 §7），本判据是其先行退化版。

用法：
  python scripts/check_kernel_freshness.py [--exe kernel/target/release/agentos-kernel.exe]
                                         [--repo <仓库根>]
退出码（与 sanitize 门禁一致的非零即红）：0 = 新鲜度成立；1 = 不合格（打印
原因与修复动作）；2 = 环境不可判（git 不可用/非仓库/无内核侧提交）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

_REPO_DEFAULT = Path(__file__).resolve().parents[1]
_EXE_REL = Path("kernel") / "target" / "release" / "agentos-kernel.exe"
_REBUILD_HINT = "修复动作：cd kernel && cargo build --release 后重新打包"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _fmt_ts(epoch_s: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch_s))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="打包前置内核新鲜度校验（A1 阶段一判据）")
    parser.add_argument(
        "--exe",
        type=Path,
        default=None,
        help=f"内核 exe 路径（默认 <repo>/{_EXE_REL.as_posix()}）",
    )
    parser.add_argument("--repo", type=Path, default=_REPO_DEFAULT, help="仓库根（默认脚本所在仓）")
    args = parser.parse_args()

    repo = args.repo.resolve()
    # 显式 --exe 相对 cwd 解析（CLI 惯例）；默认值相对仓库根
    exe = args.exe.resolve() if args.exe else (repo / _EXE_REL).resolve()

    if not exe.is_file():
        print(f"[freshness] FAIL 内核 exe 不存在：{exe}")
        print(f"             {_REBUILD_HINT}")
        return 1

    status = _git(repo, "status", "--porcelain", "--", "kernel/")
    if status.returncode != 0:
        print(f"[freshness] 环境不可判（git 不可用或非 git 仓库）：{status.stderr.strip()}")
        return 2
    dirty = [ln.rstrip() for ln in status.stdout.splitlines() if ln.strip()]
    if dirty:
        print(f"[freshness] FAIL 内核侧工作树不干净（{len(dirty)} 项）——HEAD 参照系不可靠：")
        for line in dirty[:20]:
            print(f"             {line}")
        if len(dirty) > 20:
            print(f"             ……（共 {len(dirty)} 项）")
        print("             修复动作：提交或清掉 kernel/ 未提交改动后重打包（受管还原环境")
        print("             下脏树本身即包内容漂移源，A2 面另有整树闸）")
        return 1

    log = _git(repo, "log", "-1", "--format=%ct", "--", "kernel/")
    if log.returncode != 0 or not log.stdout.strip():
        print("[freshness] 环境不可判：取不到内核侧最后提交（无提交历史？）")
        return 2
    commit_ts = int(log.stdout.strip())
    exe_ts = exe.stat().st_mtime

    print(f"[freshness] exe        {exe}")
    print(f"[freshness] exe mtime  {_fmt_ts(exe_ts)}")
    print(f"[freshness] 最后内核提交 {_fmt_ts(commit_ts)}（git log -1 -- kernel/）")
    if exe_ts < commit_ts:
        print("[freshness] FAIL exe 早于内核侧最后提交——必然陈旧（A1 险情的机械拦截）")
        print(f"             {_REBUILD_HINT}")
        return 1

    delta_min = (exe_ts - commit_ts) / 60
    print(f"[freshness] PASS 因果序成立：exe 晚于内核侧最后提交 {delta_min:.0f} 分钟")
    print("[freshness] 局限声明：mtime 只证「晚于」不证「含」，阶段二 build.rs 指纹")
    print("             注入（ADR 待立）后升级为产物自证判据")
    return 0


if __name__ == "__main__":
    sys.exit(main())
