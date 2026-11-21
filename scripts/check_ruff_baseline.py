#!/usr/bin/env python3
"""ruff 基线棘轮通用检查器（只减不增）——scripts-lint / plugins-lint 消费。

消费的基线账本（元门禁 V7 静态扫描依赖本处字面登记，勿改动态化）：
    .github/ruff-scripts-baseline.txt   （scripts-lint 车道）
    .github/ruff-plugins-baseline.txt   （plugins-lint 车道）

与 check_knip_jscpd_baseline.py 同款门禁形态：门禁 shell 先 `ruff check <目标>
|| true | tee`，本检查器消费日志对照基线文件：

    .github/ruff-<name>-baseline.txt   键: ruff_findings=N

判定（fail-closed：测不准一律判红，"没数到"绝不推断为"零违规"）：
- 日志缺 `Found N error` 汇总行 → 红（空日志 / ruff 未安装 / 启动失败 /
  输出截断 / 输出格式变更，都无法证明 ruff 真跑通）
- 日志含 invalid-syntax → 红（语法错误文件落入 lint 盲区，基线计数不可信）
- 基线文件缺失 / 无 `ruff_findings=` 键 / 值非非负整数 → 红（--name 拼错
  曾被静默当成基线 0 而判绿）
- 当前 > 基线 → 红（新增违规，拦截合并）
- 当前 < 基线 → 通过并提示可收紧（--init 打印新值，收紧须单独 commit 留归因）

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
BASELINE_KEY = "ruff_findings"
LOG_PREVIEW_CHARS = 200


def baseline_path(name: str) -> Path:
    return ROOT / ".github" / f"ruff-{name}-baseline.txt"


def parse_findings(log: str) -> int | None:
    """解析 `Found N error(s).` 汇总行；无汇总行返回 None（测量失败，不是零违规）。"""
    m = FOUND_RE.search(log)
    return int(m.group(1)) if m else None


def read_baseline(path: Path) -> int | None:
    """读基线键值；文件缺失 / 缺键 / 值非非负整数 → None（由调用方判红）。"""
    if not path.is_file():
        return None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith(f"{BASELINE_KEY}="):
            continue
        try:
            value = int(line.split("=", 1)[1])
        except ValueError:
            return None
        return value if value >= 0 else None
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="ruff 基线棘轮检查器")
    parser.add_argument("--name", required=True, help="车道名（决定基线文件 ruff-<name>-baseline.txt）")
    parser.add_argument("--from-file", required=True, help="ruff 输出日志路径")
    parser.add_argument("--init", action="store_true", help="按当前实测打印新基线（收紧用，禁止上调）")
    ns = parser.parse_args()

    log_path = Path(ns.from_file)
    if not log_path.is_file():
        print(f"ruff-{ns.name}: 日志文件不存在 {log_path}——ruff 未执行或输出未落盘，不判绿。")
        return 1
    log = log_path.read_text(encoding="utf-8", errors="replace")

    current = parse_findings(log)
    if current is None:
        preview = log.strip()[:LOG_PREVIEW_CHARS].replace("\n", " / ")
        print(
            f"ruff-{ns.name}: 日志无 `Found N error` 汇总行——无法证明 ruff 实际跑通"
            "（空日志 / ruff 未安装 / 启动失败 / 输出截断 / 格式变更皆然），"
            f"「没数到」不推断为零违规，按失败处理 | 日志首 {LOG_PREVIEW_CHARS} 字符: {preview!r}"
        )
        return 1

    if SYNTAX_RE.search(log):
        print(
            f"ruff-{ns.name}: 日志含 invalid-syntax——语法错误使文件落入 lint 盲区，"
            "基线计数不可信；先修语法（参照 trajectory_harvest 先例）。"
        )
        return 1

    if ns.init:
        print(f"{BASELINE_KEY}={current}")
        return 0

    bp = baseline_path(ns.name)
    baseline = read_baseline(bp)
    if baseline is None:
        print(
            f"ruff-{ns.name}: 基线不可读（{bp} 缺失或缺少 `{BASELINE_KEY}=<非负整数>`）——"
            "无法比对即不判绿，核对 --name 是否拼错。"
        )
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
