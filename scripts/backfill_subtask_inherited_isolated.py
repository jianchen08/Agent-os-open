"""一次性数据修复：给子任务补 `ws_meta.inherited_isolated` 标记。

BUG-FIX-fix_20260629_subtask_inherited_isolated_backfill:
背景: workspace_lifecycle._start_subtask 修复后会给「父任务隔离 + 子任务 shared」
      的子任务写入 ws_meta.inherited_isolated=True，security_check._is_isolated
      据此识别子任务"继承父隔离副本"并放行。但修复前已落盘的旧子任务
      task.metadata.ws_meta 里没有这个字段，重启恢复执行时仍然按"未隔离"
      处理 → 危险工具继续弹审批。

修复规则（幂等，可重复运行）：
  目标条件全部满足才补：
    - 有 parent_task_id（是子任务）
    - metadata.ws_meta.mode == "shared"
    - metadata.ws_meta 里无 inherited_isolated 字段（或为 False）
    - 父任务的 metadata.ws_meta.mode in {worktree, project_root, branch}
  改动：data["metadata"]["ws_meta"]["inherited_isolated"] = True

用法:
  python scripts/backfill_subtask_inherited_isolated.py           # dry-run
  python scripts/backfill_subtask_inherited_isolated.py --apply   # 实际写入
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 确保能导入 src 下模块（与同目录 fix_restart_dirty_tasks.py 一致）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import yaml  # noqa: E402

# 与 src/isolation/workspace_lifecycle.py / src/plugins/input/security_check/plugin.py
# 中 _ISOLATED_WS_MODES 保持一致；这里复制定义避免脚本导入项目运行时模块。
ISOLATED_MODES = frozenset({"worktree", "project_root", "branch"})


def _safe_load(text: str, _f: Path) -> tuple[dict | None, str | None]:
    """加载 YAML，容错修复 "key:value"（冒号无空格）的格式损坏。

    历史脏数据存在此类格式，标准 YAML 解析失败会被 TaskStorage 静默丢弃。
    与 fix_restart_dirty_tasks.py:_safe_load 同源同语义。
    """
    try:
        data = yaml.safe_load(text)
        return (data if isinstance(data, dict) else None), None
    except yaml.YAMLError:
        fixed = re.sub(r"(?m)^(\w+):(\S)", r"\1: \2", text)
        try:
            data = yaml.safe_load(fixed)
            if isinstance(data, dict):
                return data, "yaml_format_repaired"
        except yaml.YAMLError:
            pass
        return None, "yaml_parse_failed"


def _build_mode_index(data_dir: Path) -> tuple[dict[str, str], int]:
    """第一遍扫描：建 task_id → ws_meta.mode 全局映射。

    父任务通常与子任务在同一 tree_* 目录下（同一根任务的整棵树落同目录），
    但父任务理论上可能跨目录（罕见），故按全局扫描保险。

    Returns:
        (id_to_mode, parse_failed_count)
    """
    id_to_mode: dict[str, str] = {}
    parse_failed = 0
    for tree in sorted(data_dir.glob("tree_*")):
        if not tree.is_dir():
            continue
        for f in sorted(tree.glob("*.yaml")):
            data, note = _safe_load(f.read_text(encoding="utf-8"), f)
            if data is None:
                parse_failed += 1
                continue
            tid = data.get("id")
            if not tid:
                continue
            md = data.get("metadata") or {}
            wsm = md.get("ws_meta") if isinstance(md, dict) else None
            mode = wsm.get("mode") if isinstance(wsm, dict) else None
            if isinstance(mode, str):
                id_to_mode[tid] = mode
    return id_to_mode, parse_failed


def _should_fix(data: dict, id_to_mode: dict[str, str]) -> tuple[bool, str]:
    """判定一条任务是否需要补标记，并返回原因（便于 dry-run 输出）。"""
    parent_id = data.get("parent_task_id")
    if not parent_id:
        return False, "not_subtask"

    md = data.get("metadata") or {}
    wsm = md.get("ws_meta") if isinstance(md, dict) else None
    if not isinstance(wsm, dict):
        return False, "no_ws_meta"

    if wsm.get("mode") != "shared":
        return False, f"mode={wsm.get('mode')!r}_not_shared"
    if wsm.get("inherited_isolated"):
        return False, "already_marked"

    parent_mode = id_to_mode.get(parent_id)
    if parent_mode not in ISOLATED_MODES:
        return False, f"parent_mode={parent_mode!r}_not_isolated"

    return True, f"parent_mode={parent_mode}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="给子任务补 ws_meta.inherited_isolated 标记",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="实际写入（默认仅 dry-run 打印）",
    )
    args = parser.parse_args()

    data_dir = Path(__file__).resolve().parent.parent / "data" / "tasks"
    if not data_dir.exists():
        print(f"[ERROR] 数据目录不存在: {data_dir}")
        return 1

    # 第一遍：建 id → mode 索引
    id_to_mode, parse_failed_first_pass = _build_mode_index(data_dir)
    print(f"[INDEX] 共扫描 {len(id_to_mode)} 条任务建索引")
    if parse_failed_first_pass:
        print(f"[INDEX] 第一遍 YAML 解析失败 {parse_failed_first_pass} 条（已跳过）")

    # 第二遍：筛选并修复
    fixed = 0
    parse_failed = 0
    for tree in sorted(data_dir.glob("tree_*")):
        if not tree.is_dir():
            continue
        for f in sorted(tree.glob("*.yaml")):
            text = f.read_text(encoding="utf-8")
            data, note = _safe_load(text, f)
            if data is None:
                print(f"[SKIP] {f.relative_to(data_dir.parent)} ({note})")
                parse_failed += 1
                continue

            hit, reason = _should_fix(data, id_to_mode)
            if not hit:
                continue

            # 命中：在内存中打标记
            data["metadata"]["ws_meta"]["inherited_isolated"] = True
            rel = f.relative_to(data_dir.parent)
            print(f"[{'FIX' if args.apply else 'DRY'}] {rel}")
            print(f"      id={data.get('id')} parent={data.get('parent_task_id')}")
            print(f"      reason: {reason}")

            if args.apply:
                out = yaml.safe_dump(
                    data, default_flow_style=False, allow_unicode=True,
                    sort_keys=False, indent=2,
                )
                f.write_text(out, encoding="utf-8")
            fixed += 1

    action = "已修复" if args.apply else "将修复（dry-run）"
    print(f"\n=== {action} {fixed} 条子任务，跳过 {parse_failed} 条无法解析 ===")
    if not args.apply and fixed:
        print("\n确认无误后运行: python scripts/backfill_subtask_inherited_isolated.py --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
