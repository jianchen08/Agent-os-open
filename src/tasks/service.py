"""任务服务模块 — 门面模式组合类。

将 TaskService 的职责按以下 Mixin 拆分：
- _TaskCrudMixin: 创建、查询、字段更新与基础删除
- _TaskStateMixin: 状态转换、幽灵清理与评估完成
- _TaskCleanupMixin: 工作空间清理、级联删除与容器管理

本文件保留 TaskService 门面类（__init__ + 事件回调），
通过多重继承组合三个 Mixin，所有公共接口不变。
"""

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
    """推断任务 YAML 数据目录。

    service.py 位于 src/tasks/，data 目录位于项目根目录下 data/tasks/。
    """
    # src/tasks/service.py → src/tasks/ → src/ → project_root/
    return str(Path(__file__).resolve().parent.parent.parent / "data" / "tasks")


class TaskService(_TaskCrudMixin, _TaskStateMixin, _TaskCleanupMixin):
    """任务服务类。

    门面模式服务，通过多重继承组合 CRUD / 状态转换 / 资源清理三个 Mixin，
    提供全部任务的 CRUD 和状态操作。

    Args:
        task_id: 任务 ID（保留参数，兼容外部调用签名）
        initial_state: 初始状态（已弃用，保留参数签名兼容）
        event_bus: 可选的事件总线实例，用于发布任务状态变更事件
        data_dir: YAML 存储目录（门面模式使用，None 时自动推断）
    """

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
            from tasks.storage import TaskStorage

            _dir = data_dir or _default_data_dir()
            self._storage = TaskStorage(data_dir=_dir)

    def register_state_callback(self, callback: StateChangeCallback) -> None:
        """注册任务状态变更回调函数。

        Args:
            callback: 异步回调函数，签名为 async (task_id, old_status, new_status) -> None
        """
        self._state_callbacks.append(callback)

    def unregister_state_callback(self, callback: StateChangeCallback) -> None:
        """注销任务状态变更回调函数。

        Args:
            callback: 之前注册的回调函数
        """
        if callback in self._state_callbacks:
            self._state_callbacks.remove(callback)

    async def _emit_state_change(
        self,
        task_id: str,
        old_status: str,
        new_status: str,
    ) -> None:
        """通知所有注册的回调函数任务状态已变更，并通过 WebSocket 推送事件。

        Args:
            task_id: 任务 ID
            old_status: 原状态
            new_status: 新状态
        """
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
                return

            from api.websocket.message_bus import SourceType, get_message_bus
            from api.websocket.message_types import create_task_status_changed_message

            message = create_task_status_changed_message(
                task_id=task_id,
                status=new_status,
                previous_status=old_status,
                title=task.title or "",
                updated_at=task.updated_at or "",
            )

            bus = get_message_bus()
            await bus.emit(
                thread_id,
                message,
                source_type=SourceType.SYSTEM,
                source_id=f"task:{task_id}",
            )
        except Exception as exc:
            logger.debug(
                "[TaskService] task_status_changed 推送失败（非致命）task_id=%s: %s",
                task_id[:12] if task_id else "",
                exc,
            )
