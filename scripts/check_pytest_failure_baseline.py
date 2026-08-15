#!/usr/bin/env python
"""pytest 失败数基线锁（只减不增）——pre-existing 失败不误红 CI、新失败拦截合并。

与 check_frontend_baseline.py / check_rust_test_baseline.py 同构，服务
run_gates.py 的两条 Python 插件测试车道（coverage-exempt-heavy-suites 拆分后）：

  - lane=plugins-coverage：插桩 gate（豁免名单之外的全部插件测试 + 覆盖率）
  - lane=plugins-heavy   ：免插桩 gate（tests/plugins/test_plugin_smoke_matrix.py）

机制：
  - .github/pytest-failure-baseline.txt 记录各 lane 允许的 failed+errors 上限
  - pytest 输出经 tee 捕获后由 --from-file 解析（不重复跑测试）
  - 计数 > 基线 → 退出码 1；≤ 基线 → 0（允许持平，鼓励修复后收紧）
  - 找不到 pytest 汇总行（崩溃/收集失败/输出截断）→ 退出码 1，大声失败，
    不把"没数到"当"零失败"

用法：
    python scripts/check_pytest_failure_baseline.py --lane plugins-coverage --from-file out.txt
    python scripts/check_pytest_failure_baseline.py --lane plugins-coverage --from-file out.txt --init

基线维护：
  - 只许减不许增；修复 pre-existing 失败后用 --init（或手改）收紧到新计数。
  - 首次基线（2026-08-15，dev/0.2 本地实测）含 +1 余量防 CI 慢机 Timeout 抖动
    （沿 test-batch-baseline 先例）：coverage 实测 111 failed + 14 errors → 126，
    heavy 实测 10 failed → 11。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = ROOT / ".github" / "pytest-failure-baseline.txt"

LANES = ("plugins-coverage", "plugins-heavy")

# pytest 结尾汇总行，如：
#   ================== 111 failed, 999 passed, 70 skipped, 4 xfailed, 1 warning, 14 errors in 108.75s ==================
SUMMARY_RE = re.compile(r"^=+\s+(?P<counts>(?:\d+\s+\w+(?:,)?\s+)*?\d+\s+\w+)\s+in\s+[\d:.]+.*$", re.MULTILINE)
COUNT_RE = re.compile(r"(?P<n>\d+)\s+(?P<kind>failed|passed|skipped|xfailed|xpassed|error|errors|warnings?)\b")


def parse_failures(text: str) -> int | None:
    """从 pytest 输出解析 failed + error 总数；找不到汇总行返回 None。"""
    matches = SUMMARY_RE.findall(text)
    if not matches:
        return None
    counts = matches[-1]  # 最后一行汇总（防 `-q` 中途摘要干扰）
    total = 0
    for n, kind in COUNT_RE.findall(counts):
        if kind in ("failed", "error", "errors"):
            total += int(n)
    return total


def read_baseline(lane: str) -> int | None:
    if not BASELINE_FILE.exists():
        return None
    for raw in BASELINE_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == f"{lane}_failed":
            return int(value.strip())
    return None


def write_baseline(lane: str, count: int) -> None:
    """只替换本 lane 的数值行，保留其余 lane 与注释头（--init 收紧时文档不丢）。"""
    lines: list[str] = []
    if BASELINE_FILE.exists():
        for raw in BASELINE_FILE.read_text(encoding="utf-8").splitlines():
            if raw.strip().startswith(f"{lane}_failed="):
                continue
            lines.append(raw)
    lines.append(f"{lane}_failed={count}")
    BASELINE_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="pytest 失败数基线锁（只减不增）")
    parser.add_argument("--lane", choices=LANES, required=True)
    parser.add_argument("--from-file", required=True, help="tee 捕获的 pytest 输出文件")
    parser.add_argument("--init", action="store_true", help="用当前计数写入/收紧基线")
    ns = parser.parse_args()

    text = Path(ns.from_file).read_text(encoding="utf-8", errors="replace")
    current = parse_failures(text)
    if current is None:
        print("❌ 未找到 pytest 汇总行（崩溃/收集失败/输出截断）——按失败处理，不写入基线。", file=sys.stderr)
        return 1

    if ns.init:
        write_baseline(ns.lane, current)
        print(f"基线写入：{ns.lane}_failed={current}")
        return 0

    baseline = read_baseline(ns.lane)
    if baseline is None:
        print(
            f"❌ 基线文件缺少 {ns.lane}_failed 条目（{BASELINE_FILE}）。" f"首次接入请用 --init 写入实测值。",
            file=sys.stderr,
        )
        return 1

    print(f"{ns.lane}: failed+errors={current}，基线={baseline}")
    if current > baseline:
        print(f"❌ 失败数超过基线（{current} > {baseline}，只减不增）——存在新失败，拦截合并。", file=sys.stderr)
        return 1
    if current < baseline:
        print(f"💡 失败数已低于基线，可运行 --init 收紧：{ns.lane}_failed={current}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
