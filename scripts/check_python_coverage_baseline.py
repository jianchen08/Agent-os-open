#!/usr/bin/env python
"""Python 插件覆盖率基线锁（只升不降，与错误基线同机制）。

机制（对齐 check_rust_coverage_baseline.py / check_pytest_failure_baseline.py）：
- plugins-coverage gate 产出的 coverage.xml（line-rate）对照
  .github/python-coverage-baseline.txt；
- 实测 < 基线 → 退出码 1（CI 红）；
- **自动棘轮（2026-08-21 用户裁决）**：实测 ≥ 基线（绿跑）→ 自动把基线写到
  floor(实测)+1——向上取整到下一个整数百分比（47.48→48、45.5→46、恰为整数
  →再 +1），恒高于实测留压力，下轮未提升即红是预期设计。写入只替换数值行、
  保留归因注释；改动随本批 commit 留归因（CI job 内的写入随 job 丢弃，
  以仓库提交为准）；
- --init 保留为人工精确锚定（写实测原值、拒降），仅校准场景用；
- 基线文件改动一律走 commit 留归因（AGENTS.md 门禁约定）。

取代原 `coverage report --fail-under=44` 静态地板（2026-08-20 ADR：
覆盖率棘轮门禁）。起手值 44.0 = 原 fail-under，行为与旧地板持平；
此后只升不降，向 100% 推进。

用法：
    python scripts/check_python_coverage_baseline.py               # 对照 + 绿跑自动棘轮
    python scripts/check_python_coverage_baseline.py --init        # 人工精确锚定（只升）
"""

from __future__ import annotations

import argparse
import math
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


def next_pressure_line(measured_pct: float) -> int:
    """棘轮压力线：实测向上取整到下一个整数百分比（47.48→48、45.5→46、
    恰为整数→再 +1）——基线恒高于实测，绿跑即顶到下一档。"""
    return math.floor(measured_pct) + 1


def update_baseline_value(pct: float) -> None:
    """只替换 KEY= 数值行，保留归因注释（旧 write_baseline 整文件重写会抹注释）。"""
    text = BASELINE_FILE.read_text(encoding="utf-8") if BASELINE_FILE.exists() else ""
    new_line = f"{KEY}={pct:.2f}"
    if re.search(rf"{KEY}=[0-9.]+", text):
        text = re.sub(rf"{KEY}=[0-9.]+", new_line, text, count=1)
    else:
        text = (text.rstrip("\n") + "\n" if text else "") + new_line + "\n"
    BASELINE_FILE.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Python 插件覆盖率基线锁")
    parser.add_argument(
        "--xml",
        default=str(ROOT / "coverage.xml"),
        help="coverage.xml 路径（默认仓库根 coverage.xml）",
    )
    parser.add_argument("--init", action="store_true", help="用当前实测收紧基线（只升不降）")
    parser.add_argument(
        "--skip",
        action="store_true",
        help="暂时挂起基线判定（打印提示后放行）。基线值与归因注释保留不动；"
        "恢复 = 摘掉 --skip 传参，基线回到既有压力线。",
    )
    args = parser.parse_args()

    if args.skip:
        baseline = read_baseline()
        print(
            f"[python-cov] ⏸️ 覆盖率基线门禁暂时挂起（当前基线 {baseline:.2f}% 保留在"
            " .github/python-coverage-baseline.txt）。恢复 = 去掉 --skip。"
        )
        return 0

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
        update_baseline_value(pct)
        print(f"[python-cov] 基线已锚定实测: {baseline:.2f}% → {pct:.2f}%")
        return 0

    if pct < baseline:
        print(
            f"[python-cov] ❌ 行覆盖率 {pct:.2f}% 低于基线 {baseline:.2f}%（只升不降）。\n"
            "  不允许调低基线逃避；请补测试，或确认豁免名单（scripts/coverage_exempt.py）。",
            file=sys.stderr,
        )
        return 1

    ratchet_to = next_pressure_line(pct)
    update_baseline_value(ratchet_to)
    print(f"[python-cov] ✅ 行覆盖率 {pct:.2f}% ≥ 基线 {baseline:.2f}%")
    print(
        f"[python-cov] 🔧 基线自动棘轮: {baseline:.2f}% → {ratchet_to:.2f}%"
        f"（实测 {pct:.2f} 向上取整到下一整数；下轮需 ≥ {ratchet_to:.2f} 才绿）。"
    )
    print("[python-cov] 基线文件已就地更新，随本批改动 commit 留归因（CI job 内的写入随 job 丢弃，以仓库提交为准）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
