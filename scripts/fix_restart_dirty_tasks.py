"""一次性数据修复：清理"服务重启后引擎状态丢失"产生的脏数据。

BUG-FIX-fix_20260625_restart_task_becomes_failed:
背景: 修复前的旧代码 cleanup_ghost_tasks 在服务重启时把 running 任务
      强写成 failed（error=fail_reason="服务重启后引擎状态丢失"，
      子任务带 cancel_reason="父任务因服务重启失败，级联停止"）。
      上一轮已删掉旧路径 A 防止新数据产生，但已落盘的脏数据需一次性修复。

修复规则（幂等，可重复运行）：
  - status=failed + 引擎状态丢失  → status=stopped，清 error/fail_reason/cancel_reason/retry_message
  - status=running + 带级联标记    → status=stopped，清 error/fail_reason/cancel_reason/retry_message
  - status=completed（已正常完成） → status 不变，仅清 metadata 残留的 fail_reason/cancel_reason
所有修复统一写 metadata.paused_by="system" + stop_reason，与 pause_task 语义一致。

用法:
  python scripts/fix_restart_dirty_tasks.py            # dry-run，仅打印
  python scripts/fix_restart_dirty_tasks.py --apply     # 实际写入
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 确保能导入 src 下模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import yaml  # noqa: E402

# 触发修复的脏标记文案
DIRTY_ENGINE_MARK = "引擎状态丢失"
DIRTY_CASCADE_MARK = "父任务因服务重启失败"
# 修复后写入的标记
STOP_REASON = "服务重启后引擎状态丢失，已停止等待恢复"


def _safe_load(text: str, f: Path) -> tuple[dict | None, str | None]:
    """加载 YAML，容错修复 "key:value"（冒号无空格）的格式损坏。

    历史脏数据 9ebc8c3fe8ce.yaml 存在 `error:null`（无空格），
    导致标准 YAML 解析失败、TaskStorage 加载时整个任务被静默丢弃。
    此处用正则把行首 `key:value` 规整为 `key: value` 后重试。
    """
    try:
        data = yaml.safe_load(text)
        return (data if isinstance(data, dict) else None), None
    except yaml.YAMLError:
        # 修复 "key:value" → "key: value"（仅顶层 key，保留 "key: 'x:y'" 这类值）
        fixed = re.sub(r"(?m)^(\w+):(\S)", r"\1: \2", text)
        try:
            data = yaml.safe_load(fixed)
            if isinstance(data, dict):
                return data, "yaml_format_repaired"
        except yaml.YAMLError:
            pass
        return None, "yaml_parse_failed"


def _is_dirty(data: dict) -> bool:
    """判断任务是否含旧 cleanup 残留的脏标记。"""
    md = data.get("metadata") or {}
    err = data.get("error") or ""
    return (
        DIRTY_ENGINE_MARK in err
        or DIRTY_ENGINE_MARK in (md.get("fail_reason") or "")
        or DIRTY_CASCADE_MARK in (md.get("cancel_reason") or "")
    )


def _plan_fix(data: dict) -> tuple[str, dict]:
    """计算单条任务的修复计划，返回 (new_status, field_changes)。"""
    md = data.get("metadata")
    if md is None:
        md = {}
        data["metadata"] = md

    changes: dict = {}
    status = data.get("status")

    # completed 已正常完成：status 保持，仅清 metadata 残留
    if status == "completed":
        new_status = "completed"
        if "fail_reason" in md:
            md.pop("fail_reason", None)
            changes["metadata.fail_reason(removed)"] = True
        if "cancel_reason" in md:
            md.pop("cancel_reason", None)
            changes["metadata.cancel_reason(removed)"] = True
        # error 理论上为空，防御性清理脏文案
        if DIRTY_ENGINE_MARK in (data.get("error") or ""):
            data["error"] = ""
            changes["error(removed)"] = True
        return new_status, changes

    # failed / running → stopped
    new_status = "stopped"
    if status != "stopped":
        data["status"] = "stopped"
        changes["status"] = f"{status} → stopped"

    # 清理 error（脏文案）
    if data.get("error"):
        data["error"] = None
        changes["error(removed)"] = True

    # 清理 metadata 残留标记
    for key in ("fail_reason", "cancel_reason", "retry_message"):
        if md.get(key):
            md.pop(key, None)
            changes[f"metadata.{key}(removed)"] = True

    # 写入与 pause_task 一致的语义标记
    md["paused_by"] = "system"
    md["stop_reason"] = STOP_REASON
    changes["metadata.paused_by"] = "system"
    changes["metadata.stop_reason"] = "written"

    return new_status, changes


def main() -> int:
    parser = argparse.ArgumentParser(description="修复服务重启产生的脏任务数据")
    parser.add_argument(
        "--apply", action="store_true",
        help="实际写入（默认仅 dry-run 打印）",
    )
    args = parser.parse_args()

    data_dir = Path(__file__).resolve().parent.parent / "data" / "tasks"
    if not data_dir.exists():
        print(f"[ERROR] 数据目录不存在: {data_dir}")
        return 1

    fixed = 0
    skipped = 0
    failed = 0
    for tree in sorted(data_dir.glob("tree_*")):
        if not tree.is_dir():
            continue
        for f in sorted(tree.glob("*.yaml")):
            text = f.read_text(encoding="utf-8")
            data, note = _safe_load(text, f)
            if data is None:
                # 解析彻底失败：记录但继续，不阻塞其他任务修复
                print(f"[SKIP] {f.relative_to(data_dir.parent)} ({note})")
                failed += 1
                continue
            if not _is_dirty(data):
                continue

            new_status, changes = _plan_fix(data)
            if note:
                changes["[format]"] = note
            rel = f.relative_to(data_dir.parent)
            print(f"[{'FIX' if args.apply else 'DRY'}] {rel}")
            print(f"      id={data.get('id')} title={data.get('title')!r}")
            for k, v in changes.items():
                print(f"      - {k}: {v}")
            print()

            if args.apply:
                out = yaml.safe_dump(
                    data, default_flow_style=False, allow_unicode=True,
                    sort_keys=False, indent=2,
                )
                f.write_text(out, encoding="utf-8")
            fixed += 1

    action = "已修复" if args.apply else "将修复（dry-run）"
    print(f"=== {action} {fixed} 条脏数据，跳过 {failed} 条无法解析，{skipped} 条无需处理 ===")
    if not args.apply and fixed:
        print("\n确认无误后运行: python scripts/fix_restart_dirty_tasks.py --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
