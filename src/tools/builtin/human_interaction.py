"""
人类交互工具

暴露接口：
- create_human_interaction_tool(pipeline_id: str | None) -> HumanInteractionTool：create_human_interaction_tool功能
- get_tool_definition() -> Tool：get_tool_definition功能
- HumanInteractionTool：HumanInteractionTool类
"""

import logging
from typing import Any

from human_interaction import (
    InteractionMode,
    Priority,
    get_human_interaction_service,
)
from human_interaction.service import (
    InteractionCancelledError,
    InteractionDeniedError,
    InteractionTimeoutError,
)
from core.results import ToolExecutionResult
from tools.builtin.base import BuiltinTool
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)


class HumanInteractionTool(BuiltinTool):
    """
    人类交互工具

    支持两种交互模式：
    - 选择模式（choice）：弹出选择框，阻塞等待用户做出决定
    - 对话模式（conversation）：跳转到对话标签页，用户在标签页中对话
    """

    def __init__(
        self,
        pipeline_id: str | None = None,
    ):
        """初始化人类交互工具"""
        self.pipeline_id = pipeline_id

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="human_interaction",
            description="与用户交互。选择模式：弹出选择框等待用户决定；对话模式：跳转到对话标签页。",
            input_schema={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["choice", "conversation"],
                        "description": "交互模式：choice=选择模式（弹出选择框），conversation=对话模式（跳转标签页）",
                    },
                    "title": {
                        "type": "string",
                        "description": "交互标题",
                    },
                    "description": {
                        "type": "string",
                        "description": "详细说明",
                    },
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "label": {"type": "string"},
                            },
                        },
                        "description": "选项列表（选择模式）",
                    },
                    "questions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "问题列表（澄清场景）",
                    },
                    "initial_message": {
                        "type": "string",
                        "description": "开场消息（对话模式）",
                    },
                    "suggestions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "快捷回复建议（对话模式）",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "default": 300,
                        "description": "超时时间（秒）",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "normal", "high", "critical"],
                        "default": "normal",
                        "description": "优先级",
                    },
                },
                "required": ["mode", "title"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            level=ToolLevel.ALL,
            tags=["interaction", "human", "approval", "conversation"],
            injected_params=[
                "pipeline_id",
            ],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行人类交互工具"""
        mode = inputs.get("mode")

        pipeline_id = self.pipeline_id or inputs.get("pipeline_id")

        if not pipeline_id:
            return create_failure_result(
                error="缺少必要的上下文信息（pipeline_id）"
            )

        service = get_human_interaction_service()

        if mode == InteractionMode.CHOICE.value:
            return await self._execute_choice_mode(inputs, service, pipeline_id)
        elif mode == InteractionMode.CONVERSATION.value:
            return await self._execute_conversation_mode(inputs, service, pipeline_id)
        else:
            return create_failure_result(error=f"不支持的交互模式: {mode}")

    async def _execute_choice_mode(
        self,
        inputs: dict[str, Any],
        service,
        pipeline_id: str,
    ) -> ToolExecutionResult:
        """执行选择模式"""
        title = inputs.get("title", "")
        description = inputs.get("description", "")
        options = inputs.get("options")
        questions = inputs.get("questions")
        timeout_seconds = inputs.get("timeout_seconds", 300)
        priority_str = inputs.get("priority", "normal")

        priority = Priority(priority_str) if priority_str in [p.value for p in Priority] else Priority.NORMAL

        try:
            request_id = await service.create_choice_request(
                session_id=pipeline_id,
                thread_id=pipeline_id,
                tab_id=pipeline_id,
                title=title,
                description=description,
                options=options,
                questions=questions,
                timeout_seconds=timeout_seconds,
                priority=priority,
                user_id=None,
                agent_id=pipeline_id,
            )

            response = await service.wait_for_choice(request_id, timeout=timeout_seconds)

            # BUG-FIX-fix_20260408_human_interaction_response:
            # 精简返回值，只返回有意义的信息，移除 null 字段
            result = {"status": "completed", "response_type": response.get("response_type")}
            if response.get("selected_option"):
                result["selected_option"] = response["selected_option"]
            if response.get("answers"):
                result["answers"] = response["answers"]
            if response.get("feedback"):
                result["feedback"] = response["feedback"]
            return create_success_result(data=result)

        except InteractionTimeoutError as e:
            logger.warning(f"[HumanInteractionTool] 交互超时 | request_id={e.request_id}")
            return create_failure_result(
                error=f"交互超时: {e.timeout}秒内未收到响应",
                error_code="INTERACTION_TIMEOUT",
            )

        except InteractionCancelledError as e:
            logger.info(f"[HumanInteractionTool] 交互取消 | request_id={e.request_id}")
            return create_failure_result(
                error=f"交互已取消: {e.reason or '用户取消'}",
                error_code="INTERACTION_CANCELLED",
            )

        except InteractionDeniedError as e:
            logger.info(f"[HumanInteractionTool] 交互拒绝 | request_id={e.request_id}")
            return create_success_result(
                data={
                    "status": "denied",
                    "reason": e.reason or "用户拒绝",
                }
            )

        except Exception as e:
            logger.error(f"[HumanInteractionTool] 选择模式执行失败 | error={e}", exc_info=True)
            return create_failure_result(error=f"选择模式执行失败: {str(e)}")

    async def _execute_conversation_mode(
        self,
        inputs: dict[str, Any],
        service,
        pipeline_id: str,
    ) -> ToolExecutionResult:
        """执行对话模式"""
        title = inputs.get("title", "")
        description = inputs.get("description", "")
        initial_message = inputs.get("initial_message")
        suggestions = inputs.get("suggestions")
        timeout_seconds = inputs.get("timeout_seconds", 60)

        try:
            request_id = await service.create_conversation_request(
                session_id=pipeline_id,
                thread_id=pipeline_id,
                tab_id=pipeline_id,
                title=title,
                description=description,
                initial_message=initial_message,
                suggestions=suggestions,
                user_id=None,
                agent_id=pipeline_id,
            )

            # BUG-FIX-fix_20260408_human_interaction_response:
            # 等待用户到达对话页面后返回有意义的信息
            result = await service.wait_for_conversation_arrival(
                request_id, timeout=timeout_seconds
            )

            return create_success_result(data=result)

        except Exception as e:
            logger.error(f"[HumanInteractionTool] 对话模式执行失败 | error={e}", exc_info=True)
            return create_failure_result(error=f"对话模式执行失败: {str(e)}")


def create_human_interaction_tool(
    pipeline_id: str | None = None,
) -> HumanInteractionTool:
    """创建人类交互工具实例"""
    return HumanInteractionTool(
        pipeline_id=pipeline_id,
    )
