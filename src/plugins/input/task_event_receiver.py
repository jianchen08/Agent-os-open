"""任务事件接收插件。

接收任务状态变更事件，当任务到达终态时注入通知到对话中，
由主 Agent（灵汐）根据通知决定后续操作（提交新任务、重试、标记容器完成等）。
"""

from __future__ import annotations

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
        self._event_bus: Any = None
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
                event_messages.append(
                    f"[系统通知] 任务 '{event['title']}' 已完成{parent_hint}"
                )
            elif event["type"] == "task_failed":
                error = event.get("error", "未知错误")
                event_messages.append(
                    f"[系统通知] 任务 '{event['title']}' 失败: {error}{parent_hint}"
                )

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
        """尝试订阅事件总线。

        通过 ctx.get_service 获取 EventBus 并订阅任务状态变更事件。

        Args:
            ctx: 插件执行上下文
        """
        event_bus = None

        # 通过服务注册表获取 EventBus
        try:
            event_bus = ctx.get_service("event_bus")
        except KeyError:
            pass

        if event_bus is not None:
            try:
                event_bus.subscribe("task_state_changed", self._on_state_changed)
                self._subscribed = True
                self._event_bus = event_bus
                logger.info("[TaskEventReceiver] Subscribed to task_state_changed events")
            except Exception as exc:
                logger.warning("[TaskEventReceiver] Failed to subscribe: %s", exc)

        # 获取 task_service
        try:
            self._task_service = ctx.get_service("task_service")
        except KeyError:
            pass

    async def _on_state_changed(self, data: dict[str, Any]) -> None:
        """处理任务状态变更事件。

        终态（completed/failed）时将事件排入待处理队列，
        在下一轮管道迭代时注入到主 Agent 对话中。

        仅接收 parent_task_id 与当前管道 task_id 匹配的事件，
        避免并行管道之间的事件交叉污染。

        Args:
            data: 事件数据，包含 task_id, old_status, new_status, task 等信息
        """
        new_status = data.get("new_status")

        if new_status not in ("completed", "failed"):
            return

        task = data.get("task")

        if isinstance(task, dict):
            parent_id = task.get("parent_task_id", "")
        elif task and hasattr(task, "parent_task_id"):
            parent_id = getattr(task, "parent_task_id", "") or ""
        else:
            parent_id = ""

        # 子任务完成通知由 TaskWorker._build_child_notifications 在 resume 时统一处理，
        # 此处跳过以避免重复通知
        if parent_id:
            logger.debug(
                "[TaskEventReceiver] Skipping child task event: parent_id=%s (handled by TaskWorker)",
                parent_id,
            )
            return

        task_id = data.get("task_id", "")

        if isinstance(task, dict):
            task_title = task.get("title", "未知任务")
            task_error = task.get("error", "")
        elif task and hasattr(task, "title"):
            task_title = task.title
            task_error = getattr(task, "error", "") or ""
        else:
            task_title = "未知任务"
            task_error = ""

        event = {
            "type": "task_completed" if new_status == "completed" else "task_failed",
            "task_id": task_id,
            "title": task_title,
            "status": new_status,
            "error": task_error,
            "parent_task_id": parent_id,
        }
        self._pending_events.append(event)
        logger.info("[TaskEventReceiver] Queued event: %s for task %s (%s)", event["type"], task_id, task_title)

    def shutdown(self) -> None:
        """关闭插件，取消订阅。"""
        if self._subscribed:
            logger.info("[TaskEventReceiver] Shutdown, events will be discarded")
            self._pending_events.clear()
