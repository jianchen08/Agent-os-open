#!/usr/bin/env python3
"""Human Interaction MCP 服务端——纯接口适配层。

老代码从 0.1 src/human_interaction/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

from models import Priority  # noqa: E402
from service import HumanInteractionService  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("human_interaction_service")

_service: HumanInteractionService | None = None


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize human interaction service on load."""
    global _service
    _service = HumanInteractionService()
    logger.info("Human interaction service initialized")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup on unload."""
    global _service
    _service = None
    logger.info("Human interaction service unloaded")


@plugin.tool(
    name="interaction.send_notification",
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "会话ID"},
            "thread_id": {"type": "string", "description": "线程ID"},
            "title": {"type": "string", "description": "通知标题"},
            "message": {"type": "string", "description": "通知内容", "default": ""},
            "priority": {"type": "string", "enum": ["low", "normal", "high", "critical"], "default": "normal"},
        },
        "required": ["session_id", "thread_id", "title"],
    },
    description="发送非阻塞通知，不等待用户响应",
)
async def interaction_send_notification(
    session_id: str,
    thread_id: str,
    title: str,
    message: str = "",
    priority: str = "normal",
) -> dict[str, Any]:
    """Send a non-blocking notification."""
    if _service is None:
        return {"error": "Service not initialized"}
    request_id = await _service.send_notification(
        session_id=session_id,
        thread_id=thread_id,
        title=title,
        message=message,
        priority=Priority(priority),
    )
    return {"request_id": request_id, "status": "sent"}


@plugin.tool(
    name="interaction.create_choice",
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "thread_id": {"type": "string"},
            "tab_id": {"type": "string", "description": "标签页ID"},
            "title": {"type": "string", "description": "请求标题"},
            "description": {"type": "string", "default": ""},
            "options": {
                "type": "array",
                "description": "选项列表",
                "items": {"type": "object", "properties": {"id": {"type": "string"}, "label": {"type": "string"}}},
            },
            "questions": {"type": "array", "items": {"type": "string"}, "description": "澄清问题列表"},
            "timeout_seconds": {"type": "integer", "default": 86400},
            "priority": {"type": "string", "enum": ["low", "normal", "high", "critical"], "default": "normal"},
        },
        "required": ["session_id", "thread_id", "tab_id", "title"],
    },
    description="创建选择模式交互请求",
)
async def interaction_create_choice(
    session_id: str,
    thread_id: str,
    tab_id: str,
    title: str,
    description: str = "",
    options: list[dict[str, Any]] | None = None,
    questions: list[str] | None = None,
    timeout_seconds: int = 86400,
    priority: str = "normal",
) -> dict[str, Any]:
    """Create a choice-mode interaction request."""
    if _service is None:
        return {"error": "Service not initialized"}
    request_id = await _service.create_choice_request(
        session_id=session_id,
        thread_id=thread_id,
        tab_id=tab_id,
        title=title,
        description=description,
        options=options,
        questions=questions,
        timeout_seconds=timeout_seconds,
        priority=Priority(priority),
    )
    return {"request_id": request_id, "status": "pending"}


@plugin.tool(
    name="interaction.create_conversation",
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "thread_id": {"type": "string"},
            "tab_id": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string", "default": ""},
            "initial_message": {"type": "string", "description": "对话开场消息"},
            "suggestions": {"type": "array", "items": {"type": "string"}, "description": "快捷回复建议"},
        },
        "required": ["session_id", "thread_id", "tab_id", "title"],
    },
    description="创建对话模式交互请求",
)
async def interaction_create_conversation(
    session_id: str,
    thread_id: str,
    tab_id: str,
    title: str,
    description: str = "",
    initial_message: str | None = None,
    suggestions: list[str] | None = None,
) -> dict[str, Any]:
    """Create a conversation-mode interaction request."""
    if _service is None:
        return {"error": "Service not initialized"}
    request_id = await _service.create_conversation_request(
        session_id=session_id,
        thread_id=thread_id,
        tab_id=tab_id,
        title=title,
        description=description,
        initial_message=initial_message,
        suggestions=suggestions,
    )
    return {"request_id": request_id, "status": "pending"}


@plugin.tool(
    name="interaction.wait_for_choice",
    schema={
        "type": "object",
        "properties": {
            "request_id": {"type": "string", "description": "请求ID"},
            "timeout": {"type": "number", "description": "超时秒数", "default": 86400},
        },
        "required": ["request_id"],
    },
    description="等待用户选择响应",
)
async def interaction_wait_for_choice(request_id: str, timeout: float = 86400) -> dict[str, Any]:
    """Wait for user's choice response."""
    if _service is None:
        return {"error": "Service not initialized"}
    try:
        result = await _service.wait_for_choice(request_id, timeout)
        return result
    except Exception as e:
        return {"error": str(e), "request_id": request_id}


@plugin.tool(
    name="interaction.respond",
    schema={
        "type": "object",
        "properties": {
            "request_id": {"type": "string"},
            "response_type": {"type": "string", "description": "approved/denied/answered/timeout/cancelled"},
            "selected_option": {"type": "string", "description": "选中的选项ID"},
            "answers": {"type": "array", "items": {"type": "string"}, "description": "问题答案列表"},
            "feedback": {"type": "string", "description": "反馈文本"},
        },
        "required": ["request_id", "response_type"],
    },
    description="提交交互响应",
)
async def interaction_respond(
    request_id: str,
    response_type: str,
    selected_option: str | None = None,
    answers: list[str] | None = None,
    feedback: str | None = None,
) -> dict[str, Any]:
    """Submit an interaction response."""
    if _service is None:
        return {"error": "Service not initialized"}
    success = await _service.submit_response(
        request_id=request_id,
        response_type=response_type,
        selected_option=selected_option,
        answers=answers,
        feedback=feedback,
    )
    return {"success": success, "request_id": request_id}


@plugin.tool(
    name="interaction.cancel",
    schema={
        "type": "object",
        "properties": {
            "request_id": {"type": "string"},
            "reason": {"type": "string", "description": "取消原因"},
        },
        "required": ["request_id"],
    },
    description="取消交互请求",
)
async def interaction_cancel(request_id: str, reason: str | None = None) -> dict[str, Any]:
    """Cancel an interaction request."""
    if _service is None:
        return {"error": "Service not initialized"}
    success = await _service.cancel_request(request_id, reason)
    return {"success": success, "request_id": request_id}


@plugin.tool(
    name="interaction.get_pending",
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "会话ID（可选筛选）"},
            "limit": {"type": "integer", "default": 50},
        },
    },
    description="获取待处理请求列表",
)
async def interaction_get_pending(session_id: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Get pending interaction requests."""
    if _service is None:
        return {"error": "Service not initialized"}
    requests = await _service.get_pending_requests(session_id=session_id, limit=limit)
    return {"requests": requests, "count": len(requests)}


if __name__ == "__main__":
    plugin.run()
