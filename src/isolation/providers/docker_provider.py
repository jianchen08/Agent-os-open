"""
Docker 容器隔离提供者

暴露接口：
- DockerProvider：Docker 容器隔离提供者类
"""

import asyncio
import json
import logging
import shutil
from datetime import UTC, datetime
from typing import Any

from isolation.providers.base import IsolationProvider
from isolation.types import (
    EnvironmentStatus,
    ExecutionResult,
    IsolationContext,
    IsolationEnvironment,
    IsolationLevel,
)

logger = logging.getLogger(__name__)


class DockerProvider(IsolationProvider):
    """Docker 容器隔离提供者。

    在 Docker 容器内执行命令和操作，提供：
    - 容器生命周期管理：创建、启动、停止、销毁
    - 资源限制：CPU、内存、磁盘配额
    - 网络隔离：可选的网络访问控制
    - 文件系统隔离：容器内独立的文件系统

    注意：需要安装 Docker 并确保 Docker daemon 正在运行。
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化 Docker 提供者。

        Args:
            config: 配置字典，支持以下键：
                - image: 基础镜像名称（默认 python:3.12-slim）
                - cpu_limit: CPU 限制（默认 "1.0"）
                - memory_limit: 内存限制（默认 "512m"）
                - network_mode: 网络模式（默认 "bridge"）
                - workspace_mount: 是否挂载工作目录（默认 True）
        """
        self._config = config or {}
        self._image = self._config.get("image", "python:3.12-slim")
        self._cpu_limit = self._config.get("cpu_limit", "1.0")
        self._memory_limit = self._config.get("memory_limit", "512m")
        self._network_mode = self._config.get("network_mode", "bridge")
        self._workspace_mount = self._config.get("workspace_mount", True)
        self._environments: dict[str, IsolationEnvironment] = {}
        self._docker_available: bool | None = None

    def get_level(self) -> IsolationLevel:
        """获取隔离级别。"""
        return IsolationLevel.CONTAINER

    async def is_available(self) -> tuple[bool, str | None]:
        """检查 Docker 提供者是否可用。

        检查 Docker CLI 是否安装且 daemon 是否运行。
        """
        if not shutil.which("docker"):
            return False, "Docker CLI 未安装"

        try:
            process = await asyncio.create_subprocess_exec(
                "docker", "info",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
            if process.returncode != 0:
                return False, f"Docker daemon 未运行: {stderr.decode('utf-8', errors='replace')}"
            self._docker_available = True
            return True, None
        except FileNotFoundError:
            return False, "Docker CLI 未找到"
        except TimeoutError:
            return False, "Docker 检查超时"
        except Exception as e:
            return False, f"Docker 检查失败: {e}"

    async def create_environment(
        self, context: IsolationContext,
    ) -> IsolationEnvironment:
        """创建 Docker 容器环境。

        Args:
            context: 隔离上下文

        Returns:
            创建的隔离环境实例
        """
        now = datetime.now(UTC)
        env_id = f"docker-{context.task_id}"

        # 构建容器名称
        container_name = f"agent-os-{context.task_id}"

        # 构建 docker run 命令参数
        run_args = self._build_run_args(container_name, context)

        # 拉取镜像（如果本地不存在）
        await self._ensure_image()

        # 创建容器
        process = await asyncio.create_subprocess_exec(
            "docker", "create", *run_args, self._image,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)

        if process.returncode != 0:
            error_msg = stderr.decode("utf-8", errors="replace")
            logger.error("[DockerProvider] 创建容器失败 | error=%s", error_msg)
            env = IsolationEnvironment(
                env_id=env_id,
                level=IsolationLevel.CONTAINER,
                provider_type="docker",
                status=EnvironmentStatus.ERROR.value,
                context=context,
                provider_info={"error": error_msg},
                created_at=now.isoformat(),
                last_used_at=now.isoformat(),
            )
            self._environments[env_id] = env
            return env

        container_id = stdout.decode("utf-8", errors="replace").strip()

        # 启动容器
        process = await asyncio.create_subprocess_exec(
            "docker", "start", container_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(process.communicate(), timeout=15)

        env = IsolationEnvironment(
            env_id=env_id,
            level=IsolationLevel.CONTAINER,
            provider_type="docker",
            status=EnvironmentStatus.READY.value,
            context=context,
            provider_info={
                "container_id": container_id,
                "container_name": container_name,
                "image": self._image,
                "cpu_limit": self._cpu_limit,
                "memory_limit": self._memory_limit,
            },
            created_at=now.isoformat(),
            last_used_at=now.isoformat(),
        )

        self._environments[env_id] = env
        logger.info(
            "[DockerProvider] 容器已创建 | id=%s | name=%s",
            container_id[:12], container_name,
        )
        return env

    async def destroy_environment(self, env_id: str, success: bool = True) -> None:
        """销毁 Docker 容器环境。

        Args:
            env_id: 环境ID
            success: 任务是否成功完成
        """
        env = self._environments.get(env_id)
        if not env:
            return

        container_id = env.provider_info.get("container_id")
        if container_id:
            try:
                process = await asyncio.create_subprocess_exec(
                    "docker", "rm", "-f", container_id,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(process.communicate(), timeout=15)
                logger.info(
                    "[DockerProvider] 容器已销毁 | id=%s", container_id[:12],
                )
            except Exception as e:
                logger.warning(
                    "[DockerProvider] 销毁容器失败 | id=%s | error=%s",
                    container_id[:12], e,
                )

        self._environments.pop(env_id, None)

    async def execute_in_environment(
        self, env_id: str, operation: dict[str, Any],
    ) -> ExecutionResult:
        """在 Docker 容器中执行操作。

        Args:
            env_id: 环境ID
            operation: 操作描述字典

        Returns:
            执行结果
        """
        env = self._environments.get(env_id)
        if not env:
            return ExecutionResult(
                success=False, output=None, error=f"环境不存在: {env_id}",
            )

        container_id = env.provider_info.get("container_id")
        if not container_id:
            return ExecutionResult(
                success=False, output=None, error="容器ID不存在",
            )

        op_type = operation.get("type")

        if op_type == "command":
            return await self._exec_in_container(container_id, operation)
        elif op_type == "file_operation":
            return await self._file_op_in_container(container_id, operation)
        else:
            return ExecutionResult(
                success=False, output=None, error=f"不支持的操作类型: {op_type}",
            )

    async def get_environment_status(self, env_id: str) -> EnvironmentStatus:
        """获取容器环境状态。

        Args:
            env_id: 环境ID

        Returns:
            环境状态枚举值
        """
        env = self._environments.get(env_id)
        if not env:
            return EnvironmentStatus.STOPPED

        container_id = env.provider_info.get("container_id")
        if not container_id:
            return EnvironmentStatus.ERROR

        try:
            process = await asyncio.create_subprocess_exec(
                "docker", "inspect",
                "--format", "{{.State.Status}}",
                container_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
            status_str = stdout.decode("utf-8", errors="replace").strip()

            status_map = {
                "running": EnvironmentStatus.READY,
                "created": EnvironmentStatus.CREATING,
                "paused": EnvironmentStatus.BUSY,
                "exited": EnvironmentStatus.STOPPED,
                "dead": EnvironmentStatus.ERROR,
            }
            return status_map.get(status_str, EnvironmentStatus.ERROR)
        except Exception:
            return EnvironmentStatus.ERROR

    def _build_run_args(
        self, container_name: str, context: IsolationContext,
    ) -> list[str]:
        """构建 docker create 命令参数。

        Args:
            container_name: 容器名称
            context: 隔离上下文

        Returns:
            docker create 命令参数列表
        """
        args = [
            "--name", container_name,
            "--cpus", self._cpu_limit,
            "--memory", self._memory_limit,
            "--network", self._network_mode,
            "-dt",
        ]

        # 挂载工作目录
        if self._workspace_mount and context.workspace:
            args.extend(["-v", f"{context.workspace}:/workspace"])

        return args

    async def _ensure_image(self) -> None:
        """确保 Docker 镜像存在，不存在则拉取。"""
        try:
            process = await asyncio.create_subprocess_exec(
                "docker", "image", "inspect", self._image,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
            if process.returncode == 0:
                return

            logger.info("[DockerProvider] 拉取镜像 | image=%s", self._image)
            process = await asyncio.create_subprocess_exec(
                "docker", "pull", self._image,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=120)
        except Exception as e:
            logger.warning("[DockerProvider] 镜像检查/拉取失败 | error=%s", e)

    async def _exec_in_container(
        self, container_id: str, operation: dict[str, Any],
    ) -> ExecutionResult:
        """在容器中执行命令。

        Args:
            container_id: 容器ID
            operation: 操作描述字典

        Returns:
            执行结果
        """
        command = operation.get("command", "")
        timeout = operation.get("timeout", 30)
        working_dir = operation.get("working_dir", "/workspace")

        if not command:
            return ExecutionResult(success=False, output=None, error="命令不能为空")

        try:
            exec_args = [
                "docker", "exec",
                "-w", working_dir,
                container_id,
                "sh", "-c", command,
            ]

            process = await asyncio.create_subprocess_exec(
                *exec_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout,
            )

            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")
            return_code = process.returncode
            success = return_code == 0

            return ExecutionResult(
                success=success,
                output={
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "return_code": return_code,
                    "command": command,
                },
                error=None if success else stderr_text,
            )

        except TimeoutError:
            return ExecutionResult(
                success=False, output=None,
                error=f"命令执行超时（{timeout}秒）",
            )
        except Exception as e:
            return ExecutionResult(
                success=False, output=None,
                error=f"执行命令失败: {e}",
            )

    async def _file_op_in_container(
        self, container_id: str, operation: dict[str, Any],
    ) -> ExecutionResult:
        """在容器中执行文件操作。

        Args:
            container_id: 容器ID
            operation: 操作描述字典

        Returns:
            执行结果
        """
        op = operation.get("operation")
        path = operation.get("path")

        try:
            if op == "read":
                content = await self._read_container_file(container_id, path)
                return ExecutionResult(success=True, output=content)

            elif op == "write":
                content = operation.get("content", "")
                await self._write_container_file(container_id, path, content)
                return ExecutionResult(success=True, output=None)

            elif op == "exists":
                result = await self._exec_in_container(
                    container_id,
                    {"command": f"test -e '{path}' && echo 'yes' || echo 'no'"},
                )
                exists = "yes" in (result.output or {}).get("stdout", "")
                return ExecutionResult(success=True, output={"exists": exists})

            else:
                return ExecutionResult(
                    success=False, output=None,
                    error=f"不支持的文件操作: {op}",
                )

        except Exception as e:
            return ExecutionResult(
                success=False, output=None,
                error=f"文件操作失败: {e}",
            )

    async def _read_container_file(
        self, container_id: str, path: str,
    ) -> str:
        """从容器中读取文件内容。

        Args:
            container_id: 容器ID
            path: 容器内文件路径

        Returns:
            文件内容字符串
        """
        process = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "cat", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        return stdout.decode("utf-8", errors="replace")

    async def _write_container_file(
        self, container_id: str, path: str, content: str,
    ) -> None:
        """向容器中写入文件。

        Args:
            container_id: 容器ID
            path: 容器内文件路径
            content: 文件内容
        """
        # 确保目录存在
        dir_path = path.rsplit("/", 1)[0] if "/" in path else "."
        mkdir_process = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "mkdir", "-p", dir_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await mkdir_process.communicate()

        # 写入文件
        encoded = json.dumps(content)
        write_process = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id,
            "python3", "-c",
            f"import json; open('{path}','w').write(json.loads({encoded}))",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await write_process.communicate()
