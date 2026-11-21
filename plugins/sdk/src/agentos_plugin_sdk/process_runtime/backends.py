"""进程后端：本地 psutil 整树杀 / 容器 docker exec / wsl_native 传输杀。

自 plugins/shared/bash/process_manager.py 迁入（工具隔离声明化 Slice 2）。
看门狗策略层（manager.ProcessManager）只依赖 ProcessBackend 抽象，
不关心进程跑在哪。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agentos_plugin_sdk.process_runtime.transports import _wsl_transport
from agentos_plugin_sdk.process_runtime.types import ProcessBackend, WorkUnit

logger = logging.getLogger(__name__)


class LocalProcessBackend(ProcessBackend):
    """本地宿主进程后端：psutil 递归杀进程树 + 宿主内存采样。

    杀进程树逻辑（原 ProcessManager._kill_process_tree）迁入此处：
    用 psutil 枚举后代叶子→根逐个终止，防孙子进程变孤儿。
    """

    async def kill(self, unit: WorkUnit, force: bool = True) -> None:
        """psutil 递归杀整棵进程树（叶子→根），防 cargo/rustc 后代变孤儿。"""
        self._kill_tree_sync(unit.pid, force=force)

    @staticmethod
    def _kill_tree_sync(root_pid: int, force: bool) -> None:
        """同步杀树（原 _kill_process_tree，迁自 ProcessManager）。"""
        try:
            import psutil  # noqa: PLC0415
        except ImportError:
            return  # psutil 不可用时，交给调用方回退单进程杀

        try:
            parent = psutil.Process(root_pid)
        except psutil.NoSuchProcess:
            return

        # children(recursive=True) 已是叶子→...→根 的稳定顺序（psutil 保证）
        try:
            descendants = parent.children(recursive=True)
        except psutil.NoSuchProcess:
            descendants = []

        for proc in descendants:
            try:
                proc.kill() if force else proc.terminate()
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                continue

        # 最后杀根进程
        try:
            parent.kill() if force else parent.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            pass

    async def sample_memory(self) -> float | None:
        """采样宿主内存使用率（系统级，0~1）。

        用 psutil 的 virtual_memory。返回 None 表示采样不可用（psutil 缺失
        或系统接口故障），失败显式记告警——静默 None 会让水位判据无声失效，
        看门狗失去触发依据（审查 P1-23）。
        """
        try:
            import psutil  # noqa: PLC0415
        except ImportError:
            logger.warning("[Watchdog] 宿主内存采样不可用（psutil 未安装）")
            return None
        try:
            mem = psutil.virtual_memory()
            return mem.percent / 100.0
        except (psutil.Error, OSError, RuntimeError) as e:
            logger.warning("[Watchdog] 宿主内存采样失败: %s", e)
            return None

    async def sample_unit_memory(self, unit: WorkUnit) -> int | None:
        """采样单个工作单元的内存占用(RSS 字节,含子进程)。

        用 psutil 查该进程及其所有后代的 RSS 之和——单个失控进程(cargo build
        fork 一堆 rustc 各吃内存)即使占系统比例低,自身 RSS 之和也会超阈值。
        这比系统级 sample_memory 对单进程失控灵敏得多。
        采样不可用返回 None：进程已退出/拒绝访问属预期（debug 级即可），
        其余失败显式记告警，不静默（审查 P1-23）。
        """
        try:
            import psutil  # noqa: PLC0415
        except ImportError:
            logger.warning("[Watchdog] 单元内存采样不可用（psutil 未安装） | pid=%s", unit.pid)
            return None
        try:
            root = psutil.Process(unit.pid)
            total_rss = root.memory_info().rss
            for child in root.children(recursive=True):
                try:
                    total_rss += child.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return total_rss
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            logger.debug(
                "[Watchdog] 单元内存采样不可用（进程已退出或拒绝访问） | pid=%s | error=%s",
                unit.pid,
                e,
            )
            return None
        except (psutil.Error, OSError, RuntimeError) as e:
            logger.warning("[Watchdog] 单元内存采样失败 | pid=%s | error=%s", unit.pid, e)
            return None


# 本地后端单例（ProcessManager 默认后端，terminate_process 回退用它）
_local_backend: LocalProcessBackend | None = None


def _get_local_backend() -> LocalProcessBackend:
    """获取本地进程后端单例。"""
    global _local_backend  # noqa: PLW0603
    if _local_backend is None:
        _local_backend = LocalProcessBackend()
    return _local_backend


class ContainerProcessBackend(ProcessBackend):
    """容器内进程后端：`docker exec <cid> kill <pid>` 单进程杀 + 跳过内存采样。

    与本地后端的关键差异：
    - kill 只杀单 pid，不递归后代（容器内整组杀 kill -9 -- -PGID 会触发
      runc cgroup shim race 导致容器永久卡死，见 docker_provider.py 注释）；
      单进程杀在容器内是安全的，cargo/rustc 子进程在容器内独立，杀错最多
      该 task 重来。
    - sample_memory / sample_unit_memory 返回 None：容器有 cgroup -m OOM 兜底，
      不在宿主机侧采样容器进程内存。
    """

    def __init__(self, container_id: str) -> None:
        self.container_id = container_id

    async def _run_cmd(self, args: list[str], timeout: float = 30) -> tuple[int, bytes, bytes]:
        """执行命令（默认真实 docker CLI；测试时替换此方法即可 mock）。

        与 DockerProvider._run_cmd 同签名，便于复用 mock 惯例。
        """
        import subprocess as _sp  # noqa: PLC0415

        proc = _sp.run(args, capture_output=True, timeout=timeout)  # noqa: S603
        return proc.returncode, proc.stdout, proc.stderr

    def _exec_prefix(self, container_id: str) -> list[str]:
        """杀进程 argv 的传输前缀（含 shell，kill 处续接 -c inner_cmd）。

        子类（wsl_native）覆写以换传输通道。
        """
        return ["docker", "exec", container_id, "sh"]

    async def kill(self, unit: WorkUnit, force: bool = True) -> None:
        """docker exec <cid> sh -c 'kill <sig> <pid>' —— 单进程杀，不整组。

        用 sh -c 包裹走 sh 内建 kill，而非 `docker exec cid kill ...`——
        精简镜像（python:3.11-slim 等）的 PATH 里没有 kill 二进制，直接调会
        `exec: kill: executable file not found`，sh 内建 kill 在所有 POSIX shell 都有。

        unit.metadata['container_id'] 优先（看门狗/terminate 透传），
        否则用后端实例的 self.container_id。缺 container_id 视为编程错误，抛 KeyError。
        """
        container_id = unit.metadata.get("container_id", self.container_id)
        if not container_id:
            raise KeyError("container_id 缺失，无法在容器内杀进程")

        sig = "-9" if force else "-15"
        # sh -c 'kill ...'：unit.pid 是容器内 pid（int），拼进命令安全
        inner_cmd = f"kill {sig} {unit.pid} 2>/dev/null || true"
        try:
            await self._run_cmd(
                [*self._exec_prefix(container_id), "-c", inner_cmd],
                timeout=10,
            )
        except Exception as e:  # noqa: BLE001
            # kill 失败通常意味着进程已退出或 docker daemon 不可达；
            # 进程已退出是正常的，不抛。真实系统错误由上层兜底。
            logger.debug("[ContainerBackend] kill pid=%s 失败（可能已退出）: %s", unit.pid, e)

    async def sample_memory(self) -> float | None:
        """容器场景不做宿主内存采样（靠 docker -m OOM 兜底）。"""
        return None

    async def sample_unit_memory(self, unit: WorkUnit) -> int | None:
        """容器场景不做单进程内存采样（看门狗跳过内存判据，idle 兜底仍生效）。"""
        return None


# 容器后端缓存（按 container_id 单例；同一容器复用同一后端实例）
_container_backends: dict[str, ContainerProcessBackend] = {}


def _get_container_backend(container_id: str) -> ContainerProcessBackend:
    """获取/创建容器进程后端（按 container_id 缓存单例）。"""
    backend = _container_backends.get(container_id)
    if backend is None:
        backend = ContainerProcessBackend(container_id)
        _container_backends[container_id] = backend
    return backend


class WslNativeProcessBackend(ContainerProcessBackend):
    """wsl_native 环境内进程后端：wsl 传输 + sh 内建 kill 单进程杀。

    与容器后端同纪律：单进程杀不整组、不做宿主内存采样（继承 None 语义——
    wsl_native 无 cgroup 配额兜底，采样值也不代表环境配额，留待 sandbox_cmd
    强沙箱档补）。区别仅在传输前缀：wsl.exe -d 发行版 [--]。
    """

    def __init__(self, backend: dict[str, Any]) -> None:
        super().__init__(container_id="")
        self.backend = dict(backend)

    def _exec_prefix(self, container_id: str) -> list[str]:
        return [*_wsl_transport(self.backend), "--exec", "bash"]

    async def probe_health(self) -> bool | None:
        """发行版拉起级健康探测：``wsl -d <distro> [(-u user)] --exec true``。

        不能用 ``wsl --status`` 做探测：2026-11-17 事故中 WSLService 已 wedged，
        但 ``--status`` 仍返回正常（它只查服务配置、不拉起发行版），据此判定
        健康会误判并让重试继续挂死。探测超时即判不可用，探测进程本身走宿主级
        收尾（不依赖传输通道）。
        """
        argv = [*_wsl_transport(self.backend), "--exec", "true"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as e:
            logger.warning("[WslNative] 健康探测 spawn 失败: %s", e)
            return False
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=10.0)
        except TimeoutError:
            logger.warning("[WslNative] 健康探测超时（10s），判定后端不可用")
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass
            return False
        return rc == 0


# wsl_native 后端缓存（按后端配置 JSON 键单例）
_wsl_native_backends: dict[str, WslNativeProcessBackend] = {}


def _get_wsl_native_backend(backend: dict[str, Any]) -> WslNativeProcessBackend:
    """获取/创建 wsl_native 进程后端（按配置内容缓存单例）。"""
    key = repr(sorted((str(k), str(v)) for k, v in backend.items()))
    instance = _wsl_native_backends.get(key)
    if instance is None:
        instance = WslNativeProcessBackend(backend)
        _wsl_native_backends[key] = instance
    return instance


class MxcNativeProcessBackend(LocalProcessBackend):
    """MXC 执行器进程后端：宿主级整树杀 + 宿主内存采样。

    与 wsl_native/容器的关键差异：每条命令 = 一个 MXC 执行器宿主子进程
    （一次 containment 会话），没有可在沙箱内寻址的常驻 pid——杀灭与内存
    采样都作用在宿主执行器进程树上（LocalProcessBackend 同实现，psutil
    递归杀，防执行器留下的后代变孤儿）。
    """

    def __init__(self, backend: dict[str, Any]) -> None:
        self.backend = dict(backend)

    async def probe_health(self) -> bool | None:
        """执行器在位探测（不拉起沙箱，避免健康检查自身触发 containment）。"""
        executor = str(self.backend.get("executor", "") or "")
        if not executor:
            return False
        if "/" in executor or "\\" in executor:
            from pathlib import Path  # noqa: PLC0415

            return await asyncio.to_thread(Path(executor).is_file)
        import shutil  # noqa: PLC0415

        return await asyncio.to_thread(shutil.which, executor) is not None


# mxc_native 后端缓存（按后端配置 JSON 键单例）
_mxc_native_backends: dict[str, MxcNativeProcessBackend] = {}


def _get_mxc_native_backend(backend: dict[str, Any]) -> MxcNativeProcessBackend:
    """获取/创建 mxc_native 进程后端（按配置内容缓存单例）。"""
    key = repr(sorted((str(k), str(v)) for k, v in backend.items()))
    instance = _mxc_native_backends.get(key)
    if instance is None:
        instance = MxcNativeProcessBackend(backend)
        _mxc_native_backends[key] = instance
    return instance
