"""Docker 容器隔离提供者"""

import asyncio
import json
import logging
import shlex
import shutil
from datetime import UTC, datetime
from typing import Any, ClassVar

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
    """Docker 容器隔离提供者。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化 Docker 提供者。"""
        self._config = config or {}
        self._image = self._config.get("image", "agentos:latest")
        self._cpu_limit = self._config.get("cpu_limit", "1.0")
        self._memory_limit = self._config.get("memory_limit", "512m")
        # swap 默认 = memory，禁止容器通过 swap 超额使用内存
        self._memory_swap = self._config.get("memory_swap", self._memory_limit)
        # pids 限制，防 fork 炸弹吃光系统进程表
        self._pids_limit = self._config.get("pids_limit", 100)
        self._network_mode = self._config.get("network_mode", "bridge")
        # 发布端口(system-issue #3 escape hatch)：把容器端口映射到宿主。
        # 容器内 agent 起的 server 若需被宿主/其它容器访问，配置如 ["8080:8080"]。
        # 注意：仅在非 host 网络下生效；bridge(默认)正好满足。server 还必须绑
        # 0.0.0.0（绑 127.0.0.1 时即使有 -p 映射，宿主仍打不通——已复现验证）。
        # 默认空：主方案是「容器内 server + 浏览器同处容器自测」，无需映射。
        self._publish_ports = list(self._config.get("publish_ports", []))
        self._workspace_mount = self._config.get("workspace_mount", True)
        self._environments: dict[str, IsolationEnvironment] = {}
        self._docker_available: bool | None = None
        # docker CLI 调用并发上限：防止多任务同时 create/start/exec/inspect
        # 打爆 daemon（尤其 WSL2 后端，daemon GIL/锁耗尽会假死冻死整个事件循环）。
        # 默认 4，可经 config.max_docker_concurrency 覆盖。
        import asyncio  # noqa: PLC0415

        self._max_docker_concurrency = int(self._config.get("max_docker_concurrency", 4))
        self._docker_sem = asyncio.Semaphore(self._max_docker_concurrency)
        # WSL docker 模式缓存：DOCKER_HOST 指向 WSL2/TCP 时为 True（需 Windows→WSL 路径转换）
        self._wsl_docker_cache: bool | None = None
        # 首次无镜像时自动构建：本地有 Dockerfile 则 docker build（BuildKit 缓存复用本机
        # 已下载的 apt/pip/playwright 包，二次构建秒级）；无 Dockerfile 则回退 docker pull。
        # 仓库根：src/isolation/providers/ 上溯 3 级（与 stream_handler.py parents[3] 同手法）。
        from pathlib import Path  # noqa: PLC0415

        self._repo_root = Path(__file__).resolve().parents[3]
        self._auto_build = bool(self._config.get("auto_build", True))
        _df = self._config.get("dockerfile_path", "docker/agentos/Dockerfile")
        _df_path = Path(_df)
        self._dockerfile_path = _df_path if _df_path.is_absolute() else self._repo_root / _df
        _ctx = self._config.get("build_context", ".")
        _ctx_path = Path(_ctx)
        self._build_context = _ctx_path if _ctx_path.is_absolute() else self._repo_root / _ctx
        self._build_timeout = float(self._config.get("build_timeout", 1800))
        # 构建锁：防多个协程并发触发 docker build（争抢同一 Dockerfile、浪费 CPU/磁盘）。
        # 本包首个 asyncio.Lock，配合二次 inspect 实现双重检查。
        self._build_lock = asyncio.Lock()

    def _is_wsl_docker(self) -> bool:
        """判断 docker daemon 是否在 WSL2 Linux 里（非 Docker Desktop）。"""
        import os  # noqa: PLC0415

        if self._wsl_docker_cache is not None:
            return self._wsl_docker_cache
        docker_host = os.environ.get("DOCKER_HOST", "")
        # 指向 tcp:// 或 unix:// = 远程/WSL daemon；npipe/空 = Docker Desktop
        self._wsl_docker_cache = bool(docker_host) and (
            docker_host.startswith("tcp://") or docker_host.startswith("unix://") or docker_host.startswith("ssh://")
        )
        return self._wsl_docker_cache

    def _resolve_mount_path(self, workspace: str | None) -> str:
        """把 Windows 工作空间路径转换为 docker daemon 能识别的路径。"""
        if not workspace:
            return ""
        if not self._is_wsl_docker():
            return workspace
        # Windows 路径 → WSL 路径: D:\myproject\xxx -> /mnt/d/myproject/xxx
        import re  # noqa: PLC0415

        m = re.match(r"^([A-Za-z]):[\\/](.*)$", workspace.replace("\\", "/"))
        if not m:
            return workspace  # 非标准 Windows 路径，原样返回
        drive = m.group(1).lower()
        rest = m.group(2)
        return f"/mnt/{drive}/{rest}"

    def get_level(self) -> IsolationLevel:
        """获取隔离级别。"""
        return IsolationLevel.CONTAINER

    async def is_available(self) -> tuple[bool, str | None]:
        """检查 Docker 提供者是否可用。"""
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

    async def _run_cmd(
        self,
        args: list[str],
        timeout: float = 30,
        env: dict[str, str] | None = None,
        stream_log: bool = False,
    ) -> tuple[int, bytes, bytes]:
        """统一执行命令（同步 subprocess + 线程池）。

        env：可选，非 None 时合并到当前 os.environ 上（如 {DOCKER_BUILDKIT: "1"}），
        未提到的环境变量保持继承宿主，避免误清空 PATH 等关键变量。

        stream_log：为 True 时改用 Popen 实时读取 stderr（BuildKit 进度走 stderr）
        转成 INFO 日志，让 docker build 这种分钟级长任务的进度可见。
        stdout 仍整体捕获返回；stderr 逐行打日志后也整体返回（供错误 tail）。
        """
        import os
        import subprocess as _sp  # noqa: PLC0415

        def _run() -> tuple[int, bytes, bytes]:
            run_env = None
            if env:
                run_env = {**os.environ, **env}
            if not stream_log:
                proc = _sp.run(args, capture_output=True, timeout=timeout, env=run_env)  # noqa: PLW1510
                return proc.returncode, proc.stdout, proc.stderr
            # 流式模式：Popen + 逐行读 stderr(BuildKit 进度)实时打日志。
            # stdout 整体捕获；stderr 一边打日志一边累积，供错误 tail。
            proc = _sp.Popen(  # noqa: S603
                args,
                stdout=_sp.PIPE,
                stderr=_sp.PIPE,
                env=run_env,
                text=False,
                bufsize=1,
            )
            collected_err: list[bytes] = []
            collected_out: list[bytes] = []
            # stdout=PIPE/stderr=PIPE 保证两者非 None，mypy 需显式收窄。
            stdout_pipe = proc.stdout
            stderr_pipe = proc.stderr
            if stdout_pipe is None or stderr_pipe is None:  # pragma: no cover
                raise RuntimeError("Popen 未建立管道")
            try:
                # 先读 stdout 全量(stderr 同步读会阻塞,因为 BuildKit 进度全在 stderr)。
                # 策略：循环轮询 stderr 逐行实时打日志，stdout 留到末尾 read。
                # 但 stdout/stderr 若同时填满管道缓冲会死锁——为避免,用线程读 stdout。
                import threading

                def _drain_stdout() -> None:
                    collected_out.append(stdout_pipe.read())

                t = threading.Thread(target=_drain_stdout, daemon=True)
                t.start()
                for raw_line in stderr_pipe:
                    collected_err.append(raw_line)
                    line = raw_line.decode("utf-8", errors="replace").rstrip()
                    if line:
                        logger.info("[docker build] %s", line)
                proc.wait(timeout=timeout)
                t.join(timeout=5)
            except _sp.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise
            return (
                proc.returncode if proc.returncode is not None else -1,
                b"".join(collected_out),
                b"".join(collected_err),
            )

        loop = asyncio.get_event_loop()
        async with self._docker_sem:
            return await loop.run_in_executor(None, _run)

    def _make_error_environment(
        self,
        context: IsolationContext,
        now: datetime,
        error_msg: str,
    ) -> IsolationEnvironment:
        """构造一个错误状态的容器环境并注册。"""
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
        """创建 Docker 容器环境。"""
        now = datetime.now(UTC)
        name = container_name

        # 工作空间挂载校验：容器隔离的工作空间只能来自任务数据解析出来的路径。
        # 缺失或宿主机路径不存在即为功能错误——拒绝创建无挂载容器，避免命令落到
        # 空/不存在的目录却以 exit 0 静默通过（表现为“目录看不到”）。
        # 不做任何“换个目录挂载”的变通：挂载点对不上本身就是错误。
        if self._workspace_mount:
            ws_path = self._resolve_mount_path(context.workspace)
            if not ws_path:
                logger.error(
                    "[DockerProvider] 拒绝创建容器：工作空间为空（无法挂载） | task=%s",
                    context.task_id,
                )
                return self._make_error_environment(
                    context,
                    now,
                    "工作空间为空，无法挂载到容器",
                )
            from pathlib import Path  # noqa: PLC0415

            # 路径校验：
            # - Docker Desktop(daemon 在 Windows): 校验原 Windows 路径
            # - WSL docker(daemon 在 Linux): Agent 在 Windows,无法用 Path() 校验
            #   WSL 路径(/mnt/d/...在 Windows 不存在),跳过宿主校验,交给 docker
            #   daemon 在挂载时校验(挂载不存在路径 docker 会报错)
            if not self._is_wsl_docker():
                check_path = context.workspace or ""
                if check_path and not Path(check_path).exists():
                    logger.error(
                        "[DockerProvider] 拒绝创建容器：工作空间路径不存在 | task=%s | path=%s",
                        context.task_id,
                        check_path,
                    )
                    return self._make_error_environment(
                        context,
                        now,
                        f"工作空间路径不存在: {check_path}",
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
            container_id[:12],
            container_name,
        )
        return env

    async def _create_one(self, run_args: list[str]) -> tuple[str, str]:
        """执行 docker create。"""
        rc, stdout, stderr = await self._run_cmd(["docker", "create", *run_args], timeout=30)
        if rc != 0:
            return "", stderr.decode("utf-8", errors="replace")
        return stdout.decode("utf-8", errors="replace").strip(), ""

    async def _start_one(self, container_id: str) -> tuple[bool, str]:
        """执行 docker start。"""
        rc, _, stderr = await self._run_cmd(["docker", "start", container_id], timeout=15)
        if rc == 0:
            return True, ""
        return False, stderr.decode("utf-8", errors="replace")

    async def _create_and_start(
        self,
        name: str,
        run_args: list[str],
    ) -> tuple[str, str]:
        """创建并启动容器；start 失败时删除卡死容器并重建重试一次。"""
        container_id, err = await self._create_one(run_args)
        if not container_id:
            return "", err

        ok, start_err = await self._start_one(container_id)
        if ok:
            return container_id, ""

        # start 失败：删除卡死容器（释放脏挂载路径）后重建重试一次
        logger.warning(
            "[DockerProvider] 容器启动失败，删除后重建重试 | name=%s | id=%s | error=%s",
            name,
            container_id[:12],
            start_err,
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
            name,
            start_err or err,
        )
        return "", start_err or err

    async def destroy_environment(self, env_id: str, success: bool = True) -> bool:
        """销毁 Docker 容器环境。

        返回是否真正从 docker 删除成功。runc 命名空间脱节的容器 docker rm -f 会
        失败（could not kill container ... did not receive an exit event）——
        旧实现不检查 rm 返回码，照报"已销毁"并 pop env 记录，造成"内存里以为
        删了、docker 里还在"的状态脱节，后续 _find_existing_container 又捡回
        坏容器复用。修复：rm 非零时不谎报、保留 env 记录（docker 里容器仍在，
        记录不能丢），返回 False。rm 成功才 pop 记录返回 True。
        """
        env = self._environments.get(env_id)
        if not env:
            return True  # 无可销毁记录，视为已成功（幂等）

        container_id = env.provider_info.get("container_id")
        if not container_id:
            self._environments.pop(env_id, None)
            return True

        try:
            rc, _, stderr = await self._run_cmd(
                ["docker", "rm", "-f", container_id], timeout=15
            )
        except Exception as e:
            logger.warning(
                "[DockerProvider] 销毁容器异常（保留记录） | id=%s | error=%s",
                container_id[:12], e,
            )
            return False

        if rc != 0:
            # rm 失败（典型：runc 卡死删不掉）——不谎报、不 pop 记录，
            # docker 里容器仍在，保留 env 记录供排查与上层活性探针兜底。
            err_tail = stderr.decode("utf-8", errors="replace")[-200:]
            logger.warning(
                "[DockerProvider] 销毁容器失败（docker 里仍在，保留记录） | id=%s | rc=%s | err=%s",
                container_id[:12], rc, err_tail,
            )
            return False

        logger.info("[DockerProvider] 容器已销毁 | id=%s", container_id[:12])
        self._environments.pop(env_id, None)
        return True

    async def execute_in_environment(
        self,
        env_id: str,
        operation: dict[str, Any],
    ) -> ExecutionResult:
        """在 Docker 容器中执行操作。"""
        env = self._environments.get(env_id)
        if not env:
            return ExecutionResult(
                success=False,
                output=None,
                error=f"环境不存在: {env_id}",
            )

        # 创建失败的环境（如工作空间未挂载校验未过）直接返回其错误，
        # 不再尝试执行——否则会以模糊的“容器ID不存在”掩盖真实原因。
        if env.status == EnvironmentStatus.ERROR.value:
            return ExecutionResult(
                success=False,
                output=None,
                error=env.provider_info.get("error", "容器环境处于错误状态"),
            )

        container_id = env.provider_info.get("container_id")
        if not container_id:
            return ExecutionResult(
                success=False,
                output=None,
                error="容器ID不存在",
            )

        op_type = operation.get("type")

        if op_type == "command":
            return await self._exec_in_container(container_id, operation)
        if op_type == "file_operation":
            return await self._file_op_in_container(container_id, operation)
        return ExecutionResult(
            success=False,
            output=None,
            error=f"不支持的操作类型: {op_type}",
        )

    async def get_environment_status(self, env_id: str) -> EnvironmentStatus:
        """获取容器环境状态。"""
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
        self,
        container_name: str,
        context: IsolationContext,
    ) -> list[str]:
        """构建 docker create 命令参数（含 IMAGE 与 COMMAND）。"""
        args = [
            "--name",
            container_name,
            # --init: 使用 tini 作为 PID 1，回收 docker exec 产生的僵尸进程，
            # 避免僵尸累积逼近 --pids-limit 导致容器被杀
            "--init",
            # 资源约束：单容器配额 = (宿主机一半) / max_environments，
            # 由 hardware_profile 动态计算，满载时所有容器总和 = 宿主机一半。
            "--cpus",
            self._cpu_limit,
            "--memory",
            self._memory_limit,
            # swap = memory，禁止容器通过 swap 超额使用内存
            "--memory-swap",
            self._memory_swap,
            # 进程数上限，防 fork 炸弹吃光系统进程表
            "--pids-limit",
            str(self._pids_limit),
            "--network",
            self._network_mode,
        ]
        # 发布端口(system-issue #3 escape hatch)：把容器端口映射到宿主。
        # 见 __init__ 中 self._publish_ports 的说明。
        for port_spec in self._publish_ports:
            args.extend(["-p", str(port_spec)])
        args += [
            "-i",
            "-t",
        ]

        # 挂载工作目录（WSL docker 时用转换后的 /mnt/x 路径）
        if self._workspace_mount and context.workspace:
            mount_src = self._resolve_mount_path(context.workspace)
            args.extend(["-v", f"{mount_src}:/workspace"])

        # IMAGE 必须在 COMMAND 之前（docker create 语法要求）
        args.append(self._image)

        # 常驻进程：让容器保持运行，等待 docker exec 注入命令。
        # 没有这个的话容器启动后立即退出（CMD 默认 sh 无 stdin → 退出）
        # → 后续 docker exec 报 "container is not running"。
        args.extend(["sh", "-c", "tail -f /dev/null"])

        return args

    # BuildKit 缓存损坏的特征标记（小写匹配）。
    # 命中任一即判定为缓存层损坏（非 Dockerfile 内容问题），可 prune 后重试自愈。
    # 真实样本：unpigz: skipping: <stdin>: corrupted -- crc32 mismatch
    _BUILDKIT_CORRUPTION_MARKERS: ClassVar[tuple[str, ...]] = (
        "unpigz",
        "corrupted",
        "crc32 mismatch",
        "checksum mismatch",
    )

    @classmethod
    def _is_buildkit_cache_corruption(cls, err: str | bytes) -> bool:
        """判断 build 失败 stderr 是否为 BuildKit 缓存损坏（可 prune 后重试自愈）。

        与 Dockerfile 语法错、网络超时、磁盘满等"真失败"区分——后者重试无意义，
        只会白等 build_timeout 秒并吃掉一次熔断计数。
        """
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace")
        low = err.lower()
        return any(marker in low for marker in cls._BUILDKIT_CORRUPTION_MARKERS)

    # runc 命名空间脱节（setns failure）的特征标记（小写匹配）。
    # 命中任一即判定为容器命名空间已死（可 destroy+recreate 后重试自愈）。
    # 真实样本：OCI runtime exec failed: ... error executing setns process: exit status 1
    # 这类故障下 docker inspect 仍报 running（元数据撒谎），pre-exec 健康检查
    # 无法发现，只能靠 exec stderr 的 runc 特征串识别。
    _NAMESPACE_DESYNC_MARKERS: ClassVar[tuple[str, ...]] = (
        "error executing setns",
        "oci runtime exec failed",
        "unable to start container process",
    )

    @classmethod
    def _is_namespace_desync_error(cls, err: str | bytes | None) -> bool:
        """判断 exec 失败是否为 runc 命名空间脱节（可 destroy+recreate 后重试自愈）。

        与命令本身的 stderr（command not found、编译错等）区分——后者重试无意义，
        前者重建容器通常即恢复。setns 脱节时 docker inspect 仍报 running，
        pre-exec 健康检查放行，错误只在 exec 时冒泡，故需在 exec stderr 上识别。

        None 视作无错误（success=True 时 result.error 可能为 None），安全返回 False。
        """
        if not err:
            return False
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace")
        low = err.lower()
        return any(marker in low for marker in cls._NAMESPACE_DESYNC_MARKERS)

    async def _ensure_image(self) -> None:
        """确保镜像存在：本地有就用，没有则自动构建（优先），再不行才 pull。

        与旧实现的关键差异：
        1. 构建优先于拉取——agentos:latest 是本地构建镜像（不在 Docker Hub），
           旧版 docker pull 是死代码；改用 docker build + BuildKit 缓存复用本机已下载的包。
        2. 构建失败必须 raise，不再 except 吞掉——让 manager.py 的熔断器
           （连续 _MAX_ENV_FAILURES 次抛 IsolationUnrecoverableError）接管，
           避免无限 next_llm 空转（每轮白等 docker create 30s × core retry 3 次 ≈ 4min）。
        3. 加锁 + 二次检查，防多协程并发触发构建。
        4. BuildKit 缓存损坏（unpigz/crc32 mismatch）自动 prune + 重试一次：
           这是瞬时故障，清掉损坏缓存后重试通常就成功，不该计入熔断计数。
        """
        # 快路径：镜像已存在，直接返回（无锁开销）。
        rc, _, _ = await self._run_cmd(["docker", "image", "inspect", self._image], timeout=5)
        if rc == 0:
            return

        # 镜像缺失：加锁后二次检查（双重检查），确保并发只构建一次。
        async with self._build_lock:
            rc, _, _ = await self._run_cmd(["docker", "image", "inspect", self._image], timeout=5)
            if rc == 0:
                return

            # 优先自动构建：本地有 Dockerfile 则 docker build。
            # DOCKER_BUILDKIT=1 是 Dockerfile 里 --mount=type=cache 生效的前提。
            if self._auto_build and self._dockerfile_path.is_file():
                logger.info(
                    "[DockerProvider] 本地无镜像，首次自动构建"
                    "（已启用 BuildKit 缓存，二次构建会复用本机已下载的包）"
                    " | image=%s | dockerfile=%s",
                    self._image,
                    self._dockerfile_path,
                )
                build_args = [
                    "docker",
                    "build",
                    "--progress=plain",
                    "-t",
                    self._image,
                    "-f",
                    str(self._dockerfile_path),
                    str(self._build_context),
                ]
                rc, _, stderr = await self._run_cmd(
                    build_args,
                    timeout=self._build_timeout,
                    env={"DOCKER_BUILDKIT": "1"},
                    stream_log=True,
                )
                if rc == 0:
                    logger.info("[DockerProvider] 镜像构建成功 | image=%s", self._image)
                    return
                err_tail = stderr.decode("utf-8", errors="replace")[-500:]

                # BuildKit 缓存损坏：prune 清掉损坏缓存后重试一次。
                # 这是瞬时故障（WSL2 异常关机/磁盘满/BuildKit 自身 bug），
                # 非 Dockerfile 内容问题，重试通常即成功，不该白吃一次熔断计数。
                if self._is_buildkit_cache_corruption(err_tail):
                    logger.warning(
                        "[DockerProvider] build 失败为 BuildKit 缓存损坏，清理缓存后重试一次 | markers_match=%s",
                        err_tail[:200],
                    )
                    await self._run_cmd(
                        ["docker", "builder", "prune", "-f"],
                        timeout=120,
                    )
                    rc, _, stderr = await self._run_cmd(
                        build_args,
                        timeout=self._build_timeout,
                        env={"DOCKER_BUILDKIT": "1"},
                        stream_log=True,
                    )
                    if rc == 0:
                        logger.info("[DockerProvider] 缓存清理后重试构建成功 | image=%s", self._image)
                        return
                    err_tail = stderr.decode("utf-8", errors="replace")[-500:]

                # raise 而非吞：让 manager.py 熔断器接管，避免无限空转。
                raise RuntimeError(
                    f"镜像 {self._image} 自动构建失败（docker build 退出码非 0）。"
                    f"请检查 docker build 输出。stderr tail={err_tail}"
                )

            # 回退：本地无 Dockerfile（如 pip 安装的包场景），尝试拉取。
            logger.info("[DockerProvider] 本地无 Dockerfile，尝试拉取镜像 | image=%s", self._image)
            rc, _, stderr = await self._run_cmd(["docker", "pull", self._image], timeout=120)
            if rc == 0:
                return
            err_tail = stderr.decode("utf-8", errors="replace")[-500:]
            raise RuntimeError(f"镜像 {self._image} 拉取失败，且本地无 Dockerfile 可构建: {err_tail}")

    async def _exec_in_container(
        self,
        container_id: str,
        operation: dict[str, Any],
    ) -> ExecutionResult:
        """在容器中执行命令。

        命令用 setsid 包裹自成进程组，超时时整组杀——防 cargo/rustc 后代变孤儿。
        旧实现 subprocess.run(timeout=...) 只杀本地 docker exec 客户端，容器内
        后代继续跑 → 僵尸堆积 → PidsLimit 耗尽 → runc setns 崩溃。
        """
        import subprocess as _sp  # noqa: PLC0415

        command = operation.get("command", "")
        timeout = operation.get("timeout", 30)
        working_dir = operation.get("working_dir", "/workspace")

        if not command:
            return ExecutionResult(success=False, output=None, error="命令不能为空")

        # 用 setsid 包裹命令自成进程组：cargo fork 的所有 rustc/cc 归同一 PGID，
        # 超时时可 docker exec kill -- -PGID 整组杀（防孙子进程变孤儿）。
        # setsid 子进程 PID 恒等于其 PGID（POSIX 保证），写标记文件记录供 kill 读。
        # shlex.quote 防命令内单引号破坏包裹。
        quoted = shlex.quote(command)
        marker = f"/tmp/.ao_pgid_{id(operation)}"  # 用 operation id 作唯一标记
        wrapped = (
            f"setsid sh -c {quoted} < /dev/null & "
            f"echo $! > {marker}; PGID=$!; wait $PGID"
        )

        exec_args = [
            "docker",
            "exec",
            "-w",
            working_dir,
            container_id,
            "sh",
            "-c",
            wrapped,
        ]

        try:
            rc, stdout, stderr = await self._run_cmd(exec_args, timeout=timeout)

            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")
            return_code = rc
            success = return_code == 0

            # 清理 PGID 标记文件（正常完成无需遗留）
            await self._cleanup_pgid_marker(container_id, marker)

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

        except (_sp.TimeoutExpired, TimeoutError):
            # subprocess.TimeoutExpired 继承 SubprocessError（非 TimeoutError），
            # 旧 except TimeoutError 接不住 → 落到 except Exception 误报"执行命令失败"。
            # 两者都触发容器内整组杀（防孤儿），返回正确的"超时"信息。
            await self._kill_container_process_group(container_id, marker)
            return ExecutionResult(
                success=False,
                output=None,
                error=f"命令执行超时（{timeout}秒）",
            )
        except Exception as e:
            # 其他异常也尽力清理容器内可能残留的进程组
            await self._kill_container_process_group(container_id, marker)
            return ExecutionResult(
                success=False,
                output=None,
                error=f"执行命令失败: {e}",
            )

    async def _cleanup_pgid_marker(self, container_id: str, marker: str) -> None:
        """清理 PGID 标记文件（best-effort，失败仅记 debug）。"""
        try:
            await self._run_cmd(
                ["docker", "exec", container_id, "rm", "-f", marker],
                timeout=5,
            )
        except Exception:
            pass  # 标记文件清理失败不影响主流程

    async def _kill_container_process_group(self, container_id: str, marker: str) -> None:
        """超时/异常时：读 PGID 标记并 docker exec kill -- -PGID 整组杀。

        防孙子进程（cargo fork 的 rustc/cc）变孤儿继续跑——这是 setns 故障和
        PID 耗尽的根因。读不到 PGID 时降级为杀容器内本次会话的非 PID1 进程。
        best-effort：失败仅记日志，不阻断主流程返回。
        """
        pgid = await self._read_pgid_marker(container_id, marker)
        if pgid:
            try:
                await self._run_cmd(
                    ["docker", "exec", container_id, "kill", "-9", "--", f"-{pgid}"],
                    timeout=10,
                )
                logger.info(
                    "[DockerProvider] 超时清理：已整组杀容器内进程组 | cid=%s | pgid=%s",
                    container_id[:12], pgid,
                )
            except Exception as e:
                logger.warning(
                    "[DockerProvider] 超时清理：整组杀失败 | cid=%s | pgid=%s | error=%s",
                    container_id[:12], pgid, e,
                )
            finally:
                await self._cleanup_pgid_marker(container_id, marker)
            return

        # 降级：无 PGID 时杀容器内除 PID1 外的所有进程（pkill，容器已装 procps）
        try:
            await self._run_cmd(
                ["docker", "exec", container_id, "pkill", "-9", "--parent", "1"],
                timeout=10,
            )
            logger.info(
                "[DockerProvider] 超时清理：降级 pkill 容器内残留进程 | cid=%s",
                container_id[:12],
            )
        except Exception as e:
            logger.warning(
                "[DockerProvider] 超时清理：降级 pkill 失败 | cid=%s | error=%s",
                container_id[:12], e,
            )

    async def _read_pgid_marker(self, container_id: str, marker: str) -> int | None:
        """读取 PGID 标记文件。失败返回 None。"""
        try:
            rc, stdout, _ = await self._run_cmd(
                ["docker", "exec", container_id, "cat", marker],
                timeout=5,
            )
            if rc != 0:
                return None
            text = stdout.decode("utf-8", errors="replace").strip()
            for token in text.split():
                if token.isdigit():
                    return int(token)
        except Exception:
            pass
        return None

    async def _file_op_in_container(
        self,
        container_id: str,
        operation: dict[str, Any],
    ) -> ExecutionResult:
        """在容器中执行文件操作。"""
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
                success=False,
                output=None,
                error=f"不支持的文件操作: {op}",
            )

        except Exception as e:
            return ExecutionResult(
                success=False,
                output=None,
                error=f"文件操作失败: {e}",
            )

    async def _read_container_file(
        self,
        container_id: str,
        path: str,
    ) -> str:
        """从容器中读取文件内容。"""
        _, stdout, _ = await self._run_cmd(["docker", "exec", container_id, "cat", path], timeout=10)
        return stdout.decode("utf-8", errors="replace")

    async def _write_container_file(
        self,
        container_id: str,
        path: str,
        content: str,
    ) -> None:
        """向容器中写入文件。"""
        # 确保目录存在
        dir_path = path.rsplit("/", 1)[0] if "/" in path else "."
        await self._run_cmd(["docker", "exec", container_id, "mkdir", "-p", dir_path], timeout=10)

        # 写入文件
        encoded = json.dumps(content)
        await self._run_cmd(
            [
                "docker",
                "exec",
                container_id,
                "python3",
                "-c",
                f"import json; open('{path}','w').write(json.loads({encoded}))",
            ],
            timeout=10,
        )

