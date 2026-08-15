#!/usr/bin/env python3
"""容器隔离系统 MCP 服务端——纯接口适配层。

老代码从 0.1 src/isolation/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

[来源: docs/working/module_migration_plan.md §4.2]
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from checkpoint import CheckpointManager
from isolation_types import (
    IsolationLevel,
    OperationType,
    TaskType,
)

# 直接导入同目录的老代码（文件就在旁边，不需要额外路径前缀）
from manager import IsolationManager
from permission_checker import PermissionChecker
from permission_policy import PermissionPolicyManager

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("isolation_service")

# 全局服务实例
_manager: IsolationManager | None = None
_checkpoint_mgr: CheckpointManager | None = None
_permission_checker: PermissionChecker | None = None


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """初始化隔离管理器。"""
    global _manager, _checkpoint_mgr, _permission_checker

    config = plugin.get_config()
    logger.info("[isolation] 加载插件，配置项数: %d", len(config))

    # 初始化隔离管理器（老代码内部 try/except 会优雅处理 ConfigCenter 缺失）
    _manager = IsolationManager()
    await _manager.start()

    # 初始化检查点管理器
    _checkpoint_mgr = CheckpointManager(project_root=os.getcwd())

    # 初始化权限检查器
    _permission_checker = PermissionChecker()

    logger.info("[isolation] 隔离服务已启动")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """停止隔离管理器。"""
    global _manager, _checkpoint_mgr, _permission_checker

    if _manager:
        await _manager.stop()
        _manager = None
    _checkpoint_mgr = None
    _permission_checker = None

    logger.info("[isolation] 隔离服务已停止")


@plugin.tool(
    name="isolation.create_env",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务 ID"},
            "task_type": {
                "type": "string",
                "enum": ["project", "module", "atomic"],
                "default": "atomic",
                "description": "任务类型",
            },
            "operation_type": {
                "type": "string",
                "description": "操作类型（如 code_execution, file_operation）",
            },
            "workspace": {"type": "string", "description": "工作目录路径"},
            "parent_workspace": {"type": "string", "description": "父任务工作目录"},
            "parent_env_id": {"type": "string", "description": "父环境 ID"},
            "isolation_level": {
                "type": "string",
                "enum": ["isolated", "non_isolated"],
                "description": "隔离级别（默认 isolated）",
            },
        },
        "required": ["task_id"],
    },
    description="创建或获取隔离环境",
)
async def isolation_create_env(
    task_id: str,
    task_type: str = "atomic",
    operation_type: str | None = None,
    workspace: str | None = None,
    parent_workspace: str | None = None,
    parent_env_id: str | None = None,
    is_root_task: bool = True,
    isolation_level: str | None = None,
    metadata: dict[str, Any] | None = None,
    parent_task_id: str | None = None,
    tool_name: str | None = None,
) -> dict[str, Any]:
    """创建或复用隔离环境（同 workspace 共享容器）。"""
    if _manager is None:
        return {"error": "隔离服务未初始化"}

    level = None
    if isolation_level:
        level = IsolationLevel(isolation_level)

    env = await _manager.get_or_create_environment(
        task_id=task_id,
        task_type=TaskType(task_type),
        operation_type=OperationType(operation_type) if operation_type else None,
        parent_env_id=parent_env_id,
        workspace=workspace,
        parent_workspace=parent_workspace,
        is_root_task=is_root_task,
        isolation_level=level,
        metadata=metadata,
        parent_task_id=parent_task_id,
        tool_name=tool_name,
    )
    return {
        "env_id": env.env_id,
        "level": env.level.value,
        "provider_type": env.provider_type,
        "status": env.status,
        "context_task_id": env.context.task_id,
    }


@plugin.tool(
    name="isolation.execute",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务 ID"},
            "task_type": {
                "type": "string",
                "enum": ["project", "module", "atomic"],
                "default": "atomic",
            },
            "operation": {
                "type": "object",
                "description": "操作描述（type/command/files 等）",
            },
            "operation_type": {"type": "string"},
            "workspace": {"type": "string"},
            "parent_workspace": {"type": "string"},
        },
        "required": ["task_id", "operation"],
    },
    description="在隔离环境中执行操作",
)
async def isolation_execute(
    task_id: str,
    operation: dict[str, Any],
    task_type: str = "atomic",
    operation_type: str | None = None,
    parent_env_id: str | None = None,
    workspace: str | None = None,
    parent_workspace: str | None = None,
    is_root_task: bool = True,
    isolation_level: str | None = None,
    parent_task_id: str | None = None,
    tool_name: str | None = None,
) -> dict[str, Any]:
    """在隔离环境中执行操作（自动复用/创建环境）。"""
    if _manager is None:
        return {"error": "隔离服务未初始化"}

    level = None
    if isolation_level:
        level = IsolationLevel(isolation_level)

    result = await _manager.execute_in_isolation(
        task_id=task_id,
        task_type=TaskType(task_type),
        operation=operation,
        operation_type=OperationType(operation_type) if operation_type else None,
        parent_env_id=parent_env_id,
        workspace=workspace,
        parent_workspace=parent_workspace,
        is_root_task=is_root_task,
        isolation_level=level,
        parent_task_id=parent_task_id,
        tool_name=tool_name,
    )
    return result.to_dict()


@plugin.tool(
    name="isolation.destroy_env",
    schema={
        "type": "object",
        "properties": {
            "env_id": {"type": "string", "description": "环境 ID"},
            "task_id": {"type": "string", "description": "任务 ID（按任务销毁）"},
            "success": {"type": "boolean", "default": True, "description": "任务是否成功完成"},
        },
    },
    description="销毁隔离环境",
)
async def isolation_destroy_env(
    env_id: str | None = None,
    task_id: str | None = None,
    success: bool = True,
) -> dict[str, Any]:
    """销毁隔离环境（按 env_id 或 task_id）。"""
    if _manager is None:
        return {"error": "隔离服务未初始化"}

    if task_id:
        await _manager.destroy_by_task_id(task_id, success=success)
        return {"destroyed": True, "task_id": task_id}
    if env_id:
        await _manager.destroy_environment(env_id, success=success)
        return {"destroyed": True, "env_id": env_id}
    return {"error": "必须提供 env_id 或 task_id"}


@plugin.tool(
    name="isolation.list_envs",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "按任务 ID 筛选（可选）"},
            "level": {
                "type": "string",
                "enum": ["isolated", "non_isolated"],
                "description": "按隔离级别筛选（可选）",
            },
        },
    },
    description="列出隔离环境",
)
async def isolation_list_envs(
    task_id: str | None = None,
    level: str | None = None,
) -> dict[str, Any]:
    """列出隔离环境。"""
    if _manager is None:
        return {"error": "隔离服务未初始化"}

    env_level = None
    if level:
        env_level = IsolationLevel(level)

    envs = await _manager.list_environments(task_id=task_id, level=env_level)
    return {
        "environments": [
            {
                "env_id": e.env_id,
                "level": e.level.value,
                "provider_type": e.provider_type,
                "status": e.status,
                "task_id": e.context.task_id,
            }
            for e in envs
        ],
        "total": len(envs),
    }


@plugin.tool(
    name="isolation.check_policy",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要检查的文件路径"},
            "workspace": {"type": "string", "description": "工作目录"},
            "operation": {
                "type": "string",
                "enum": ["read", "write"],
                "description": "操作类型",
            },
        },
        "required": ["path", "operation"],
    },
    description="检查文件操作权限",
)
async def isolation_check_policy(
    path: str,
    operation: str = "read",
    workspace: str | None = None,
) -> dict[str, Any]:
    """检查文件操作是否符合权限策略。"""
    if _permission_checker is None:
        return {"error": "权限检查器未初始化"}

    # 使用默认策略（允许工作空间内的读写）
    policy = PermissionPolicyManager().get_default_policy()

    if operation == "read":
        allowed, message = _permission_checker.check_read_permission(
            path, workspace, policy
        )
    else:
        allowed, message = _permission_checker.check_write_permission(
            path, workspace, policy, operation
        )

    return {"allowed": allowed, "message": message}


@plugin.tool(
    name="isolation.checkpoint",
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "restore", "cleanup", "list"],
                "description": "检查点操作",
            },
            "task_id": {"type": "string", "description": "任务 ID"},
            "workspace": {"type": "string", "description": "工作目录"},
            "files_to_backup": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要备份的文件列表",
            },
        },
        "required": ["action"],
    },
    description="检查点管理（创建/恢复/清理/列出）",
)
async def isolation_checkpoint(
    action: str,
    task_id: str | None = None,
    workspace: str | None = None,
    files_to_backup: list[str] | None = None,
) -> dict[str, Any]:
    """检查点管理操作。"""
    if _checkpoint_mgr is None:
        return {"error": "检查点管理器未初始化"}

    if action == "create":
        if not task_id or not workspace:
            return {"error": "create 需要 task_id 和 workspace"}
        cp = _checkpoint_mgr.create_checkpoint(task_id, workspace, files_to_backup)
        return {"created": True, "checkpoint_id": cp.task_id}

    if action == "restore":
        if not task_id:
            return {"error": "restore 需要 task_id"}
        ok = _checkpoint_mgr.restore_checkpoint(task_id)
        return {"restored": ok}

    if action == "cleanup":
        if not task_id:
            return {"error": "cleanup 需要 task_id"}
        ok = _checkpoint_mgr.cleanup_checkpoint(task_id)
        return {"cleaned": ok}

    if action == "list":
        checkpoints = _checkpoint_mgr.list_checkpoints()
        return {"checkpoints": checkpoints, "total": len(checkpoints)}

    return {"error": f"未知操作: {action}"}


@plugin.tool(
    name="isolation.stats",
    schema={
        "type": "object",
        "properties": {},
    },
    description="获取隔离系统统计信息",
)
async def isolation_stats() -> dict[str, Any]:
    """获取隔离环境统计信息。"""
    if _manager is None:
        return {"error": "隔离服务未初始化"}

    return _manager.get_stats()


if __name__ == "__main__":
    plugin.run()
