#!/usr/bin/env python
"""TDD 合规检查：PR 中有源码变更时，必须有对应的测试变更。

检查逻辑：
1. 用 git diff 获取 PR 变更的文件列表
2. 区分"源码文件"和"测试文件"
3. 如果有源码变更但零测试变更 → 失败（除非显式声明 [skip-tdd]）

豁免规则（不触发检查）：
- PR 标题/描述含 [skip-tdd]（仅限配置/文档/重构类变更）
- 只改了 .md / .yaml / .json / .toml / .txt / .env / Dockerfile
- 只改了 frontend/（前端有独立测试体系）

用法：
    python scripts/check_tdd_compliance.py                 # 检查工作区 vs main
    python scripts/check_tdd_compliance.py --base develop  # 指定基线分支

CI 中由 ci.yml 的 tdd-gate job 调用。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 源码文件扩展名（变更这些需要测试变更）
SOURCE_EXTENSIONS = {".py", ".rs"}

# 源码文件路径模式（这些路径下的变更算"源码变更"）
SOURCE_PATH_PATTERNS = [
    r"^src/",            # Python 主源码
    r"^plugins/.*/server\.py$",
    r"^plugins/.*/tool\.py$",
    r"^plugins/.*/plugin\.py$",  # pipeline 插件
    r"^plugins/shared/",   # 插件共享代码
    r"^kernel/crates/.+\.rs$",  # Rust 内核源码
]

# 测试文件路径模式（这些路径下的变更算"测试变更"）
TEST_PATH_PATTERNS = [
    r"/tests?/.*test_.*\.py$",
    r"/test_[^/]+\.py$",
    r"_test\.rs$",        # Rust 单元测试文件
    r"/tests/.*\.rs$",    # Rust 集成测试
    r"tests/.*\.py$",
    r"conftest\.py$",     # pytest 配置也算测试基础设施
]

# 豁免：纯这些文件类型的变更不触发检查
EXEMPT_EXTENSIONS = {".md", ".yaml", ".yml", ".json", ".toml", ".txt", ".env", ".cfg", ".ini", ".lock"}

# 豁免路径（变更这些路径不触发检查）
EXEMPT_PATHS = [
    "frontend/",          # 前端有独立测试体系
    "docs/",
    ".github/",
    "docker/",
    "scripts/",           # CI/运维脚本（check_tdd 自身除外）
    "config/rules/",      # agent 规则配置
]


def _run_git(args: list[str]) -> str | None:
    """运行 git 命令，返回 stdout；失败返回 None（与合法空输出区分，禁止静默吞错）。"""
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"[tdd-gate] git {args[0]} 调用异常: {e}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(f"[tdd-gate] git {args[0]} 失败: {proc.stderr.strip()}", file=sys.stderr)
        return None
    return proc.stdout.strip()


def get_changed_files(base: str = "main") -> list[str] | None:
    """获取相对 base 分支变更的文件列表；git 不可用时返回 None（门禁须失败）。"""
    # 优先用 GitHub Actions 的环境变量（PR base）
    if not base:
        base = os.environ.get("GITHUB_BASE_REF", "main")

    # merge-base 确保 fork point 对齐（避免目标分支自身演进导致的全量 diff）
    merge_base = _run_git(["merge-base", f"origin/{base}", "HEAD"]) or f"origin/{base}"
    candidates = [
        ["diff", "--name-only", "--diff-filter=AMR", merge_base],
        # 回退：工作区 vs HEAD（本地开发场景）
        ["diff", "--name-only", "--diff-filter=AMR", "HEAD"],
        ["diff", "--name-only", "--cached", "--diff-filter=AMR"],
    ]
    ran_any = False
    diff_output = ""
    for args in candidates:
        out = _run_git(args)
        if out is None:
            continue
        ran_any = True
        if out:
            diff_output = out
            break
    if not ran_any:
        # 三级 diff 全部无法执行：无法判定变更面，门禁必须失败而非假绿
        return None
    return [f for f in diff_output.splitlines() if f.strip()]


def is_source_file(filepath: str) -> bool:
    """是否为需要测试覆盖的源码文件。"""
    return any(re.search(p, filepath) for p in SOURCE_PATH_PATTERNS)


def is_test_file(filepath: str) -> bool:
    """是否为测试文件。"""
    return any(re.search(p, filepath) for p in TEST_PATH_PATTERNS)


def is_exempt(filepath: str) -> bool:
    """是否豁免检查（纯配置/文档/前端）。"""
    ext = Path(filepath).suffix
    if ext in EXEMPT_EXTENSIONS:
        return True
    return any(filepath.startswith(p) for p in EXEMPT_PATHS)


def main() -> int:
    parser = argparse.ArgumentParser(description="TDD 合规检查")
    parser.add_argument("--base", default="", help="基线分支（默认 main 或 GitHub PR base）")
    args = parser.parse_args()

    changed = get_changed_files(args.base)
    if changed is None:
        print("[tdd-gate] ❌ git 不可用/命令失败，无法获取变更列表——门禁失败（禁止假绿）", file=sys.stderr)
        return 1
    if not changed:
        print("[tdd-gate] 无变更文件，跳过检查")
        return 0

    # 分类
    source_changes = []
    test_changes = []
    other_changes = []
    for f in changed:
        if is_exempt(f):
            continue
        if is_test_file(f):
            test_changes.append(f)
        elif is_source_file(f):
            source_changes.append(f)
        else:
            other_changes.append(f)

    if not source_changes:
        print(f"[tdd-gate] ✅ 无源码变更（测试 {len(test_changes)}，其他 {len(other_changes)}），跳过检查")
        return 0

    if test_changes:
        print(
            f"[tdd-gate] ✅ 源码变更 {len(source_changes)} 个 + 测试变更 {len(test_changes)} 个，"
            "TDD 合规"
        )
        return 0

    # 有源码变更但零测试变更 → 检查是否声明 skip
    # GitHub Actions 的 PR 标题在 GITHUB_REF 或 commit message
    skip_markers = ["[skip-tdd]", "[no-tdd]"]
    commit_msg = _run_git(["log", "--format=%s%n%b", "-1"]) or ""
    if any(marker in commit_msg for marker in skip_markers):
        print(
            f"[tdd-gate] ⚠️ 源码变更 {len(source_changes)} 个但零测试变更——"
            "已声明 [skip-tdd]，允许通过（请确保是纯重构/配置变更）"
        )
        return 0

    print(
        f"[tdd-gate] ❌ 源码变更 {len(source_changes)} 个但零测试变更！\n"
        "TDD 规范要求：改动源码必须同时提交测试（先红后绿）。\n\n"
        "变更的源码文件：",
        file=sys.stderr,
    )
    for f in source_changes[:20]:
        print(f"  {f}", file=sys.stderr)
    if len(source_changes) > 20:
        print(f"  ...（共 {len(source_changes)} 个）", file=sys.stderr)
    print(
        "\n修复方式：\n"
        "  1. 为变更的源码补充/修改测试（推荐，TDD 流程）\n"
        "  2. 如确属纯重构/配置变更（不改行为），在 commit message 加 [skip-tdd]\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
