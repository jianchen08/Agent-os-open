"""
触发器设置工具

暴露接口：
- get_tool_definition() -> Tool：get_tool_definition功能
- TriggerSetupTool：TriggerSetupTool类
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from core.results import ToolExecutionResult
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)
from triggers.message_queue import (
    TriggerMessage,
    get_trigger_message_queue,
)

logger = logging.getLogger(__name__)


class TriggerSetupTool:
    """
    触发器设置工具

    允许 Agent 设置触发器，在指定条件满足时向当前会话注入消息。

    触发器只能触发自己所在的会话，通过注入参数自动获取：
    - session_id: 系统注入
    - execution_id: 系统注入

    使用示例:
        # 延迟触发
        trigger_setup(
            trigger_type="delay",
            delay_seconds=300,
            message="请检查任务状态"
        )

        # 定时触发
        trigger_setup(
            trigger_type="schedule",
            schedule_time="2026-03-15T18:00:00",
            message="下班前检查任务进度"
        )
    """

    MAX_DELAY_SECONDS = 86400  # 24小时
    MAX_SCHEDULE_HOURS = 168  # 7天
    MAX_TRIGGERS_PER_SESSION = 10

    def __init__(self):
        """初始化触发器设置工具"""
        self._queue = get_trigger_message_queue()

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="trigger_setup",
            description="设置触发器，在指定条件满足时向当前会话注入消息并触发执行。触发器只能触发自己所在的会话。支持延迟触发、定时触发、事件触发和条件触发四种类型。",
            input_schema={
                "type": "object",
                "properties": {
                    "trigger_type": {
                        "type": "string",
                        "enum": ["delay", "schedule", "event", "condition"],
                        "description": "触发类型: delay=延迟触发(几秒后), schedule=定时触发(指定时间), event=事件触发, condition=条件触发",
                    },
                    "message": {
                        "type": "string",
                        "description": "触发时注入的消息内容",
                    },
                    "delay_seconds": {
                        "type": "integer",
                        "description": "延迟秒数（trigger_type=delay 时必填），最小 1 秒，最大 86400 秒（24小时）",
                    },
                    "schedule_time": {
                        "type": "string",
                        "description": "定时触发时间（trigger_type=schedule 时必填），ISO 8601 格式，如: 2026-03-15T15:00:00",
                    },
                    "event_type": {
                        "type": "string",
                        "description": "监听的事件类型（trigger_type=event 时必填），如: task_completed, file_changed",
                    },
                    "condition": {
                        "type": "string",
                        "description": "条件表达式（trigger_type=condition 时必填），如: task_status == 'pending'",
                    },
                },
                "required": ["trigger_type", "message"],
            },
            injected_params=["session_id", "execution_id"],
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            level=ToolLevel.SYSTEM,
            requires_approval=False,
            tags=["trigger", "automation", "self-trigger"],
            when_to_use=[
                "需要延迟执行某项任务时",
                "需要在特定时间点执行任务时",
                "需要监听某个事件并响应时",
                "需要等待某个条件满足时执行任务时",
            ],
            when_not_to_use=[
                "需要立即执行任务时（直接执行即可）",
                "需要触发其他会话时（触发器只能触发自己）",
            ],
            caveats=[
                "触发器只能触发自己所在的会话",
                "延迟时间最大为24小时",
                "定时触发时间不能超过7天",
                "单会话最多设置10个触发器",
            ],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行触发器设置"""
        trigger_type = inputs.get("trigger_type")
        message = inputs.get("message")
        session_id = inputs.get("session_id")
        execution_id = inputs.get("execution_id")

        if not trigger_type:
            return create_failure_result(
                error="缺少必需参数: trigger_type",
                error_code="MISSING_TRIGGER_TYPE",
            )

        if not message:
            return create_failure_result(
                error="缺少必需参数: message",
                error_code="MISSING_MESSAGE",
            )

        if not session_id:
            return create_failure_result(
                error="缺少注入参数: session_id",
                error_code="MISSING_SESSION_ID",
            )

        if not execution_id:
            execution_id = f"exec_{uuid.uuid4().hex[:12]}"

        if self._queue.size(session_id) >= self.MAX_TRIGGERS_PER_SESSION:
            return create_failure_result(
                error=f"单会话触发器数量已达上限 ({self.MAX_TRIGGERS_PER_SESSION})",
                error_code="TRIGGER_LIMIT_EXCEEDED",
            )

        try:
            if trigger_type == "delay":
                return await self._setup_delay_trigger(
                    inputs, session_id, execution_id, message
                )
            elif trigger_type == "schedule":
                return await self._setup_schedule_trigger(
                    inputs, session_id, execution_id, message
                )
            elif trigger_type == "event":
                return await self._setup_event_trigger(
                    inputs, session_id, execution_id, message
                )
            elif trigger_type == "condition":
                return await self._setup_condition_trigger(
                    inputs, session_id, execution_id, message
                )
            else:
                return create_failure_result(
                    error=f"不支持的触发类型: {trigger_type}",
                    error_code="INVALID_TRIGGER_TYPE",
                )

        except Exception as e:
            logger.error(f"[TriggerSetupTool] 设置触发器失败: {e}", exc_info=True)
            return create_failure_result(
                error=f"设置触发器失败: {str(e)}",
                error_code="TRIGGER_SETUP_FAILED",
            )

    async def _setup_delay_trigger(
        self,
        inputs: dict[str, Any],
        session_id: str,
        execution_id: str,
        message: str,
    ) -> ToolExecutionResult:
        """设置延迟触发器"""
        delay_seconds = inputs.get("delay_seconds")

        if delay_seconds is None:
            return create_failure_result(
                error="delay 类型触发器需要提供 delay_seconds 参数",
                error_code="MISSING_DELAY_SECONDS",
            )

        if not isinstance(delay_seconds, int) or delay_seconds < 1:
            return create_failure_result(
                error="delay_seconds 必须是大于 0 的整数",
                error_code="INVALID_DELAY_SECONDS",
            )

        if delay_seconds > self.MAX_DELAY_SECONDS:
            return create_failure_result(
                error=f"延迟时间超过最大限制 ({self.MAX_DELAY_SECONDS} 秒 = 24小时)",
                error_code="DELAY_EXCEEDS_LIMIT",
            )

        trigger_id = f"trigger_delay_{uuid.uuid4().hex[:12]}"
        expires_at = datetime.utcnow() + timedelta(seconds=delay_seconds)

        trigger_message = TriggerMessage(
            id=trigger_id,
            session_id=session_id,
            execution_id=execution_id,
            content=message,
            priority=inputs.get("priority", 0),
            expires_at=expires_at,
            metadata={
                "trigger_type": "delay",
                "delay_seconds": delay_seconds,
            },
        )

        self._queue.push(trigger_message)

        logger.info(
            f"[TriggerSetupTool] 延迟触发器已设置 | "
            f"trigger_id={trigger_id} | "
            f"session_id={session_id} | "
            f"delay_seconds={delay_seconds}"
        )

        return create_success_result(
            data={
                "success": True,
                "trigger_id": trigger_id,
                "message": f"触发器已设置，将在 {delay_seconds} 秒后触发",
            },
            metadata={
                "trigger_type": "delay",
                "delay_seconds": delay_seconds,
                "expires_at": expires_at.isoformat(),
            },
        )

    async def _setup_schedule_trigger(
        self,
        inputs: dict[str, Any],
        session_id: str,
        execution_id: str,
        message: str,
    ) -> ToolExecutionResult:
        """设置定时触发器"""
        schedule_time_str = inputs.get("schedule_time")

        if not schedule_time_str:
            return create_failure_result(
                error="schedule 类型触发器需要提供 schedule_time 参数",
                error_code="MISSING_SCHEDULE_TIME",
            )

        try:
            schedule_time = datetime.fromisoformat(schedule_time_str.replace("Z", "+00:00"))
        except ValueError:
            return create_failure_result(
                error=f"无效的时间格式: {schedule_time_str}，应为 ISO 8601 格式",
                error_code="INVALID_SCHEDULE_TIME",
            )

        now = datetime.utcnow()
        if schedule_time < now:
            return create_failure_result(
                error="定时触发时间不能早于当前时间",
                error_code="SCHEDULE_TIME_IN_PAST",
            )

        max_schedule_time = now + timedelta(hours=self.MAX_SCHEDULE_HOURS)
        if schedule_time > max_schedule_time:
            return create_failure_result(
                error=f"定时触发时间超过最大限制 ({self.MAX_SCHEDULE_HOURS} 小时 = 7天)",
                error_code="SCHEDULE_TIME_EXCEEDS_LIMIT",
            )

        trigger_id = f"trigger_schedule_{uuid.uuid4().hex[:12]}"

        trigger_message = TriggerMessage(
            id=trigger_id,
            session_id=session_id,
            execution_id=execution_id,
            content=message,
            priority=inputs.get("priority", 0),
            expires_at=schedule_time + timedelta(minutes=5),
            metadata={
                "trigger_type": "schedule",
                "schedule_time": schedule_time_str,
            },
        )

        self._queue.push(trigger_message)

        logger.info(
            f"[TriggerSetupTool] 定时触发器已设置 | "
            f"trigger_id={trigger_id} | "
            f"session_id={session_id} | "
            f"schedule_time={schedule_time_str}"
        )

        return create_success_result(
            data={
                "success": True,
                "trigger_id": trigger_id,
                "message": f"触发器已设置，将在 {schedule_time_str} 触发",
            },
            metadata={
                "trigger_type": "schedule",
                "schedule_time": schedule_time_str,
            },
        )

    async def _setup_event_trigger(
        self,
        inputs: dict[str, Any],
        session_id: str,
        execution_id: str,
        message: str,
    ) -> ToolExecutionResult:
        """设置事件触发器"""
        event_type = inputs.get("event_type")

        if not event_type:
            return create_failure_result(
                error="event 类型触发器需要提供 event_type 参数",
                error_code="MISSING_EVENT_TYPE",
            )

        trigger_id = f"trigger_event_{uuid.uuid4().hex[:12]}"

        trigger_message = TriggerMessage(
            id=trigger_id,
            session_id=session_id,
            execution_id=execution_id,
            content=message,
            priority=inputs.get("priority", 0),
            expires_at=datetime.utcnow() + timedelta(hours=24),
            metadata={
                "trigger_type": "event",
                "event_type": event_type,
            },
        )

        self._queue.push(trigger_message)

        logger.info(
            f"[TriggerSetupTool] 事件触发器已设置 | "
            f"trigger_id={trigger_id} | "
            f"session_id={session_id} | "
            f"event_type={event_type}"
        )

        return create_success_result(
            data={
                "success": True,
                "trigger_id": trigger_id,
                "message": f"事件触发器已设置，监听事件: {event_type}",
            },
            metadata={
                "trigger_type": "event",
                "event_type": event_type,
            },
        )

    async def _setup_condition_trigger(
        self,
        inputs: dict[str, Any],
        session_id: str,
        execution_id: str,
        message: str,
    ) -> ToolExecutionResult:
        """设置条件触发器"""
        condition = inputs.get("condition")

        if not condition:
            return create_failure_result(
                error="condition 类型触发器需要提供 condition 参数",
                error_code="MISSING_CONDITION",
            )

        trigger_id = f"trigger_condition_{uuid.uuid4().hex[:12]}"

        trigger_message = TriggerMessage(
            id=trigger_id,
            session_id=session_id,
            execution_id=execution_id,
            content=message,
            priority=inputs.get("priority", 0),
            expires_at=datetime.utcnow() + timedelta(hours=24),
            metadata={
                "trigger_type": "condition",
                "condition": condition,
            },
        )

        self._queue.push(trigger_message)

        logger.info(
            f"[TriggerSetupTool] 条件触发器已设置 | "
            f"trigger_id={trigger_id} | "
            f"session_id={session_id} | "
            f"condition={condition}"
        )

        return create_success_result(
            data={
                "success": True,
                "trigger_id": trigger_id,
                "message": f"条件触发器已设置，条件: {condition}",
            },
            metadata={
                "trigger_type": "condition",
                "condition": condition,
            },
        )
