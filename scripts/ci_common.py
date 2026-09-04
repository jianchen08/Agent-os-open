"""CI 变更面解析公共库（车道分档 / 关联测试选择共用）。

变更范围解析契约与 scripts/check_diff_coverage.py 对齐，优先级从高到低：
  --base REF（显式）> GITHUB_BASE_REF（PR 事件，merge-base）>
  GITHUB_EVENT_BEFORE（push 事件，两点 diff）> HEAD^..HEAD（本地默认）。
无可解析范围（空仓）返回 None，调用方按全量处理（fail-open：拿不准就全跑）。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 插件平铺模块 + SDK 源码注入（与 scripts/run_gates.py 的 _PLUGINS_ENV 同契约）：
# 插件就地测试与 tests/ 集成测试 import 邻插件模块 / agentos_plugin_sdk 依赖此路径。
PLUGINS_ENV = {"PYTHONPATH": os.pathsep.join([str(ROOT / "plugins"), str(ROOT / "plugins" / "sdk" / "src")])}


def _git(*args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git {' '.join(args)} 失败")
    return proc.stdout.strip()


def _rev_exists(ref: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def diff_names(a: str, b: str) -> list[str]:
    out = _git("diff", "--name-only", a, b)
    return [line for line in out.splitlines() if line]


def resolve_changed_files(base: str = "") -> tuple[list[str], str] | None:
    """返回 (变更文件相对路径列表, 范围说明)；无可解析范围返回 None。"""
    base = base or os.environ.get("GITHUB_BASE_REF", "")
    if base:
        for cand in (f"origin/{base}", base):
            if _rev_exists(cand):
                mb = _git("merge-base", cand, "HEAD")
                return diff_names(mb, "HEAD"), f"merge-base({cand}, HEAD)"
        raise SystemExit(f"[ci] 基线分支不可解析: {base}")

    before = os.environ.get("GITHUB_EVENT_BEFORE", "").strip()
    if before and set(before) != {"0"} and _rev_exists(before):
        return diff_names(before, "HEAD"), f"{before[:12]}..HEAD（push 口径）"

    try:
        parent = _git("rev-parse", "HEAD^")
    except RuntimeError:
        return None
    return diff_names(parent, "HEAD"), "HEAD^..HEAD（push/本地口径：最后一个提交）"
