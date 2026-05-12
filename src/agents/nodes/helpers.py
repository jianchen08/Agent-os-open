"""
Agent 节点辅助函数

提供节点函数使用的通用辅助功能
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def _create_execution_record(
    session_id: str,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_id: str,
    task_id: str | None,
    executor_type: str | None = None,
    executor_id: str | None = None,
    executor_name: str | None = None,
) -> str | None:
    """
    创建工具执行记录

    注意：此函数只负责创建数据库记录，不发送 WebSocket 事件。
    WebSocket 事件由 stream_processor.py 中的统一消息中心处理。

    Args:
        session_id: 会话ID
        tool_name: 工具名称
        tool_args: 工具参数
        tool_id: 工具调用ID
        task_id: 任务ID（可选）
        executor_type: 执行者类型（agent/tool/workflow）
        executor_id: 执行者ID
        executor_name: 执行者名称

    Returns:
        记录ID 或 None
    """
    from src.agents.execution_record_helper import create_tool_execution_record
    from src.db.connection import get_session_context

    try:
        async with get_session_context() as db:
            record_id = await create_tool_execution_record(
                db=db,
                session_id=session_id,
                tool_name=tool_name,
                tool_args=tool_args,
                parent_record_id=task_id,
                task_id=task_id,
                tool_call_id=tool_id,
                executor_type=executor_type,
                executor_id=executor_id,
                executor_name=executor_name,
            )

        logger.debug(
            f"[工具节点] 创建执行记录成功 | record_id={record_id} | tool_name={tool_name}"
        )
        return record_id
    except Exception as db_error:
        logger.warning(
            f"[工具节点] 创建执行记录失败 | tool_name={tool_name} | error={db_error}"
        )
        return None


async def _update_execution_record(
    record_id: str,
    session_id: str,
    tool_name: str,
    success: bool,
    output: Any,
    error: str | None,
    duration_ms: int,
) -> None:
    """
    更新工具执行记录

    注意：此函数只负责更新数据库记录，不发送 WebSocket 事件。
    WebSocket 事件由 stream_processor.py 中的统一消息中心处理。

    Args:
        record_id: 记录ID
        session_id: 会话ID
        tool_name: 工具名称
        success: 是否成功
        output: 输出结果
        error: 错误信息
        duration_ms: 执行时长（毫秒）
    """
    from src.agents.execution_record_helper import update_tool_execution_record
    from src.db.connection import get_session_context

    try:
        async with get_session_context() as db:
            await update_tool_execution_record(
                db=db,
                record_id=record_id,
                success=success,
                output=output,
                error=error,
                duration_ms=duration_ms,
            )

        logger.debug(
            f"[工具节点] 更新执行记录成功 | record_id={record_id} | success={success}"
        )
    except Exception as db_error:
        logger.warning(
            f"[工具节点] 更新执行记录失败 | record_id={record_id} | error={db_error}"
        )


async def _add_message_to_context_store(
    layered_context_store: Any,
    content: str,
    tool_name: str,
    tool_id: str,
) -> None:
    """
    将工具消息添加到分层上下文存储

    Args:
        layered_context_store: 分层上下文存储实例
        content: 消息内容
        tool_name: 工具名称
        tool_id: 工具调用ID
    """
    try:
        tool_msg_dict = {
            "role": "tool",
            "content": content,
            "name": tool_name,
            "tool_call_id": tool_id,
        }
        await layered_context_store.add_message(tool_msg_dict)
    except Exception as e:
        logger.warning(
            f"[工具节点] 添加工具消息到 LayeredContextStore 失败: {e}"
        )


async def _build_result(
    state: dict[str, Any],
    tool_messages: list[Any],
    new_tool_calls: list[dict[str, Any]],
    layered_context_store: Any | None = None,
) -> dict[str, Any]:
    """
    构建执行结果

    同时返回 messages 字段（供 LangGraph 流式输出）和同步到 layered_context_store（供后续 LLM 调用）

    Args:
        state: 当前状态
        tool_messages: 工具消息列表
        new_tool_calls: 新的工具调用记录
        layered_context_store: 分层上下文存储（可选）

    Returns:
        状态更新字典（包含 messages 字段）
    """
    tool_calls_history = state.get("tool_calls", [])

    logger.info(
        f"[工具节点] 工具执行完成 | total={len(new_tool_calls)} | "
        f"success={sum(1 for c in new_tool_calls if c['success'])}"
    )
    logger.info(f"[工具节点] 返回消息数量: {len(tool_messages)}")

    # 添加详细日志,追踪返回给 LLM 的消息
    for i, msg in enumerate(tool_messages):
        content_preview = str(msg.content)[:200] if msg.content else ""
        logger.info(
            f"[工具节点] 返回消息 {i + 1} | "
            f"type={type(msg).__name__} | "
            f"tool_call_id={msg.tool_call_id} | "
            f"name={msg.name} | "
            f"content_preview={content_preview}..."
        )

    # BUG-FIX-fix_20260226_tool_callback: 移除重复添加工具消息到 LayeredContextStore
    # 问题根因: 工具消息在 execute_tools_node 中已经通过 _add_message_to_context_store 添加过了
    # 修复方案: 移除 _build_result 中的重复添加逻辑，避免消息重复
    # 注意: 工具消息已经在 execute_tools_node 的主循环中添加到 layered_context_store._messages 了

    return {
        "messages": tool_messages,
        "pending_tool_calls": [],
        "tool_calls": tool_calls_history + new_tool_calls,
    }
