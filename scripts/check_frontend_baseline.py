#!/usr/bin/env python
"""前端测试基线锁：防止 vitest 失败数 + ESLint error 数增长。

机制：
- .github/frontend-baseline.txt 记录当前允许的失败数上限（vitest failures + eslint errors）
- 新代码若让失败数增加 → CI 失败
- 失败数减少 → 自动更新基线（鼓励治理）

用法（CI 或本地）:
    python scripts/check_frontend_baseline.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
BASELINE_FILE = ROOT / ".github" / "frontend-baseline.txt"


def count_vitest_failures() -> int:
    """运行 vitest 并解析失败测试数。"""
    result = subprocess.run(
        ["npx", "vitest", "run"],
        capture_output=True,
        text=True,
        cwd=FRONTEND,
        shell=True,
    )
    output = result.stdout + result.stderr
    # 匹配 "Tests  109 failed | 737 passed (846)"
    m = re.search(r"(\d+)\s+failed", output)
    return int(m.group(1)) if m else 0


def count_eslint_errors() -> int:
    """运行 ESLint 并解析 error 数。"""
    result = subprocess.run(
        ["npm", "run", "lint"],
        capture_output=True,
        text=True,
        cwd=FRONTEND,
        shell=True,
    )
    output = result.stdout + result.stderr
    # 匹配 "✖ 853 problems (33 errors, 820 warnings)"
    m = re.search(r"\((\d+)\s+errors?,", output)
    return int(m.group(1)) if m else 0


def read_baseline() -> tuple[int, int]:
    """读取基线文件，返回 (vitest_failures, eslint_errors)。"""
    if not BASELINE_FILE.exists():
        return (0, 0)
    lines = BASELINE_FILE.read_text().strip().split("\n")
    vitest = eslint = 0
    for line in lines:
        if line.startswith("vitest_failures="):
            vitest = int(line.split("=")[1])
        elif line.startswith("eslint_errors="):
            eslint = int(line.split("=")[1])
    return (vitest, eslint)


def write_baseline(vitest: int, eslint: int) -> None:
    """写入基线文件。"""
    BASELINE_FILE.write_text(
        f"# 前端测试基线（只许减不许增，见 scripts/check_frontend_baseline.py）\n"
        f"vitest_failures={vitest}\n"
        f"eslint_errors={eslint}\n"
    )


def main() -> int:
    base_v, base_e = read_baseline()
    print("运行 vitest（约 30s）...")
    cur_v = count_vitest_failures()
    print("运行 ESLint...")
    cur_e = count_eslint_errors()

    print(f"\n         基线    当前")
    print(f"vitest:  {base_v:<6}  {cur_v}")
    print(f"eslint:  {base_e:<6}  {cur_e}")

    increased = (cur_v > base_v) or (cur_e > base_e)
    decreased = (cur_v < base_v) or (cur_e < base_e)

    if increased:
        parts = []
        if cur_v > base_v:
            parts.append(f"vitest {base_v}→{cur_v} (+{cur_v - base_v})")
        if cur_e > base_e:
            parts.append(f"eslint {base_e}→{cur_e} (+{cur_e - base_e})")
        print(f"\n❌ 失败数增加了（{', '.join(parts)}）")
        print("请修复新增的失败，或在 .github/frontend-baseline.txt 调整基线（仅允许减少）。")
        return 1

    if decreased:
        parts = []
        if cur_v < base_v:
            parts.append(f"vitest {base_v}→{cur_v}")
        if cur_e < base_e:
            parts.append(f"eslint {base_e}→{cur_e}")
        print(f"\n✅ 失败数减少了（{', '.join(parts)}），自动更新基线")
        write_baseline(cur_v, cur_e)
        return 0

    print(f"\n✅ 与基线持平，无新增失败")
    return 0


if __name__ == "__main__":
    sys.exit(main())
