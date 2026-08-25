#!/usr/bin/env python
"""改动行覆盖率门禁（diff coverage，目标 100%）。

口径（ADR 2026-08-20 覆盖率棘轮门禁）：
- 本 PR/本次推送**新增的源码行**必须全部被测试覆盖（fail-under=100）；
  整体覆盖率由各轨道基线锁（check_*_coverage_baseline.py / 前端基线）棘轮推进，
  本门禁保证"新增债务为零"。
- 覆盖率数据 = 同车道刚跑完的 coverage.xml（coverage.py）或 lcov
  （cargo-llvm-cov / vitest），只判 HEAD 时点的行命中，与历史无关。

对比范围（diff range）解析，优先级从高到低：
1. --range A..B（本地手工指定）；
2. --base <ref> 或环境变量 GITHUB_BASE_REF（PR 事件）：merge-base(ref, HEAD)..HEAD；
3. 无 base（push 事件 / 本地默认）：HEAD^..HEAD——每次推送只查最后一个提交，
   历史提交不重查（每次改动当次给压力，不搞累计清算）。

逃生口（与 TDD gate 同风格）：range 内任一 commit message 含 [skip-diff-cov]
→ 放行并大声警告（仅限纯重构/配置/文档类变更滥用必追责）。

scope/omit 约定：
- --scope 为路径前缀（可多个），--ext 为扩展名过滤（可多个），
  --omit 为正则（对规范化 posix 相对路径 search，可多个）——须与各轨道
  coverage 度量口径的 include/omit 对齐（见 run_gates.py 各 gate 定义处的注释）。
- 在 scope 且未被 omit 的改动文件若不出现在覆盖率文件里 → fail-loud
  （度量面漂移，宁可红不可静默放行）。

用法：
    python scripts/check_diff_coverage.py --coverage-file coverage.xml \
        --format xml --scope plugins --ext .py --omit '^plugins/sdk/'
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_MARKER = "[skip-diff-cov]"
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _git(args: list[str]) -> str:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {proc.stderr.strip()}")
    return proc.stdout


def norm_path(raw: str) -> str:
    """规范化为仓库相对 posix 路径：去 b/ 前缀、反斜杠转正斜杠、去 ROOT 前缀。"""
    p = raw.replace("\\", "/").lstrip("./")
    if p.startswith("b/"):
        p = p[2:]
    # 绝对路径（vitest lcov 常见）→ 剥仓库根前缀
    root_posix = str(ROOT).replace("\\", "/").rstrip("/")
    if p.lower().startswith(root_posix.lower() + "/"):
        p = p[len(root_posix) + 1 :]
    return p


# ── 覆盖率文件解析 ────────────────────────────────────────────────

LineMap = dict[int, bool]  # 行号 → 是否覆盖


def parse_coverage_xml(text: str) -> dict[str, LineMap]:
    """解析 coverage.py 的 XML（实测 7.x 格式：class filename 相对 <source> 根）。

    路径统一解析为仓库相对 posix 路径：绝对路径剥 ROOT；相对路径逐个
    <source> 拼接尝试（真实文件 = source 根 + filename），都不中则按
    相对 ROOT 的旧格式兜底。行命中取 number/hits 属性。
    """
    root = ET.fromstring(text)
    root_posix = str(ROOT).replace("\\", "/").rstrip("/")
    sources = [(s.text or "").replace("\\", "/").rstrip("/") for s in root.iter("source") if s.text]

    def resolve(raw: str) -> str:
        p = raw.replace("\\", "/")
        if ":" in p.split("/", 1)[0] or p.startswith("/"):
            # 绝对路径：剥仓库根
            if p.lower().startswith(root_posix.lower() + "/"):
                return p[len(root_posix) + 1 :]
            return p
        for src in sources:
            cand = f"{src}/{p}"
            if cand.lower().startswith(root_posix.lower() + "/"):
                return cand[len(root_posix) + 1 :]
        return p

    files: dict[str, LineMap] = {}

    def register(raw_path: str, line_elems: list[ET.Element]) -> None:
        p = resolve(raw_path)
        if not p:
            return
        lines: LineMap = {}
        for ln in line_elems:
            nr = ln.get("number") or ln.get("nr")
            if nr is None or not nr.isdigit():
                continue
            hits = ln.get("hits")
            covered = bool(hits and hits.isdigit() and int(hits) > 0)
            lines[int(nr)] = covered
        files[p] = lines

    for cls in root.iter("class"):
        line_elems = [ln for lines_el in cls.iter("lines") for ln in lines_el.iter("line")]
        if cls.get("filename"):
            register(cls.get("filename") or "", line_elems)
    for f in root.iter("file"):
        line_elems = [ln for lines_el in f.iter("lines") for ln in lines_el.iter("line")]
        if f.get("path"):
            register(f.get("path") or "", line_elems)
    return files


def parse_lcov(text: str) -> dict[str, LineMap]:
    """解析 lcov：SF:<path> 开节，DA:<line>,<count> 计行（count>0 为覆盖）。"""
    files: dict[str, LineMap] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("SF:"):
            current = norm_path(line[3:])
            files.setdefault(current, {})
        elif line.startswith("DA:") and current is not None:
            parts = line[3:].split(",")
            if len(parts) >= 2 and parts[0].isdigit():
                try:
                    files[current][int(parts[0])] = int(parts[1]) > 0
                except ValueError:
                    continue
    return files


def load_coverage(path: Path, fmt: str) -> dict[str, LineMap]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if fmt == "xml":
        return parse_coverage_xml(text)
    files = parse_lcov(text)
    # vitest lcov 的 SF 在部分环境（Windows 本地）是相对其项目根（frontend/）的
    # 相对路径，与 git diff 的仓库相对路径错位（度量面漂移 fail-loud）——按覆盖
    # 率文件所在项目根回拼仓库前缀；CI/Linux 写绝对路径、norm_path 已剥 ROOT，
    # 不走此分支。仓库相对路径真实存在的键不回拼（防双前缀）。
    cov_project_root = path.resolve().parent.parent
    prefix = cov_project_root.relative_to(ROOT.resolve()).as_posix()
    resolved: dict[str, LineMap] = {}
    if prefix and prefix != ".":
        for key, line_map in files.items():
            repo_key = key if (ROOT / key).exists() else f"{prefix}/{key}"
            if repo_key in resolved:
                resolved[repo_key].update(line_map)
            else:
                resolved[repo_key] = line_map
        return resolved
    return files


# ── diff 解析 ─────────────────────────────────────────────────────


def parse_unified_diff(text: str) -> dict[str, list[int]]:
    """从 git diff -U0 输出提取每个文件新增的行号（含新文件；删除行不含）。"""
    added: dict[str, list[int]] = {}
    current: str | None = None
    pending_start: int | None = None
    pending_count = 0

    def flush() -> None:
        nonlocal pending_start, pending_count
        if current is not None and pending_start is not None and pending_count > 0:
            added[current].extend(range(pending_start, pending_start + pending_count))
        pending_start, pending_count = None, 0

    for line in text.splitlines():
        if line.startswith("+++ "):
            flush()
            path = line[4:].strip()
            current = None if path == "/dev/null" else norm_path(path)
            if current is not None:
                added.setdefault(current, [])
        elif line.startswith("@@") and current is not None:
            flush()
            m = HUNK_RE.match(line)
            if m:
                pending_start = int(m.group(1))
                pending_count = 1 if m.group(2) is None else int(m.group(2))
        elif line.startswith("--- "):
            continue
    flush()
    return added


# ── 范围解析与主流程 ──────────────────────────────────────────────


def resolve_range(args: argparse.Namespace) -> tuple[str, str, str] | None:
    """返回 (diff_a, diff_b, 说明)；无可查范围返回 None。"""
    if args.range:
        a, _, b = args.range.partition("..")
        return a.strip(), (b.strip() or "HEAD"), f"--range {args.range}"

    base = args.base or os.environ.get("GITHUB_BASE_REF", "")
    if base:
        for cand in (f"origin/{base}", base):
            if (
                subprocess.run(
                    ["git", "-C", str(ROOT), "rev-parse", "--verify", "--quiet", cand],
                    capture_output=True,
                    check=False,
                ).returncode
                == 0
            ):
                try:
                    mb = _git(["merge-base", cand, "HEAD"]).strip()
                    return mb, "HEAD", f"merge-base({cand}, HEAD)"
                except RuntimeError:
                    pass
        print(f"[diff-cov] ❌ 基线分支不可解析: {base}", file=sys.stderr)
        raise SystemExit(1)

    # push / 本地默认：只查最后一个提交
    head = _git(["rev-parse", "HEAD"]).strip()
    try:
        parent = _git(["rev-parse", "HEAD^"]).strip()
    except RuntimeError:
        print("[diff-cov] 范围为空（根提交或无 HEAD^），放行")
        return None
    return parent, head, "HEAD^..HEAD（push 口径：只查最后一个提交）"


def range_has_skip_marker(a: str, b: str) -> bool:
    try:
        logs = _git(["log", "--format=%B", f"{a}..{b}"])
    except RuntimeError:
        return False
    return SKIP_MARKER in logs


def main() -> int:
    parser = argparse.ArgumentParser(description="改动行覆盖率门禁（diff coverage）")
    parser.add_argument("--coverage-file", required=True, help="coverage.xml / lcov 文件路径")
    parser.add_argument("--format", choices=["xml", "lcov"], required=True)
    parser.add_argument("--scope", action="append", required=True, help="路径前缀（可多次）")
    parser.add_argument("--ext", action="append", required=True, help="扩展名（可多次，如 .py）")
    parser.add_argument("--omit", action="append", default=[], help="省略正则（可多次）")
    parser.add_argument("--base", help="基线分支（默认取 GITHUB_BASE_REF）")
    parser.add_argument("--range", help="显式范围 A..B（本地手工）")
    parser.add_argument("--fail-under", type=float, default=100.0)
    args = parser.parse_args()

    cov_path = Path(args.coverage_file)
    if not cov_path.is_absolute():
        cov_path = ROOT / cov_path
    if not cov_path.exists():
        print(f"[diff-cov] ❌ 覆盖率文件不存在: {cov_path}", file=sys.stderr)
        return 1

    rng = resolve_range(args)
    if rng is None:
        return 0
    a, b, desc = rng
    print(f"[diff-cov] 对比范围: {desc}")

    if range_has_skip_marker(a, b):
        print(f"[diff-cov] ⚠️  {SKIP_MARKER} 已声明，放行（仅限纯重构/配置/文档变更，滥用必追责）")
        return 0

    diff_text = _git(["diff", "-U0", "--no-color", "--no-ext-diff", "-M", "--no-prefix", a, b, "--", *args.scope])
    added = parse_unified_diff(diff_text)

    omits = [re.compile(p) for p in args.omit]
    exts = tuple(e.lower() for e in args.ext)
    coverage = load_coverage(cov_path, args.format)

    verbose = os.environ.get("AGENTOS_GATE_VERBOSE") == "1"
    measured = uncovered_total = 0
    missing_files: list[str] = []
    report: list[str] = []

    for path in sorted(added):
        if not path.lower().endswith(exts):
            continue
        if any(o.search(path) for o in omits):
            continue
        lines = added[path]
        if path not in coverage:
            missing_files.append(path)
            continue
        line_map = coverage[path]
        uncovered = sorted(n for n in lines if line_map.get(n) is False)
        # 不在 line_map 的行 = pragma 排除/非可执行行，不计入度量
        hit_lines = [n for n in lines if n in line_map]
        measured += len(hit_lines)
        uncovered_total += len(uncovered)
        if uncovered:
            report.append(f"  {path}: 未覆盖改动行 {len(uncovered)} 处 -> {uncovered}")
        elif verbose:
            report.append(f"  {path}: 改动 {len(hit_lines)} 行全覆盖 ✓")

    if missing_files:
        print(
            "[diff-cov] ❌ 以下在 scope 内的改动文件未出现在覆盖率文件里"
            "（度量面漂移，fail-loud；检查 coverage source/include 配置）：",
            file=sys.stderr,
        )
        for p in missing_files:
            print(f"  - {p}", file=sys.stderr)
        return 1

    if measured == 0:
        print("[diff-cov] 本次改动无可度量源码行（纯文档/配置/测试文件），放行")
        return 0

    pct = 100.0 * (measured - uncovered_total) / measured
    for line in report:
        print(line)
    print(
        f"[diff-cov] 改动行覆盖率 {pct:.2f}%（{measured - uncovered_total}/{measured} 行，阈值 {args.fail_under:g}%）"
    )
    if pct < args.fail_under:
        print(
            "[diff-cov] ❌ 改动行覆盖率低于阈值——新增代码必须带满覆盖测试"
            f"（或确属纯重构时在 commit message 声明 {SKIP_MARKER}）。",
            file=sys.stderr,
        )
        return 1
    print("[diff-cov] ✅ 改动行全覆盖")
    return 0


if __name__ == "__main__":
    sys.exit(main())
