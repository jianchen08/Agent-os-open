#!/usr/bin/env python3
"""docs/working/INDEX.md 新鲜度闸（F3①，gate-change ADR
2026-11-04-gate-surface-hardening-index-freshness-and-schema-dual-source）。

缺陷（复核件 _修复复核_F2F3F7.md F3 段）：INDEX 是快照、无自动再生成触发——
活体演示 42194d443 在 INDEX 提交前 2 分钟入库，致其落地即滞后 1 件；此后每次
入库新件都持续过期。本闸把「INDEX ≡ 提交面」变成机械执法。

比对面 = **git 跟踪面**（HEAD ∪ 暂存区；CI 干净检出同语义），非裸磁盘：
- 磁盘语义在多会话工作区会把他会话在飞未跟踪件计入期望，逼出含外来件条目
  的 INDEX；这类 INDEX 落库后在 CI 干净检出下永红（外来件无磁盘对应物）。
  跟踪面两边过滤后三视角（本地/CI/他会话在飞）语义一致；
- F3 缺口本体（已提交件未入索引）恰是跟踪面语义：暂存区新件即入比对面，
  提交瞬间不再滞后。

执法面 = 跟踪面 .md 的**条目覆盖与链接正确性**（条目目标路径序列全等）：
- 缺条目（提交面新件未入索引）→ 红；
- 条目在但链接错指（覆盖假象）→ 红；
- 不执法面：条目标题文本与计数行（生成器磁盘语义与跟踪面语义的耦合漂移，
  重跑生成器自然收敛；标题级比对在多会话脏工作区产生假红，见 ADR 被否方案）；
- 未跟踪件条目不执法但**显性计数留痕**（禁静默，会话在飞件待其入库自然吻合）。

机制：import 复用 gen_working_index.collect/render 产出期望条目序列（非
subprocess）；跟踪面 = ``git ls-files -- docs/working``（暂存区即入面）；
git 不可判 fail-closed（exit 2）。修复路径恒一：跑生成器重生成 INDEX。

用法：
    python scripts/check_working_index_fresh.py            # 全仓执法（CI/fast 闸）
    python scripts/check_working_index_fresh.py --root <仓库根>
"""

from __future__ import annotations

import argparse
import difflib
import re
import subprocess
import sys
from pathlib import Path

_SCRIPTS = str(Path(__file__).resolve().parent)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import gen_working_index as gwi  # noqa: E402 —— import 复用收集+渲染，勿 subprocess

_WORKING_REL = "docs/working"

# 条目行形态：- [name](link) — title（title 可缺省）。link = 根层文件名或
# ../<相对 posix 路径>（gen_working_index.render 的两种出口形态）。
_ENTRY_RE = re.compile(r"^- \[(?P<name>[^\]]+)\]\((?P<link>[^)]+)\)")


def _tracked_files(root: Path) -> set[str]:
    """git 跟踪面（HEAD ∪ 暂存区）下 docs/working 的相对仓根 posix 路径集。

    git 不可判时抛 RuntimeError（调用方 fail-closed）——新鲜度判定失去
    比对基准时宁红勿哑。
    """
    proc = subprocess.run(
        # -z：NUL 分隔原始字节名，绕开 core.quotepath 对非 ASCII 路径的八进制
        # 转义（本仓 docs/working 大量 CJK 文件名，转义态与 UTF-8 名永不相等）。
        ["git", "-C", str(root), "ls-files", "-z", "--", _WORKING_REL],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-files 失败：{proc.stderr.decode('utf-8', 'replace').strip()}")
    return {p for p in proc.stdout.decode("utf-8").split("\0") if p}


def _entry_targets(text: str) -> list[str]:
    """解析文本中全部条目行的链接目标（相对 working 的 posix 路径，保序）。"""
    targets: list[str] = []
    for line in text.split("\n"):
        m = _ENTRY_RE.match(line)
        if not m:
            continue
        link = m.group("link")
        # 根层条目 link=文件名；子目录条目 link=../<rel>。统一成相对 working posix。
        target = link[3:] if link.startswith("../") else link
        targets.append(target)
    return targets


def _foreign_note(foreign: list[str]) -> str:
    """未跟踪件条目的显性留痕文本（点名前 8 件，禁静默）。"""
    if not foreign:
        return ""
    names = ", ".join(foreign[:8]) + ("…" if len(foreign) > 8 else "")
    return f"，另有 {len(foreign)} 条未跟踪件条目不执法（待其入库自然吻合）：{names}"


def check(root: Path, tracked: set[str] | None = None) -> int:
    """新鲜度判定主逻辑。tracked=None 时走 git ls-files（测试可注入显式集）。

    Returns:
        0 一致 / 1 过期或缺失 / 2 环境不可判（fail-closed）。
    """
    working = root / _WORKING_REL
    index_path = working / "INDEX.md"
    try:
        tracked = _tracked_files(root) if tracked is None else tracked
    except RuntimeError as exc:
        print(f"FAIL 环境不可判：{exc}", file=sys.stderr)
        return 2
    if not tracked:
        print(f"FAIL 跟踪面为空（{_WORKING_REL} 下无跟踪文件）——环境异常，宁红勿哑")
        return 1
    if not working.is_dir():
        print(f"FAIL 目录不存在：{working}", file=sys.stderr)
        return 2
    if not index_path.is_file():
        print("FAIL: docs/working/INDEX.md 不存在——运行 python scripts/gen_working_index.py 生成")
        return 1

    # 期望面 = 生成器对磁盘的产出（import 复用 collect+render），过滤到跟踪面。
    expected_text = gwi.render(gwi.collect(working), working)
    expected_targets = [t for t in _entry_targets(expected_text) if f"{_WORKING_REL}/{t}" in tracked]
    # 现库条目同样过滤到跟踪面；未跟踪件条目显性计数（禁静默）。
    all_targets = _entry_targets(index_path.read_text(encoding="utf-8"))
    actual_tracked = [t for t in all_targets if f"{_WORKING_REL}/{t}" in tracked]
    foreign = [t for t in all_targets if f"{_WORKING_REL}/{t}" not in tracked]

    foreign_note = _foreign_note(foreign)
    if expected_targets == actual_tracked:
        print(f"PASS: docs/working/INDEX.md 覆盖与跟踪面一致（{len(actual_tracked)} 条{foreign_note}）")
        return 0

    print(
        "FAIL: docs/working/INDEX.md 相对提交面过期（F3①）——修复：python scripts/gen_working_index.py 后随本件同批提交"
    )
    diff = list(
        difflib.unified_diff(
            actual_tracked,
            expected_targets,
            fromfile="INDEX.md(跟踪面条目)",
            tofile="期望(生成器×跟踪面)",
            lineterm="",
            n=0,
        )
    )
    shown = [ln for ln in diff if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))]
    for ln in shown[:10]:
        print(f"  {ln}")
    if len(shown) > 10:
        print(f"  … 共 {len(shown)} 行差异（略）")
    if foreign:
        print(f"  （{foreign_note}）")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="docs/working/INDEX.md 新鲜度闸（跟踪面语义）")
    parser.add_argument("--root", help="仓库根（缺省 = 脚本所在仓）")
    ns = parser.parse_args(argv)
    root = Path(ns.root).resolve() if ns.root else Path(__file__).resolve().parent.parent
    return check(root)


if __name__ == "__main__":
    sys.exit(main())
