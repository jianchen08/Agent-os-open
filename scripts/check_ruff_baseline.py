#!/usr/bin/env python3
"""ruff 基线棘轮通用检查器（只减不增）——scripts-lint / plugins-lint 消费。

消费的基线账本（元门禁 V7 静态扫描依赖本处字面登记，勿改动态化）：
    .github/ruff-scripts-baseline.txt   （scripts-lint 车道）
    .github/ruff-plugins-baseline.txt   （plugins-lint 车道）

与 check_knip_jscpd_baseline.py 同款门禁形态：门禁 shell 先 `ruff check <目标>
|| true | tee`，本检查器消费日志对照基线文件：

    .github/ruff-<name>-baseline.txt   键: ruff_findings=N

- 当前 > 基线 → exit 1（新增违规，拦截合并）
- 当前 < 基线 → 提示可收紧（--init 打印新值，收紧须单独 commit 留归因）
- 日志含 invalid-syntax → 直接红（语法错误文件落入 lint 盲区，基线不可信）

用法：
    python scripts/check_ruff_baseline.py --name scripts --from-file log.txt
    python scripts/check_ruff_baseline.py --name scripts --init --from-file log.txt
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FOUND_RE = re.compile(r"^Found (\d+) error", re.M)
SYNTAX_RE = re.compile(r"invalid-syntax")


def baseline_path(name: str) -> Path:
    return ROOT / ".github" / f"ruff-{name}-baseline.txt"


def parse_findings(log: str) -> int:
    m = FOUND_RE.search(log)
    return int(m.group(1)) if m else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ruff 基线棘轮检查器")
    parser.add_argument("--name", required=True, help="车道名（决定基线文件 ruff-<name>-baseline.txt）")
    parser.add_argument("--from-file", required=True, help="ruff 输出日志路径")
    parser.add_argument("--init", action="store_true", help="按当前实测打印新基线（收紧用，禁止上调）")
    ns = parser.parse_args()

    log = Path(ns.from_file).read_text(encoding="utf-8", errors="replace")
    current = parse_findings(log)

    bp = baseline_path(ns.name)
    baseline = 0
    if bp.exists():
        for raw in bp.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("ruff_findings="):
                baseline = int(line.split("=", 1)[1])

    if ns.init:
        print(f"ruff_findings={current}")
        return 0

    if SYNTAX_RE.search(log):
        print(f"ruff-{ns.name}: 日志含 invalid-syntax——语法错误使文件落入 lint 盲区，"
              "基线计数不可信；先修语法（参照 trajectory_harvest 先例）。")
        return 1

    if current > baseline:
        print(f"ruff-{ns.name}: 违规 {current} > 基线 {baseline}（只减不增）——存在新增违规，拦截合并。")
        return 1
    if current < baseline:
        print(f"ruff-{ns.name}: 通过（当前 {current} < 基线 {baseline}，可 --init 收紧并 commit 留归因）。")
        return 0
    print(f"ruff-{ns.name}: 通过（当前 {current} = 基线 {baseline}）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
