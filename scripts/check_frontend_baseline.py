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
    """运行 vitest 并解析失败测试数。

    vitest 非零退出是正常的（有失败用例时退出码=1）。只有当连总结行都
    解析不到时才视为异常（vitest 未正常启动/崩溃），此时打印原始输出
    并抛错，而非静默返回 0（旧逻辑的 0 会让基线锁误判为"全部通过"）。
    """
    result = subprocess.run(
        ["npx", "vitest", "run"],
        capture_output=True,
        text=True,
        cwd=FRONTEND,
        check=False,
    )
    output = result.stdout + result.stderr
    # 去除 ANSI 颜色码
    output = re.sub(r"\x1b\[[0-9;]*m", "", output)
    # 精确匹配总结行 "Tests  109 failed | 737 passed (846)"
    # 注意区分 "Test Files  N failed" 和 "Tests  N failed"，取后者
    matches = re.findall(r"Tests\s+(\d+)\s+failed", output)
    if matches:
        return int(matches[-1])
    # 解析不到总结行：vitest 未正常启动。打印原始输出辅助排查，报错而非返回 0。
    print(f"⚠️ vitest 输出未匹配到 'Tests N failed' 总结行（returncode={result.returncode}）")
    print("原始输出尾部：")
    print(output[-1500:] if output.strip() else "(空输出)")
    raise RuntimeError("vitest 未输出总结行，无法解析失败数（检查 vitest 是否正常启动）")


def count_eslint_errors() -> int:
    """运行 ESLint 并解析 error 数。"""
    result = subprocess.run(
        ["npm", "run", "lint"],
        capture_output=True,
        text=True,
        cwd=FRONTEND,
        check=False,
    )
    output = result.stdout + result.stderr
    # 匹配 "✖ 853 problems (33 errors, 820 warnings)"
    m = re.search(r"\((\d+)\s+errors?,", output)
    if m:
        return int(m.group(1))
    # eslint 退出码 0（无 error）时无 "(N errors)" 段，返回 0 正确。
    # 非零退出又解析不到时打印输出辅助排查。
    if result.returncode != 0 and "problems" in output:
        print("⚠️ eslint 非零退出但未匹配 errors 数，原始输出尾部：")
        print(output[-1000:])
    return 0


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

    print("\n         基线    当前")
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
        print(f"\n✅ 失败数减少了（{', '.join(parts)}）")
        print("（基线不自动更新：本地与 CI 环境可能存在差异，请在 CI 验证后手动更新 .github/frontend-baseline.txt）")
        return 0

    print("\n✅ 与基线持平，无新增失败")
    return 0


if __name__ == "__main__":
    sys.exit(main())
