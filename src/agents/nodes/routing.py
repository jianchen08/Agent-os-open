"""
路由函数

提供 LangGraph StateGraph 中的 should_continue 条件路由函数

使用策略模式重构，支持渐进式重复调用处理。
"""

import logging
from typing import Literal

from langchain_core.messages import SystemMessage

from src.agents.routing_engine import (
    get_default_routing_engine,
)
from src.agents.state import AgentState

logger = logging.getLogger(__name__)


def should_continue(state: AgentState) -> Literal["tools", "evaluate_reminder", "end"]:
    """
    条件路由函数

    决定是继续执行工具、触发评估提醒还是结束

    使用策略模式的路由引擎，支持扩展。

    Args:
        state: 当前状态

    Returns:
        下一个节点名称：
        - "tools": 执行工具
        - "evaluate_reminder": 触发评估提醒（Agent 输出文本但未调用 task_evaluate）
        - "end": 结束执行
    """
    # 获取默认路由引擎
    engine = get_default_routing_engine()

    # 执行路由决策
    result, decision = engine.evaluate(state)

    # 处理注入的警告消息
    if result == "tools" and state.get("_routing_warning"):
        warning = state["_routing_warning"]
        logger.info(f"[路由] 注入警告消息到上下文: {warning[:100]}...")

        # 将警告消息添加到 messages
        if "messages" not in state:
            state["messages"] = []
        state["messages"].append(SystemMessage(content=warning))

        # 清除警告标记
        del state["_routing_warning"]

    return result
