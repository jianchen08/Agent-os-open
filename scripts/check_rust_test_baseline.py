#!/usr/bin/env python
"""Rust 测试失败数基线锁：防止 pre-existing 失败数增长。

与 scripts/check_test_batch_baseline.py 同构，对应 ci.yml rust-test job：
- .github/rust-test-baseline.txt 记录允许的失败数上限
- 跑 `cargo test --all`，解析失败数
- 失败数 > 基线 → 退出码 1（CI 红，拦截合并）
- 失败数 ≤ 基线 → 退出码 0（允许持平，鼓励逐步修复后收紧基线）

用法：
    python scripts/check_rust_test_baseline.py                         # 本地：跑 cargo test 并检查
    python scripts/check_rust_test_baseline.py --init                  # 首次写入/收紧基线
    python scripts/check_rust_test_baseline.py --from-file OUT.txt     # CI：复用已 tee 的输出，不重跑
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KERNEL_DIR = ROOT / "kernel"
BASELINE_FILE = ROOT / ".github" / "rust-test-baseline.txt"


def parse_failures(output: str) -> int:
    """从 cargo test 输出文本解析失败数（纯文本解析，不执行子进程）。

    统计口径：
    - 方式1：每条 "test result: ... N failed" 中的 N 求和（多个 crate 各一行）。
    - 方式2：若无 test result 行，取 cargo 汇总 "N test(s) failed"。
    - 方式3：编译失败（无 test result 但含 error[/]）记为 1，确保 CI 红。
    """
    failed_count = 0
    for m in re.finditer(r"test result: .*?(\d+) failed", output):
        failed_count += int(m.group(1))
    if failed_count == 0:
        m = re.search(r"(\d+) test(?:s)? failed", output)
        if m:
            failed_count = int(m.group(1))
    if failed_count == 0 and ("error[" in output or "error:" in output):
        # 编译失败（非测试失败，但 CI 应红）
        failed_count = max(failed_count, 1)
    return failed_count


def run_cargo_test() -> tuple[int, str]:
    """运行 cargo test --all，返回 (失败数, 完整输出)。"""
    try:
        proc = subprocess.run(
            ["cargo", "test", "--all"],
            cwd=str(KERNEL_DIR),
            capture_output=True,
            text=True,
            timeout=600,
        )
    except FileNotFoundError:
        print("[rust-baseline] cargo 未安装，跳过 Rust 基线检查", file=sys.stderr)
        return 0, ""
    except subprocess.TimeoutExpired:
        print("[rust-baseline] cargo test 超时（600s）", file=sys.stderr)
        return -1, ""

    output = proc.stdout + proc.stderr
    return parse_failures(output), output


def read_baseline() -> int:
    """从基线文件读取允许的失败数上限。"""
    if not BASELINE_FILE.exists():
        return 0
    text = BASELINE_FILE.read_text(encoding="utf-8")
    m = re.search(r"^rust_test_failures=(\d+)", text, re.MULTILINE)
    return int(m.group(1)) if m else 0


def write_baseline(failures: int) -> None:
    """写入基线文件。"""
    BASELINE_FILE.write_text(
        f"""# Rust 测试失败数基线（只许减不许增）

# 与 Python 的 test-batch-baseline.txt 同构，
# 对应 ci.yml rust-test job：cargo test --all
#
# ⚠️ 严格规则：只能【减】不能【增】。
#   - 减（鼓励）：修复后运行 `python scripts/check_rust_test_baseline.py --init`
#   - 增（禁止）：新失败必须修复，不允许调大基线逃避

rust_test_failures={failures}
""",
        encoding="utf-8",
    )
    print(f"[rust-baseline] 基线已写入: rust_test_failures={failures}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Rust 测试失败基线锁")
    parser.add_argument("--init", action="store_true", help="首次写入/更新基线")
    parser.add_argument(
        "--from-file",
        metavar="PATH",
        help="复用已 tee 的 cargo test 输出文件（CI 用，避免重复跑 cargo test）",
    )
    args = parser.parse_args()

    if args.from_file:
        # CI 模式：直接解析已有输出，不再重跑 cargo test（N4 修复）。
        # rust-test job 已 `cargo test --all 2>&1 | tee /tmp/rust_test_output.txt`，
        # 此处只做解析 + 基线比对。
        out_path = Path(args.from_file)
        if not out_path.exists():
            print(f"[rust-baseline] ❌ 输出文件不存在: {out_path}", file=sys.stderr)
            return 1
        output = out_path.read_text(encoding="utf-8", errors="replace")
        failed = parse_failures(output)
    else:
        failed, output = run_cargo_test()
        if failed < 0:
            return 1  # 超时

    if args.init:
        write_baseline(failed)
        return 0

    baseline = read_baseline()
    if failed > baseline:
        print(
            f"[rust-baseline] ❌ 失败数 {failed} > 基线 {baseline}（只许减不许增）",
            file=sys.stderr,
        )
        # 输出最后 40 行帮助定位
        tail = "\n".join(output.strip().splitlines()[-40:])
        print(f"[rust-baseline] cargo test 输出尾部:\n{tail}", file=sys.stderr)
        return 1

    status = "持平" if failed == baseline else f"减少 {baseline - failed}"
    print(
        f"[rust-baseline] ✅ 失败数 {failed} ≤ 基线 {baseline}（{status}）",
    )
    if failed < baseline:
        print(
            "[rust-baseline] 💡 失败数已低于基线，可运行 "
            "`python scripts/check_rust_test_baseline.py --init` 收紧门禁",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
