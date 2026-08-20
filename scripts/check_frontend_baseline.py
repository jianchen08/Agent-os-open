#!/usr/bin/env python
"""前端测试基线锁：vitest 失败数 + ESLint error 数只减不增，
vitest 覆盖率%（Lines）只升不降。

机制：
- .github/frontend-baseline.txt 记录基线（vitest_failures / eslint_errors /
  vitest_coverage_pct）
- 失败数增加或覆盖率跌破基线 → CI 失败；治理后手动收紧基线（--init）
- 覆盖率数据缺失（vitest 没跑 coverage）：基线仍是未校准 token（=1）时
  警告放行；已校准（>1）则红——配置了 coverage 却不产数据 = 度量链断裂，
  fail-loud（ADR 2026-08-20 覆盖率棘轮门禁）。

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
    """从 ESLint 输出文本解析 error 数。匹配 "✖ 853 problems (33 errors, 820 warnings)"。

    无 "problems" 汇总行 = 0 error（干净通过）；有汇总行但正则不命中 = 输出异常，
    抛错而非静默返回 0（与 parse_vitest_failures 同口径，避免基线锁误判）。
    """
    m = re.search(r"\((\d+)\s+errors?,", output)
    if m:
        return int(m.group(1))
    if "problems" in output:
        print("⚠️ ESLint 输出含 problems 汇总但无法解析 error 数")
        print("原始输出尾部：")
        print(output[-1500:])
        raise RuntimeError("ESLint 输出格式异常，无法解析 error 数")
    return 0


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


def parse_vitest_coverage_pct(output: str) -> float | None:
    """从 vitest --coverage 输出解析整体 Lines 覆盖率 %。

    优先 text-summary 的 "Lines  : xx.xx%" 行；退回 "All files" 表行的
    % Lines 列（第 4 个数值列）。无覆盖率数据返回 None（调用方按校准态判定）。
    """
    clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
    m = re.search(r"^Lines\s*:\s*([\d.]+)\s*%", clean, re.MULTILINE)
    if m:
        return float(m.group(1))
    for row in re.finditer(r"^All files\s*\|(.+)$", clean, re.MULTILINE):
        # text 表行的四个数值列（Stmts/Branch/Funcs/Lines）不带 % 符号；
        # 第 5 列 Uncovered 行号可能带数字，取前 4 个数值即止。
        nums = re.findall(r"(\d+(?:\.\d+)?)", row.group(1))
        if len(nums) >= 4:  # Stmts / Branch / Funcs / Lines
            return float(nums[3])
    return None


def read_baseline() -> tuple[int, int, float]:
    """读取基线文件，返回 (vitest_failures, eslint_errors, vitest_coverage_pct)。"""
    if not BASELINE_FILE.exists():
        return (0, 0, 0.0)
    vitest = eslint = 0
    coverage = 0.0
    for line in BASELINE_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("vitest_failures="):
            vitest = int(line.split("=")[1])
        elif line.startswith("eslint_errors="):
            eslint = int(line.split("=")[1])
        elif line.startswith("vitest_coverage_pct="):
            coverage = float(line.split("=")[1])
    return (vitest, eslint, coverage)


def write_baseline(vitest: int, eslint: int, coverage: float) -> None:
    """写入基线文件（注释口径见调用方——--init 时保留原文件注释不在此重建）。"""
    BASELINE_FILE.write_text(
        f"# 前端测试基线（vitest 失败数/eslint 错误数只减不增；\n"
        f"# 覆盖率%只升不降。见 scripts/check_frontend_baseline.py）\n"
        f"vitest_failures={vitest}\n"
        f"eslint_errors={eslint}\n"
        f"vitest_coverage_pct={coverage:.2f}\n",
        encoding="utf-8",
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

    base_v, base_e, base_c = read_baseline()

    if args.vitest_file or args.eslint_file:
        # CI 模式：复用 frontend-test job 已 tee 的输出（N1 修复，避免重跑）。
        vitest_out = Path(args.vitest_file).read_text(encoding="utf-8", errors="replace") if args.vitest_file else ""
        eslint_out = Path(args.eslint_file).read_text(encoding="utf-8", errors="replace") if args.eslint_file else ""
        cur_v = parse_vitest_failures(vitest_out) if vitest_out else 0
        cur_e = parse_eslint_errors(eslint_out) if eslint_out else 0
        cur_c = parse_vitest_coverage_pct(vitest_out) if vitest_out else None
    else:
        print("运行 vitest（约 30s）...")
        cur_v = count_vitest_failures()
        print("运行 ESLint...")
        cur_e = count_eslint_errors()
        cur_c = None  # 本地模式跑的是无插桩 vitest run，无覆盖率数据

    print("\n              基线      当前")
    print(f"vitest失败:   {base_v:<8}  {cur_v}")
    print(f"eslint错误:   {base_e:<8}  {cur_e}")
    cov_disp = f"{cur_c:.2f}%" if cur_c is not None else "无数据"
    print(f"覆盖率(Lines):{base_c:<8.2f}  {cov_disp}")

    if args.init:
        if cur_c is not None and cur_c < base_c:
            print(
                f"\n❌ --init 拒绝降覆盖率基线：实测 {cur_c:.2f}% < 现基线 {base_c:.2f}%（棘轮不可逆）",
            )
            return 1
        # 无覆盖率数据时保留原覆盖率基线值（只收紧失败数）
        new_c = cur_c if cur_c is not None else base_c
        write_baseline(cur_v, cur_e, new_c)
        print(f"\n✅ 基线已写入: vitest={cur_v} eslint={cur_e} coverage={new_c:.2f}%")
        return 0

    failed = False

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
        failed = True
    elif decreased:
        parts = []
        if cur_v < base_v:
            parts.append(f"vitest {base_v}→{cur_v}")
        if cur_e < base_e:
            parts.append(f"eslint {base_e}→{cur_e}")
        print(f"\n✅ 失败数减少了（{', '.join(parts)}）")
        print("（基线不自动更新：请在 CI 验证后手动收紧 .github/frontend-baseline.txt）")

    # 覆盖率维度（只升不降；未校准 token=1 时缺数据仅警告）
    if cur_c is None:
        if base_c > 1:
            print("\n❌ 覆盖率数据缺失：基线已校准（>1%）而 vitest 未产出覆盖率——度量链断裂，fail-loud")
            failed = True
        else:
            print("\n⚠️ 覆盖率数据缺失且基线未校准（token=1）：警告放行；CI 首绿后立即用实测收紧基线")
    elif cur_c < base_c:
        print(f"\n❌ 覆盖率跌破基线：{cur_c:.2f}% < {base_c:.2f}%（只升不降）")
        failed = True
    elif cur_c > base_c:
        print(f"\n✅ 覆盖率高于基线（{cur_c:.2f}% > {base_c:.2f}%）：请 --init 收紧")

    if failed:
        return 1
    if not increased and not decreased and cur_c == base_c:
        print("\n✅ 与基线持平，无新增失败")
    return 0


if __name__ == "__main__":
    sys.exit(main())
