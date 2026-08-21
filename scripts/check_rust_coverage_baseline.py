#!/usr/bin/env python
"""Rust 覆盖率基线锁（阶段 2.3）：把 Rust line% 纳入"只升不降"门禁。

机制：
- cargo-llvm-cov 产出 lcov（CI），本脚本解析 line% 并对照 .github/rust-coverage-baseline.txt。
- line% < 基线 → 退出码 1（CI 红）。
- **自动棘轮（2026-08-21 用户裁决）**：line% ≥ 基线（绿跑）→ 自动把基线写到
  floor(实测)+1——向上取整到下一个整数百分比（85.49→86、恰为整数→再 +1），
  恒高于实测留压力，下轮未提升即红是预期设计。写入只替换数值行、保留归因
  注释；改动随本批 commit 留归因（CI job 内的写入随 job 丢弃，以仓库为准）。
- --init 保留为人工精确锚定（写实测原值、拒降），仅校准场景用。

用法：
    python scripts/check_rust_coverage_baseline.py --lcov coverage.lcov   # 对照 + 绿跑自动棘轮
    python scripts/check_rust_coverage_baseline.py --lcov coverage.lcov --init
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KERNEL_DIR = ROOT / "kernel"
BASELINE_FILE = ROOT / ".github" / "rust-coverage-baseline.txt"
KEY = "rust_line_coverage"

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
    m = re.search(rf"{KEY}=([0-9.]+)", BASELINE_FILE.read_text(encoding="utf-8"))
    return float(m.group(1)) if m else 0.0


def next_pressure_line(measured_pct: float) -> int:
    """棘轮压力线：实测向上取整到下一个整数百分比（85.49→86、恰为整数→再 +1）。"""
    return math.floor(measured_pct) + 1


def update_baseline_value(pct: float) -> None:
    """只替换 KEY= 数值行，保留归因注释（旧 write_baseline 整文件重写会抹注释）。"""
    text = BASELINE_FILE.read_text(encoding="utf-8") if BASELINE_FILE.exists() else ""
    new_line = f"{KEY}={pct:.1f}"
    if re.search(rf"{KEY}=[0-9.]+", text):
        text = re.sub(rf"{KEY}=[0-9.]+", new_line, text, count=1)
    else:
        text = (text.rstrip("\n") + "\n" if text else "") + new_line + "\n"
    BASELINE_FILE.write_text(text, encoding="utf-8")


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
        if pct + 1e-6 < baseline:
            print(
                f"[rust-cov] ❌ --init 拒绝降基线：实测 {pct:.2f} < 现基线 {baseline:.1f}。\n"
                "  棘轮不可逆；如确有正当理由（度量口径变更等），手改基线文件并在 commit\n"
                "  message 留归因。",
                file=sys.stderr,
            )
            return 1
        update_baseline_value(pct)
        print(f"[rust-cov] ✅ 基线已锚定实测：{KEY}={pct:.1f}")
        return 0

    if pct + 1e-6 < baseline:
        print(
            f"[rust-cov] ❌ line% {pct:.2f} < 基线 {baseline:.1f}（只升不降）",
            file=sys.stderr,
        )
        return 1

    ratchet_to = next_pressure_line(pct)
    update_baseline_value(ratchet_to)
    status = "持平" if abs(pct - baseline) < 1e-6 else f"上升 {pct - baseline:.2f}"
    print(f"[rust-cov] ✅ line% {pct:.2f} ≥ 基线 {baseline:.1f}（{status}）")
    print(
        f"[rust-cov] 🔧 基线自动棘轮: {baseline:.1f} → {ratchet_to:.1f}"
        f"（实测 {pct:.2f} 向上取整到下一整数；下轮需 ≥ {ratchet_to:.1f} 才绿）。"
    )
    print(
        "[rust-cov] 基线文件已就地更新，随本批改动 commit 留归因"
        "（CI job 内的写入随 job 丢弃，以仓库提交为准）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
