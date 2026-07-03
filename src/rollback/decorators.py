"""
回滚装饰器

提供自动记录操作日志的装饰器
"""

import functools
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.rollback.manager import get_rollback_manager
from src.rollback.models import OperationType

logger = logging.getLogger(__name__)


def reversible_operation(
    tool_name: str,
    operation_type: OperationType,
    target_param: str = "path",
    capture_before_state: bool = True,
    reverse_handler: str | None = None,
):
    """
    可逆操作装饰器

    自动记录操作日志，支持回滚

    Args:
        tool_name: 工具名称
        operation_type: 操作类型
        target_param: 目标参数名（用于提取操作目标）
        capture_before_state: 是否捕获操作前状态
        reverse_handler: 逆操作处理器名称

    使用示例:
        @reversible_operation(
            tool_name="file_write",
            operation_type=OperationType.UPDATE,
            target_param="path",
        )
        async def write_file(self, inputs: Dict[str, Any]) -> ToolResult:
            ...
    """

    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(self, inputs: dict[str, Any], *args, **kwargs):
            task_id = inputs.get("task_id") or kwargs.get("task_id")
            target = inputs.get(target_param, "")
            manager = get_rollback_manager()

            # 捕获操作前状态
            before_state = None
            if capture_before_state:
                before_state = await _capture_state(operation_type, target)

            # 执行原始操作
            result = await func(self, inputs, *args, **kwargs)

            # 如果有 task_id，记录操作日志
            if task_id and result.success:
                try:
                    # 捕获操作后状态
                    after_state = await _capture_state(operation_type, target)

                    # 记录操作
                    await manager.record_operation(
                        task_id=task_id,
                        tool_name=tool_name,
                        operation_type=operation_type,
                        target=target,
                        params=inputs,
                        before_state=before_state,
                        after_state=after_state,
                        reversible=True,
                        reverse_action={"handler": reverse_handler or f"{tool_name}_reverser"},
                    )
                except Exception as e:
                    logger.warning(f"记录操作日志失败: {e}")

            return result

        return wrapper

    return decorator


async def _capture_state(operation_type: OperationType, target: str) -> dict[str, Any] | None:
    """
    捕获操作前/后状态

    Args:
        operation_type: 操作类型
        target: 操作目标

    Returns:
        状态字典
    """
    if not target:
        return None

    # 文件操作：捕获文件内容
    if operation_type in (
        OperationType.CREATE,
        OperationType.UPDATE,
        OperationType.DELETE,
    ):
        return await _capture_file_state(target)

    return None


async def _capture_file_state(path_str: str) -> dict[str, Any] | None:
    """捕获文件状态"""
    try:
        path = Path(path_str)

        if not path.exists():
            return {"exists": False}

        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = path.read_text(encoding="gbk", errors="ignore")

            stat = path.stat()
            return {
                "exists": True,
                "is_file": True,
                "content": content,
                "size": stat.st_size,
                "modified": stat.st_mtime,
            }

        if path.is_dir():
            return {
                "exists": True,
                "is_file": False,
                "is_dir": True,
            }

    except Exception as e:
        logger.warning(f"捕获文件状态失败: {e}")

    return None


class OperationRecorder:
    """
    操作记录器

    用于手动记录操作日志
    """

    def __init__(self, task_id: str):
        """
        初始化记录器

        Args:
            task_id: 任务 ID
        """
        self.task_id = task_id
        self.manager = get_rollback_manager()

    async def record(
        self,
        tool_name: str,
        operation_type: OperationType,
        target: str,
        params: dict[str, Any],
        before_state: dict[str, Any] | None = None,
        after_state: dict[str, Any] | None = None,
        reversible: bool = True,
        reverse_action: dict[str, Any] | None = None,
    ) -> str:
        """
        记录操作

        Args:
            tool_name: 工具名称
            operation_type: 操作类型
            target: 操作目标
            params: 操作参数
            before_state: 操作前状态
            after_state: 操作后状态
            reversible: 是否可逆
            reverse_action: 逆操作定义

        Returns:
            操作日志 ID
        """
        return await self.manager.record_operation(
            task_id=self.task_id,
            tool_name=tool_name,
            operation_type=operation_type,
            target=target,
            params=params,
            before_state=before_state,
            after_state=after_state,
            reversible=reversible,
            reverse_action=reverse_action,
        )

    async def create_checkpoint(self, name: str | None = None, description: str | None = None) -> str:
        """
        创建检查点

        Args:
            name: 检查点名称
            description: 检查点描述

        Returns:
            检查点 ID
        """
        return await self.manager.create_checkpoint(
            task_id=self.task_id,
            name=name,
            description=description,
        )

    async def rollback(
        self,
        to_checkpoint: str | None = None,
        steps: int | None = None,
    ):
        """
        回滚操作

        Args:
            to_checkpoint: 回滚到指定检查点
            steps: 回滚步数

        Returns:
            回滚结果
        """
        return await self.manager.rollback(
            task_id=self.task_id,
            to_checkpoint=to_checkpoint,
            steps=steps,
        )
