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
from tools.builtin.shared.formatters import format_size
from tools.builtin.workspace_aware import WorkspaceAwareMixin
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)

MAX_FILE_PATHS = 10
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024


class HumanInteractionTool(BuiltinTool, WorkspaceAwareMixin):
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
            description="与用户交互。选择模式：弹出选择框等待用户决定；对话模式：跳转到对话标签页；通知模式：非阻塞推送信息。注意：使用 file_paths 展示文件时必须使用 choice 或 conversation 模式，不能使用 notification 模式。",
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
                    "file_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "需要展示给用户的文件路径列表。系统会自动读取文件内容并在交互面板中展示。以下两种情况都必须使用此参数：（1）主动展示——当你需要将文件内容、设计方案、代码变更等信息呈现给用户查看或审批时（如通知模式推送文件、选择/对话模式展示文件变更）；（2）用户请求——当用户明确要求查看某个文件、某个结果，或要求省略/跳过某些内容并需要确认时。使用此参数时必须选择 choice 或 conversation 模式，不支持 notification 模式。支持相对路径（基于工作空间）和绝对路径，单文件不超过10MB，最多10个文件。",
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
                "workspace",
            ],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行人类交互工具"""
        self._init_workspace(inputs)

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

    def _parse_agent_level(self, inputs: dict[str, Any]) -> str:
        """从输入参数中解析代理层级标识"""
        raw = inputs.get("parent_agent_level", 1)
        try:
            val = int(str(raw).upper().lstrip("L"))
        except (ValueError, TypeError):
            val = 1
        return f"L{val}"

    def _validate_file_paths(self, inputs: dict[str, Any]) -> ToolExecutionResult | None:
        """校验 file_paths 参数的合法性。

        校验规则：
        - file_paths 为 None 或空列表 → 合法，直接返回 None
        - file_paths 超过 MAX_FILE_PATHS 个 → 返回错误
        - 逐个路径校验：文件不存在、路径是目录、文件超过大小限制 → 收集错误

        Args:
            inputs: 工具执行时接收的输入参数字典

        Returns:
            None 表示校验通过，ToolExecutionResult 表示校验失败
        """
        file_paths = inputs.get("file_paths")

        if file_paths is None or (isinstance(file_paths, list) and len(file_paths) == 0):
            return None

        if len(file_paths) > MAX_FILE_PATHS:
            return create_failure_result(
                error=f"文件路径数量超过限制 ({len(file_paths)} > {MAX_FILE_PATHS})",
                error_code="INVALID_FILE_PATHS",
            )

        errors: list[str] = []
        for path_str in file_paths:
            path = self.resolve_path(path_str)

            if not path.exists():
                errors.append(f"文件不存在: {path_str}")
                continue

            if not path.is_file():
                errors.append(f"路径不是文件: {path_str}")
                continue

            file_size = path.stat().st_size
            if file_size > MAX_FILE_SIZE_BYTES:
                errors.append(
                    f"文件过大 ({format_size(file_size)})，超过限制 ({format_size(MAX_FILE_SIZE_BYTES)}): {path_str}"
                )

        if errors:
            return create_failure_result(
                error="; ".join(errors),
                error_code="INVALID_FILE_PATHS",
            )

        return None

    async def _execute_choice_mode(
        self,
        inputs: dict[str, Any],
        service,
        pipeline_id: str,
    ) -> ToolExecutionResult:
        """执行选择模式"""
        # 校验 file_paths 参数
        validation_error = self._validate_file_paths(inputs)
        if validation_error is not None:
            return validation_error

        title = inputs.get("title", "")
        description = inputs.get("description", "")
        options = inputs.get("options")
        questions = inputs.get("questions")
        timeout_seconds = inputs.get("timeout_seconds", 86400)
        priority_str = inputs.get("priority", "normal")

        priority = Priority(priority_str) if priority_str in [p.value for p in Priority] else Priority.NORMAL

        agent_level_str = self._parse_agent_level(inputs)

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
                file_paths=inputs.get("file_paths"),
                user_id=None,
                agent_id=pipeline_id,
                agent_level=agent_level_str,
            )

            response = await service.wait_for_choice(
                request_id, timeout=timeout_seconds,
            )

            logger.info(
                "[HumanInteractionTool] wait_for_choice() 返回 | request_id=%s | response=%s",
                request_id, {k: v for k, v in response.items() if k != "answers"},
            )

            selected_id = response.get("selected_option")
            result: dict[str, Any] = {
                "status": "completed",
                "response_type": response.get("response_type"),
            }
            if selected_id:
                result["selected_option"] = selected_id
                if isinstance(options, list):
                    for opt in options:
                        if isinstance(opt, dict) and opt.get("id") == selected_id:
                            result["selected_option_label"] = opt.get("label", selected_id)
                            break
            if response.get("answers"):
                result["answers"] = response["answers"]
            if response.get("feedback"):
                result["feedback"] = response["feedback"]
            logger.info(
                "[HumanInteractionTool] 选择模式完成 | request_id=%s | selected=%s | status=%s",
                request_id, result.get("selected_option"), result.get("status"),
            )
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
                    "selected_option": "reject",
                    "reason": e.reason or "用户拒绝",
                }
            )

        except CancelledError:
            logger.info(
                "[HumanInteractionTool] 管道被取消 | "
                "request_id=%s",
                request_id,
            )
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
        # 校验 file_paths 参数
        validation_error = self._validate_file_paths(inputs)
        if validation_error is not None:
            return validation_error

        title = inputs.get("title", "")
        description = inputs.get("description", "")
        initial_message = inputs.get("initial_message")
        suggestions = inputs.get("suggestions")
        timeout_seconds = inputs.get("timeout_seconds", 86400)

        agent_level_str = self._parse_agent_level(inputs)

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
                file_paths=inputs.get("file_paths"),
                user_id=None,
                agent_id=pipeline_id,
                agent_level=agent_level_str,
            )

            response = await service.wait_for_choice(
                request_id, timeout=timeout_seconds,
            )

            resp_type = response.get("response_type", "")
            feedback = response.get("feedback", "")

            if resp_type == "approved":
                result = {
                    "status": "user_arrived",
                    "conversation_mode": True,
                    "selected_option": None,
                    "message": (
                        feedback or "用户已进入对话标签页。"
                        "【重要指令】你不得再输出任何文字，不得再调用任何工具（尤其是 human_interaction）。"
                        "管道已自动挂起，等待用户在对话标签页中发起新消息后才会唤醒你。"
                        "你现在什么都不需要做。"
                    ),
                }
            else:
                result = {
                    "status": "completed",
                    "response_type": resp_type,
                    "selected_option": None,
                }
                if feedback:
                    result["feedback"] = feedback
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
                    "selected_option": "reject",
                    "conversation_mode": True,
                    "reason": e.reason or "用户拒绝",
                }
            )

        except CancelledError:
            logger.info(
                "[HumanInteractionTool] 管道被取消 | "
                "request_id=%s",
                request_id,
            )
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
        if inputs.get("file_paths"):
            return create_failure_result(
                error="通知模式不支持 file_paths 参数，请使用 choice 或 conversation 模式",
                error_code="INVALID_PARAMS",
            )

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
