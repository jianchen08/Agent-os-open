"""
执行工具节点

提供 LangGraph StateGraph 中的 execute_tools_node 函数
"""

import json
import logging
import time
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.types import RunnableConfig

from src.agents.state import AgentState

logger = logging.getLogger(__name__)


async def execute_tools_node(
    state: AgentState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """
    执行工具节点

    Args:
        state: 当前状态
        config: 运行时配置（包含 configurable 中的运行时对象）

    Returns:
        状态更新字典
    """
    from langchain_core.messages import SystemMessage

    from src.agents.formatters import ToolMessageFormatter
    from src.agents.nodes.helpers import (
        _add_message_to_context_store,
        _build_result,
        _create_execution_record,
        _update_execution_record,
    )
    from src.agents.utils import DuplicateCallDetector
    from src.tools.executor import ExecutionContext as ToolExecutionContext
    from src.tools.reasoning import ReasoningRequiredError

    pending_calls = state.get("pending_tool_calls", [])

    # 优先从 configurable 获取运行时对象（避免序列化问题）
    if config and "configurable" in config:
        tool_executor = config["configurable"].get("tool_executor")
        layered_context_store = config["configurable"].get("layered_context_store")
    else:
        # 回退到从 state 获取（向后兼容）
        tool_executor = state.get("tool_executor")
        layered_context_store = state.get("layered_context_store")

    tool_calls_history = state.get("tool_calls", [])
    context = state.get("context", {})

    if not pending_calls:
        logger.debug("[工具节点] 无待执行的工具调用")
        return {"pending_tool_calls": []}

    # 记录检测到的重复调用（但不阻止执行，由 should_continue 处理渐进式警告）
    # 这样 Agent 有机会意识到问题并主动改正
    duplicate_info = DuplicateCallDetector.check_duplicate(
        tool_calls_history, min_consecutive_calls=2
    )
    if duplicate_info:
        logger.warning(
            f"[工具节点] 检测到重复调用 | "
            f"tool_name={duplicate_info.get('tool_name')} | "
            f"将在路由阶段进行渐进式处理"
        )
        # 记录到 state，让 should_continue 处理
        state["_detected_duplicate_call"] = duplicate_info

    logger.info(f"[工具节点] 开始执行工具 | count={len(pending_calls)}")

    tool_messages: list[ToolMessage] = []
    new_tool_calls: list[dict[str, Any]] = []

    # 从 context 中获取 task_id（如果存在）
    task_id = context.get("task_id") if isinstance(context, dict) else None

    # 将 dict 转换为 ExecutionContext 对象
    if isinstance(context, dict):
        exec_context = ToolExecutionContext(
            session_id=context.get("session_id", ""),
            user_id=context.get("user_id"),
            metadata=context.get("metadata", {}),
        )
    else:
        exec_context = context

    for tc in pending_calls:
        tool_name = tc.get("name", "")
        llm_args = tc.get("args", {})
        tool_id = tc.get("id", "")

        # 复制 LLM 参数
        tool_args = llm_args.copy()

        # 获取工具定义，检查注入参数
        injected_params = _get_injected_params(tool_executor, tool_name)
        if injected_params:
            # 注入系统参数（从 context 中获取）
            for param in injected_params:
                if param not in tool_args:  # 不覆盖 LLM 已提供的值
                    injected_value = _get_injected_value(param, context, record_id=None)
                    if injected_value is not None:
                        tool_args[param] = injected_value
                        logger.debug(
                            f"[工具节点] 注入参数 | tool={tool_name} | param={param} | value={injected_value}"
                        )

        logger.info(
            f"[工具节点] 执行工具 | name={tool_name} | id={tool_id} | task_id={task_id}"
        )
        logger.debug(
            f"[工具节点] 工具参数 | name={tool_name} | args={json.dumps(tool_args, ensure_ascii=False)}"
        )

        start_time = time.time()
        record_id = None
        session_id = context.get("session_id", "")

        try:
            # 创建执行记录并推送 WebSocket 事件
            if session_id:
                # 从 layered_context_store 获取 executor 信息
                executor_type = getattr(layered_context_store, 'executor_type', None)
                executor_id = getattr(layered_context_store, 'executor_id', None)
                executor_name = getattr(layered_context_store, 'executor_name', None)

                record_id = await _create_execution_record(
                    session_id=session_id,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    tool_id=tool_id,
                    task_id=task_id,
                    executor_type=executor_type,
                    executor_id=executor_id,
                    executor_name=executor_name,
                )

            # BUG-FIX: 在 record_id 创建后，再次注入 tool_record_id 参数
            # 问题根因: 之前的代码在 record_id 创建前就注入了参数，导致 tool_record_id 为 None
            # 修复方案: 在 record_id 创建后，检查并注入 tool_record_id 到 tool_args
            if record_id and "tool_record_id" not in tool_args:
                tool_args["tool_record_id"] = record_id
                logger.debug(
                    f"[工具节点] 延迟注入 tool_record_id | tool={tool_name} | record_id={record_id}"
                )

            if tool_executor:
                # 使用工具执行器（带推理检查）
                try:
                    # 准备执行上下文（包含消息历史和工具记录ID）
                    exec_context_with_metadata = ToolExecutionContext(
                        session_id=exec_context.session_id,
                        user_id=exec_context.user_id,
                        metadata={
                            **exec_context.metadata,
                            "messages": state.get("messages", []),
                            "tool_call_id": tool_id,
                            "tool_record_id": record_id,
                        },
                    )

                    result = await tool_executor.execute(
                        tool_name=tool_name,
                        inputs=tool_args,
                        context=exec_context_with_metadata,
                    )
                    success = result.success
                    output = result.data if success else result.error
                    error = None if success else result.error

                except ReasoningRequiredError as reasoning_error:
                    # 需要推理：注入推理提示，让 LLM 重新思考
                    logger.warning(
                        f"[工具节点] 工具需要推理 | "
                        f"tool={tool_name} | "
                        f"retry={reasoning_error.retry_count}"
                    )

                    # 创建推理提示消息
                    reasoning_prompt = SystemMessage(
                        content=reasoning_error.reasoning_prompt
                    )

                    # 返回推理提示，不执行工具
                    return {
                        "messages": [reasoning_prompt],
                        "pending_tool_calls": pending_calls,
                    }
            else:
                # 无执行器，返回错误
                success = False
                output = None
                error = "工具执行器未配置"

            duration_ms = int((time.time() - start_time) * 1000)

            # 更新执行记录并推送 WebSocket 事件
            if record_id and session_id:
                await _update_execution_record(
                    record_id=record_id,
                    session_id=session_id,
                    tool_name=tool_name,
                    success=success,
                    output=output,
                    error=error,
                    duration_ms=duration_ms,
                )

            call_record = {
                "tool_name": tool_name,
                "inputs": tool_args,
                "llm_args": llm_args,
                "output": output,
                "success": success,
                "error": error,
                "duration_ms": duration_ms,
                "execution_record_id": record_id,
            }
            new_tool_calls.append(call_record)

            # 使用 ToolMessageFormatter 格式化消息
            if success:
                output_preview = ToolMessageFormatter.truncate_output(output)
                logger.info(
                    f"[工具节点] 工具执行成功 | name={tool_name} | duration_ms={duration_ms}"
                )
                logger.debug(
                    f"[工具节点] 工具输出 | name={tool_name} | output={output_preview}"
                )
                llm_content, _ = ToolMessageFormatter.format_success_message(
                    tool_name=tool_name,
                    output=output,
                    record_id=record_id,
                )
            else:
                logger.warning(
                    f"[工具节点] 工具执行失败 | name={tool_name} | duration_ms={duration_ms} | error={error}"
                )
                llm_content, _ = ToolMessageFormatter.format_error_message(
                    tool_name=tool_name,
                    error=error,
                )

            # 创建给 LLM 的 ToolMessage
            tool_messages.append(
                ToolMessage(
                    content=llm_content,
                    tool_call_id=tool_id,
                    name=tool_name,
                )
            )

            # 将工具消息添加到分层上下文存储
            if layered_context_store:
                await _add_message_to_context_store(
                    layered_context_store=layered_context_store,
                    content=llm_content,
                    tool_name=tool_name,
                    tool_id=tool_id,
                )

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)

            logger.exception(
                f"[工具节点] 工具执行异常 | name={tool_name} | duration_ms={duration_ms} | error={str(e)}"
            )

            # 更新执行记录（修复：异常时也需要更新数据库记录）
            if record_id and session_id:
                await _update_execution_record(
                    record_id=record_id,
                    session_id=session_id,
                    tool_name=tool_name,
                    success=False,
                    output=None,
                    error=str(e),
                    duration_ms=duration_ms,
                )

            call_record = {
                "tool_name": tool_name,
                "inputs": tool_args,
                "llm_args": llm_args,
                "success": False,
                "error": str(e),
                "duration_ms": duration_ms,
            }
            new_tool_calls.append(call_record)

            # 使用 ToolMessageFormatter 格式化异常消息
            llm_content, _ = ToolMessageFormatter.format_exception_message(
                tool_name=tool_name,
                exception=e,
            )

            tool_messages.append(
                ToolMessage(
                    content=llm_content,
                    tool_call_id=tool_id,
                    name=tool_name,
                )
            )

            # 将异常工具消息添加到分层上下文存储
            if layered_context_store:
                await _add_message_to_context_store(
                    layered_context_store=layered_context_store,
                    content=llm_content,
                    tool_name=tool_name,
                    tool_id=tool_id,
                )

    return await _build_result(
        state=state,
        tool_messages=tool_messages,
        new_tool_calls=new_tool_calls,
        layered_context_store=layered_context_store,
    )


