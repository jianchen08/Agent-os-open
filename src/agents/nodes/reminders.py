"""
评估提醒节点

提供 LangGraph StateGraph 中的 evaluate_reminder_node 函数
"""

import logging
from typing import Any

from langchain_core.messages import SystemMessage

from src.agents.state import AgentState

logger = logging.getLogger(__name__)


async def evaluate_reminder_node(state: AgentState) -> dict[str, Any]:
    """
    评估提醒节点

    当 Agent 输出文本响应但未调用 task_evaluate 时，
    注入一条系统消息提醒 Agent 检查任务是否完成并提交评估。

    Args:
        state: 当前状态

    Returns:
        状态更新字典，包含提醒消息
    """
    # 获取 task_id
    context = state.get("context", {})
    task_id = None
    if isinstance(context, dict):
        task_id = context.get("task_id") or context.get("metadata", {}).get("task_id")
    elif hasattr(context, "metadata"):
        task_id = getattr(context, "metadata", {}).get("task_id")

    if not task_id:
        logger.warning("[评估提醒] 无法获取 task_id，跳过提醒")
        return {}

    # 获取当前提醒次数
    evaluate_reminder_count = state.get("evaluate_reminder_count", 0)

    # 构建提醒消息
    reminder_message = SystemMessage(
        content=(
            f"【系统提醒】请检查任务验收标准(AC)是否已满足：\n"
            f'- 如果已完成所有AC：调用 task_evaluate(action="auto_complete", task_id="{task_id}") 提交评估\n'
            f"- 如果尚未完成：继续执行任务，完成后再提交评估"
        )
    )

    logger.info(
        f"[评估提醒] 注入提醒消息 | task_id={task_id} | reminder_count={evaluate_reminder_count + 1}"
    )

    return {
        "messages": [reminder_message],
        "evaluate_reminder_count": evaluate_reminder_count + 1,
        "final_output": None,  # 清除 final_output，让 Agent 继续处理
    }
