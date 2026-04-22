"""任务事件接收插件 — 将任务完成/失败事件注入到用户输入中。

功能：
- 订阅 task_completed / task_failed 事件
- 在每轮 LLM 调用前，将待处理事件注入到 user_input
- 让主 Agent 知道子任务的完成情况
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IInputPlugin, InputResult, PluginContext

logger = logging.getLogger(__name__)


class TaskEventReceiverPlugin(IInputPlugin):
    """任务事件接收插件。

    接收任务系统的事件，将任务完成/失败信息注入到用户输入中。

    Attributes:
        _pending_events: 待处理的事件列表
        _event_bus: 事件总线实例
    """

    name = "task_event_receiver"
    priority = 5  # 在 memory_read 之后，knowledge_inject 之前

    def __init__(self) -> None:
        """初始化插件。"""
        self._pending_events: list[dict[str, Any]] = []
        self._event_bus: Any = None
        self._subscribed = False
        self._current_task_id: str = ""

    def _on_task_completed(self, event: dict[str, Any]) -> None:
        """任务完成事件处理。"""
        if not self._is_relevant_event(event):
            return
        self._pending_events.append({
            "type": "task_completed",
            "data": event,
        })
        logger.debug("[TaskEventReceiver] 收到任务完成事件: %s", event.get("task_id"))

    def _on_task_failed(self, event: dict[str, Any]) -> None:
        """任务失败事件处理。"""
        if not self._is_relevant_event(event):
            return
        self._pending_events.append({
            "type": "task_failed",
            "data": event,
        })
        logger.debug("[TaskEventReceiver] 收到任务失败事件: %s", event.get("task_id"))

    def _is_relevant_event(self, event: dict[str, Any]) -> bool:
        """判断事件是否属于当前管道的子任务。

        仅当事件的 parent_task_id 与当前管道 task_id 匹配时才接收，
        避免并行管道之间的事件交叉污染。

        Args:
            event: 事件数据

        Returns:
            事件是否与当前管道相关
        """
        if not self._current_task_id:
            return True
        parent_id = ""
        task = event.get("task")
        if isinstance(task, dict):
            parent_id = task.get("parent_task_id", "")
        elif task and hasattr(task, "parent_task_id"):
            parent_id = getattr(task, "parent_task_id", "") or ""
        if parent_id != self._current_task_id:
            logger.debug(
                "[TaskEventReceiver] Skipping event: parent_id=%s != current=%s",
                parent_id,
                self._current_task_id,
            )
            return False
        return True

    def _subscribe_events(self, ctx: PluginContext) -> None:
        """订阅事件。"""
        if self._subscribed:
            return

        try:
            self._event_bus = ctx.get_service("event_bus")
            if self._event_bus:
                self._event_bus.subscribe("task_completed", self._on_task_completed)
                self._event_bus.subscribe("task_failed", self._on_task_failed)
                self._subscribed = True
                logger.debug("[TaskEventReceiver] 已订阅任务事件")
        except Exception as exc:
            logger.warning("[TaskEventReceiver] 订阅事件失败: %s", exc)

    async def execute(self, ctx: PluginContext) -> InputResult:
        """执行插件：将待处理事件注入到用户输入中。

        Args:
            ctx: 插件上下文

        Returns:
            InputResult 包含更新后的状态
        """
        # 首次执行时订阅事件
        if not self._subscribed:
            self._current_task_id = ctx.state.get("task_id", "")
            self._subscribe_events(ctx)

        # 没有待处理事件，直接返回
        if not self._pending_events:
            return InputResult(state_updates={})

        # 构建事件消息
        event_messages = []
        for event in self._pending_events:
            if event["type"] == "task_completed":
                data = event["data"]
                event_messages.append(
                    f"[任务完成] '{data.get('title')}'\n"
                    f"结果: {data.get('result', '无')[:200]}"
                )
            elif event["type"] == "task_failed":
                data = event["data"]
                event_messages.append(
                    f"[任务失败] '{data.get('title')}'\n"
                    f"原因: {data.get('detail', '未知错误')}"
                )

        # 清空已处理事件
        self._pending_events.clear()

        # 注入到用户输入
        if event_messages:
            events_text = "\n\n".join(event_messages)
            original_input = ctx.state.get("user_input", "")
            new_input = f"{events_text}\n\n{original_input}".strip()

            logger.info("[TaskEventReceiver] 注入 %d 个任务事件", len(event_messages))
            return InputResult(state_updates={"user_input": new_input})

        return InputResult(state_updates={})
