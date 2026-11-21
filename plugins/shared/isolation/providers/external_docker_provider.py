"""外部指定容器隔离提供者（Terminal-Bench 参赛通道）。

收养「本系统之外创建并持有生命周期」的 Docker 容器（Harbor 起的任务容器），
bash 类工具经 `docker exec` 在其中执行。与 DockerProvider 的本质差异三条：

1. **不创建容器**：目标容器由外部（Harbor）事先起好；本 provider 只做
   docker inspect 收养验证——容器不存在/不可达时显式失败（fail-closed），
   绝不降级到别的 provider 或自建容器。
2. **销毁不删容器**：容器终态是 Terminal-Bench verifier 的判分对象，归
   Harbor 所有。destroy_environment 只解除本系统的环境登记，永不 docker rm。
3. **不参与自愈重建**：manager 的 setns/EIO 自愈语义=销毁+重建容器，对外部
   容器是破坏性的（会毁掉判分现场）。外部环境一律跳过自愈重建，错误如实
   回传（provider_type="external_docker" 是 manager 分流的判据）。

工作目录语义：宿主侧 bash 工具的 working_dir（Windows 宿主路径）经 SDK
ProcessManager 恒映射为容器内 `/workspace`，而 TB 任务镜像不带 /workspace——
收养时 `mkdir -p /workspace` 保证 exec -w 永不失败；绑定时声明的 `workdir`
（容器内任务真身路径，如 TB task workdir）作为 isolation.execute 操作的
默认工作目录并写入 provider_info（供 prompt 组装/审计），模型经指令 cd 到
任务目录工作。
"""

import asyncio
import json
import logging
import shlex
from datetime import UTC, datetime
from typing import Any, cast

from providers.base import IsolationProvider

from agentos_plugin_sdk.isolation_types import (
    EnvironmentStatus,
    ExecutionResult,
    IsolationContext,
    IsolationEnvironment,
    IsolationLevel,
)

logger = logging.getLogger(__name__)

#: provider_type 标识（manager 分流与审计用；"docker" 是本系统自建容器，
#: "external_docker" 是外部持有容器——两者销毁/自愈语义不同，不可混用）。
PROVIDER_TYPE = "external_docker"


