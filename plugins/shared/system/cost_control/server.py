#!/usr/bin/env python3
"""成本控制 MCP 服务端——纯接口适配层。

老代码从 0.1 src/cost_control/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

[来源: docs/working/module_migration_plan.md §5.1]
"""
from __future__ import annotations

import logging
import sys
import os
from dataclasses import asdict
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))  # 让同目录老代码的导入可用

from agentos_plugin_sdk import AgentOSPlugin

from budget_manager import (
    BudgetAlert,
    BudgetAlertAction,
    BudgetAlertLevel,
    BudgetManager,
    BudgetStatus,
    get_budget_manager,
    reset_budget_manager,
)
from config import CostControlConfig, get_cost_control_config
from exceptions import BudgetExceededException, QuotaExhaustedException

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("cost_control_service")

_budget_manager: BudgetManager | None = None


def _serialize_alert(alert: BudgetAlert | None) -> dict[str, Any] | None:
    """将 BudgetAlert 序列化为可 JSON 化的 dict。"""
    if alert is None:
        return None
    data = asdict(alert)
    data["level"] = alert.level.value
    data["timestamp"] = alert.timestamp.isoformat()
    if alert.action_taken is not None:
        data["action_taken"] = alert.action_taken.value
    return data


def _serialize_status(status: BudgetStatus) -> dict[str, Any]:
    """将 BudgetStatus 序列化为可 JSON 化的 dict。"""
    data = asdict(status)
    data["alert_level"] = status.alert_level.value
    return data


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """插件加载时初始化预算管理器。"""
    global _budget_manager
    config = plugin.get_config()
    cost_config = get_cost_control_config()
    _budget_manager = BudgetManager(config=cost_config)
    logger.info("cost_control service initialized")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """插件卸载时清理。"""
    global _budget_manager
    _budget_manager = None
    reset_budget_manager()


@plugin.tool(
    name="cost_control.check_budget",
    schema={
        "type": "object",
        "properties": {
            "estimated_tokens": {"type": "integer", "minimum": 0, "description": "预估 Token 数"},
            "user_id": {"type": "string", "description": "用户 ID（可选）"},
            "task_id": {"type": "string", "description": "任务 ID（可选）"},
            "session_id": {"type": "string", "description": "会话 ID（可选）"},
        },
        "required": ["estimated_tokens"],
    },
    description="Check if budget allows the estimated token usage",
)
async def cost_control_check_budget(
    estimated_tokens: int,
    user_id: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """检查预算是否允许执行。

    Raises:
        BudgetExceededException: 预算超限（任务/会话级别）
        QuotaExhaustedException: 配额耗尽（全局级别）
    """
    try:
        result = await _budget_manager.check_budget(
            estimated_tokens=estimated_tokens,
            user_id=user_id,
            task_id=task_id,
            session_id=session_id,
        )
        return {"allowed": result}
    except BudgetExceededException as e:
        return {"allowed": False, "error": e.to_dict()}
    except QuotaExhaustedException as e:
        return {"allowed": False, "error": e.to_dict()}


@plugin.tool(
    name="cost_control.record_usage",
    schema={
        "type": "object",
        "properties": {
            "tokens": {"type": "integer", "minimum": 0, "description": "使用的 Token 数"},
            "model": {"type": "string", "description": "模型名称"},
            "user_id": {"type": "string", "description": "用户 ID（可选）"},
            "task_id": {"type": "string", "description": "任务 ID（可选）"},
            "session_id": {"type": "string", "description": "会话 ID（可选）"},
        },
        "required": ["tokens", "model"],
    },
    description="Record token usage and check for budget alerts",
)
async def cost_control_record_usage(
    tokens: int,
    model: str,
    user_id: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """记录 Token 使用量，返回告警信息（如果有）。"""
    alert = await _budget_manager.record_usage(
        tokens=tokens,
        model=model,
        user_id=user_id,
        task_id=task_id,
        session_id=session_id,
    )
    return {"recorded": True, "alert": _serialize_alert(alert)}


@plugin.tool(
    name="cost_control.get_status",
    schema={
        "type": "object",
        "properties": {
            "user_id": {"type": "string", "description": "用户 ID（可选）"},
            "task_id": {"type": "string", "description": "任务 ID（可选）"},
            "session_id": {"type": "string", "description": "会话 ID（可选）"},
        },
    },
    description="Get current budget status for a scope",
)
async def cost_control_get_status(
    user_id: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """获取预算状态。"""
    status = _budget_manager.get_budget_status(
        user_id=user_id,
        task_id=task_id,
        session_id=session_id,
    )
    return _serialize_status(status)


@plugin.tool(
    name="cost_control.get_statistics",
    schema={
        "type": "object",
        "properties": {},
    },
    description="Get global usage statistics",
)
async def cost_control_get_statistics() -> dict[str, Any]:
    """获取全局使用统计。"""
    return _budget_manager.get_usage_statistics()


@plugin.tool(
    name="cost_control.reset_task_budget",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务 ID"},
        },
        "required": ["task_id"],
    },
    description="Reset budget tracking for a specific task",
)
async def cost_control_reset_task_budget(task_id: str) -> dict[str, Any]:
    """重置任务预算。"""
    await _budget_manager.reset_task_budget(task_id)
    return {"reset": True, "task_id": task_id}


@plugin.tool(
    name="cost_control.reset_session_budget",
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "会话 ID"},
        },
        "required": ["session_id"],
    },
    description="Reset budget tracking for a specific session",
)
async def cost_control_reset_session_budget(session_id: str) -> dict[str, Any]:
    """重置会话预算。"""
    await _budget_manager.reset_session_budget(session_id)
    return {"reset": True, "session_id": session_id}


if __name__ == "__main__":
    plugin.run()