def _get_injected_params(tool_executor: Any, tool_name: str) -> list[str]:
    """
    获取工具的注入参数列表

    Args:
        tool_executor: 工具执行器
        tool_name: 工具名称

    Returns:
        注入参数列表
    """
    if not tool_executor:
        return []

    try:
        # 从工具注册表获取工具定义
        registry = getattr(tool_executor, "_registry", None)
        if not registry:
            return []

        tool = registry.get_tool(tool_name)
        if not tool:
            return []

        # 返回注入参数列表
        return getattr(tool, "injected_params", [])
    except Exception as e:
        logger.debug(f"[工具节点] 获取注入参数失败 | tool={tool_name} | error={e}")
        return []


def _get_injected_value(
    param: str,
    context: dict[str, Any],
    record_id: str | None = None,
) -> Any:
    """
    获取注入参数的值

    Args:
        param: 参数名称
        context: 执行上下文
        record_id: 执行记录 ID（用于 tool_record_id）

    Returns:
        参数值，如果无法获取则返回 None
    """
    # 参数名映射
    param_mapping = {
        "session_id": lambda ctx: ctx.get("session_id"),
        "user_id": lambda ctx: ctx.get("user_id"),
        "thread_id": lambda ctx: ctx.get("session_id"),  # thread_id 映射到 session_id
        "tool_record_id": lambda ctx: record_id or ctx.get("tool_record_id"),
        "execution_id": lambda ctx: ctx.get("execution_id"),
        "agent_id": lambda ctx: ctx.get("agent_id"),
        "task_id": lambda ctx: ctx.get("task_id"),
    }

    if param in param_mapping:
        try:
            return param_mapping[param](context)
        except Exception as e:
            logger.debug(f"[工具节点] 获取注入参数值失败 | param={param} | error={e}")
            return None

    # 未知参数，尝试从 context 获取
    return context.get(param)