class ExternalDockerProvider(IsolationProvider):
    """外部指定 Docker 容器提供者（收养不创建、销毁不删、不参与自愈重建）。"""

    def __init__(
        self,
        container_ref: str,
        workdir: str = "/workspace",
        config: dict[str, Any] | None = None,
    ) -> None:
        """初始化提供者。

        Args:
            container_ref: 外部容器名或 id（docker exec/inspect 均按此定位）。
            workdir: 绑定的容器内工作目录（isolation.execute 操作的默认 cwd；
                记录进 provider_info。bash 工具路径的 exec -w 恒为 /workspace，
                收养时确保存在）。
            config: 可选配置（max_docker_concurrency 覆盖并发上限）。
        """
        if not container_ref or not str(container_ref).strip():
            raise ValueError("container_ref 不能为空（外部容器通道必须有明确目标容器）")
        self._container_ref = str(container_ref).strip()
        self._workdir = workdir or "/workspace"
        self._config = config or {}
        self._environments: dict[str, IsolationEnvironment] = {}
        import asyncio as _asyncio  # noqa: PLC0415

        max_concurrency = int(self._config.get("max_docker_concurrency", 4))
        self._docker_sem = _asyncio.Semaphore(max_concurrency)

    # ── 基础属性 ────────────────────────────────────────────────

    @property
    def container_ref(self) -> str:
        """绑定的外部容器名或 id。"""
        return self._container_ref

    @property
    def workdir(self) -> str:
        """绑定的容器内工作目录。"""
        return self._workdir

    def get_level(self) -> IsolationLevel:
        """外部容器同属容器级隔离（级别语义与 DockerProvider 一致）。"""
        return IsolationLevel.CONTAINER

    # ── docker CLI 通道（与 DockerProvider 同构：同步 subprocess + 线程池）──

    async def _run_cmd(
        self,
        args: list[str],
        timeout: float = 30,
    ) -> tuple[int, bytes, bytes]:
        """执行 docker CLI 命令（同步 subprocess 出事件循环，信号量限并发）。"""
        import subprocess as _sp  # noqa: PLC0415

        def _run() -> tuple[int, bytes, bytes]:
            proc = _sp.run(args, capture_output=True, timeout=timeout)  # noqa: PLW1510
            return proc.returncode, proc.stdout, proc.stderr

        loop = asyncio.get_event_loop()
        async with self._docker_sem:
            return await loop.run_in_executor(None, _run)

    async def is_available(self) -> tuple[bool, str | None]:
        """Docker CLI/daemon 可用性（外部容器通道同样依赖本机 docker）。"""
        import asyncio  # noqa: PLC0415
        import shutil  # noqa: PLC0415

        # PATH 扫描是同步 I/O——async 面一律 to_thread 下放（async-blocking 闸）
        if not await asyncio.to_thread(shutil.which, "docker"):
            return False, "Docker CLI 未安装"
        try:
            rc, _, stderr = await self._run_cmd(
                ["docker", "version", "--format", "{{.Server.Version}}"], timeout=15
            )
            if rc != 0:
                err = stderr.decode("utf-8", errors="replace")
                return False, f"Docker daemon 未运行: {err}"
            return True, None
        except FileNotFoundError:
            return False, "Docker CLI 未找到"
        except TimeoutError:
            return False, "Docker 检查超时"
        except Exception as e:
            return False, f"Docker 检查失败: {e}"

    # ── 收养验证（bind 时与 create 时共用） ─────────────────────

    async def inspect_target(self) -> tuple[int, str, str, str]:
        """docker inspect 目标容器。

        Returns:
            (rc, container_id, container_name, stderr_tail)：rc==0 时 id/name
            非空（--format 取 Id/.Name）；rc!=0 时 id/name 为空串、stderr_tail
            携带原因（如 "No such container"）。
        """
        rc, stdout, stderr = await self._run_cmd(
            [
                "docker",
                "inspect",
                "--format",
                "{{.Id}}|{{.Name}}",
                self._container_ref,
            ],
            timeout=15,
        )
        err_tail = stderr.decode("utf-8", errors="replace").strip()
        if rc != 0:
            return rc, "", "", err_tail
        text = stdout.decode("utf-8", errors="replace").strip()
        container_id, _, raw_name = text.partition("|")
        # inspect .Name 带前导斜杠（Docker API 约定），剥掉得到裸容器名
        container_name = raw_name.lstrip("/") if raw_name else self._container_ref
        return 0, container_id, container_name, ""

    # ── 环境生命周期 ────────────────────────────────────────────

    def _make_error_environment(
        self,
        context: IsolationContext,
        now: datetime,
        error_msg: str,
    ) -> IsolationEnvironment:
        """构造错误状态环境并登记（调用方按 provider_error 显式失败）。"""
        env_id = f"external-{self._container_ref}"
        env = IsolationEnvironment(
            env_id=env_id,
            level=IsolationLevel.CONTAINER,
            provider_type=PROVIDER_TYPE,
            status=EnvironmentStatus.ERROR.value,
            context=context,
            provider_info={
                "error": error_msg,
                "container_ref": self._container_ref,
                "external": True,
            },
            created_at=now.isoformat(),
            last_used_at=now.isoformat(),
        )
        self._environments[env_id] = env
        return env

    async def create_environment(
        self,
        context: IsolationContext,
        container_name: str | None = None,
    ) -> IsolationEnvironment:
        """收养外部容器为隔离环境（幂等：重复调用复用同一登记）。

        容器不存在/daemon 不可达 → 返回 ERROR 状态环境（provider_info.error
        携带原因），**绝不降级**——调用方（isolation_guard）据此把要求容器
        隔离的工具标 blocked。
        """
        now = datetime.now(UTC)
        rc, container_id, resolved_name, err_tail = await self.inspect_target()
        if rc != 0:
            logger.error(
                "[ExternalDocker] 外部容器收养失败（显式失败，不降级）"
                " | ref=%s | err=%s",
                self._container_ref,
                err_tail,
            )
            return self._make_error_environment(context, now, f"外部容器不可达: {err_tail}")

        env_id = resolved_name or self._container_ref
        # bash 工具路径的 exec -w 恒落 /workspace（SDK ProcessManager 对非
        # POSIX working_dir 的映射约定），TB 任务镜像不带该目录——收养时确保
        # 存在（幂等 mkdir -p；失败只留痕不阻断收养：部分极简镜像 mkdir 失败
        # 时 exec 会带出明确错误，不静默吞）。
        ensure_rc, _, ensure_err = await self._run_cmd(
            ["docker", "exec", env_id, "mkdir", "-p", "/workspace"],
            timeout=15,
        )
        if ensure_rc != 0:
            logger.warning(
                "[ExternalDocker] 确保 /workspace 失败（exec 时会显式报错）"
                " | container=%s | err=%s",
                env_id,
                ensure_err.decode("utf-8", errors="replace")[:200],
            )

        env = IsolationEnvironment(
            env_id=env_id,
            level=IsolationLevel.CONTAINER,
            provider_type=PROVIDER_TYPE,
            status=EnvironmentStatus.READY.value,
            context=context,
            provider_info={
                "container_id": container_id,
                "container_name": env_id,
                "container_ref": self._container_ref,
                "workdir": self._workdir,
                "external": True,
                "image": "",
            },
            created_at=now.isoformat(),
            last_used_at=now.isoformat(),
        )
        self._environments[env_id] = env
        logger.info(
            "[ExternalDocker] 外部容器已收养 | name=%s | id=%s | workdir=%s",
            env_id,
            container_id[:12],
            self._workdir,
        )
        return env

    async def destroy_environment(self, env_id: str, success: bool = True) -> bool:
        """解除本系统环境登记；**永不删除外部容器**。

        容器终态归 Harbor/verifier 所有（TB 判分对象），本系统退出只做登记
        清理。恒返回 True（登记不存在 = 幂等成功）。
        """
        existed = self._environments.pop(env_id, None)
        logger.info(
            "[ExternalDocker] 外部环境登记已解除（容器保留，归外部所有）"
            " | env_id=%s | had_registration=%s",
            env_id,
            existed is not None,
        )
        return True

    async def get_environment_status(self, env_id: str) -> EnvironmentStatus:
        """按 docker inspect 实时状态映射环境状态。"""
        env = self._environments.get(env_id)
        target = (env.provider_info.get("container_name") if env else None) or env_id
        try:
            rc, stdout, _ = await self._run_cmd(
                ["docker", "inspect", "--format", "{{.State.Status}}", target],
                timeout=10,
            )
            if rc != 0:
                return EnvironmentStatus.ERROR
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

    # ── 执行面 ──────────────────────────────────────────────────

    async def execute_in_environment(
        self,
        env_id: str,
        operation: dict[str, Any],
    ) -> ExecutionResult:
        """在外部容器中执行操作（协议与 DockerProvider 一致）。

        支持 operation.type = "command"（command/timeout/working_dir）与
        "file_operation"（operation=read/write/exists + path/content）。
        working_dir 缺省用绑定 workdir（容器内任务真身路径）。
        """
        env = self._environments.get(env_id)
        if not env:
            return ExecutionResult(
                success=False,
                output=None,
                error=f"环境不存在: {env_id}",
            )
        if env.status == EnvironmentStatus.ERROR.value:
            return ExecutionResult(
                success=False,
                output=None,
                error=env.provider_info.get("error", "外部容器环境处于错误状态"),
            )

        op_type = operation.get("type")
        if op_type == "command":
            return await self._exec_in_container(env_id, operation)
        if op_type == "file_operation":
            return await self._file_op_in_container(env_id, operation)
        return ExecutionResult(
            success=False,
            output=None,
            error=f"不支持的操作类型: {op_type}",
        )

    async def _exec_in_container(
        self,
        container_name: str,
        operation: dict[str, Any],
    ) -> ExecutionResult:
        """docker exec 执行命令（working_dir 缺省=绑定 workdir）。"""
        import subprocess as _sp  # noqa: PLC0415

        command = operation.get("command", "")
        timeout = operation.get("timeout", 30)
        working_dir = str(operation.get("working_dir") or self._workdir)

        if not command:
            return ExecutionResult(success=False, output=None, error="命令不能为空")

        exec_args = [
            "docker",
            "exec",
            "-w",
            working_dir,
            container_name,
            "sh",
            "-c",
            command,
        ]
        try:
            rc, stdout, stderr = await self._run_cmd(exec_args, timeout=float(timeout))
            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")
            success = rc == 0
            return ExecutionResult(
                success=success,
                output={
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "return_code": rc,
                    "command": command,
                },
                error=None if success else (stderr_text or stdout_text or f"exit code {rc}"),
            )
        except (_sp.TimeoutExpired, TimeoutError):
            return ExecutionResult(
                success=False,
                output=None,
                error=f"命令执行超时（{timeout}秒）",
            )
        except Exception as e:
            return ExecutionResult(
                success=False,
                output=None,
                error=f"执行命令失败: {e}",
            )

    async def _file_op_in_container(
        self,
        container_name: str,
        operation: dict[str, Any],
    ) -> ExecutionResult:
        """容器内文件操作（read/write/exists；协议与 DockerProvider 一致）。"""
        op = operation.get("operation")
        path = cast(str, operation.get("path"))

        try:
            if op == "read":
                content = await self._read_container_file(container_name, path)
                return ExecutionResult(success=True, output=content)

            if op == "write":
                content = operation.get("content", "")
                await self._write_container_file(container_name, path, content)
                return ExecutionResult(success=True, output=None)

            if op == "exists":
                result = await self._exec_in_container(
                    container_name,
                    {"command": f"test -e {shlex.quote(path)} && echo yes || echo no"},
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

    async def _read_container_file(self, container_name: str, path: str) -> str:
        """docker exec cat 读文件。"""
        _, stdout, stderr = await self._run_cmd(
            ["docker", "exec", container_name, "cat", path], timeout=10
        )
        if stdout == b"" and stderr:
            raise RuntimeError(stderr.decode("utf-8", errors="replace"))
        return stdout.decode("utf-8", errors="replace")

    async def _write_container_file(self, container_name: str, path: str, content: str) -> None:
        """向容器写文件（path/content 走独立 argv，杜绝注入）。"""
        dir_path = path.rsplit("/", 1)[0] if "/" in path else "."
        await self._run_cmd(
            ["docker", "exec", container_name, "mkdir", "-p", dir_path], timeout=10
        )
        encoded = json.dumps(content)
        rc, _, stderr = await self._run_cmd(
            [
                "docker",
                "exec",
                container_name,
                "python3",
                "-c",
                "import json,sys; open(sys.argv[1],'w').write(json.loads(sys.argv[2]))",
                path,
                encoded,
            ],
            timeout=10,
        )
        if rc != 0:
            raise RuntimeError(
                "容器内写文件失败: " + stderr.decode("utf-8", errors="replace")[:200]
            )
