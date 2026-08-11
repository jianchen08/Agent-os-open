"""任务事件接收插件。

接收任务状态变更事件，当任务到达终态时注入通知到对话中，
由主 Agent（灵汐）根据通知决定后续操作（提交新任务、重试、标记容器完成等）。
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)


class TaskEventReceiverPlugin(IInputPlugin):
    """接收任务事件并注入到对话中。

    订阅任务状态变更事件，当任务到达终态（completed/failed）时，
    将通知注入到 user_input 中，由主 Agent 根据通知决定后续操作。
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化插件。

        Args:
            config: 插件配置字典
        """
        self._config = config or {}
        self._pending_events: list[dict[str, Any]] = []
        self._subscribed = False
        self._task_service: Any = None
        self._current_task_id: str = ""

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "task_event_receiver"

    @property
    def priority(self) -> int:
        """插件执行优先级（在 memory_read 之后）。"""
        return 40

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """处理输入状态，注入待处理的事件。

        每轮管道执行前，检查是否有待处理的任务终态事件，
        如果有则注入到 user_input 中。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含状态更新的插件执行结果
        """
        # 首次执行时订阅事件总线
        if not self._subscribed:
            self._current_task_id = ctx.state.get("task_id", "")
            self._try_subscribe(ctx)

        if not self._pending_events:
            return PluginResult(state_updates={})

        # 构建事件通知文本
        event_messages = []
        for event in self._pending_events:
            parent_hint = ""
            pid = event.get("parent_task_id", "")
            if pid:
                parent_hint = f" [容器 {pid}]"
            if event["type"] == "task_completed":
                event_messages.append(f"[系统通知] 任务 '{event['title']}' 已完成{parent_hint}")
            elif event["type"] == "task_failed":
                error = event.get("error", "未知错误")
                event_messages.append(f"[系统通知] 任务 '{event['title']}' 失败: {error}{parent_hint}")

        # 注入到 user_input
        state_updates: dict[str, Any] = {}
        if event_messages:
            events_text = "\n".join(event_messages)
            original_input = ctx.state.get("user_input", "")
            state_updates["user_input"] = f"{events_text}\n\n{original_input}".strip()
            logger.info("[TaskEventReceiver] Injected %d events into user_input", len(self._pending_events))

        # 清空已处理的事件
        self._pending_events.clear()
        return PluginResult(state_updates=state_updates)

    def _try_subscribe(self, ctx: PluginContext) -> None:
        """通过 TaskService 注册状态变更回调。

        Args:
            ctx: 插件执行上下文
        """
        with contextlib.suppress(KeyError):
            self._task_service = ctx.get_service("task_service")

        if self._task_service is not None:
            try:
                self._task_service.register_state_callback(self._on_state_changed)
                self._subscribed = True
                logger.info("[TaskEventReceiver] Registered state callback via TaskService")
            except Exception as exc:
                logger.warning("[TaskEventReceiver] Failed to register callback: %s", exc)

    async def _on_state_changed(self, task_id: str, old_status: str, new_status: str) -> None:
        """处理任务状态变更回调。

        终态（completed/failed）时将事件排入待处理队列，
        在下一轮管道迭代时注入到主 Agent 对话中。

        仅接收根任务（无 parent_task_id 且无 parent_pipeline_id）的终态事件，
        子任务通知由 TaskWorker._notify_suspended_pipelines 统一处理。

        Args:
            task_id: 任务 ID
            old_status: 变更前状态
            new_status: 变更后状态
        """
        if new_status not in ("completed", "failed"):
            return

        task = None
        if self._task_service and task_id:
            with contextlib.suppress(Exception):
                task = self._task_service.get_task(task_id)

        if isinstance(task, dict):
            parent_id = task.get("parent_task_id", "")
            ppl_id = task.get("parent_pipeline_id", "")
            task_title = task.get("title", "未知任务")
            task_error = task.get("error", "")
        elif task and hasattr(task, "parent_task_id"):
            parent_id = getattr(task, "parent_task_id", "") or ""
            ppl_id = getattr(task, "parent_pipeline_id", "") or ""
            task_title = getattr(task, "title", "未知任务")
            task_error = getattr(task, "error", "") or ""
        else:
            parent_id = ""
            ppl_id = ""
            task_title = "未知任务"
            task_error = ""

        if parent_id:
            logger.debug(
                "[TaskEventReceiver] Skipping child task event: parent_id=%s (handled by TaskWorker)",
                parent_id,
            )
            return

        if ppl_id:
            logger.debug(
                "[TaskEventReceiver] Skipping child task event: parent_pipeline_id=%s (handled by _notify_suspended_pipelines)",
                ppl_id,
            )
            return

        evt = {
            "type": "task_completed" if new_status == "completed" else "task_failed",
            "task_id": task_id,
            "title": task_title,
            "status": new_status,
            "error": task_error,
            "parent_task_id": parent_id,
        }
        self._pending_events.append(evt)
        logger.info("[TaskEventReceiver] Queued event: %s for task %s (%s)", evt["type"], task_id, task_title)

    def shutdown(self) -> None:
        """关闭插件，注销回调。"""
        if self._subscribed and self._task_service:
            with contextlib.suppress(Exception):
                self._task_service.unregister_state_callback(self._on_state_changed)
            logger.info("[TaskEventReceiver] Shutdown, callback unregistered")
            self._pending_events.clear()
