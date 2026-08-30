# -*- coding: utf-8 -*-
"""P2 插件侧：task_service 事件派生（执行后删除）。"""
import json

BASE = r"D:\myproject\container_e17cc5927dfd\plugins\shared"

# ===== 1. plugin.json：domain_event 钩子 + event-bus 信封 =====
p = BASE + r"\system\tasks\plugin.json"
d = json.load(open(p, encoding="utf-8"))
hooks = d["capabilities"].setdefault("lifecycle_hooks", [])
if "domain_event" not in hooks:
    hooks.append("domain_event")
grants = d.setdefault("granted_capabilities", [])
if "event-bus" not in grants:
    grants.append("event-bus")
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
open(p, "a", encoding="utf-8", newline="\n").write("\n")
print("plugin.json ok")

# ===== 2. events.py：任务域事件派生 =====
events_py = '''"""任务域事件派生（ADR 2026-08-28 事件下沉）。

内核 run 终态只广播运行域 run.* 事件；本模块订阅 run.completed / run.failed，
经 pipeline-state.list 读该管道最终 state，按任务域语义派生
task_completed / task_failed 并经 event-bus.emit_domain 发回域事件总线——
裁决词汇（task.status 值、task.* 键面）归任务域所有者（本插件），内核零知识。

判定语义（与内核原 derive_run_terminal_events 行为逐条对齐）：
- 任务管道判据 = state 含 ``task.`` 前缀键且不含 ``task.owned.``（后者是
  父管道登记子任务的键，不是任务自身声明）；
- run.failed + 任务管道 → task_failed；
- run.completed：suspended / router.stop_reason=user_requested → 不派生
  （挂起与用户停止都不是任务终态）；task.status=completed → task_completed；
  task.status=failed → task_failed；其余（pending/pending_evaluation 等）→
  不派生（杜绝"跑完就假完成通知上级"）。

事件标签：pipeline_id / thread_id / task_id / parent_pipeline_id / user_id——
triggers_ext 的父任务通知注入器（_auto_notify_parent）依赖这组标签。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_TASK_PREFIX = "task."
_OWNED_PREFIX = "task.owned."


def _is_task_state(row: dict[str, Any]) -> bool:
    """任务管道判据：含 task.* 自身键且不含 task.owned.* 登记键。"""
    has_task = False
    for key in row:
        k = str(key)
        if k.startswith(_OWNED_PREFIX):
            continue
        if k.startswith(_TASK_PREFIX):
            has_task = True
            break
    return has_task


def _tag(row: dict[str, Any], key: str) -> Any:
    val = row.get(key)
    return val if val is not None else ""


def derive_task_terminal_events(
    event_name: str, row: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    """由 run 终态事件 + state 摘要行派生任务域事件（纯函数，可单测）。

    Returns:
        [(event_name, tags)]——空列表 = 该 run 不派生任务域事件。
    """
    if not isinstance(row, dict) or not _is_task_state(row):
        return []
    tags = {
        "pipeline_id": _tag(row, "pipeline_id"),
        "thread_id": _tag(row, "thread_id"),
        "task_id": _tag(row, "task.id"),
        "parent_pipeline_id": _tag(row, "lineage.parent_pipeline_id"),
        "user_id": _tag(row, "task.submitted_by"),
    }
    if event_name == "run.failed":
        return [("task_failed", tags)]
    if event_name != "run.completed":
        return []
    status = str(row.get("task.status") or "")
    if status == "completed":
        return [("task_completed", tags)]
    if status == "failed":
        return [("task_failed", tags)]
    # 评估未通过（pending/pending_evaluation/running）不派生——完成唯一判据 =
    # task_evaluate 评估通过落 task.status=completed
    return []


async def handle_run_terminal_event(
    event_name: str, params: dict[str, Any], state_capability: Any, bus_capability: Any
) -> int:
    """入口：查 state 摘要 → 派生 → 经 event-bus.emit_domain 发回域总线。

    Args:
        event_name: run.completed / run.failed。
        params: 域事件标签（取 pipeline_id 定位管道）。
        state_capability: pipeline-state 能力句柄（list 取摘要行）。
        bus_capability: event-bus 能力句柄（call emit_domain）。

    Returns:
        派生发出的事件数（0 = 未派生）。
    """
    pipeline_id = str(params.get("pipeline_id") or "")
    if not pipeline_id:
        return 0
    rows = await state_capability.call("list", {})
    if not isinstance(rows, list):
        return 0
    row = next(
        (r for r in rows if isinstance(r, dict) and str(r.get("pipeline_id") or "") == pipeline_id),
        None,
    )
    if row is None:
        return 0
    emitted = 0
    for name, tags in derive_task_terminal_events(event_name, row):
        try:
            await bus_capability.call("emit_domain", {"event": name, "tags": tags})
            emitted += 1
            logger.info(
                "[task_service] 任务域事件派生 | event=%s | pipeline_id=%s | task_id=%s",
                name, tags.get("pipeline_id"), tags.get("task_id"),
            )
        except Exception:  # noqa: BLE001
            logger.exception("[task_service] emit_domain 失败 | event=%s", name)
    return emitted
'''

p = BASE + r"\system\tasks\events.py"
open(p, "w", encoding="utf-8", newline="\n").write(events_py)
print("events.py ok")

# ===== 3. server.py：注册域事件入口 =====
p = BASE + r"\system\tasks\server.py"
s = open(p, encoding="utf-8").read()
old = '''from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402,PLC0415

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("task_service")'''
new = '''from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402,PLC0415

import events as task_events  # noqa: E402,PLC0415

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("task_service")'''
assert old in s
s = s.replace(old, new, 1)

# 在 on_domain_event 注册：找 plugin.on_load 附近或 _get_service 之后插入
anchor = '''def _get_service() -> TaskService:
    """获取全局 TaskService 实例，未初始化时抛出 RuntimeError。"""
    if _service is None:
        raise RuntimeError("TaskService not initialized. Was on_load called?")
    return _service'''
assert anchor in s
s = s.replace(anchor, anchor + '''


@plugin.on_domain_event
async def _on_domain_event(params: dict) -> None:  # type: ignore[name-defined]
    """任务域事件派生入口：订阅 run 终态，派生 task_completed/task_failed。

    判定与发射语义见 events.py（ADR 2026-08-28 事件下沉——任务域裁决词汇
    归本插件，内核只发 run.*）。
    """
    event_name = str(params.get("event") or "")
    if event_name not in ("run.completed", "run.failed"):
        return
    try:
        state_cap = plugin.get_capability("pipeline-state")
        bus_cap = plugin.get_capability("event-bus")
    except (KeyError, AttributeError):
        logger.warning("[task_service] 能力句柄缺席，跳过任务域事件派生")
        return
    await task_events.handle_run_terminal_event(event_name, params, state_cap, bus_cap)''', 1)
open(p, "w", encoding="utf-8", newline="\n").write(s)
print("server.py ok")
