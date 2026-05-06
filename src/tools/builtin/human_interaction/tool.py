"""
人类交互工具

暴露接口：
- create_human_interaction_tool(pipeline_id: str | None) -> HumanInteractionTool：create_human_interaction_tool功能
- get_tool_definition() -> Tool：get_tool_definition功能
- HumanInteractionTool：HumanInteractionTool类
"""

import asyncio
from asyncio import CancelledError
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

    支持三种交互模式：
    - 选择模式（choice）：弹出选择框，阻塞等待用户做出决定
    - 对话模式（conversation）：跳转到对话标签页，用户在标签页中对话
    - 通知模式（notification）：非阻塞推送信息到前端，不等待用户响应
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
            description="与用户交互。选择模式：弹出选择框等待用户决定；对话模式：跳转到对话标签页；通知模式：非阻塞推送信息。",
            input_schema={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["choice", "conversation", "notification"],
                        "description": "交互模式：choice=选择模式（弹出选择框），conversation=对话模式（跳转标签页），notification=通知模式（非阻塞推送）",
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
                        "description": "开场消息（对话模式）/ 通知内容（通知模式）",
                    },
                    "suggestions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "快捷回复建议（对话模式）",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "default": 86400,
                        "description": "超时时间（秒）",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "normal", "high", "critical"],
                        "default": "normal",
                        "description": "优先级",
                    },
                    "progress": {
                        "type": "number",
                        "description": "进度百分比 0-100（通知模式）",
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
        # DIAG: check if pipeline task is already being cancelled
        _current_task = asyncio.current_task()
        if _current_task:
            _cancelling = _current_task.cancelling()
            if _cancelling > 0:
                logger.warning(
                    "[HumanInteractionTool] Task already cancelling! "
                    "cancelling=%d — tool execution will fail",
                    _cancelling,
                )

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
        elif mode == InteractionMode.NOTIFICATION.value:
            return await self._execute_notification_mode(inputs, service, pipeline_id)
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
        timeout_seconds = inputs.get("timeout_seconds", 86400)
        priority_str = inputs.get("priority", "normal")

        priority = Priority(priority_str) if priority_str in [p.value for p in Priority] else Priority.NORMAL

        request_id: str | None = None
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

            response = await service.wait_for_choice(
                request_id, timeout=timeout_seconds,
            )

            result = {
                "status": "completed",
                "response_type": response.get("response_type"),
            }
            if response.get("selected_option"):
                result["selected_option"] = response["selected_option"]
            if response.get("answers"):
                result["answers"] = response["answers"]
            if response.get("feedback"):
                result["feedback"] = response["feedback"]
            return create_success_result(data=result)

        except InteractionTimeoutError as e:
            logger.warning(
                "[HumanInteractionTool] 交互超时 | "
                "request_id=%s", e.request_id,
            )
            return create_failure_result(
                error=(
                    f"人类交互超时（等待了{e.timeout}秒），"
                    "用户未在规定时间内响应。"
                    "你可以根据当前任务上下文决定下一步操作。"
                ),
                error_code="INTERACTION_TIMEOUT",
            )

        except InteractionCancelledError as e:
            logger.info(
                "[HumanInteractionTool] 交互取消 | "
                "request_id=%s", e.request_id,
            )
            return create_failure_result(
                error=(
                    f"人类交互已取消: {e.reason or '用户取消'}。"
                    "你可以根据当前任务上下文决定下一步操作。"
                ),
                error_code="INTERACTION_CANCELLED",
            )

        except InteractionDeniedError as e:
            logger.info(
                "[HumanInteractionTool] 交互拒绝 | "
                "request_id=%s", e.request_id,
            )
            return create_success_result(
                data={
                    "status": "denied",
                    "reason": e.reason or "用户拒绝",
                }
            )

        except CancelledError:
            logger.info(
                "[HumanInteractionTool] 管道被取消 | "
                "request_id=%s",
                request_id,
            )
            # 清理残留请求，防止堆积
            if request_id:
                try:
                    await service.cancel_request(
                        request_id, reason="pipeline_cancelled",
                    )
                except Exception:
                    pass
            raise

        except Exception as e:
            logger.error(
                "[HumanInteractionTool] 选择模式执行失败 | "
                "error=%s", e, exc_info=True,
            )
            return create_failure_result(
                error=(
                    f"人类交互执行失败: {str(e)}。"
                    "你可以根据当前任务上下文决定下一步操作。"
                ),
                error_code="INTERACTION_FAILED",
            )

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
        timeout_seconds = inputs.get("timeout_seconds", 86400)

        request_id: str | None = None
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

            response = await service.wait_for_choice(
                request_id, timeout=timeout_seconds,
            )

            result = {
                "status": "completed",
                "response_type": response.get("response_type"),
            }
            if response.get("feedback"):
                result["feedback"] = response["feedback"]
            return create_success_result(data=result)

        except InteractionTimeoutError as e:
            logger.warning(
                "[HumanInteractionTool] 对话超时 | "
                "request_id=%s", e.request_id,
            )
            return create_failure_result(
                error=(
                    f"对话超时（等待了{e.timeout}秒），"
                    "用户未在规定时间内响应。"
                    "你可以根据当前任务上下文决定下一步操作。"
                ),
                error_code="INTERACTION_TIMEOUT",
            )

        except InteractionCancelledError as e:
            logger.info(
                "[HumanInteractionTool] 对话取消 | "
                "request_id=%s", e.request_id,
            )
            return create_failure_result(
                error=(
                    f"对话已取消: {e.reason or '用户取消'}。"
                    "你可以根据当前任务上下文决定下一步操作。"
                ),
                error_code="INTERACTION_CANCELLED",
            )

        except InteractionDeniedError as e:
            logger.info(
                "[HumanInteractionTool] 对话拒绝 | "
                "request_id=%s", e.request_id,
            )
            return create_success_result(
                data={
                    "status": "denied",
                    "reason": e.reason or "用户拒绝",
                }
            )

        except CancelledError:
            logger.info(
                "[HumanInteractionTool] 管道被取消 | "
                "request_id=%s",
                request_id,
            )
            # 清理残留请求，防止堆积
            if request_id:
                try:
                    await service.cancel_request(
                        request_id, reason="pipeline_cancelled",
                    )
                except Exception:
                    pass
            raise

        except Exception as e:
            logger.error(
                "[HumanInteractionTool] 对话模式执行失败 | "
                "error=%s", e, exc_info=True,
            )
            return create_failure_result(
                error=(
                    f"人类交互执行失败: {str(e)}。"
                    "你可以根据当前任务上下文决定下一步操作。"
                ),
                error_code="INTERACTION_FAILED",
            )


    async def _execute_notification_mode(
        self,
        inputs: dict[str, Any],
        service,
        pipeline_id: str,
    ) -> ToolExecutionResult:
        """执行通知模式，非阻塞发送通知后立即返回。"""
        title = inputs.get("title", "")
        description = inputs.get("description", "")
        initial_message = inputs.get("initial_message")
        progress = inputs.get("progress")
        priority_str = inputs.get("priority", "normal")

        priority = Priority(priority_str) if priority_str in [p.value for p in Priority] else Priority.NORMAL

        try:
            request_id = await service.send_notification(
                session_id=pipeline_id,
                thread_id=pipeline_id,
                title=title,
                message=description or initial_message or "",
                priority=priority,
                progress=progress,
                agent_id=pipeline_id,
            )

            return create_success_result(
                data={
                    "status": "sent",
                    "request_id": request_id,
                    "message": "通知已发送",
                }
            )

        except Exception as e:
            logger.error(
                "[HumanInteractionTool] 通知模式执行失败 | "
                "error=%s", e, exc_info=True,
            )
            return create_failure_result(
                error=(
                    f"通知发送失败: {str(e)}。"
                    "你可以根据当前任务上下文决定下一步操作。"
                ),
                error_code="INTERACTION_FAILED",
            )


def create_human_interaction_tool(
    pipeline_id: str | None = None,
) -> HumanInteractionTool:
    """创建人类交互工具实例"""
    return HumanInteractionTool(
        pipeline_id=pipeline_id,
    )
