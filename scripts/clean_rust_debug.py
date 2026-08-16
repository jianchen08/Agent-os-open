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
    python scripts/clean_rust_debug.py --with-release   # 连 release 一起清（下次启动全量重编）
"""

from __future__ import annotations

import argparse
import shutil
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KERNEL_TARGET = ROOT / "kernel" / "target"


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


def collect_targets(with_release: bool) -> list[Path]:
    """汇总要删除的目录：kernel 主仓 + 插件子仓的 debug（及可选 release）。"""

    dirs: list[Path] = [
        KERNEL_TARGET / "debug",
        KERNEL_TARGET / "llvm-cov-target",
    ]
    # 插件 crate 自带 target/（tool_core、sensitive_checker、spill_guard…），glob 兜底未来新增。
    dirs.extend(sorted(ROOT.glob("plugins/**/target/debug")))
    if with_release:
        dirs.append(KERNEL_TARGET / "release")
        dirs.extend(sorted(ROOT.glob("plugins/**/target/release")))
    return [d for d in dirs if d.is_dir()]


def main() -> int:
    parser = argparse.ArgumentParser(description="清理 Rust debug/覆盖率编译产物")
    parser.add_argument(
        "--with-release",
        action="store_true",
        help="连 release 产物一起删（运行时内核二进制也会重建，慎用）",
    )
    args = parser.parse_args()

    targets = collect_targets(args.with_release)
    if not targets:
        print("没有可清理的目录。")
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
