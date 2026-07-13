#!/usr/bin/env python
"""mypy 基线锁：防止类型错误数增长。

机制：
- .github/mypy-baseline.txt 记录当前允许的 mypy 错误数上限
- 新代码若让错误数增加 → CI 失败
- 错误数减少 → 自动更新基线（鼓励治理）

用法（CI 或本地）:
    python scripts/check_mypy_baseline.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = ROOT / ".github" / "mypy-baseline.txt"


def count_mypy_errors() -> int:
    """运行 mypy 并返回错误数。"""
    result = subprocess.run(
        ["mypy", "src/", "--config-file", "pyproject.toml", "--no-incremental"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    # mypy 输出里 count "error:" 行
    output = result.stdout + result.stderr
    return sum(1 for line in output.splitlines() if "error:" in line)


def read_baseline() -> int:
    """读取基线文件。"""
    if not BASELINE_FILE.exists():
        return 0
    return int(BASELINE_FILE.read_text().strip())


def write_baseline(count: int) -> None:
    """写入基线文件。"""
    BASELINE_FILE.write_text(f"{count}\n")


def main() -> int:
    baseline = read_baseline()
    current = count_mypy_errors()

    print(f"基线: {baseline}")
    print(f"当前: {current}")

    if current > baseline:
        print(f"\n❌ mypy 错误数增加了 {current - baseline} 个（{baseline} → {current}）")
        print("新代码引入了类型错误。请修复，或在 .github/mypy-baseline.txt 调整基线（仅允许减少）。")
        return 1

    if current < baseline:
        print(f"\n✅ mypy 错误数减少了 {baseline - current} 个（{baseline} → {current}）")
        print("（基线不自动更新：本地与 CI 环境可能存在差异，请在 CI 验证后手动更新 .github/mypy-baseline.txt）")
        return 0

    print(f"\n✅ mypy 错误数与基线持平（{current}），无新增类型错误")
    return 0


if __name__ == "__main__":
    sys.exit(main())
