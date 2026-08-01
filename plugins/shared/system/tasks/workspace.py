"""任务工作空间解析器。

统一的入口：从任务数据（task.metadata.ws_meta）获取实际工作空间路径。
不做 resolve_workspace 链计算，ws_meta 是唯一可信来源。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def resolve_task_workspace(task: Any) -> str | None:
    """从任务数据解析工作空间绝对路径。

    数据来源：task.metadata["ws_meta"]，由 on_task_start → _persist_ws_meta
    同步写入内存 task 对象，包含 git worktree 的实际绝对路径。
    如果 metadata 中缺失，先从 lifecycle 恢复到内存。

    ws_meta.path 已由 WorkspaceLifecycleManager 正确设置：
    - shared 模式：子任务共享父工作空间（path 即为父空间路径）
    - worktree 模式：子任务有独立的 worktree 路径
    - plain 模式：子任务有独立的目录路径
    因此直接使用 ws_meta.path，不再额外拼接 task.id。

    Args:
        task: TaskModel 实例，需有 id, metadata, parent_task_id 属性

    Returns:
        绝对工作空间路径字符串；不可解析时返回 None
    """
    metadata = task.metadata if task.metadata else {}

    ws_meta = metadata.get("ws_meta")
    if not ws_meta or not isinstance(ws_meta, dict):
        _restore_from_lifecycle(task, metadata)

    ws_meta = metadata.get("ws_meta")
    if not ws_meta or not isinstance(ws_meta, dict):
        return None

    ws_path = ws_meta.get("path")
    if not ws_path:
        return None

    p = Path(ws_path)
    if not p.is_absolute():
        p = Path.cwd() / p

    return str(p)


def _restore_from_lifecycle(task: Any, metadata: dict) -> None:
    """从 workspace_lifecycle_manager 加载 ws_meta 到 task.metadata（装饰性恢复）。

    M3（2026-08-01）：插件自包含化——workspace_lifecycle_manager 是 channel_api 进程
    注册的服务，插件进程不可达。ws_meta 已持久化到 task.metadata（本函数原是优化：
    首次加载时从 lifecycle 缓存补一次），缺失不影响功能（后续读取走 task.metadata）。
    故插件路径降级为 no-op。:8988 主进程路径仍走原逻辑（src/tasks/workspace.py）。
    """
    # 插件路径：lifecycle 不可达，ws_meta 已在 task.metadata，直接返回。
    return
