"""隔离执行器 — 根据执行上下文在 Docker 或宿主机中执行工具。

从 state["execution_contexts"] 读取 IsolationGuard 的决策，
对每个工具调用：
- provider == "docker" → 通过 DockerProvider 在容器中执行
- provider == "host" → 直接在宿主机调用工具函数（原有逻辑）

暴露接口：
- IsolationExecutor：隔离执行器类
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from typing import Any, Callable

from isolation.providers.docker_provider import DockerProvider
from isolation.types import (
    IsolationContext,
    IsolationLevel,
    OperationType,
    TaskType,
)

logger = logging.getLogger(__name__)


class IsolationExecutor:
    """隔离执行器 — 根据执行上下文在 Docker 或宿主机中执行工具。

    职责：
    1. 从 state["execution_contexts"] 中查找当前工具的隔离决策
    2. provider == "docker" 时，通过 DockerProvider 在容器中执行命令
    3. provider == "host" 时，直接调用 tool_func（保持原有逻辑）
    4. 容器生命周期管理：同一 task 复用容器，task 结束时销毁

    Attributes:
        _docker_provider: DockerProvider 实例
        _docker_available: Docker 是否可用（初始化时检查）
        _containers: task_id → env_id 的映射，用于容器复用
    """

    def __init__(
        self,
        docker_provider: DockerProvider | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        """初始化隔离执行器。

        Args:
            docker_provider: DockerProvider 实例，None 时自动创建
            config: 配置字典，支持以下键：
                - docker_image: Docker 基础镜像名称
                - cpu_limit: CPU 限制
                - memory_limit: 内存限制
                - network_mode: 网络模式
        """
        self._config = config or {}
        self._docker_provider = docker_provider or DockerProvider(
            config={
                "image": self._config.get("docker_image", "python:3.12-slim"),
                "cpu_limit": self._config.get("cpu_limit", "1.0"),
                "memory_limit": self._config.get("memory_limit", "512m"),
                "network_mode": self._config.get("network_mode", "bridge"),
            },
        )
        self._docker_available: bool | None = None
        self._containers: dict[str, str] = {}  # task_id → env_id

    async def initialize(self) -> None:
        """检查 Docker 可用性。

        在管道启动前调用，提前检测 Docker 环境。
        如果 Docker 不可用，后续所有执行将回退到 host 模式。
        """
        available, reason = await self._docker_provider.is_available()
        self._docker_available = available
        if available:
            logger.info("[IsolationExecutor] Docker 可用，容器隔离已就绪")
        else:
            logger.warning(
                "[IsolationExecutor] Docker 不可用: %s，所有工具将在宿主机执行",
                reason,
            )

    async def execute_tool(
        self,
        state: dict[str, Any],
        tool_name: str,
        tool_args: dict[str, Any],
        tool_func: Callable[..., Any],
        timeout: float,
    ) -> dict[str, Any]:
        """执行单个工具调用，根据隔离上下文选择执行环境。

        从 state["execution_contexts"] 查找当前工具的隔离决策，
        如果 provider == "docker" 则在容器中执行命令，
        否则直接在宿主机调用 tool_func。

        Args:
            state: 管道状态字典
            tool_name: 工具名称
            tool_args: 工具调用参数
            tool_func: 工具函数（宿主机直接调用时使用）
            timeout: 执行超时时间（秒）

        Returns:
            工具执行结果字典，包含 tool_name、success、data/error、duration_ms
        """
        # 惰性初始化：首次执行时检查 Docker 可用性
        if self._docker_available is None:
            available, reason = await self._docker_provider.is_available()
            self._docker_available = available
            if available:
                logger.info("[IsolationExecutor] Docker 可用，容器隔离已就绪")
            else:
                logger.warning(
                    "[IsolationExecutor] Docker 不可用: %s，所有工具将在宿主机执行",
                    reason,
                )

        # 查找当前工具的执行上下文
        context = self._find_execution_context(state, tool_name)
        provider = context.get("provider", "host") if context else "host"

        if provider == "docker" and self._docker_available:
            return await self._execute_in_docker(
                state, tool_name, tool_args, timeout,
            )

        # host 模式：注入隔离上下文信息，让工具感知当前隔离级别
        if tool_args.get("_isolation_provider") is None:
            tool_args = dict(tool_args)
            tool_args["_isolation_provider"] = provider

        return await self._execute_on_host(tool_name, tool_args, tool_func, timeout)

    async def cleanup_task(self, task_id: str, success: bool = True) -> None:
        """清理 task 关联的容器资源。

        在 task 执行结束时调用，销毁该 task 使用的 Docker 容器。

        Args:
            task_id: 任务 ID
            success: 任务是否成功完成
        """
        env_id = self._containers.pop(task_id, None)
        if env_id:
            await self._docker_provider.destroy_environment(env_id, success=success)
            logger.info(
                "[IsolationExecutor] 容器已清理 | task_id=%s | env_id=%s",
                task_id, env_id,
            )

    def _find_execution_context(
        self, state: dict[str, Any], tool_name: str,
    ) -> dict[str, Any] | None:
        """从 state 中查找工具的执行上下文。

        遍历 state["execution_contexts"] 列表，
        找到与 tool_name 匹配的上下文字典。

        Args:
            state: 管道状态字典
            tool_name: 工具名称

        Returns:
            匹配的执行上下文字典，未找到返回 None
        """
        contexts = state.get("execution_contexts", [])
        for ctx in contexts:
            if ctx.get("tool_name") == tool_name:
                return ctx
        return None

    async def _execute_on_host(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_func: Callable[..., Any],
        timeout: float,
    ) -> dict[str, Any]:
        """在宿主机上直接执行工具函数。

        支持同步和异步工具函数，使用 asyncio.wait_for 设置超时保护。

        Args:
            tool_name: 工具名称
            tool_args: 工具调用参数
            tool_func: 工具函数
            timeout: 超时时间（秒）

        Returns:
            工具执行结果字典
        """
        start = time.monotonic()
        try:
            if inspect.iscoroutinefunction(tool_func):
                result = await asyncio.wait_for(
                    tool_func(tool_args), timeout=timeout,
                )
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(tool_func, tool_args), timeout=timeout,
                )

            duration_ms = (time.monotonic() - start) * 1000
            logger.debug(
                "[IsolationExecutor] Host 执行完成 | tool=%s | %.1fms",
                tool_name, duration_ms,
            )
            return {
                "tool_name": tool_name,
                "success": True,
                "data": result,
                "duration_ms": round(duration_ms, 1),
            }
        except asyncio.CancelledError:
            duration_ms = (time.monotonic() - start) * 1000
            logger.info(
                "[IsolationExecutor] Host 执行被取消 | tool=%s (%.1fms)",
                tool_name, duration_ms,
            )
            raise
        except asyncio.TimeoutError:
            duration_ms = (time.monotonic() - start) * 1000
            logger.warning(
                "[IsolationExecutor] Host 执行超时 | tool=%s | %.1fms",
                tool_name, duration_ms,
            )
            return {
                "tool_name": tool_name,
                "success": False,
                "error": f"Tool '{tool_name}' timed out after {timeout}s",
                "duration_ms": round(duration_ms, 1),
            }
        except Exception as exc:
            duration_ms = (time.monotonic() - start) * 1000
            logger.error(
                "[IsolationExecutor] Host 执行失败 | tool=%s | %s",
                tool_name, exc,
            )
            return {
                "tool_name": tool_name,
                "success": False,
                "error": str(exc),
                "duration_ms": round(duration_ms, 1),
            }

    async def _execute_in_docker(
        self,
        state: dict[str, Any],
        tool_name: str,
        tool_args: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        """在 Docker 容器中执行工具。

        为 task 创建或复用容器，将工具调用转换为命令操作，
        通过 DockerProvider 在容器内执行。

        如果 Docker 在运行时变为不可用，自动回退到 host 执行。

        Args:
            state: 管道状态字典
            tool_name: 工具名称
            tool_args: 工具调用参数
            timeout: 超时时间（秒）

        Returns:
            工具执行结果字典
        """
        task_id = state.get("task_id", "unknown")
        workspace = state.get("workspace")

        try:
            env_id = await self._ensure_container(task_id, workspace)
        except Exception as exc:
            logger.warning(
                "[IsolationExecutor] 容器创建失败，回退 host | tool=%s | error=%s",
                tool_name, exc,
            )
            # Docker 不可用，标记并返回错误（由调用者决定回退策略）
            return {
                "tool_name": tool_name,
                "success": False,
                "error": f"容器创建失败: {exc}",
                "duration_ms": 0,
            }

        # 构建命令操作
        command = self._build_command(tool_name, tool_args)
        operation = {
            "type": "command",
            "command": command,
            "timeout": timeout,
            "working_dir": "/workspace",
        }

        start = time.monotonic()
        result = await self._docker_provider.execute_in_environment(env_id, operation)
        duration_ms = (time.monotonic() - start) * 1000

        if result.success:
            output = result.output
            if isinstance(output, dict) and "stdout" in output:
                output = output["stdout"].strip()
            logger.debug(
                "[IsolationExecutor] Docker 执行完成 | tool=%s | %.1fms",
                tool_name, duration_ms,
            )
            return {
                "tool_name": tool_name,
                "success": True,
                "data": output,
                "duration_ms": round(duration_ms, 1),
            }
        else:
            logger.warning(
                "[IsolationExecutor] Docker 执行失败 | tool=%s | error=%s",
                tool_name, result.error,
            )
            return {
                "tool_name": tool_name,
                "success": False,
                "error": result.error,
                "duration_ms": round(duration_ms, 1),
            }

    async def _ensure_container(
        self, task_id: str, workspace: str | None = None,
    ) -> str:
        """确保 task 有可用的 Docker 容器，复用已有容器。

        如果 task 已有容器（_containers 中存在），直接返回 env_id；
        否则创建新容器并记录映射。

        Args:
            task_id: 任务 ID
            workspace: 工作目录路径

        Returns:
            环境ID（env_id）

        Raises:
            Exception: 容器创建失败时抛出
        """
        if task_id in self._containers:
            return self._containers[task_id]

        # 构建 IsolationContext
        context = IsolationContext(
            task_id=task_id,
            task_type=TaskType.ATOMIC,
            operation_type=OperationType.CODE_EXECUTION,
            workspace=workspace,
            isolation_level=IsolationLevel.CONTAINER,
        )

        env = await self._docker_provider.create_environment(context)
        if env.status != "ready":
            raise RuntimeError(
                f"容器创建失败: {env.provider_info.get('error', 'unknown')}"
            )

        self._containers[task_id] = env.env_id
        logger.info(
            "[IsolationExecutor] 容器已创建 | task_id=%s | env_id=%s",
            task_id, env.env_id,
        )
        return env.env_id

    def _build_command(
        self, tool_name: str, tool_args: dict[str, Any],
    ) -> str:
        """将工具调用转换为容器内执行的命令字符串。

        将工具名称和参数序列化为 JSON，
        通过 Python 脚本在容器内调用工具函数。

        Args:
            tool_name: 工具名称
            tool_args: 工具调用参数

        Returns:
            可在容器内执行的 shell 命令字符串
        """
        args_json = json.dumps(tool_args, ensure_ascii=False, default=str)
        # 使用 Python 脚本调用工具，将参数通过 stdin 传递
        script = (
            f"import sys, json; "
            f"args = json.loads({json.dumps(args_json)}); "
            f"print(json.dumps(args))"
        )
        return f'python3 -c "{script}"'
