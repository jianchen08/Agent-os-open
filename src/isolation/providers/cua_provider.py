"""
Cua Docker 隔离提供者

使用 Docker SDK 创建容器进行隔离
"""

import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

from src.isolation.providers.base import IsolationProvider
from src.isolation.types import (
    EnvironmentStatus,
    ExecutionResult,
    IsolationContext,
    IsolationEnvironment,
    IsolationLevel,
)

logger = logging.getLogger(__name__)


class CuaProvider(IsolationProvider):
    """Cua Docker 隔离提供者

    使用 Docker SDK 创建和管理容器，提供半隔离环境。
    """

    def __init__(
        self,
        image: str = "python:3.11-slim",
        memory_limit: str = "2g",
        cpu_limit: str = "2",
    ):
        """初始化 Cua 提供者

        Args:
            image: Docker 镜像名称
            memory_limit: 内存限制（如 "2g"）
            cpu_limit: CPU 限制（如 "2"）
        """
        self._image = image
        self._memory_limit = memory_limit
        self._cpu_limit = cpu_limit
        self._docker_client = None
        self._environments: dict[str, IsolationEnvironment] = {}

    def get_level(self) -> IsolationLevel:
        """获取隔离级别"""
        return IsolationLevel.CONTAINER

    async def is_available(self) -> tuple[bool, str | None]:
        """检查 Docker 是否可用

        Returns:
            (是否可用, 不可用原因)
        """
        try:
            # 尝试导入 docker
            import importlib  # noqa: PLC0415

            if importlib.util.find_spec("docker") is None:
                return False, "Docker SDK 未安装。请运行: pip install docker"

            # 尝试连接 Docker daemon
            import docker  # noqa: PLC0415

            client = docker.from_env()
            client.ping()
            client.close()

            return True, None

        except Exception as e:
            error_msg = str(e)
            if "ConnectionRefusedError" in error_msg or "connect" in error_msg.lower():
                return False, "Docker 未运行。请启动 Docker Desktop 或 Docker daemon"
            if "timeout" in error_msg.lower():
                return False, "Docker 响应超时。请检查 Docker 服务状态"
            return False, f"Docker 不可用: {error_msg}"

    async def create_environment(self, context: IsolationContext) -> IsolationEnvironment:
        """创建 Docker 容器环境

        Args:
            context: 隔离上下文

        Returns:
            隔离环境

        Raises:
            RuntimeError: 如果 Docker 不可用
        """
        # 检查可用性
        available, error = await self.is_available()
        if not available:
            raise RuntimeError(f"Docker 不可用: {error}")

        try:
            import docker  # noqa: PLC0415

            # 初始化 Docker 客户端
            if self._docker_client is None:
                self._docker_client = docker.from_env()

            now = datetime.now(UTC)
            container_name = f"cua-{context.task_id}"

            # 创建容器
            container = self._docker_client.containers.create(
                image=self._image,
                name=container_name,
                detach=True,
                # 资源限制
                mem_limit=self._memory_limit,
                cpu_quota=int(float(self._cpu_limit) * 100000),
                cpu_period=100000,
                # 保持容器运行
                command="tail -f /dev/null",
                # 自动清理
                auto_remove=False,
            )

            # 启动容器
            container.start()

            env = IsolationEnvironment(
                env_id=container.id,
                level=IsolationLevel.CONTAINER,
                provider_type="cua",
                status=EnvironmentStatus.READY.value,
                context=context,
                provider_info={
                    "container_id": container.id,
                    "container_name": container_name,
                    "image": self._image,
                    "memory_limit": self._memory_limit,
                    "cpu_limit": self._cpu_limit,
                },
                created_at=now.isoformat(),
                last_used_at=now.isoformat(),
            )

            self._environments[container.id] = env
            logger.info(f"创建 Docker 容器: {container_name} ({container.id})")

            return env

        except Exception as e:
            logger.error(f"创建 Docker 容器失败: {e}", exc_info=True)
            raise RuntimeError(f"创建 Docker 容器失败: {str(e)}")  # noqa: B904

    async def destroy_environment(self, env_id: str) -> None:
        """销毁容器

        Args:
            env_id: 环境 ID（容器 ID）
        """
        try:
            if self._docker_client and env_id in self._environments:
                # 停止并删除容器
                try:
                    container = self._docker_client.containers.get(env_id)
                    container.stop(timeout=5)
                    container.remove()
                    logger.info(f"销毁 Docker 容器: {env_id}")
                except Exception as e:
                    logger.warning(f"清理容器失败（可能已不存在）: {e}")

            self._environments.pop(env_id, None)

        except Exception as e:
            logger.error(f"销毁环境失败: {e}", exc_info=True)

    async def execute_in_environment(self, env_id: str, operation: dict[str, Any]) -> ExecutionResult:
        """在容器中执行操作

        Args:
            env_id: 环境 ID（容器 ID）
            operation: 操作定义

        Returns:
            执行结果
        """
        if not self._docker_client:
            return ExecutionResult(
                success=False,
                output=None,
                error="Docker 客户端未初始化",
            )

        try:
            container = self._docker_client.containers.get(env_id)

            op_type = operation.get("type")

            if op_type == "command":
                return await self._execute_command(container, operation)
            if op_type == "python_code":
                return await self._execute_python_code(container, operation)
            if op_type == "file_operation":
                return await self._execute_file_op(container, operation)
            return ExecutionResult(
                success=False,
                output=None,
                error=f"不支持的操作类型: {op_type}",
            )

        except Exception as e:
            logger.error(f"在容器中执行操作失败: {e}", exc_info=True)
            return ExecutionResult(
                success=False,
                output=None,
                error=f"执行失败: {str(e)}",
            )

    async def _execute_command(self, container, operation: dict[str, Any]) -> ExecutionResult:
        """执行 Shell 命令

        Args:
            container: Docker 容器对象
            operation: 操作定义

        Returns:
            执行结果
        """
        command = operation.get("command")
        if not command:
            return ExecutionResult(success=False, output=None, error="命令不能为空")

        operation.get("timeout", 30)

        try:
            # 在容器中执行命令
            exit_code, output = container.exec_run(
                cmd=command,
                stdout=True,
                stderr=True,
                demux=True,  # 分离 stdout 和 stderr
            )

            # 解码输出
            stdout_text = ""
            stderr_text = ""

            if output[0]:
                stdout_text = output[0].decode("utf-8", errors="replace")
            if output[1]:
                stderr_text = output[1].decode("utf-8", errors="replace")

            success = exit_code == 0

            return ExecutionResult(
                success=success,
                output={
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "return_code": exit_code,
                    "command": command,
                },
                error=None if success else stderr_text,
            )

        except Exception as e:
            return ExecutionResult(
                success=False,
                output=None,
                error=f"执行命令失败: {str(e)}",
            )

    async def _execute_python_code(self, container, operation: dict[str, Any]) -> ExecutionResult:
        """执行 Python 代码

        Args:
            container: Docker 容器对象
            operation: 操作定义

        Returns:
            执行结果
        """
        code = operation.get("code")
        if not code:
            return ExecutionResult(
                success=False,
                output=None,
                error="代码不能为空",
            )

        # 构建 Python 命令
        # 使用 -c 参数执行代码
        command = f'python -c "{code.replace(chr(34), chr(39))}"'

        return await self._execute_command(container, {"type": "command", "command": command})

    async def _execute_file_op(self, container, operation: dict[str, Any]) -> ExecutionResult:
        """执行文件操作

        Args:
            container: Docker 容器对象
            operation: 操作定义

        Returns:
            执行结果
        """
        op = operation.get("operation")
        path = operation.get("path")

        try:
            if op == "exists":
                # 检查文件是否存在
                exit_code, output = container.exec_run(f"test -f {path}", demux=True)
                exists = exit_code == 0

                return ExecutionResult(success=True, output={"exists": exists})

            if op == "delete":
                # 删除文件
                exit_code, output = container.exec_run(f"rm -f {path}", demux=True)

                return ExecutionResult(
                    success=exit_code == 0,
                    output=None,
                    error=None if exit_code == 0 else "删除失败",
                )

            return ExecutionResult(
                success=False,
                output=None,
                error=f"不支持的文件操作: {op}",
            )

        except Exception as e:
            return ExecutionResult(
                success=False,
                output=None,
                error=f"文件操作失败: {str(e)}",
            )

    async def get_environment_status(self, env_id: str) -> EnvironmentStatus:
        """获取容器状态

        Args:
            env_id: 环境 ID（容器 ID）

        Returns:
            容器状态
        """
        env = self._environments.get(env_id)
        if not env:
            return EnvironmentStatus.STOPPED

        try:
            if self._docker_client:
                container = self._docker_client.containers.get(env_id)
                container_status = container.status.lower()

                # 映射 Docker 状态到我们的状态
                status_map = {
                    "created": EnvironmentStatus.CREATING.value,
                    "running": EnvironmentStatus.READY.value,
                    "paused": EnvironmentStatus.BUSY.value,
                    "restarting": EnvironmentStatus.BUSY.value,
                    "exited": EnvironmentStatus.STOPPED.value,
                    "removing": EnvironmentStatus.STOPPING.value,
                    "dead": EnvironmentStatus.ERROR.value,
                }

                return EnvironmentStatus(status_map.get(container_status, EnvironmentStatus.READY.value))

        except Exception:
            pass

        return EnvironmentStatus(env.status)

    def __del__(self):
        """析构函数，清理资源"""
        if self._docker_client:
            with contextlib.suppress(Exception):
                self._docker_client.close()
