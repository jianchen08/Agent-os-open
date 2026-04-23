"""
隔离系统的工具集成

提供便捷的函数用于在隔离环境中执行操作
"""

import logging
from typing import Any

from isolation.manager import get_isolation_manager
from isolation.types import IsolationLevel, OperationType, TaskType

logger = logging.getLogger(__name__)


async def execute_with_isolation(
    task_id: str,
    task_type: str,
    operation: dict[str, Any],
    operation_type: str | None = None,
    parent_env_id: str | None = None,
    workspace: str | None = None,
    isolation_level: str | None = None,
) -> dict[str, Any]:
    """在隔离环境中执行操作"""
    try:
        task_type_enum = TaskType(task_type)
        operation_type_enum = OperationType(operation_type) if operation_type else None
        isolation_level_enum = IsolationLevel(isolation_level) if isolation_level else None

        manager = await get_isolation_manager()

        result = await manager.execute_in_isolation(
            task_id=task_id,
            task_type=task_type_enum,
            operation=operation,
            operation_type=operation_type_enum,
            parent_env_id=parent_env_id,
            workspace=workspace,
            isolation_level=isolation_level_enum,
        )

        return result.to_dict()

    except ValueError as e:
        logger.error(f"参数错误: {e}")
        return {
            "success": False,
            "output": None,
            "error": f"参数错误: {str(e)}",
        }
    except Exception as e:
        logger.error(f"执行失败: {e}", exc_info=True)
        return {
            "success": False,
            "output": None,
            "error": f"执行失败: {str(e)}",
        }


async def get_isolation_level(
    task_id: str,
    task_type: str,
    operation_type: str | None = None,
) -> dict[str, Any]:
    """获取任务将使用的隔离级别"""
    try:
        manager = await get_isolation_manager()

        # 检查提供者可用性
        available = await manager._check_providers_availability()

        # 决策隔离级别
        task_type_enum = TaskType(task_type)
        operation_type_enum = OperationType(operation_type) if operation_type else None
        tool_category = operation_type_enum.value if operation_type_enum else None

        policy = await manager._decider.decide(
            tool_name=task_type_enum.value,
            tool_category=tool_category,
            available_providers=available,
        )

        return {
            "level": policy.isolation.value,
            "requires_approval": policy.approval,
            "execution": policy.execution,
            "available_providers": {
                level.value: is_avail for level, is_avail in available.items()
            },
        }

    except Exception as e:
        logger.error(f"获取隔离级别失败: {e}")
        return {
            "error": str(e),
        }


async def list_environments(
    task_id: str | None = None, level: str | None = None
) -> dict[str, Any]:
    """列出隔离环境"""
    try:
        manager = await get_isolation_manager()

        level_enum = IsolationLevel(level) if level else None

        envs = await manager.list_environments(task_id=task_id, level=level_enum)

        return {
            "environments": [
                {
                    "env_id": env.env_id,
                    "level": env.level.value,
                    "status": env.status,
                    "task_id": env.context.task_id,
                    "task_type": env.context.task_type.value,
                    "created_at": env.created_at,
                    "last_used_at": env.last_used_at,
                }
                for env in envs
            ],
            "total": len(envs),
        }

    except Exception as e:
        logger.error(f"列出环境失败: {e}")
        return {
            "error": str(e),
        }


async def destroy_environment(env_id: str, success: bool = True) -> dict[str, Any]:
    """销毁隔离环境"""
    try:
        manager = await get_isolation_manager()
        await manager.destroy_environment(env_id, success=success)

        return {
            "success": True,
            "env_id": env_id,
        }

    except Exception as e:
        logger.error(f"销毁环境失败: {e}")
        return {
            "success": False,
            "error": str(e),
        }


async def get_manager_stats() -> dict[str, Any]:
    """获取管理器统计信息"""
    try:
        manager = await get_isolation_manager()
        return manager.get_stats()

    except Exception as e:
        logger.error(f"获取统计信息失败: {e}")
        return {
            "error": str(e),
        }
