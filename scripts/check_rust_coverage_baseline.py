#!/usr/bin/env python
"""Rust 覆盖率基线锁（阶段 2.3）：把 Rust line% 纳入"只升不降"门禁。

机制：
- cargo-llvm-cov 产出 lcov（CI），本脚本解析 line% 并对照 .github/rust-coverage-baseline.txt。
- line% < 基线 → 退出码 1（CI 红）。
- line% ≥ 基线 → 放行（鼓励治理后 --init 收紧）。

用法：
    python scripts/check_rust_coverage_baseline.py --lcov coverage.lcov   # CI：解析已有 lcov
    python scripts/check_rust_coverage_baseline.py --lcov coverage.lcov --init
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KERNEL_DIR = ROOT / "kernel"
BASELINE_FILE = ROOT / ".github" / "rust-coverage-baseline.txt"

# lcov 行覆盖：每条 "DA:<line>,<count>[,...]" 记一行；count>0 视为已覆盖。
DA_RE = re.compile(r"^DA:\d+,\d+")


def parse_lcov_line_pct(lcov_path: Path) -> float | None:
    """从 lcov 文件计算整体行覆盖率（%）。无 DA 行返回 None。"""
    covered = 0
    total = 0
    for line in lcov_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("DA:"):
            continue
        # DA:<line>,<count>[,<checksum>]
        parts = line[3:].split(",")
        if len(parts) < 2:
            continue
        total += 1
        try:
            if int(parts[1]) > 0:
                covered += 1
        except ValueError:
            continue
    if total == 0:
        return None
    return covered / total * 100.0


def read_baseline() -> float:
    if not BASELINE_FILE.exists():
        return 0.0
    m = re.search(r"rust_line_coverage=([0-9.]+)", BASELINE_FILE.read_text(encoding="utf-8"))
    return float(m.group(1)) if m else 0.0


def write_baseline(pct: float) -> None:
    BASELINE_FILE.write_text(
        "# Rust 行覆盖率基线（line%，只升不降，见 scripts/check_rust_coverage_baseline.py）\n"
        "# 阶段 2.3：cargo-llvm-cov 产出 lcov，本脚本解析 line% 对照此基线。\n"
        "# 保守起手（现状 64.2%，invoker 插桩 STATUS_ACCESS_VIOLATION 噪声已排除/容错）；\n"
        "# CI 跑出实测后 --init 收紧，逐步向表 D 的 P 级目标推。\n"
        f"rust_line_coverage={pct:.1f}\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Rust 覆盖率基线锁")
    parser.add_argument("--lcov", required=True, help="cargo-llvm-cov 产出的 lcov 文件路径")
    parser.add_argument("--init", action="store_true", help="用当前 line%% 写入/收紧基线")
    args = parser.parse_args()

    lcov = Path(args.lcov)
    if not lcov.exists():
        print(f"[rust-cov] ❌ lcov 文件不存在: {lcov}", file=sys.stderr)
        return 1

    pct = parse_lcov_line_pct(lcov)
    if pct is None:
        print("[rust-cov] ⚠️ lcov 无 DA 行（覆盖率数据为空），跳过门禁", file=sys.stderr)
        return 0

    baseline = read_baseline()
    print(f"[rust-cov] line% = {pct:.2f}（基线 {baseline:.1f}）")

    if args.init:
        write_baseline(pct)
        print(f"[rust-cov] ✅ 基线已写入：rust_line_coverage={pct:.1f}")
        return 0

    if pct + 1e-6 < baseline:
        print(
            f"[rust-cov] ❌ line% {pct:.2f} < 基线 {baseline:.1f}（只升不降）",
            file=sys.stderr,
        )
        return 1

    status = "持平" if abs(pct - baseline) < 1e-6 else f"上升 {pct - baseline:.2f}"
    print(f"[rust-cov] ✅ line% {pct:.2f} ≥ 基线 {baseline:.1f}（{status}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
