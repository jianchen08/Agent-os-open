"""任务服务模块 — 门面模式组合类。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from src.tasks._task_cleanup import _TaskCleanupMixin
from src.tasks._task_crud import _TaskCrudMixin
from src.tasks._task_state import _TaskStateMixin

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
            from tasks.storage import TaskStorage  # noqa: PLC0415

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
        # 绑定日志上下文，使后续日志自动携带 task_id
        from src.core.logging import LogContext  # noqa: PLC0415

        LogContext.bind(task_id=task_id)

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
        """实际执行 WebSocket 推送。"""
        try:
            if self._storage is None:
                return

            task = self._storage.get(task_id)
            if task is None:
                return

            thread_id = task.metadata.get("session_id") if task.metadata else None
            if not thread_id:
                logger.error(
                    "[TaskService] task metadata 缺 session_id，task_status_changed 未推送 | task=%s",
                    task_id[:12] if task_id else "",
                )
                return

            from channels.websocket.ws_handler import ws_interaction_notifier  # noqa: PLC0415

            _user_id = (task.metadata.get("user_id") if task.metadata else "") or ""
            if not _user_id:
                logger.debug(
                    "[TaskService] task metadata 缺 user_id，task_status_changed 未推送 | task=%s",
                    task_id[:12] if task_id else "",
                )
                return

            await ws_interaction_notifier.send_to_user(
                _user_id,
                {
                    "type": "task_status_changed",
                    "data": {
                        "task_id": task_id,
                        "status": new_status,
                        "previous_status": old_status,
                        "title": task.title or "",
                        "updated_at": task.updated_at or "",
                        "thread_id": thread_id,
                    },
                },
            )
        except Exception as exc:
            logger.debug(
                "[TaskService] task_status_changed 推送失败（非致命）task_id=%s: %s",
                task_id[:12] if task_id else "",
                exc,
            )
