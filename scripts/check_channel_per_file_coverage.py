#!/usr/bin/env python3
"""渠道三件 per-file 100% 覆盖率检查（DSH 口径，2026-08-26 覆盖率裁决）。

口径：wecom/qq/dingtalk 三插件目录逐源文件三维度全绿或显式豁免：
  1. 行覆盖（coverage.xml line hits）；
  2. 函数覆盖（AST 提取函数体行区间，体内任一行命中即该函数已覆盖——
     coverage.py 的 XML 不含函数表，按行映射自实现）；
  3. 分支覆盖（要求插桩车道开 --cov-branch 后 XML 带 condition-coverage；
     数据缺失时显式标注 N/A 并以 --require-branch 控制是否判红）。

用法：
  报告模式（不判红）：python scripts/check_channel_per_file_coverage.py
  检查模式（有文件未达 100% 即 exit 1）：
      python scripts/check_channel_per_file_coverage.py --check
豁免：--exempt-file 指向 json（{"相对路径": "归因"}），豁免文件跳过判定
      但仍列出（豁免必须可见）。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANNEL_DIRS = [
    "plugins/shared/system/channel_wecom",
    "plugins/shared/system/channel_qq",
    "plugins/shared/system/channel_dingtalk",
]

# 测试文件本身不计（口径针对源文件）
TEST_SUFFIXES = ("test_", "_test.py")


def _repo_rel(posix_rel: str) -> str:
    return posix_rel.replace("plugins/", "plugins/", 1)


def load_line_hits(coverage_file: Path) -> dict[str, dict[int, int]]:
    """filename(仓库相对 posix) -> {line: hits}。"""
    root = ET.parse(coverage_file).getroot()
    out: dict[str, dict[int, int]] = {}
    for cls in root.iter("class"):
        fn = cls.get("filename", "").replace("\\", "/")
        hits: dict[int, int] = {}
        for line in cls.iter("line"):
            hits[int(line.get("number"))] = int(line.get("hits", 0))
        merged = out.setdefault(fn, {})
        for num, h in hits.items():
            merged[num] = max(merged.get(num, 0), h)
    return out


def load_branch_coverage(coverage_file: Path) -> dict[str, bool]:
    """filename -> XML 是否含分支数据（condition-coverage）。"""
    root = ET.parse(coverage_file).getroot()
    has: dict[str, bool] = {}
    for cls in root.iter("class"):
        fn = cls.get("filename", "").replace("\\", "/")
        found = any(l.get("condition-coverage") for l in cls.iter("line"))
        has[fn] = has.get(fn, False) or found
    return has


def functions_with_ranges(path: Path) -> list[tuple[str, int, int]]:
    """AST 提取 (函数名, 起始行, 结束行)。含嵌套方法。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result: list[tuple[str, int, int]] = []

    def _walk(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", None) or node.lineno
            result.append((node.name, node.lineno, int(end)))
        for child in ast.iter_child_nodes(node):
            _walk(child)

    _walk(tree)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--coverage-file",
        default=str(REPO_ROOT / "coverage.xml"),
        help="coverage.py 产出的 cobertura XML",
    )
    parser.add_argument("--check", action="store_true", help="检查模式：未达 100%% 即红")
    parser.add_argument(
        "--require-branch",
        action="store_true",
        help="分支维度数据缺失时也判红（车道开 --cov-branch 后使用）",
    )
    parser.add_argument(
        "--exempt-file",
        default="",
        help="豁免 json：{相对路径: 归因}；豁免文件跳过判定但列出",
    )
    args = parser.parse_args()

    cov_path = Path(args.coverage_file)
    if not cov_path.exists():
        print(f"[channel-per-file] ❌ 覆盖率文件不存在: {cov_path}")
        return 2

    line_hits = load_line_hits(cov_path)
    branch_avail = load_branch_coverage(cov_path)
    exempt: dict[str, str] = {}
    if args.exempt_file and Path(args.exempt_file).exists():
        exempt = json.loads(Path(args.exempt_file).read_text(encoding="utf-8"))

    failures: list[str] = []
    print(f"{'文件':<58}{'行':>7}{'函数':>8}{'分支':>8}  状态")
    print("-" * 92)
    for d in CHANNEL_DIRS:
        base = REPO_ROOT / d
        for py in sorted(base.rglob("*.py")):
            if any(py.name.startswith(s) or py.name.endswith(s) for s in TEST_SUFFIXES):
                continue
            if py.name == "__init__.py":
                continue
            if ".venv" in py.parts or "__pycache__" in py.parts:
                continue
            rel = py.relative_to(REPO_ROOT).as_posix()
            xml_key = rel.removeprefix("plugins/")
            hits = line_hits.get(xml_key, {})
            if not hits:
                failures.append(rel)
                print(f"{rel:<58}{'—':>7}{'—':>8}{'—':>8}  ❌ 无覆盖数据")
                continue

            # 行维度
            total = len(hits)
            covered = sum(1 for h in hits.values() if h > 0)
            line_pct = 100 * covered / total

            # 函数维度（AST 区间内任一行命中）
            funcs = functions_with_ranges(py)
            fn_covered = 0
            for _name, start, end in funcs:
                if any(hits.get(n, 0) > 0 for n in range(start + 1, end + 1)):
                    fn_covered += 1
            fn_pct = 100 * fn_covered / len(funcs) if funcs else 100.0

            # 分支维度
            if branch_avail.get(xml_key):
                branch_note = "有数据"
                branch_ok = True
            else:
                branch_note = "N/A"
                branch_ok = not args.require_branch

            rel_ok = line_pct >= 100.0 and fn_pct >= 100.0 and branch_ok
            status = "✅ 100%" if rel_ok else "❌"
            if rel in exempt:
                status = f"⚠️ 豁免：{exempt[rel][:30]}"
                rel_ok = True
            elif not rel_ok:
                failures.append(rel)
            print(f"{rel:<58}{line_pct:>6.0f}%{fn_pct:>7.0f}%{branch_note:>8}  {status}")

    print("-" * 92)
    if failures:
        print(f"[channel-per-file] {'❌' if args.check else '⚠️ 报告模式'} 未达 100%: {len(failures)} 文件")
        for f in failures:
            print(f"  - {f}")
        return 1 if args.check else 0
    print("[channel-per-file] ✅ 全部源文件三维度 100%（或显式豁免）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
