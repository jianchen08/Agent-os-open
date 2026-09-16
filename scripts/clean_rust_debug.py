#!/usr/bin/env python
"""Rust 编译产物清理：调试/覆盖率轮次结束后回收磁盘空间。

cargo 不会自动回收旧产物，debug 符号与 llvm-cov 插桩目录会随每轮
调试/覆盖率持续增长。定位问题用的运行时二进制是
kernel/target/release/agentos-kernel(.exe)，debug 产物与覆盖率插桩
产物在调试结束后没有保留价值，本脚本负责把它们清掉。

清理目标（保留策略：release 产物默认保留，除非 --with-release）：
- kernel/target/debug/            dev/test 构建产物（含残留的 incremental/）
- kernel/target/llvm-cov-target/  cargo-llvm-cov 插桩构建
- plugins/**/target/debug/        各插件 crate 的 debug 产物

用法：
    python scripts/clean_rust_debug.py                  # 清理 debug + 覆盖率产物
    python scripts/clean_rust_debug.py --older-than 3   # 增量回收旧 fingerprint 副本
    python scripts/clean_rust_debug.py --with-release   # 连 release 一起清（下次启动全量重编）

增量模式（--older-than N）：cargo 命中缓存靠 fingerprint 精确匹配，源码/依赖
一变就产生整套新副本而旧副本永不回收——只往前走的工作流再也不会命中它们
（实测一个月可堆 ~30G 死重）。判定单位是「同 crate 名下的 hash 簇」：
- 同一 crate 存在多个 hash 副本时，保留 mtime 最新的簇，其余簇冷置满 N 天回收；
- 只有一个 hash 簇的 crate 一律不动——哪怕很老（lockfile 未变的第三方依赖
  仍是活缓存，按 mtime 一刀切会误删并引发近乎全量的重编）。
被占用/权限不足的条目跳过留待下轮，因此本模式恒以 0 退出
（run_gates kernel-artifact-sweep 车道自动执行）。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KERNEL_TARGET = ROOT / "kernel" / "target"

# profile 直下（或其 triple 子目录下）由 cargo 按指纹管理的目录。
# deps/examples 按「文件簇」处理；build/.fingerprint/incremental 按「目录簇」处理。
_FINGERPRINT_DIRS = {"deps", "examples", "build", ".fingerprint", "incremental"}
_DIR_CLUSTER_DIRS = {"build", ".fingerprint", "incremental"}
# cargo 产物命名：{stem}-{16位hex hash}(-{ext})？，stem 可含连字符（非贪婪到 16hex 锚定）。
_HASH_RE = re.compile(r"^(?P<stem>.+)-(?P<hash>[0-9a-f]{16})(?P<ext>\..+)?$")


def _force_rmtree(path: Path) -> None:
    """Windows 兼容删除：清只读位后重试；目录被占用则抛 OSError 交上层提示。

    注意：保持 onerror= 而非 3.12+ 的 onexc=——pyproject.toml 声明
    requires-python = ">=3.11"，onexc 参数在 3.11 上不存在（3.12 才加入）。
    onerror 自 3.12 起 DeprecationWarning，本机 3.14 实测仍可正常工作；
    若未来最低支持版本升到 >=3.12，再迁移为 onexc=。
    """

    def _onerror(func, target, exc_info):
        try:
            Path(target).chmod(stat.S_IWRITE)
            func(target)
        except OSError:
            raise

    shutil.rmtree(path, onerror=_onerror)


def _dir_size(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n = int(n) // 1024
    return f"{n}B"


class SweepStats:
    """增量回收的记账：释放字节、删除条目数、跳过数。"""

    def __init__(self) -> None:
        self.freed = 0
        self.removed = 0
        self.skipped = 0


def _entry_mtime(p: Path) -> float:
    """条目 mtime；摸不到的当作最新（inf）——删除判定永远保守。"""
    try:
        return p.stat().st_mtime
    except OSError:
        return float("inf")


def _delete_entry(entry: Path, stats: SweepStats) -> None:
    try:
        if entry.is_dir():
            size = _dir_size(entry)
            _force_rmtree(entry)
        else:
            size = entry.stat().st_size
            try:
                entry.unlink()
            except PermissionError:
                entry.chmod(stat.S_IWRITE)
                entry.unlink()
        stats.freed += size
        stats.removed += 1
    except OSError:
        stats.skipped += 1


def _drop_stale_clusters(
    clusters: dict[tuple[str, str], list[Path]], cutoff: float, stats: SweepStats
) -> None:
    """删掉各 stem 下「非最新 hash 簇且冷置早于 cutoff」的簇。

    最新簇（该 stem 内 mtime 最大者）与冷置未满阈值的簇一律保留——
    前者是当前 fingerprint 的活缓存，后者给"回退源码"留缓冲窗口。
    """
    newest_by_stem: dict[str, float] = {}
    for (stem, _hash), entries in clusters.items():
        mt = max(_entry_mtime(e) for e in entries)
        newest_by_stem[stem] = max(newest_by_stem.get(stem, 0.0), mt)

    for (stem, _hash), entries in clusters.items():
        mt = max(_entry_mtime(e) for e in entries)
        if mt >= newest_by_stem[stem] or mt >= cutoff:
            continue
        for entry in entries:
            _delete_entry(entry, stats)


def _profile_dirs(target_root: Path) -> list[Path]:
    """target 根对应的 profile 目录：root 自身即 profile（…/target/debug），
    或其下的 debug/release（llvm-cov-target 形态）。"""
    if (target_root / "deps").is_dir() or (target_root / ".fingerprint").is_dir():
        return [target_root]
    return [d for d in (target_root / "debug", target_root / "release") if d.is_dir()]


def _cluster_subdirs(profile: Path) -> list[Path]:
    """profile 直下或其 triple 子目录下的指纹目录（deps/build/.fingerprint/…）。"""
    found: list[Path] = []
    try:
        level1 = sorted(profile.iterdir())
    except OSError:
        return found
    for l1 in level1:
        if not l1.is_dir():
            continue
        if l1.name in _FINGERPRINT_DIRS:
            found.append(l1)
            continue
        try:
            for l2 in sorted(l1.iterdir()):
                if l2.is_dir() and l2.name in _FINGERPRINT_DIRS:
                    found.append(l2)
        except OSError:
            continue
    return found


def sweep_old_fingerprints(profile: Path, cutoff: float, stats: SweepStats) -> None:
    """在一个 profile 目录内回收旧 hash 簇；无 hash 命名的产物一律保守不动。"""
    for d in _cluster_subdirs(profile):
        dir_mode = d.name in _DIR_CLUSTER_DIRS
        clusters: dict[tuple[str, str], list[Path]] = {}
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for entry in entries:
            m = _HASH_RE.match(entry.name)
            if m is None:
                continue
            if dir_mode and not entry.is_dir():
                continue
            if not dir_mode and entry.is_dir():
                continue
            clusters.setdefault((m["stem"], m["hash"]), []).append(entry)
        _drop_stale_clusters(clusters, cutoff, stats)


def collect_targets(with_release: bool) -> list[Path]:
    """汇总要删除的目录：kernel 主仓 + 插件子仓的 debug（及可选 release）。"""

    dirs: list[Path] = [
        KERNEL_TARGET / "debug",
        KERNEL_TARGET / "llvm-cov-target",
    ]
    # 插件 crate 自带 target/（tool_core、sensitive_checker、spill_guard…）。
    # 必须用固定深度 glob：`**` 会全树递归 plugins/（各插件 venv 使其达 20万+
    # 文件，Python glob 逐层 scandir 实测卡死数十分钟）；插件布局深度有限
    # （plugins/sdk 等单层、plugins/shared/<类>/<名> 三层），两档覆盖。
    for pattern in ("plugins/*/target/debug", "plugins/*/*/*/target/debug"):
        dirs.extend(sorted(ROOT.glob(pattern)))
    if with_release:
        dirs.append(KERNEL_TARGET / "release")
        for pattern in ("plugins/*/target/release", "plugins/*/*/*/target/release"):
            dirs.extend(sorted(ROOT.glob(pattern)))
    return [d for d in dirs if d.is_dir()]


def main() -> int:
    parser = argparse.ArgumentParser(description="清理 Rust debug/覆盖率编译产物")
    parser.add_argument(
        "--with-release",
        action="store_true",
        help="连 release 产物一起删（运行时内核二进制也会重建，慎用）",
    )
    parser.add_argument(
        "--older-than",
        type=int,
        metavar="N",
        help="增量回收：同 crate 最新 hash 副本永远保留，旧副本冷置满 N 天回收"
        "（单副本 crate 不动；占用条目跳过，恒 exit 0）",
    )
    args = parser.parse_args()
    if args.older_than is not None and args.older_than < 1:
        parser.error("--older-than 需 >= 1（天）")

    targets = collect_targets(args.with_release)
    if not targets:
        print("没有可清理的目录。")
        return 0

    if args.older_than is not None:
        cutoff = time.time() - args.older_than * 86400
        stats = SweepStats()
        for d in targets:
            sys.stdout.write(f"扫描 {d.relative_to(ROOT)} …\r")
            sys.stdout.flush()
            for profile in _profile_dirs(d):
                sweep_old_fingerprints(profile, cutoff, stats)
        note = f"  跳过 {stats.skipped}（占用/权限，下轮再试）" if stats.skipped else ""
        print(f"[sweep] 回收旧 fingerprint 条目 {stats.removed} 项 释放 {_human(stats.freed)}{note}")
        print("（同 crate 最新副本与阈值内旧副本保留）")
        return 0

    freed = 0
    failed: list[Path] = []
    for d in targets:
        sys.stdout.write(f"计算 {d.relative_to(ROOT)} …\r")
        sys.stdout.flush()
        size = _dir_size(d)
        try:
            _force_rmtree(d)
        except OSError as e:
            failed.append(d)
            print(f"[跳过] {d.relative_to(ROOT)}：{e}")
            if d.exists():
                # 目录仍在 → 多半被运行中的 exe/测试进程占用
                print("        （提示：若有 kernel/插件进程在跑，先停掉再清理）")
            continue
        freed += size
        print(f"[已删] {d.relative_to(ROOT)}  释放 {_human(size)}   ")

    print(f"\n合计释放 {_human(freed)}。")
    if failed:
        print("以下目录未能删除（被占用或权限不足）：")
        for d in failed:
            print(f"  - {d.relative_to(ROOT)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
