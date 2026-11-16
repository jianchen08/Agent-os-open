"""onboarding_service 进度存储（用户空间文件）。

布局：``<user_config_dir>/onboarding/progress.json``，形状
``{[walkthrough_id]: {[step_id]: {done, done_at, how}}}``。done=false 的更新
摘除条目（不落 tombstone）。写入方（server.py）负责目录创建与原子落盘前的
id 合法性校验（stepId 必须存在于已装载内容，防幽灵键）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

ProgressDict = dict[str, dict[str, dict[str, Any]]]


class ProgressCorruptError(Exception):
    """进度文件存在但不是合法 JSON（显式报错，不静默重置）。"""


def load_progress(path: Path) -> ProgressDict:
    """读进度文件；不存在返回空表；损坏抛 :class:`ProgressCorruptError`。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProgressCorruptError(f"进度文件损坏: {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def save_progress(path: Path, progress: ProgressDict) -> None:
    """原子落盘（tmp + replace），父目录不存在则创建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(progress, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def apply_update(
    progress: ProgressDict,
    valid_ids: dict[str, set[str]],
    update: dict[str, Any],
) -> tuple[ProgressDict, str | None]:
    """合并单条步骤更新。

    Args:
        progress: 当前进度（就地不修改，返回新 dict）。
        valid_ids: 合法 {walkthrough_id: {step_id, ...}}（来自已装载内容）。
        update: ``{walkthrough_id, step_id, done, how}``。

    Returns:
        (new_progress, error)：error 非 None 表示 id 非法或载荷形状错误。
    """
    wid = update.get("walkthrough_id")
    sid = update.get("step_id")
    done = update.get("done")
    how = update.get("how", "manual")
    if not isinstance(wid, str) or not isinstance(sid, str):
        return progress, "walkthrough_id / step_id 必须是非空字符串"
    if wid not in valid_ids or sid not in valid_ids[wid]:
        return progress, f"未知步骤 {wid}/{sid}（必须存在于已装载内容）"
    if not isinstance(done, bool):
        return progress, "done 必须是布尔值"
    if not isinstance(how, str) or not how:
        return progress, "how 必须是非空字符串"
    new_progress: ProgressDict = {w: {s: dict(v) for s, v in steps.items()}
                                 for w, steps in progress.items()}
    if not done:
        new_progress.get(wid, {}).pop(sid, None)
        if new_progress.get(wid) == {}:
            new_progress.pop(wid, None)
        return new_progress, None
    existing = new_progress.get(wid, {}).get(sid)
    if isinstance(existing, dict) and existing.get("done") is True:
        return progress, None  # 首次完成留痕：重复完成不改写 done_at/how（可审计）
    new_progress.setdefault(wid, {})[sid] = {"done": True, "done_at": time.time(), "how": how}
    return new_progress, None
