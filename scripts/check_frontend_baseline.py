#!/usr/bin/env python
"""前端测试基线锁：防止 vitest 失败数 + ESLint error 数增长。

机制：
- .github/frontend-baseline.txt 记录当前允许的失败数上限（vitest failures + eslint errors）
- 新代码若让失败数增加 → CI 失败
- 失败数减少 → 鼓励治理（手动收紧基线）

用法：
    python scripts/check_frontend_baseline.py                                  # 本地：自跑 vitest+eslint 并检查
    python scripts/check_frontend_baseline.py --vitest-file A --eslint-file B   # CI：复用已 tee 的输出
    python scripts/check_frontend_baseline.py --init                            # 用当前结果写入/收紧基线
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
BASELINE_FILE = ROOT / ".github" / "frontend-baseline.txt"


def parse_vitest_failures(output: str) -> int:
    """从 vitest 输出文本解析失败测试数（纯文本解析，不执行子进程）。

    匹配总结行 "Tests  109 failed | 737 passed (846)"。注意区分
    "Test Files  N failed" 和 "Tests  N failed"，取后者（最后一个匹配）。
    解析不到 → 抛错（而非静默返回 0，避免基线锁误判"全部通过"）。
    """
    output = re.sub(r"\x1b\[[0-9;]*m", "", output)  # 去 ANSI 颜色码
    matches = re.findall(r"Tests\s+(\d+)\s+failed", output)
    if matches:
        return int(matches[-1])
    print("⚠️ vitest 输出未匹配到 'Tests N failed' 总结行")
    print("原始输出尾部：")
    print(output[-1500:] if output.strip() else "(空输出)")
    raise RuntimeError("vitest 未输出总结行，无法解析失败数（检查 vitest 是否正常启动）")


def parse_eslint_errors(output: str) -> int:
    """从 ESLint 输出文本解析 error 数。匹配 "✖ 853 problems (33 errors, 820 warnings)"。"""
    m = re.search(r"\((\d+)\s+errors?,", output)
    return int(m.group(1)) if m else 0


def count_vitest_failures() -> int:
    """运行 vitest 并解析失败测试数。"""
    result = subprocess.run(
        ["npx", "vitest", "run"],
        capture_output=True,
        text=True,
        cwd=FRONTEND,
        check=False,
    )
    return parse_vitest_failures(result.stdout + result.stderr)


def count_eslint_errors() -> int:
    """运行 ESLint 并解析 error 数。"""
    result = subprocess.run(
        ["npm", "run", "lint"],
        capture_output=True,
        text=True,
        cwd=FRONTEND,
        check=False,
    )
    return parse_eslint_errors(result.stdout + result.stderr)


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
    parser = argparse.ArgumentParser(description="前端测试基线锁")
    parser.add_argument(
        "--vitest-file",
        metavar="PATH",
        help="复用已 tee 的 vitest 输出文件（CI 用，避免重复跑 vitest）",
    )
    parser.add_argument(
        "--eslint-file",
        metavar="PATH",
        help="复用已 tee 的 eslint 输出文件（CI 用，避免重复跑 lint）",
    )
    parser.add_argument("--init", action="store_true", help="用当前结果写入/收紧基线")
    args = parser.parse_args()

    base_v, base_e = read_baseline()

    if args.vitest_file or args.eslint_file:
        # CI 模式：复用 frontend-test job 已 tee 的输出（N1 修复，避免重跑）。
        vitest_out = Path(args.vitest_file).read_text(encoding="utf-8", errors="replace") if args.vitest_file else ""
        eslint_out = Path(args.eslint_file).read_text(encoding="utf-8", errors="replace") if args.eslint_file else ""
        cur_v = parse_vitest_failures(vitest_out) if vitest_out else 0
        cur_e = parse_eslint_errors(eslint_out) if eslint_out else 0
    else:
        print("运行 vitest（约 30s）...")
        cur_v = count_vitest_failures()
        print("运行 ESLint...")
        cur_e = count_eslint_errors()

    print("\n         基线    当前")
    print(f"vitest:  {base_v:<6}  {cur_v}")
    print(f"eslint:  {base_e:<6}  {cur_e}")

    if args.init:
        write_baseline(cur_v, cur_e)
        print(f"\n✅ 基线已写入: vitest={cur_v} eslint={cur_e}")
        return 0

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
        print("（基线不自动更新：请在 CI 验证后手动收紧 .github/frontend-baseline.txt）")
        return 0

    print("\n✅ 与基线持平，无新增失败")
    return 0


if __name__ == "__main__":
    sys.exit(main())
