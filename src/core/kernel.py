"""
资源调度内核

管理系统的核心资源：CPU、内存、进程锁、文件锁等

平台兼容性说明：
- Unix/Linux/macOS: 使用 fcntl 实现进程锁
- Windows: 使用 msvcrt.locking 实现文件锁（功能受限）
- 跨平台: FileLock 使用 asyncio.Lock，可在所有平台使用
"""

import asyncio
import logging
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import psutil

# 平台检测
IS_WINDOWS = sys.platform == "win32"

# 根据平台导入相应的锁模块
if IS_WINDOWS:
    import msvcrt
else:
    import fcntl

logger = logging.getLogger(__name__)


class ResourceLimit:
    """资源限制配置"""

    def __init__(
        self,
        max_cpu_percent: float = 80.0,
        max_memory_percent: float = 80.0,
        max_concurrent_processes: int = 10,
    ):
        self.max_cpu_percent = max_cpu_percent
        self.max_memory_percent = max_memory_percent
        self.max_concurrent_processes = max_concurrent_processes


class ProcessLock:
    """
    进程锁（跨平台实现）

    平台差异：
    - Unix/Linux/macOS: 使用 fcntl.flock()，支持真正的进程间锁
    - Windows: 使用 msvcrt.locking()，仅支持同进程内的文件锁定

    注意：Windows 版本的进程锁功能受限，建议使用 FileLock 替代
    """

    def __init__(self, lock_file: Path):
        # 使用系统临时目录，避免硬编码路径
        if not lock_file.is_absolute():
            Path(tempfile.gettempdir()) / "agent_locks"
        else:
            pass

        self.lock_file = lock_file
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)

        # 文件描述符（Unix）或文件句柄（Windows）
        self._fd: int | None = None
        self._file_handle = None

    def acquire(self) -> bool:
        """
        获取锁

        Returns:
            是否成功获取锁
        """
        try:
            if IS_WINDOWS:
                # Windows 实现：使用 msvcrt.locking
                # 注意：Windows 的文件锁仅对同一进程内的线程有效
                # 不同进程间的锁需要使用其他机制（如命名互斥体）
                try:
                    # 以读写模式打开文件
                    self._file_handle = open(self.lock_file, "w")
                    self._fd = self._file_handle.fileno()

                    # 尝试锁定文件（_LK_NBLCK = 非阻塞锁）
                    msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                    logger.debug(f"Windows 进程锁已获取: {self.lock_file}")
                    return True
                except OSError as e:
                    logger.warning(
                        f"Windows 进程锁获取失败: {self.lock_file}, 错误: {e}"
                    )
                    # 清理资源
                    if self._file_handle:
                        self._file_handle.close()
                        self._file_handle = None
                    self._fd = None
                    return False
            else:
                # Unix/Linux/macOS 实现：使用 fcntl.flock
                self._fd = os.open(self.lock_file, os.O_CREAT | os.O_WRONLY)
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                logger.debug(f"Unix 进程锁已获取: {self.lock_file}")
                return True
        except (OSError, BlockingIOError) as e:
            logger.warning(f"进程锁获取失败: {self.lock_file}, 错误: {e}")
            return False

    def release(self) -> None:
        """释放锁"""
        if self._fd is not None:
            try:
                if IS_WINDOWS:
                    # Windows 释放锁
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                    if self._file_handle:
                        self._file_handle.close()
                        self._file_handle = None
                else:
                    # Unix 释放锁
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                    os.close(self._fd)
                logger.debug(f"进程锁已释放: {self.lock_file}")
            except Exception as e:
                logger.error(f"释放进程锁失败: {e}")
            finally:
                self._fd = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class FileLock:
    """文件锁"""

    def __init__(self, file_path: Path, timeout: float = 30.0):
        self.file_path = file_path
        self.lock_file = Path(f"{file_path}.lock")
        self.timeout = timeout
        self._lock: asyncio.Lock | None = None

    async def __aenter__(self):
        """异步上下文管理器入口"""
        if self._lock is None:
            self._lock = asyncio.Lock()

        acquired = await asyncio.wait_for(self._lock.acquire(), timeout=self.timeout)
        if not acquired:
            raise TimeoutError(f"获取文件锁超时: {self.file_path}")

        # 创建锁文件
        self.lock_file.touch(exist_ok=True)

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出"""
        if self._lock:
            self._lock.release()

        # 删除锁文件
        try:
            if self.lock_file.exists():
                self.lock_file.unlink()
        except Exception as e:
            logger.error(f"删除锁文件失败: {e}")


class ResourceMonitor:
    """资源监控器"""

    def __init__(self, limits: ResourceLimit):
        self.limits = limits
        self._monitoring = False
        self._monitor_task: asyncio.Task | None = None

    async def get_cpu_percent(self) -> float:
        """获取 CPU 使用率"""
        return psutil.cpu_percent(interval=0.1)

    async def get_memory_percent(self) -> float:
        """获取内存使用率"""
        memory = psutil.virtual_memory()
        return memory.percent

    async def get_active_processes(self) -> set[int]:
        """获取活跃进程集合"""
        current_process = psutil.Process()
        try:
            children = current_process.children(recursive=True)
            return {p.pid for p in children}
        except Exception as e:
            logger.error(f"获取子进程失败: {e}")
            return set()

    async def check_resources(self) -> dict[str, bool]:
        """
        检查资源是否在限制内

        Returns:
            资源状态字典
        """
        cpu_percent = await self.get_cpu_percent()
        memory_percent = await self.get_memory_percent()
        active_processes = await self.get_active_processes()

        return {
            "cpu_ok": cpu_percent < self.limits.max_cpu_percent,
            "memory_ok": memory_percent < self.limits.max_memory_percent,
            "processes_ok": len(active_processes)
            < self.limits.max_concurrent_processes,
            "cpu_percent": cpu_percent,
            "memory_percent": memory_percent,
            "active_processes": len(active_processes),
        }

    async def wait_for_resources(
        self, check_interval: float = 1.0, max_wait: float = 60.0
    ) -> bool:
        """
        等待资源可用

        Args:
            check_interval: 检查间隔（秒）
            max_wait: 最大等待时间（秒）

        Returns:
            是否在等待时间内资源变为可用
        """
        waited = 0.0
        while waited < max_wait:
            status = await self.check_resources()
            if all([status["cpu_ok"], status["memory_ok"], status["processes_ok"]]):
                return True

            logger.debug(
                f"资源不可用，等待中... CPU: {status['cpu_percent']:.1f}%, "
                f"内存: {status['memory_percent']:.1f}%, "
                f"进程: {status['active_processes']}"
            )

            await asyncio.sleep(check_interval)
            waited += check_interval

        return False

    async def start_monitoring(self, callback=None):
        """
        开始资源监控

        Args:
            callback: 资源超限时的回调函数
        """
        if self._monitoring:
            return

        self._monitoring = True
        self._monitor_task = asyncio.create_task(self._monitor_loop(callback))

    async def stop_monitoring(self):
        """停止资源监控"""
        self._monitoring = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

    async def _monitor_loop(self, callback=None):
        """监控循环"""
        while self._monitoring:
            status = await self.check_resources()

            # 检查是否超限
            if not status["cpu_ok"]:
                logger.warning(
                    f"CPU 使用率超限: {status['cpu_percent']:.1f}% > {self.limits.max_cpu_percent}%"
                )
                if callback:
                    await callback("cpu", status)

            if not status["memory_ok"]:
                logger.warning(
                    f"内存使用率超限: {status['memory_percent']:.1f}% > {self.limits.max_memory_percent}%"
                )
                if callback:
                    await callback("memory", status)

            if not status["processes_ok"]:
                logger.warning(
                    f"进程数超限: {status['active_processes']} > {self.limits.max_concurrent_processes}"
                )
                if callback:
                    await callback("processes", status)

            await asyncio.sleep(5.0)  # 每5秒检查一次


class Kernel:
    """资源调度内核"""

    def __init__(self, limits: ResourceLimit | None = None):
        """
        初始化内核

        Args:
            limits: 资源限制配置
        """
        self.limits = limits or ResourceLimit()
        self.monitor = ResourceMonitor(self.limits)
        self._process_locks: dict[str, ProcessLock] = {}
        self._file_locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def acquire_process_lock(self, lock_name: str):
        """
        获取进程锁

        Args:
            lock_name: 锁名称

        注意：
        - 在 Unix/Linux/macOS 上提供真正的进程间锁
        - 在 Windows 上功能受限，建议使用 FileLock 替代
        """
        if lock_name not in self._process_locks:
            # 使用系统临时目录（跨平台兼容）
            lock_file = (
                Path(tempfile.gettempdir()) / "agent_locks" / f"{lock_name}.lock"
            )
            self._process_locks[lock_name] = ProcessLock(lock_file)

        lock = self._process_locks[lock_name]

        # 在单独的线程中获取锁，避免阻塞事件循环
        loop = asyncio.get_event_loop()
        acquired = await loop.run_in_executor(None, lock.acquire)

        if not acquired:
            raise RuntimeError(f"无法获取进程锁: {lock_name}")

        try:
            yield
        finally:
            await loop.run_in_executor(None, lock.release)

    @asynccontextmanager
    async def acquire_file_lock(self, file_path: Path):
        """
        获取文件锁

        Args:
            file_path: 文件路径
        """
        lock = FileLock(file_path)
        async with lock:
            yield

    async def ensure_resources(self) -> bool:
        """
        确保资源可用

        Returns:
            资源是否可用
        """
        return await self.monitor.wait_for_resources()

    async def start(self):
        """启动内核"""
        await self.monitor.start_monitoring()
        logger.info("资源调度内核已启动")

    async def stop(self):
        """停止内核"""
        await self.monitor.stop_monitoring()

        # 释放所有进程锁
        for lock in self._process_locks.values():
            lock.release()

        logger.info("资源调度内核已停止")

    async def get_resource_status(self) -> dict[str, any]:
        """
        获取资源状态

        Returns:
            资源状态字典
        """
        return await self.monitor.check_resources()


# 全局内核实例
_kernel: Kernel | None = None


def get_kernel() -> Kernel:
    """获取全局内核实例"""
    global _kernel
    if _kernel is None:
        _kernel = Kernel()
    return _kernel
