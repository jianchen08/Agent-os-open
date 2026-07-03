"""外部工具沙箱执行环境。

暴露接口：
- ExternalToolSandbox：复用 isolation 模块的沙箱执行实现
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from tools.external.exceptions import ExternalTimeoutError, SandboxError
from tools.external.interfaces import IExternalToolSandbox
from tools.external.types import SandboxResourceLimits

logger = logging.getLogger(__name__)


class ExternalToolSandbox(IExternalToolSandbox):
    """沙箱执行环境。

    复用 isolation 模块的 IsolationManager 创建隔离执行环境。
    支持资源限制、执行超时控制和沙箱生命周期管理。

    生命周期：创建 → 就绪 → 执行 → 清理
    """

    def __init__(self) -> None:
        """初始化沙箱管理器。"""
        self._sandboxes: dict[str, dict[str, Any]] = {}
        self._logger = logging.getLogger(__name__)

    async def create_sandbox(
        self,
        tool_name: str,
        resource_limits: SandboxResourceLimits | None = None,
    ) -> str:
        """创建沙箱环境。

        Args:
            tool_name: 工具名称
            resource_limits: 资源限制配置

        Returns:
            沙箱 ID

        Raises:
            SandboxError: 创建失败
        """
        sandbox_id = f"ext_{tool_name}_{uuid.uuid4().hex[:8]}"
        limits = resource_limits or SandboxResourceLimits()

        try:
            # 尝试使用 isolation 模块创建环境
            sandbox_env = await self._create_isolation_environment(
                sandbox_id,
                tool_name,
                limits,
            )

            self._sandboxes[sandbox_id] = {
                "tool_name": tool_name,
                "limits": limits,
                "env": sandbox_env,
                "status": "ready",
            }

            self._logger.info(
                "沙箱已创建 | sandbox_id=%s | tool=%s | cpu=%.1f | mem=%dMB",
                sandbox_id,
                tool_name,
                limits.cpu_limit,
                limits.memory_limit_mb,
            )
            return sandbox_id

        except Exception as e:
            raise SandboxError(
                message=f"沙箱创建失败: {e}",
                tool_name=tool_name,
                sandbox_id=sandbox_id,
                cause=e,
            ) from e

    async def execute_in_sandbox(
        self,
        sandbox_id: str,
        command: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """在沙箱中执行命令。

        Args:
            sandbox_id: 沙箱 ID
            command: 要执行的命令
            timeout: 超时时间

        Returns:
            执行结果

        Raises:
            SandboxError: 沙箱不存在或执行失败
            ExternalTimeoutError: 执行超时
        """
        sandbox = self._get_sandbox(sandbox_id)
        effective_timeout = timeout or sandbox["limits"].timeout_seconds

        try:
            sandbox["status"] = "busy"
            result = await asyncio.wait_for(
                self._execute_command(sandbox_id, sandbox, command),
                timeout=effective_timeout,
            )
            sandbox["status"] = "ready"
            return result

        except asyncio.TimeoutError:
            sandbox["status"] = "error"
            raise ExternalTimeoutError(  # noqa: B904
                message=f"沙箱执行超时 ({effective_timeout}s)",
                tool_name=sandbox["tool_name"],
                timeout_seconds=effective_timeout,
            )
        except Exception as e:
            sandbox["status"] = "error"
            raise SandboxError(
                message=f"沙箱执行失败: {e}",
                tool_name=sandbox["tool_name"],
                sandbox_id=sandbox_id,
                cause=e,
            ) from e

    async def destroy_sandbox(self, sandbox_id: str) -> None:
        """销毁沙箱环境。

        Args:
            sandbox_id: 沙箱 ID
        """
        sandbox = self._sandboxes.pop(sandbox_id, None)
        if sandbox is None:
            self._logger.warning(
                "沙箱不存在 | sandbox_id=%s",
                sandbox_id,
            )
            return

        try:
            env = sandbox.get("env")
            if env is not None and hasattr(env, "env_id"):
                # 尝试通过 IsolationManager 清理
                await self._destroy_isolation_environment(env)

            self._logger.info(
                "沙箱已销毁 | sandbox_id=%s",
                sandbox_id,
            )
        except Exception as e:
            self._logger.error(
                "沙箱清理失败 | sandbox_id=%s | error=%s",
                sandbox_id,
                e,
            )

    def get_sandbox_status(self, sandbox_id: str) -> str | None:
        """获取沙箱状态。

        Args:
            sandbox_id: 沙箱 ID

        Returns:
            状态字符串，不存在返回 None
        """
        sandbox = self._sandboxes.get(sandbox_id)
        return sandbox["status"] if sandbox else None

    def list_sandboxes(self) -> list[dict[str, Any]]:
        """列出所有沙箱。"""
        return [
            {
                "sandbox_id": sid,
                "tool_name": info["tool_name"],
                "status": info["status"],
                "limits": {
                    "cpu": info["limits"].cpu_limit,
                    "memory_mb": info["limits"].memory_limit_mb,
                },
            }
            for sid, info in self._sandboxes.items()
        ]

    async def destroy_all(self) -> None:
        """销毁所有沙箱。"""
        ids = list(self._sandboxes.keys())
        for sandbox_id in ids:
            await self.destroy_sandbox(sandbox_id)

    def _get_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        """获取沙箱信息。

        Args:
            sandbox_id: 沙箱 ID

        Returns:
            沙箱信息字典

        Raises:
            SandboxError: 沙箱不存在
        """
        sandbox = self._sandboxes.get(sandbox_id)
        if sandbox is None:
            raise SandboxError(
                message=f"沙箱不存在: {sandbox_id}",
                sandbox_id=sandbox_id,
            )
        return sandbox

    async def _create_isolation_environment(
        self,
        sandbox_id: str,
        tool_name: str,
        limits: SandboxResourceLimits,
    ) -> Any:
        """通过 isolation 模块创建隔离环境。

        Args:
            sandbox_id: 沙箱 ID
            tool_name: 工具名称
            limits: 资源限制

        Returns:
            隔离环境对象（或 None）
        """
        try:
            from isolation.manager import get_isolation_manager  # noqa: PLC0415
            from isolation.types import (  # noqa: PLC0415
                IsolationLevel,
                OperationType,
                TaskType,
            )

            manager = await get_isolation_manager()
            env = await manager.get_or_create_environment(
                task_id=sandbox_id,
                task_type=TaskType.ATOMIC,
                operation_type=OperationType.CODE_EXECUTION,
                isolation_level=IsolationLevel.CONTAINER,
                metadata={
                    "tool_name": tool_name,
                    "cpu_limit": limits.cpu_limit,
                    "memory_limit_mb": limits.memory_limit_mb,
                },
            )
            return env

        except ImportError:
            self._logger.warning(
                "isolation 模块不可用，使用模拟沙箱 | sandbox_id=%s",
                sandbox_id,
            )
            return {"sandbox_id": sandbox_id, "mock": True}

    async def _execute_command(
        self,
        sandbox_id: str,
        sandbox: dict[str, Any],
        command: str,
    ) -> dict[str, Any]:
        """在隔离环境中执行命令。

        Args:
            sandbox_id: 沙箱 ID
            sandbox: 沙箱信息
            command: 命令

        Returns:
            执行结果
        """
        env = sandbox.get("env")
        sandbox["tool_name"]

        # 如果是模拟沙箱
        if isinstance(env, dict) and env.get("mock"):
            return {
                "success": True,
                "output": f"mock execution of: {command[:100]}",
                "sandbox_id": sandbox_id,
            }

        # 真实隔离环境
        try:
            from isolation.manager import get_isolation_manager  # noqa: PLC0415
            from isolation.types import TaskType  # noqa: PLC0415

            manager = await get_isolation_manager()
            operation = {
                "type": "command",
                "command": command,
            }
            result = await manager.execute_in_isolation(
                task_id=sandbox_id,
                task_type=TaskType.ATOMIC,
                operation=operation,
                tool_name=sandbox.get("tool_name"),
            )

            return {
                "success": result.success,
                "output": result.output,
                "error": result.error,
                "sandbox_id": sandbox_id,
            }
        except ImportError:
            return {
                "success": False,
                "error": "isolation 模块不可用",
                "sandbox_id": sandbox_id,
            }

    async def _destroy_isolation_environment(self, env: Any) -> None:
        """通过 isolation 模块销毁隔离环境。"""
        try:
            from isolation.manager import get_isolation_manager  # noqa: PLC0415

            manager = await get_isolation_manager()
            await manager.destroy_environment(env.env_id)
        except ImportError:
            pass
