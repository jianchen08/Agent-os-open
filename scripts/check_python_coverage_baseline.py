#!/usr/bin/env python
"""Python 插件覆盖率基线锁（只升不降，与错误基线同机制）。

机制（对齐 check_rust_coverage_baseline.py / check_pytest_failure_baseline.py）：
- plugins-coverage gate 产出的 coverage.xml（line-rate）对照
  .github/python-coverage-baseline.txt；
- 实测 < 基线 → 退出码 1（CI 红）；实测 ≥ 基线 → 放行；
- 覆盖率提升后运行 --init 收紧基线（只许升不许降，防止棘轮倒退）；
- 基线文件改动一律走 commit 留归因（AGENTS.md 门禁约定）。

取代原 `coverage report --fail-under=44` 静态地板（2026-08-20 ADR：
覆盖率棘轮门禁）。起手值 44.0 = 原 fail-under，行为与旧地板持平；
此后只升不降，向 100% 推进。

用法：
    python scripts/check_python_coverage_baseline.py               # CI：对照基线
    python scripts/check_python_coverage_baseline.py --init        # 收紧基线（只升）
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = ROOT / ".github" / "python-coverage-baseline.txt"
KEY = "python_line_coverage"


def parse_coverage_line_pct(xml_path: Path) -> float | None:
    """从 coverage.xml 根节点 line-rate（0..1）换算整体行覆盖率 %。

    无 line-rate 属性（空报告）返回 None，由调用方 fail-loud。
    """
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError as e:
        print(f"[python-cov] ❌ coverage.xml 解析失败: {e}", file=sys.stderr)
        return None
    rate = root.get("line-rate")
    if rate is None:
        return None
    try:
        return float(rate) * 100.0
    except ValueError:
        return None


def read_baseline() -> float:
    if not BASELINE_FILE.exists():
        return 0.0
    m = re.search(rf"{KEY}=([0-9.]+)", BASELINE_FILE.read_text(encoding="utf-8"))
    return float(m.group(1)) if m else 0.0


def write_baseline(pct: float) -> None:
    BASELINE_FILE.write_text(
        "# Python 插件整体行覆盖率基线（line%，只升不降，\n"
        "# 见 scripts/check_python_coverage_baseline.py；数据源 plugins-coverage\n"
        "# gate 的 coverage.xml）。提升覆盖率后 --init 收紧；终极目标 100%。\n"
        f"{KEY}={pct:.2f}\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Python 插件覆盖率基线锁")
    parser.add_argument(
        "--xml",
        default=str(ROOT / "coverage.xml"),
        help="coverage.xml 路径（默认仓库根 coverage.xml）",
    )
    parser.add_argument("--init", action="store_true", help="用当前实测收紧基线（只升不降）")
    args = parser.parse_args()

    xml_path = Path(args.xml)
    if not xml_path.exists():
        print(f"[python-cov] ❌ coverage.xml 不存在: {xml_path}", file=sys.stderr)
        return 1

    pct = parse_coverage_line_pct(xml_path)
    if pct is None:
        print("[python-cov] ❌ coverage.xml 无 line-rate（空报告？），拒绝放行", file=sys.stderr)
        return 1

    baseline = read_baseline()
    if args.init:
        if pct < baseline:
            print(
                f"[python-cov] ❌ --init 拒绝降基线：实测 {pct:.2f}% < 现基线 {baseline:.2f}%。\n"
                "  棘轮不可逆；如确有正当理由（度量口径变更等），手改基线文件并在 commit\n"
                "  message 留归因。",
                file=sys.stderr,
            )
            return 1
        write_baseline(pct)
        print(f"[python-cov] 基线已收紧: {baseline:.2f}% → {pct:.2f}%")
        return 0

    if pct < baseline:
        print(
            f"[python-cov] ❌ 行覆盖率 {pct:.2f}% 低于基线 {baseline:.2f}%（只升不降）。\n"
            "  不允许调低基线逃避；请补测试，或确认豁免名单（scripts/coverage_exempt.py）。",
            file=sys.stderr,
        )
        return 1
    print(f"[python-cov] ✅ 行覆盖率 {pct:.2f}% ≥ 基线 {baseline:.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
