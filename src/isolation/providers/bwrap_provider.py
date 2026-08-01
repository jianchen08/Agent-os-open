"""BwrapProvider：基于 bubblewrap (bwrap) 的轻量沙箱隔离提供者。

对称于 DockerProvider，提供 CONTAINER 级隔离，但无需 docker daemon：
- 创建：spawn `bwrap ... sh -c "tail -f /dev/null"`，PID 1 常驻（镜像 docker 模型）。
- 执行：用 `nsenter` 进入 PID 1 的 namespace 注入命令（镜像 docker exec）。
- 销毁：kill PID 1（SIGTERM → 2s 超时 → SIGKILL）。

设计文档：docs/working/design/bwrap_isolation_migration_plan.md
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import shutil
from datetime import datetime
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

# 危险环境变量前缀/全名：注入子进程会破坏沙箱隔离（LD_PRELOAD 劫持、密钥泄漏等）。
# SANDBOX_* 前缀作为显式 passthrough 保留。
_DANGEROUS_ENV_FULL = frozenset({
    "LD_AUDIT",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "SSLKEYLOGFILE",
    "SSH_AUTH_SOCK",
})
_DANGEROUS_ENV_PREFIXES = ("LD_", "DYLD_", "DYLD_", "_API_KEY", "API_KEY")
# 显式放行前缀（即便命中危险规则也保留）
_SAFE_ENV_PREFIXES = ("SANDBOX_",)


def _is_dangerous_env_key(key: str) -> bool:
    """判断环境变量名是否危险（应清除）。"""
    if any(key.startswith(p) for p in _SAFE_ENV_PREFIXES):
        return False
    if key in _DANGEROUS_ENV_FULL:
        return True
    upper = key.upper()
    return (
        any(upper.startswith(p) for p in ("LD_", "DYLD_"))
        or upper.endswith("_API_KEY")
        or key.endswith("API_KEY")
    )


class BwrapProvider(IsolationProvider):
    """bubblewrap 轻量沙箱 provider（CONTAINER 级）。

    进程模型：bwrap 启动后 PID 1 = `tail -f /dev/null` 常驻，所有命名空间
    （user/pid/net）绑定在该进程上。命令注入靠 nsenter 进入其 namespace。
    """

    def __init__(self) -> None:
        # env_id -> IsolationEnvironment（含 provider_info.bwrap_pid）
        self._environments: dict[str, IsolationEnvironment] = {}

    def get_level(self) -> IsolationLevel:
        """BwrapProvider 对应 CONTAINER 级。"""
        return IsolationLevel.CONTAINER

    async def is_available(self) -> tuple[bool, str | None]:
        """检查 bwrap 是否在 PATH 上。"""
        if shutil.which("bwrap") is None:
            return False, "bwrap not found on PATH (install bubblewrap)"
        return True, None

    # ------------------------------------------------------------------
    # spawn seam（测试替换点）：封装真实 bwrap 进程启动
    # ------------------------------------------------------------------

    async def _spawn_bwrap(
        self, argv: list[str], env: dict[str, str]
    ) -> asyncio.subprocess.Process:
        """启动 bwrap 长跑进程（PID 1 = tail -f /dev/null）。

        生产路径用 asyncio.create_subprocess_exec；测试替换此方法以避免真 spawn。
        """
        return await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,  # = bwrap --new-session 的进程级等价
        )

    # ------------------------------------------------------------------
    # argv 构造（纯逻辑，无副作用）
    # ------------------------------------------------------------------

    def _build_argv(self, workspace: str, name: str) -> list[str]:
        """构造 Linux bwrap 启动 argv。

        Args:
            workspace: 宿主工作目录，绑定到容器内 /workspace。
            name: 容器名（同时派生 hostname：agentos-<name>）。

        Returns:
            bwrap 命令 argv，最后一项为常驻 PID 1（tail -f /dev/null）。
        """
        return [
            "bwrap",
            # 只读绑定整个根文件系统（沙箱内能看到系统二进制，但不能改）
            "--ro-bind", "/", "/",
            # 工作目录读写绑定（docker_provider 约定挂载点 /workspace）
            "--bind", workspace, "/workspace",
            # 全新 tmpfs / proc / dev（隔离进程视图与临时文件）
            "--tmpfs", "/tmp",
            "--proc", "/proc",
            "--dev-bind", "/dev/null", "/dev/null",
            # 命名空间隔离：user（无特权）/ pid（独立进程树）/ net-try（尽力隔离网络）
            "--unshare-user",
            "--unshare-pid",
            "--unshare-net-try",
            # hostname 派生自容器名
            "--hostname", f"agentos-{name}",
            # 孤儿清理（父进程死则 bwrap 死）+ 新会话（与控制终端解耦，防信号泄漏）
            "--die-with-parent",
            "--new-session",
            # ★ 常驻 PID 1：tail -f /dev/null 永不退出，命名空间随之常驻
            "sh", "-c", "tail -f /dev/null",
        ]

    # ------------------------------------------------------------------
    # 环境变量清洗（纯逻辑）
    # ------------------------------------------------------------------

    def _clear_dangerous_env(self, env: dict[str, str]) -> dict[str, str]:
        """清除危险环境变量，保留 SANDBOX_* passthrough。

        LD_PRELOAD/DYLD_* 可劫持子进程；*_API_KEY/SSLKEYLOGFILE 会泄漏密钥。
        SANDBOX_* 前缀是显式设计给沙箱配置的 passthrough，必须保留。
        """
        return {k: v for k, v in env.items() if not _is_dangerous_env_key(k)}

    # ------------------------------------------------------------------
    # _run_cmd seam：nsenter 命令注入的执行封装（测试替换点）
    # 与 DockerProvider._run_cmd 同签名，便于复用 mock 惯例。
    # ------------------------------------------------------------------

    async def _run_cmd(
        self, args: list[str], timeout: float = 30
    ) -> tuple[int, bytes, bytes]:
        """执行命令（默认真实 subprocess；测试替换此方法即可 mock）。

        与 DockerProvider._run_cmd 同签名同约定（见 docker_provider.py:135）。
        """
        import subprocess as _sp  # noqa: PLC0415

        proc = _sp.run(args, capture_output=True, timeout=timeout)  # noqa: S603
        return proc.returncode, proc.stdout, proc.stderr

    # ------------------------------------------------------------------
    # IsolationProvider 抽象方法实现
    # ------------------------------------------------------------------

    async def create_environment(
        self,
        context: IsolationContext,
        container_name: str | None = None,
    ) -> IsolationEnvironment:
        """创建 bwrap 常驻沙箱环境。

        spawn `bwrap ... sh -c "tail -f /dev/null"`，PID 1 常驻。
        env_id == container_name（与 docker_provider 隐式契约一致）。
        """
        now = datetime.now()
        name = container_name or f"bwrap-{context.task_id}"
        workspace = context.workspace or os.getcwd()

        argv = self._build_argv(workspace=workspace, name=name)
        env = self._clear_dangerous_env(dict(os.environ))

        try:
            proc = await self._spawn_bwrap(argv, env)
        except Exception as exc:  # noqa: BLE001
            logger.error("[BwrapProvider] spawn 失败 | name=%s | error=%s", name, exc)
            return IsolationEnvironment(
                env_id=name,
                level=IsolationLevel.CONTAINER,
                provider_type="bwrap",
                status=EnvironmentStatus.ERROR.value,
                context=context,
                provider_info={"provider_kind": "bwrap", "error": str(exc)},
                created_at=now.isoformat(),
            )

        bwrap_pid = proc.pid
        env_obj = IsolationEnvironment(
            env_id=name,
            level=IsolationLevel.CONTAINER,
            provider_type="bwrap",
            status=EnvironmentStatus.READY.value,
            context=context,
            provider_info={
                "provider_kind": "bwrap",
                "bwrap_pid": bwrap_pid,
                "argv": argv,
                "platform": "linux",
            },
            created_at=now.isoformat(),
            last_used_at=now.isoformat(),
        )
        self._environments[name] = env_obj
        logger.info("[BwrapProvider] 沙箱已创建 | name=%s | pid=%s", name, bwrap_pid)
        return env_obj

    async def destroy_environment(self, env_id: str, success: bool = True) -> bool:
        """销毁沙箱：SIGTERM 等 2s 超时后 SIGKILL（镜像 docker_provider 语义）。

        bwrap 进程被杀后，其绑定的所有命名空间随之释放。
        """
        env = self._environments.get(env_id)
        if env is None:
            return False
        pid = env.provider_info.get("bwrap_pid")
        if not pid:
            self._environments.pop(env_id, None)
            return False

        killed = await self._terminate_pid(pid)
        self._environments.pop(env_id, None)
        logger.info(
            "[BwrapProvider] 沙箱已销毁 | name=%s | pid=%s | killed=%s",
            env_id, pid, killed,
        )
        return True

    async def _terminate_pid(self, pid: int) -> bool:
        """SIGTERM → 等 2s → SIGKILL。返回是否最终被杀掉。"""
        import os as _os  # noqa: PLC0415

        try:
            _os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True  # 已不存在
        except Exception:  # noqa: BLE001
            return False

        try:
            await asyncio.wait_for(_wait_pid_exit(pid), timeout=2.0)
            return True
        except asyncio.TimeoutError:
            # SIGTERM 超时，escalate SIGKILL
            try:
                _os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                return True
            except Exception:  # noqa: BLE001
                return False
            await _wait_pid_exit(pid)
            return True

    async def execute_in_environment(
        self, env_id: str, operation: dict[str, Any]
    ) -> ExecutionResult:
        """在沙箱内执行操作：用 nsenter 进入 bwrap PID 的 namespace 注入命令。"""
        env = self._environments.get(env_id)
        if env is None:
            return ExecutionResult(
                success=False, output=None, error=f"环境不存在: {env_id}"
            )
        if env.status == EnvironmentStatus.ERROR.value:
            return ExecutionResult(
                success=False, output=None,
                error=env.provider_info.get("error", "环境处于错误状态"),
            )

        pid = env.provider_info.get("bwrap_pid")
        op_type = operation.get("type")

        if op_type == "command":
            return await self._exec_in_sandbox(pid, operation)
        return ExecutionResult(
            success=False, output=None,
            error=f"不支持的操作类型: {op_type}",
        )

    async def _exec_in_sandbox(
        self, bwrap_pid: int, operation: dict[str, Any]
    ) -> ExecutionResult:
        """nsenter 注入命令（镜像 docker_provider._exec_in_container）。"""
        command = operation.get("command", "")
        workdir = operation.get("workdir") or "/workspace"
        if not command:
            return ExecutionResult(success=False, output=None, error="空命令")

        argv = [
            "nsenter",
            "--target", str(bwrap_pid),
            "--pid", "--mount", "--net", "--ipc",
            "sh", "-c", f"cd {workdir} && {command}",
        ]
        rc, stdout, stderr = await self._run_cmd(argv, timeout=operation.get("timeout", 30))
        success = rc == 0
        return ExecutionResult(
            success=success,
            output={"stdout": stdout.decode("utf-8", errors="replace"),
                    "stderr": stderr.decode("utf-8", errors="replace"),
                    "returncode": rc},
            error=None if success else stderr.decode("utf-8", errors="replace"),
        )

    async def get_environment_status(self, env_id: str) -> EnvironmentStatus:
        """按 bwrap PID 是否存活判断环境状态。"""
        env = self._environments.get(env_id)
        if env is None:
            return EnvironmentStatus.STOPPED
        pid = env.provider_info.get("bwrap_pid")
        if not pid:
            return EnvironmentStatus.ERROR
        return EnvironmentStatus.READY if _pid_alive(pid) else EnvironmentStatus.STOPPED


# ----------------------------------------------------------------------
# 模块级辅助（纯函数，可独立单测）
# ----------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """判断 pid 是否存活（signal 0 探测）。"""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:  # noqa: BLE001
        return False


async def _wait_pid_exit(pid: int) -> None:
    """轮询等待 pid 退出（os.kill 不能 await，用轮询模拟）。

    bwrap 是被 _spawn_bwrap 启动的子进程，正常应通过 proc.wait() 回收；
    但 destroy 可能在外部上下文调用（拿不到 proc 对象），故轮询 pid 存活状态。
    """
    import os as _os  # noqa: PLC0415

    for _ in range(200):  # 最多轮询 ~2s（与 _terminate_pid 超时对齐）
        if not _pid_alive(pid):
            return
        await asyncio.sleep(0.01)
