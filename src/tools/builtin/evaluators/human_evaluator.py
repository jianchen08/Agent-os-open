"""
人工评估器

请求人工审核/审批

使用统一的人类交互抽象层实现人工评估功能。
"""

from datetime import datetime
from typing import Any

from src.core.human_interaction import (
    InteractionType,
    ResponseType,
    get_human_interaction_service,
)
from src.core.results import ToolExecutionResult
from src.tools.types import (
    Tool,
    ToolCategory,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class HumanEvaluator:
    """人工评估器 - 使用统一人类交互抽象层"""

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="human_evaluator",
            description="人工评估器：请求人工审核/审批，支持审批模式和对话模式",
            input_schema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "审核标题",
                    },
                    "description": {
                        "type": "string",
                        "description": "审核说明",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["approval", "review", "conversation"],
                        "description": "审核类型：approval=审批模式, review=审核模式, conversation=对话模式",
                        "default": "approval",
                    },
                    "checklist": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "检查清单",
                    },
                    "timeout_hours": {
                        "type": "number",
                        "description": "超时时间（小时）",
                        "default": 24,
                    },
                    "request_id": {
                        "type": "string",
                        "description": "交互请求 ID（用于查询或提交结果）",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["request", "submit", "check", "conversation"],
                        "description": "操作类型",
                        "default": "request",
                    },
                    "approved": {
                        "type": "boolean",
                        "description": "审核结果（submit 时使用）",
                    },
                    "feedback": {
                        "type": "string",
                        "description": "审核反馈（submit 时使用）",
                    },
                    "conversation_message": {
                        "type": "string",
                        "description": "对话消息（conversation 模式时使用）",
                    },
                    "jump_url": {
                        "type": "string",
                        "description": "对话模式跳转 URL（可选）",
                    },
                },
                "required": ["title"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            requires_approval=False,
            tags=["evaluator", "human", "approval", "review", "conversation"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行人工审核"""
        action = inputs.get("action", "request")

        if action == "request":
            return await self._request_review(inputs)
        if action == "submit":
            return await self._submit_review(inputs)
        if action == "check":
            return await self._check_review(inputs)
        if action == "conversation":
            return await self._start_conversation(inputs)

        return create_failure_result(error=f"不支持的操作: {action}")

    async def _request_review(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """请求人工审核"""
        title = inputs.get("title")
        description = inputs.get("description", "")
        review_type = inputs.get("type", "approval")
        checklist = inputs.get("checklist", [])
        timeout_hours = inputs.get("timeout_hours", 24)

        # 使用统一人类交互服务
        service = get_human_interaction_service()

        # 根据类型选择交互模式
        interaction_type = (
            InteractionType.APPROVAL if review_type == "approval"
            else InteractionType.CONVERSATION
        )

        request = await service.request_interaction(
            interaction_type=interaction_type,
            title=title,
            description=description,
            source="human_evaluator",
            source_id="",
            context={
                "checklist": checklist,
                "review_type": review_type,
            },
            options={
                "actions": [
                    {"id": "approve", "label": "通过"},
                    {"id": "reject", "label": "拒绝"},
                    {"id": "modify", "label": "修改后通过"},
                ]
            },
            timeout_seconds=int(timeout_hours * 3600),
        )

        return create_success_result(
            data={
                "passed": False,
                "score": 0,
                "feedback": "已创建审核请求，等待人工审核",
                "details": {
                    "request_id": request.request_id,
                    "title": title,
                    "type": review_type,
                    "status": "pending",
                    "expires_at": request.expires_at.isoformat() if request.expires_at else None,
                },
            }
        )

    async def _submit_review(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """提交审核结果"""
        request_id = inputs.get("request_id")
        approved = inputs.get("approved")
        feedback = inputs.get("feedback", "")

        if not request_id:
            return create_failure_result(
                error="request_id 不能为空",
                error_code="MISSING_REQUEST_ID",
            )

        # 使用统一人类交互服务
        service = get_human_interaction_service()

        # 映射响应类型
        response_type = ResponseType.APPROVED if approved else ResponseType.REJECTED

        success = await service.submit_response(
            request_id=request_id,
            response_type=response_type,
            responder_id="human_evaluator",
            responder_name="Human Evaluator",
            comment=feedback,
        )

        if not success:
            return create_failure_result(
                error=f"提交审核结果失败: {request_id}",
                error_code="SUBMIT_FAILED",
            )

        return create_success_result(
            data={
                "passed": approved,
                "score": 100 if approved else 0,
                "feedback": feedback or ("审核通过" if approved else "审核未通过"),
                "details": {
                    "request_id": request_id,
                    "status": "approved" if approved else "rejected",
                    "reviewed_at": datetime.now().isoformat(),
                },
            }
        )

    async def _check_review(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """检查审核状态"""
        request_id = inputs.get("request_id")

        if not request_id:
            return create_failure_result(
                error="request_id 不能为空",
                error_code="MISSING_REQUEST_ID",
            )

        # 使用统一人类交互服务
        service = get_human_interaction_service()
        request = await service.get_request(request_id)

        if not request:
            return create_failure_result(
                error=f"审核请求不存在: {request_id}",
                error_code="REQUEST_NOT_FOUND",
            )

        status = request.status.value

        if status == "pending":
            return create_success_result(
                data={
                    "passed": False,
                    "score": 0,
                    "feedback": "等待人工审核",
                    "details": {
                        "request_id": request_id,
                        "status": "pending",
                        "expires_at": request.expires_at.isoformat() if request.expires_at else None,
                    },
                }
            )
        if status == "expired":
            return create_success_result(
                data={
                    "passed": False,
                    "score": 0,
                    "feedback": "审核请求已过期",
                    "details": {"request_id": request_id, "status": "expired"},
                }
            )

        # 已审核
        approved = request.status.value == "approved"
        return create_success_result(
            data={
                "passed": approved,
                "score": 100 if approved else 0,
                "feedback": request.response.comment or "",
                "details": {
                    "request_id": request_id,
                    "status": status,
                    "reviewed_at": request.response.responded_at.isoformat() if request.response else None,
                },
            }
        )

    async def _start_conversation(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """启动对话模式"""
        title = inputs.get("title")
        description = inputs.get("description", "")
        message = inputs.get("conversation_message", "")
        jump_url = inputs.get("jump_url")

        # 使用统一人类交互服务
        service = get_human_interaction_service()

        request = await service.request_conversation(
            title=title,
            description=description,
            source="human_evaluator",
            source_id="",
            context={
                "initial_message": message,
            },
            jump_url=jump_url,
        )

        return create_success_result(
            data={
                "passed": False,
                "score": 0,
                "feedback": "已创建对话请求，等待人工响应",
                "details": {
                    "request_id": request.request_id,
                    "title": title,
                    "type": "conversation",
                    "status": "pending",
                    "jump_url": jump_url,
                },
            }
        )
