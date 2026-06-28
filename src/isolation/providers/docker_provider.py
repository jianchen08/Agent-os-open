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
                - image: 基础镜像名称（默认 agentos:latest）
                - cpu_limit: CPU 限制（默认 "1.0"）
                - memory_limit: 内存限制（默认 "512m"）
                - memory_swap: swap 限制（默认等于 memory_limit，禁止 swap 放大）
                - pids_limit: 进程数上限（默认 100，防 fork 炸弹）
                - network_mode: 网络模式（默认 "bridge"）
                - workspace_mount: 是否挂载工作目录（默认 True）
        """
        self._config = config or {}
        self._image = self._config.get("image", "agentos:latest")
        self._cpu_limit = self._config.get("cpu_limit", "1.0")
        self._memory_limit = self._config.get("memory_limit", "512m")
        # swap 默认 = memory，禁止容器通过 swap 超额使用内存
        self._memory_swap = self._config.get("memory_swap", self._memory_limit)
        # pids 限制，防 fork 炸弹吃光系统进程表
        self._pids_limit = self._config.get("pids_limit", 100)
        self._network_mode = self._config.get("network_mode", "bridge")
        self._workspace_mount = self._config.get("workspace_mount", True)
        self._environments: dict[str, IsolationEnvironment] = {}
        self._docker_available: bool | None = None
        # docker CLI 调用并发上限：防止多任务同时 create/start/exec/inspect
        # 打爆 daemon（尤其 WSL2 后端，daemon GIL/锁耗尽会假死冻死整个事件循环）。
        # 默认 4，可经 config.max_docker_concurrency 覆盖。
        import asyncio  # noqa: PLC0415
        self._max_docker_concurrency = int(self._config.get("max_docker_concurrency", 4))
        self._docker_sem = asyncio.Semaphore(self._max_docker_concurrency)

    def get_level(self) -> IsolationLevel:
        """获取隔离级别。"""
        return IsolationLevel.CONTAINER

    async def is_available(self) -> tuple[bool, str | None]:
        """检查 Docker 提供者是否可用。

        检查 Docker CLI 是否安装且 daemon 是否运行。
        用同步 subprocess + 线程池执行，避免 asyncio.create_subprocess_exec
        在 Windows 上的静默失败问题（Python 3.12/3.14 已知问题）。
        """
        if not shutil.which("docker"):
            return False, "Docker CLI 未安装"

        try:
            # 用同步 subprocess 替代 asyncio subprocess（Windows 兼容性）
            import subprocess as _sp  # noqa: PLC0415
            loop = asyncio.get_event_loop()
            proc = await loop.run_in_executor(
                None,
                lambda: _sp.run(  # noqa: PLW1510
                    ["docker", "version", "--format", "{{.Server.Version}}"],
                    capture_output=True,
                    timeout=15,
                ),
            )
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
                return False, f"Docker daemon 未运行: {err}"
            self._docker_available = True
            return True, None
        except FileNotFoundError:
            return False, "Docker CLI 未找到"
        except TimeoutError:
            return False, "Docker 检查超时"
        except Exception as e:
            return False, f"Docker 检查失败: {e}"

    async def _run_cmd(self, args: list[str], timeout: float = 30) -> tuple[int, bytes, bytes]:
        """统一执行命令（同步 subprocess + 线程池）。

        替代 asyncio.create_subprocess_exec，避免 Windows asyncio subprocess 静默失败。
        通过全局信号量限制并发 docker CLI 调用数，防止连接风暴压垮 daemon
        （多任务同时 create/start/exec 会让 daemon 锁耗尽，触发假死）。

        Args:
            args: 命令参数列表（如 ["docker", "exec", container, "sh", "-c", cmd]）
            timeout: 超时秒数

        Returns:
            (returncode, stdout_bytes, stderr_bytes)
        """
        import subprocess as _sp  # noqa: PLC0415

        def _run():
            proc = _sp.run(args, capture_output=True, timeout=timeout)  # noqa: PLW1510
            return proc.returncode, proc.stdout, proc.stderr

        loop = asyncio.get_event_loop()
        async with self._docker_sem:
            return await loop.run_in_executor(None, _run)

    def _make_error_environment(
        self,
        context: IsolationContext,
        now: datetime,
        error_msg: str,
    ) -> IsolationEnvironment:
        """构造一个错误状态的容器环境并注册。

        容器创建失败、或工作空间挂载校验失败时复用：无 container_id，
        使用带 task_id 的合成 env_id 兜底；后续 execute_in_environment
        会据 status=ERROR 直接返回 provider_info["error"]，不再尝试执行。
        """
        env_id = f"docker-{context.task_id}"
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

    async def create_environment(
        self,
        context: IsolationContext,
        container_name: str,
    ) -> IsolationEnvironment:
        """创建 Docker 容器环境。

        Args:
            context: 隔离上下文
            container_name: 容器名称，由调用方统一确定（保证可复用/可销毁）。

        Returns:
            创建的隔离环境实例
        """
        now = datetime.now(UTC)
        name = container_name

        # 工作空间挂载校验：容器隔离的工作空间只能来自任务数据解析出来的路径。
        # 缺失或宿主机路径不存在即为功能错误——拒绝创建无挂载容器，避免命令落到
        # 空/不存在的目录却以 exit 0 静默通过（表现为“目录看不到”）。
        # 不做任何“换个目录挂载”的变通：挂载点对不上本身就是错误。
        # BUG-FIX-fix_20260625_isolated_no_workspace
        if self._workspace_mount:
            ws_path = context.workspace
            if not ws_path:
                logger.error(
                    "[DockerProvider] 拒绝创建容器：工作空间为空（无法挂载） | task=%s",
                    context.task_id,
                )
                return self._make_error_environment(
                    context, now, "工作空间为空，无法挂载到容器",
                )
            from pathlib import Path  # noqa: PLC0415
            if not Path(ws_path).exists():
                logger.error(
                    "[DockerProvider] 拒绝创建容器：工作空间路径不存在 | task=%s | path=%s",
                    context.task_id, ws_path,
                )
                return self._make_error_environment(
                    context, now, f"工作空间路径不存在: {ws_path}",
                )

        # 构建 docker create 命令参数（_build_run_args 已含 IMAGE 与 COMMAND）
        run_args = self._build_run_args(name, context)

        # 拉取镜像（如果本地不存在）
        await self._ensure_image()

        # 创建并启动容器；start 失败时删除卡死容器并重建重试一次（见 _create_and_start）
        container_id, error_msg = await self._create_and_start(name, run_args)
        if not container_id:
            logger.error("[DockerProvider] 容器创建/启动失败 | error=%s", error_msg)
            return self._make_error_environment(context, now, error_msg)

        # 统一使用 container_name 作为 env_id（由 workspace 决定），
        # 保证同一 workspace 无论容器是否重建，env_id 始终一致。
        env_id = container_name

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

    async def _create_one(self, run_args: list[str]) -> tuple[str, str]:
        """执行 docker create。

        Args:
            run_args: docker create 参数（含 IMAGE 与 COMMAND）

        Returns:
            (container_id, error)。成功时 error 为空；失败时 container_id 为空。
        """
        rc, stdout, stderr = await self._run_cmd(
            ["docker", "create", *run_args], timeout=30
        )
        if rc != 0:
            return "", stderr.decode("utf-8", errors="replace")
        return stdout.decode("utf-8", errors="replace").strip(), ""

    async def _start_one(self, container_id: str) -> tuple[bool, str]:
        """执行 docker start。

        Returns:
            (是否成功, error)。成功时 error 为空。
        """
        rc, _, stderr = await self._run_cmd(
            ["docker", "start", container_id], timeout=15
        )
        if rc == 0:
            return True, ""
        return False, stderr.decode("utf-8", errors="replace")

    async def _create_and_start(
        self, name: str, run_args: list[str],
    ) -> tuple[str, str]:
        """创建并启动容器；start 失败时删除卡死容器并重建重试一次。

        Docker Desktop (WSL2) 的 bind-mount 代理路径在 start 时才建立，
        频繁创建/删除容器会使该路径进入脏状态（mkdir: file exists），
        导致 start 失败、容器卡在 created(exit 128)。删除卡死容器后脏
        路径常自愈，故重试一次。仍失败时清理并返回空 container_id，
        由调用方标记 ERROR 环境（不再误标 READY 触发后续 "is not running"）。
        整机反复卡死时需运维手段：wsl --shutdown 后重启 Docker Desktop。

        Args:
            name: 容器名（日志用）
            run_args: docker create 参数

        Returns:
            (container_id, error)。成功时 error 为空；失败时 container_id 为空。
        """
        container_id, err = await self._create_one(run_args)
        if not container_id:
            return "", err

        ok, start_err = await self._start_one(container_id)
        if ok:
            return container_id, ""

        # start 失败：删除卡死容器（释放脏挂载路径）后重建重试一次
        logger.warning(
            "[DockerProvider] 容器启动失败，删除后重建重试 | name=%s | id=%s | error=%s",
            name, container_id[:12], start_err,
        )
        await self._run_cmd(["docker", "rm", "-f", container_id], timeout=15)

        container_id, err = await self._create_one(run_args)
        if not container_id:
            return "", err
        ok, start_err = await self._start_one(container_id)
        if ok:
            return container_id, ""

        # 重试仍失败：清理并返回错误
        await self._run_cmd(["docker", "rm", "-f", container_id], timeout=15)
        logger.error(
            "[DockerProvider] 容器启动重试仍失败 | name=%s | error=%s",
            name, start_err or err,
        )
        return "", start_err or err

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
                await self._run_cmd(["docker", "rm", "-f", container_id], timeout=15)
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

        # 创建失败的环境（如工作空间未挂载校验未过）直接返回其错误，
        # 不再尝试执行——否则会以模糊的“容器ID不存在”掩盖真实原因。
        if env.status == EnvironmentStatus.ERROR.value:
            return ExecutionResult(
                success=False, output=None,
                error=env.provider_info.get("error", "容器环境处于错误状态"),
            )

        container_id = env.provider_info.get("container_id")
        if not container_id:
            return ExecutionResult(
                success=False, output=None, error="容器ID不存在",
            )

        op_type = operation.get("type")

        if op_type == "command":
            return await self._exec_in_container(container_id, operation)
        if op_type == "file_operation":
            return await self._file_op_in_container(container_id, operation)
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
            rc, stdout, _ = await self._run_cmd(
                ["docker", "inspect", "--format", "{{.State.Status}}", container_id],
                timeout=5,
            )
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
        """构建 docker create 命令参数（含 IMAGE 与 COMMAND）。

        Docker CLI 格式：docker create [OPTIONS] IMAGE [COMMAND] [ARG...]
        IMAGE 必须出现在 COMMAND 之前，否则 Docker 会把 COMMAND 的首个
        token（如 "sh"）误判为镜像名去拉取，导致 "Unable to find image
        'sh:latest'" 错误。

        Args:
            container_name: 容器名称
            context: 隔离上下文

        Returns:
            docker create 命令参数列表（OPTIONS + IMAGE + COMMAND）
        """
        args = [
            "--name", container_name,
            # --init: 使用 tini 作为 PID 1，回收 docker exec 产生的僵尸进程，
            # 避免僵尸累积逼近 --pids-limit 导致容器被杀
            "--init",
            # 资源约束：单容器配额 = (宿主机一半) / max_environments，
            # 由 hardware_profile 动态计算，满载时所有容器总和 = 宿主机一半。
            "--cpus", self._cpu_limit,
            "--memory", self._memory_limit,
            # swap = memory，禁止容器通过 swap 超额使用内存
            "--memory-swap", self._memory_swap,
            # 进程数上限，防 fork 炸弹吃光系统进程表
            "--pids-limit", str(self._pids_limit),
            "--network", self._network_mode,
            "-i", "-t",
        ]

        # 挂载工作目录
        if self._workspace_mount and context.workspace:
            args.extend(["-v", f"{context.workspace}:/workspace"])

        # IMAGE 必须在 COMMAND 之前（docker create 语法要求）
        args.append(self._image)

        # 常驻进程：让容器保持运行，等待 docker exec 注入命令。
        # 没有这个的话容器启动后立即退出（CMD 默认 sh 无 stdin → 退出）
        # → 后续 docker exec 报 "container is not running"。
        args.extend(["sh", "-c", "tail -f /dev/null"])

        return args

    async def _ensure_image(self) -> None:
        """确保 Docker 镜像存在，不存在则拉取。"""
        import subprocess as _sp  # noqa: PLC0415

        try:
            rc, _, _ = await self._run_cmd(
                ["docker", "image", "inspect", self._image], timeout=5
            )
            if rc == 0:
                return

            logger.info("[DockerProvider] 拉取镜像 | image=%s", self._image)
            await self._run_cmd(["docker", "pull", self._image], timeout=120)

            # 镜像拉取后清理悬挂镜像（旧版被替换后的 <none>:<none>）
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: _sp.run(  # noqa: PLW1510
                    ["docker", "image", "prune", "-f"],
                    capture_output=True, timeout=30,
                ),
            )
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

            rc, stdout, stderr = await self._run_cmd(exec_args, timeout=timeout)

            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")
            return_code = rc
            success = return_code == 0

            return ExecutionResult(
                success=success,
                output={
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "return_code": return_code,
                    "command": command,
                },
                error=None if success else (stderr_text or stdout_text or f"exit code {return_code}"),
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

            if op == "write":
                content = operation.get("content", "")
                await self._write_container_file(container_id, path, content)
                return ExecutionResult(success=True, output=None)

            if op == "exists":
                result = await self._exec_in_container(
                    container_id,
                    {"command": f"test -e '{path}' && echo 'yes' || echo 'no'"},
                )
                exists = "yes" in (result.output or {}).get("stdout", "")
                return ExecutionResult(success=True, output={"exists": exists})

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
        _, stdout, _ = await self._run_cmd(
            ["docker", "exec", container_id, "cat", path], timeout=10
        )
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
        await self._run_cmd(
            ["docker", "exec", container_id, "mkdir", "-p", dir_path], timeout=10
        )

        # 写入文件
        encoded = json.dumps(content)
        await self._run_cmd(
            [
                "docker", "exec", container_id,
                "python3", "-c",
                f"import json; open('{path}','w').write(json.loads({encoded}))",
            ],
            timeout=10,
        )
