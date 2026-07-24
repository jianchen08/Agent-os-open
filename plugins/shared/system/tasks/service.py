"""任务服务模块 — 门面模式组合类。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from _task_cleanup import _TaskCleanupMixin
from _task_crud import _TaskCrudMixin
from _task_state import _TaskStateMixin

logger = logging.getLogger(__name__)

StateChangeCallback = Callable[[str, str, str], Awaitable[None]]


def _default_data_dir() -> str:
    """推断任务 YAML 数据目录。"""
    # src/tasks/service.py → src/tasks/ → src/ → project_root/
    return str(Path(__file__).resolve().parent.parent.parent / "data" / "tasks")


class TaskService(_TaskCrudMixin, _TaskStateMixin, _TaskCleanupMixin):
    """任务服务类。"""

    def __init__(
        self,
        task_id: str | None = None,
        initial_state: str = "pending",
        event_bus: Any | None = None,
        data_dir: str | None = None,
    ) -> None:
        self.task_id = task_id
        self._event_bus = event_bus
        self._state_callbacks: list[StateChangeCallback] = []

        # 门面模式的存储层（仅 task_id=None 时初始化）
        self._storage: Any = None
        if task_id is None:
            from storage import TaskStorage  # noqa: PLC0415

            _dir = data_dir or _default_data_dir()
            self._storage = TaskStorage(data_dir=_dir)

    def register_state_callback(self, callback: StateChangeCallback) -> None:
        """注册任务状态变更回调函数。"""
        self._state_callbacks.append(callback)

    def unregister_state_callback(self, callback: StateChangeCallback) -> None:
        """注销任务状态变更回调函数。"""
        if callback in self._state_callbacks:
            self._state_callbacks.remove(callback)

    async def _emit_state_change(
        self,
        task_id: str,
        old_status: str,
        new_status: str,
    ) -> None:
        """通知所有注册的回调函数任务状态已变更，并通过 WebSocket 推送事件。"""
        # DEBT: 原 src.core.logging.LogContext.bind 不可用（插件环境无 src/）。ceiling: 日志上下文不携带 task_id。upgrade: 当日志系统迁移完成后恢复绑定。
        logger.debug("state change: %s -> %s | task=%s", old_status, new_status, task_id[:12] if task_id else "")

        for cb in self._state_callbacks:
            try:
                await cb(task_id, old_status, new_status)
            except Exception as exc:
                logger.debug("state callback 执行失败: %s", exc)

        # 非阻塞推送 task_status_changed WebSocket 事件
        self._push_status_change_ws(task_id, old_status, new_status)

    def _push_status_change_ws(
        self,
        task_id: str,
        old_status: str,
        new_status: str,
    ) -> None:
        """通过 MessageBus 推送任务状态变更 WebSocket 事件（fire-and-forget）。"""
        with contextlib.suppress(RuntimeError):
            asyncio.create_task(
                self._do_push_status_change_ws(task_id, old_status, new_status),
            )

    async def _do_push_status_change_ws(
        self,
        task_id: str,
        old_status: str,
        new_status: str,
    ) -> None:
        """实际执行任务状态变更推送。

        0.2 推送改走 frontend.emit capability（ADR §3.5），SDK 暂未实现该 capability；
        当前推送静默跳过，0.2 栈不再依赖 0.1 的 src/channels/websocket/
        ws_interaction_notifier（task_11 P2-7）。待 SDK 实现后改用
        ctx.frontend.emit(event="task_status_changed", scope=...) 恢复。
        """
        return
