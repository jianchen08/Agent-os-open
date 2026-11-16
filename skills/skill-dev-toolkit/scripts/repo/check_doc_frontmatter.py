#!/usr/bin/env python3
# @feature: FP-0.2.〇 文档 frontmatter 强制闸
"""新增文档 frontmatter 强制闸（提案 P-01 / E2-1 / E0-29、30）。

执法面：变更集中**新增**的 docs/**.md 必带三字段 frontmatter——doc_id/type/status，
枚举真值单源 = graph_model（DOC_ID_RE/DOC_TYPES/DOC_STATUSES），解析复用
parse_frontmatter（零 PyYAML）。存量不回填（方案已裁）：只查新增，存量由
「触碰即补」写面纪律兜底。docs/decisions/ 豁免（ADR 五段格式是独立既有约定，
frontmatter 化属另一个决策）。

变更集口径：暂存区新增（git diff --cached --diff-filter=A，pre-commit 主战场）
∪ 基线范围新增（--base 或 GITHUB_BASE_REF 的 merge-base..HEAD，PR CI 复核）。
刻意**不做 HEAD^..HEAD 兜底**：多会话并行仓里会把他人上一笔提交的无 fm 文档
false red 到当前提交者头上。退出码 0=绿 / 1=红。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_model as gm  # noqa: E402 —— 枚举真值单源

ROOT = Path(__file__).resolve().parent.parent
SCOPE_PREFIX = "docs/"
EXCLUDED_PREFIXES = ("docs/decisions/",)


def _git_new_files(args: list[str]) -> set[str]:
    try:
        out = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if out.returncode != 0:
        return set()
    return {p for p in out.stdout.decode("utf-8", "replace").split("\0") if p}


def staged_new() -> set[str]:
    return _git_new_files(["diff", "--cached", "--diff-filter=A", "--name-only", "-z"])


def range_new(base: str) -> set[str]:
    if not base:
        return set()
    tip = base
    if not base.startswith(".."):
        mb = subprocess.run(
            ["git", "merge-base", base, "HEAD"],
            cwd=str(ROOT),
            capture_output=True,
            timeout=60,
            check=False,
        )
        if mb.returncode == 0:
            tip = mb.stdout.decode().strip()
    return _git_new_files(["diff", f"{tip}..HEAD", "--diff-filter=A", "--name-only", "-z"])


def in_scope(rel: str) -> bool:
    posix = rel.replace("\\", "/")
    return (
        posix.startswith(SCOPE_PREFIX)
        and posix.endswith(".md")
        and not any(posix.startswith(p) for p in EXCLUDED_PREFIXES)
    )


def check(rel: str) -> str | None:
    """单文件三字段校验：返回错误描述（None=合法/豁免）。"""
    path = ROOT / rel
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"{rel}: 不可读: {exc}"
    fields, _ = gm.parse_frontmatter(text)
    if not fields:
        return f"{rel}: 新增 docs 文档缺 frontmatter 三字段（doc_id/type/status，规范 §一）"
    doc_id = fields.get("doc_id", "")
    problems: list[str] = []
    if not doc_id:
        problems.append("缺 doc_id")
    elif not gm.DOC_ID_RE.match(doc_id):
        problems.append(f"doc_id 形态非法: {doc_id}（应为 YYYYMMDD-小写连字符串）")
    if fields.get("type", "") not in gm.DOC_TYPES:
        problems.append(f"type 非法或缺失: {fields.get('type', '')!r}（允许 {sorted(gm.DOC_TYPES)}）")
    if fields.get("status", "") not in gm.DOC_STATUSES:
        problems.append(f"status 非法或缺失: {fields.get('status', '')!r}（允许 {sorted(gm.DOC_STATUSES)}）")
    return f"{rel}: {'；'.join(problems)}" if problems else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="新增文档 frontmatter 强制闸")
    parser.add_argument(
        "--base", default=os.environ.get("GITHUB_BASE_REF", ""), help="基线分支（PR CI 复核用；缺省只查暂存区新增）"
    )
    args = parser.parse_args(argv)

    candidates = {p for p in (staged_new() | range_new(args.base)) if in_scope(p)}
    errors = [e for rel in sorted(candidates) if (e := check(rel))]
    if not candidates:
        print("doc-frontmatter: 变更集无新增 docs 文档，绿")
        return 0
    for line in errors:
        print(line)
    print(
        f"doc-frontmatter: 新增 {len(candidates)} 件，{'全部合规' if not errors else f'{len(errors)} 件违规'}"
        "（三字段=doc_id/type/status，枚举见 docs/schemas/文档类型规范.md）"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
