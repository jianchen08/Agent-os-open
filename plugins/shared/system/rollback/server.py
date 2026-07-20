#!/usr/bin/env python3
"""Rollback Service MCP 服务端——纯接口适配层。

老代码从 0.1 src/rollback/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

核心能力：
- rollback.create_checkpoint: 为任务创建回滚检查点
- rollback.record_operation: 记录操作日志（文件写入、git提交、API调用等）
- rollback.execute: 回滚任务操作到指定检查点或回退N步

[来源: docs/working/module_migration_plan.md §六 P2 迁移]
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from lingxi_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("rollback_service")

# 全局 RollbackManager 实例
_manager: Any = None


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize rollback manager on load."""
    global _manager
    from manager import RollbackManager

    _manager = RollbackManager()
    logger.info("Rollback service loaded")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup on unload."""
    global _manager
    _manager = None
    logger.info("Rollback service unloaded")


def _ensure_manager() -> Any:
    """获取 manager 实例，如果未初始化则延迟创建。"""
    global _manager
    if _manager is None:
        from manager import RollbackManager

        _manager = RollbackManager()
    return _manager


@plugin.tool(
    name="rollback.create_checkpoint",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task identifier"},
            "name": {"type": "string", "description": "Checkpoint name (optional)"},
            "description": {"type": "string", "description": "Checkpoint description (optional)"},
            "metadata": {"type": "object", "description": "Additional checkpoint metadata (optional)"},
        },
        "required": ["task_id"],
    },
    description="Create a rollback checkpoint for a task",
)
async def rollback_create_checkpoint(
    task_id: str,
    name: str | None = None,
    description: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a rollback checkpoint.

    Args:
        task_id: Task identifier.
        name: Optional checkpoint name.
        description: Optional checkpoint description.
        metadata: Optional metadata dict.

    Returns:
        Dict with checkpoint_id.
    """
    manager = _ensure_manager()
    checkpoint_id = await manager.create_checkpoint(
        task_id=task_id,
        name=name,
        description=description,
        metadata=metadata,
    )
    return {"checkpoint_id": checkpoint_id}


@plugin.tool(
    name="rollback.record_operation",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task identifier"},
            "tool_name": {"type": "string", "description": "Tool that performed the operation"},
            "operation_type": {
                "type": "string",
                "enum": ["create", "update", "delete", "execute"],
                "description": "Operation type",
            },
            "target": {"type": "string", "description": "Operation target (file path, API URL, etc.)"},
            "params": {"type": "object", "description": "Operation parameters"},
            "before_state": {"type": "object", "description": "State before operation (optional)"},
            "after_state": {"type": "object", "description": "State after operation (optional)"},
            "reversible": {"type": "boolean", "default": True},
            "reverse_action": {"type": "object", "description": "Reverse action definition (optional)"},
            "checkpoint_id": {"type": "string", "description": "Associated checkpoint ID (optional)"},
        },
        "required": ["task_id", "tool_name", "operation_type", "target", "params"],
    },
    description="Record an operation log entry for potential rollback",
)
async def rollback_record_operation(
    task_id: str,
    tool_name: str,
    operation_type: str,
    target: str,
    params: dict[str, Any],
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
    reversible: bool = True,
    reverse_action: dict[str, Any] | None = None,
    checkpoint_id: str | None = None,
) -> dict[str, Any]:
    """Record an operation log entry.

    Args:
        task_id: Task identifier.
        tool_name: Tool that performed the operation.
        operation_type: One of create/update/delete/execute.
        target: Operation target (file path, API URL, etc.).
        params: Operation parameters.
        before_state: State snapshot before operation.
        after_state: State snapshot after operation.
        reversible: Whether this operation can be reversed.
        reverse_action: Reverse action definition.
        checkpoint_id: Associated checkpoint ID.

    Returns:
        Dict with operation_id.
    """
    from models import OperationType

    manager = _ensure_manager()
    op_type = OperationType(operation_type)
    operation_id = await manager.record_operation(
        task_id=task_id,
        tool_name=tool_name,
        operation_type=op_type,
        target=target,
        params=params,
        before_state=before_state,
        after_state=after_state,
        reversible=reversible,
        reverse_action=reverse_action,
        checkpoint_id=checkpoint_id,
    )
    return {"operation_id": operation_id}


@plugin.tool(
    name="rollback.execute",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task identifier"},
            "to_checkpoint": {"type": "string", "description": "Rollback to this checkpoint ID (optional)"},
            "steps": {"type": "integer", "description": "Roll back the last N operations (optional)"},
        },
        "required": ["task_id"],
    },
    description="Roll back task operations to a checkpoint or N steps back",
)
async def rollback_execute(
    task_id: str,
    to_checkpoint: str | None = None,
    steps: int | None = None,
) -> dict[str, Any]:
    """Execute a rollback.

    Either to_checkpoint or steps must be provided. If both given, to_checkpoint
    takes precedence.

    Args:
        task_id: Task identifier.
        to_checkpoint: Rollback to this checkpoint (all operations after it get reversed).
        steps: Roll back the last N operations.

    Returns:
        RollbackResult dict with success, rolled_back_count, errors, etc.
    """
    manager = _ensure_manager()
    result = await manager.rollback(
        task_id=task_id,
        to_checkpoint=to_checkpoint,
        steps=steps,
    )
    return result.to_dict()


if __name__ == "__main__":
    plugin.run()
