"""
人工审批节点

提供 LangGraph StateGraph 中的 human_approval_node 函数
使用统一的人类交互抽象层
"""

import logging
from typing import Any

from src.agents.state import AgentState
from src.core.human_interaction.models import (
    ApprovalOption,
    InteractionRequest,
    InteractionSource,
    Priority,
)
from src.core.human_interaction.service import get_human_interaction_service

logger = logging.getLogger(__name__)


async def human_approval_node(state: AgentState) -> dict[str, Any]:
    """
    人工审批节点

    使用统一的人类交互抽象层处理审批请求

    Args:
        state: 当前状态

    Returns:
        状态更新字典
    """
    pending_calls = state.get("pending_tool_calls", [])

    if not pending_calls:
        return {}

    requires_approval = state.get("requires_approval", False)
    if not requires_approval:
        return {}

    thread_id = state.get("thread_id", "")
    session_id = state.get("session_id")
    agent_id = state.get("agent_id")

    tool_names = [tc.get("name") for tc in pending_calls]
    tool_args = [tc.get("args") for tc in pending_calls]

    title = f"工具调用审批: {', '.join(tool_names)}"
    description = "即将执行以下工具调用:\n" + "\n".join(
        f"- {name}({args})" for name, args in zip(tool_names, tool_args, strict=False)
    )

    approval_options = [
        ApprovalOption(id="approve", label="批准", is_default=True),
        ApprovalOption(id="deny", label="拒绝", is_destructive=True),
        ApprovalOption(id="modify", label="修改参数"),
    ]

    request = InteractionRequest.create_approval_request(
        thread_id=thread_id,
        title=title,
        description=description,
        operation="tool_call_batch",
        risk_level=7,
        options=approval_options,
        source=InteractionSource.TOOL_CALL,
        agent_id=agent_id,
        priority=Priority.HIGH,
        timeout=300.0,
        data={
            "tool_calls": pending_calls,
        },
        session_id=session_id,
    )

    service = get_human_interaction_service()

    try:
        request_id = await service.request_interaction(request)

        response = await service.wait_for_response(request_id)

        if response.is_approved:
            if response.modified_data:
                return {
                    "modified_tool_calls": response.modified_data.get("tool_calls", pending_calls),
                }
            return {}
        else:
            return {
                "should_stop": True,
                "error": response.reason or "工具调用被拒绝",
                "pending_tool_calls": [],
            }

    except Exception as e:
        logger.error(f"[human_approval_node] 审批处理失败: {e}")
        return {
            "should_stop": True,
            "error": f"审批处理失败: {str(e)}",
            "pending_tool_calls": [],
        }


async def request_human_approval(
    thread_id: str,
    title: str,
    description: str,
    operation: str,
    risk_level: int = 5,
    options: list[ApprovalOption] | None = None,
    agent_id: str | None = None,
    timeout: float = 300.0,
    data: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """
    请求人工审批的便捷函数

    Args:
        thread_id: 线程 ID
        title: 请求标题
        description: 请求描述
        operation: 操作名称
        risk_level: 风险等级 (1-10)
        options: 审批选项列表
        agent_id: Agent ID
        timeout: 超时时间
        data: 附加数据
        session_id: 会话 ID

    Returns:
        (是否批准, 修改后的数据或 None)
    """
    request = InteractionRequest.create_approval_request(
        thread_id=thread_id,
        title=title,
        description=description,
        operation=operation,
        risk_level=risk_level,
        options=options,
        source=InteractionSource.AGENT_REQUEST,
        agent_id=agent_id,
        timeout=timeout,
        data=data,
        session_id=session_id,
    )

    service = get_human_interaction_service()

    try:
        request_id = await service.request_interaction(request)
        response = await service.wait_for_response(request_id)

        return response.is_approved, response.modified_data

    except Exception as e:
        logger.error(f"[request_human_approval] 审批请求失败: {e}")
        return False, None


async def request_conversation(
    thread_id: str,
    title: str,
    topic: str,
    description: str = "",
    agent_id: str | None = None,
    suggestions: list[str] | None = None,
    timeout: float = 600.0,
    session_id: str | None = None,
) -> tuple[bool, str, list[dict[str, Any]]]:
    """
    请求与用户对话的便捷函数

    Args:
        thread_id: 线程 ID
        title: 请求标题
        topic: 对话主题
        description: 请求描述
        agent_id: Agent ID（用于前端跳转）
        suggestions: 建议选项
        timeout: 超时时间
        session_id: 会话 ID

    Returns:
        (是否完成, 对话结论, 对话历史)
    """
    request = InteractionRequest.create_conversation_request(
        thread_id=thread_id,
        title=title,
        topic=topic,
        description=description,
        source=InteractionSource.AGENT_REQUEST,
        agent_id=agent_id,
        timeout=timeout,
        suggestions=suggestions,
        session_id=session_id,
    )

    service = get_human_interaction_service()

    try:
        request_id = await service.request_interaction(request)
        response = await service.wait_for_response(request_id)

        return (
            response.response_type.value == "conversation_end",
            response.conversation_result or "",
            response.conversation_messages,
        )

    except Exception as e:
        logger.error(f"[request_conversation] 对话请求失败: {e}")
        return False, "", []
