"""WebSocket 消息类型工厂函数。

提供创建交互请求和取消消息的工厂方法，
供 WebSocketNotifier 等模块使用。
"""

from __future__ import annotations

from typing import Any


def create_interaction_request_message(
    *,
    thread_id: str,
    request_id: str,
    interaction_type: str,
    mode: str,
    title: str,
    description: str | None = None,
    priority: str = "normal",
    timeout: int | None = None,
    approval_options: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
    conversation_context: dict[str, Any] | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """创建交互请求消息。

    Args:
        thread_id: 会话线程 ID
        request_id: 交互请求唯一标识
        interaction_type: 交互类型（如 approval、input）
        mode: 交互模式
        title: 交互标题
        description: 交互描述
        priority: 优先级（normal / high / low）
        timeout: 超时时间（秒）
        approval_options: 审批选项列表
        context: 附加上下文
        conversation_context: 对话上下文
        agent_id: 发起交互的 Agent ID

    Returns:
        标准化的交互请求消息字典
    """
    message: dict[str, Any] = {
        "type": "interaction_request",
        "data": {
            "thread_id": thread_id,
            "request_id": request_id,
            "interaction_type": interaction_type,
            "mode": mode,
            "title": title,
            "priority": priority,
        },
    }

    # 仅在非空时添加可选字段
    if description is not None:
        message["data"]["description"] = description
    if timeout is not None:
        message["data"]["timeout"] = timeout
    if approval_options is not None:
        message["data"]["approval_options"] = approval_options
    if context is not None:
        message["data"]["context"] = context
    if conversation_context is not None:
        message["data"]["conversation_context"] = conversation_context
    if agent_id is not None:
        message["data"]["agent_id"] = agent_id

    return message


def create_interaction_cancelled_message(
    *,
    thread_id: str,
    request_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """创建交互取消消息。

    Args:
        thread_id: 会话线程 ID
        request_id: 被取消的交互请求 ID
        reason: 取消原因

    Returns:
        标准化的交互取消消息字典
    """
    message: dict[str, Any] = {
        "type": "interaction_cancelled",
        "data": {
            "thread_id": thread_id,
            "request_id": request_id,
        },
    }

    if reason is not None:
        message["data"]["reason"] = reason

    return message
