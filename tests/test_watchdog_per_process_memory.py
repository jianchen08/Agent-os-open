"""看门狗单进程内存维度测试(真实进程)。

核心保障验证:一个进程自己吃内存到阈值(不是等整个系统快满),
看门狗就把它杀掉。这是用户原意——"进程吃到一定程度就杀"。

现状 gap:LocalProcessBackend.sample_memory 读整个宿主内存(31GB),
单个进程吃 2GB 只占 6%,触发不了 85% 水位——等于对单进程失控无效。
修复:加单进程内存维度,某工作单元自身 RSS 超阈值(如配置的 unit_memory_limit
的 80%)就判为失控候选,按 idle 杀。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.builtin.bash.process_manager import LocalProcessBackend, ProcessManager
from tools.builtin.bash.types import ProcessInfo, WorkUnit


def _spawn_memory_eater(mb: int) -> subprocess.Popen:
    """起一个子进程,分配 mb MB 内存并持有,返回 Popen。"""
    script = (
        f"import time; x=[bytearray(1024*1024) for _ in range({mb})]; time.sleep(120)"
    )
    return subprocess.Popen([sys.executable, "-c", script])


# ---------------------------------------------------------------------------
# 1. sample_unit_memory:能查到单进程的真实 RSS（非 None、非 0）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_backend_sample_unit_memory_returns_real_rss():
    """sample_unit_memory 返回单进程真实 RSS(字节),不是 None/0。"""
    backend = LocalProcessBackend()
    proc = _spawn_memory_eater(100)
    try:
        await asyncio.sleep(1.5)  # 等吃上内存
        unit = WorkUnit(pid=proc.pid, command="eater")
        rss = await backend.sample_unit_memory(unit)
    finally:
        proc.kill()
        proc.wait(timeout=5)

    assert rss is not None, "应能查到单进程 RSS"
    assert rss > 50 * 1024 * 1024, f"吃了 100MB,RSS 应 >50MB,实际 {rss/1024/1024:.0f}MB"


# ---------------------------------------------------------------------------
# 2. 端到端:单个进程吃内存超自身阈值 → 看门狗杀它(不等系统满)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_kills_process_by_per_unit_memory_e2e():
    """端到端:一个进程吃 300MB(超过配置的 unit_memory_limit 80%=160MB),
    看门狗按单进程内存维度判它失控,真的杀掉它。

    这是关键保障:不等整个系统(31GB)快满,单进程吃到 300MB 就动手。
    """
    pm = ProcessManager()
    # 配置：单工作单元内存阈值 200MB(进程吃超此值即判失控)
    pm._unit_memory_limit = 200 * 1024 * 1024  # 200MB

    killed_pids: list[int] = []

    class PerUnitAwareBackend(LocalProcessBackend):
        async def kill(self, unit: WorkUnit, force: bool = True) -> None:
            killed_pids.append(unit.pid)
            await super().kill(unit, force=force)

    backend = PerUnitAwareBackend()
    pm._memory_backend = backend

    # 起一个吃 300MB 的进程(超过 160MB 阈值)
    proc = _spawn_memory_eater(300)
    await asyncio.sleep(2.0)  # 等吃上内存
    pid = proc.pid

    pm.active_processes[pid] = ProcessInfo(
        pid=pid,
        command="rogue 300MB eater",
        start_time=time.time(),
        log_file=Path("/tmp/fake_unit.log"),
        process=None,
        status="running",
        last_access_time=time.time() - 60,  # idle 60s
        backend=backend,
    )

    try:
        # 触发巡检:应因单进程内存超阈值而杀掉它
        await pm._watchdog_check_once()
        await asyncio.sleep(0.5)

        assert pid in killed_pids, (
            f"看门狗应因单进程内存超阈值杀掉 {pid}(300MB>160MB 阈值),"
            f"实际只杀了 {killed_pids}"
        )
        assert proc.poll() is not None, f"进程 {pid} 应已被看门狗真实杀掉"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        pm.active_processes.clear()


# ---------------------------------------------------------------------------
# 3. 反例:进程内存没超阈值 → 不杀(防误杀正常 build)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_no_kill_when_per_unit_memory_under_threshold():
    """进程吃 50MB(低于 160MB 阈值)→ 看门狗不杀(防误杀正常命令)。"""
    pm = ProcessManager()
    pm._unit_memory_limit = 200 * 1024 * 1024  # 200MB

    killed_pids: list[int] = []

    class Backend(LocalProcessBackend):
        async def kill(self, unit, force=True):
            killed_pids.append(unit.pid)

    backend = Backend()
    pm._memory_backend = backend

    proc = _spawn_memory_eater(50)  # 只吃 50MB,低于阈值
    await asyncio.sleep(1.5)
    pid = proc.pid

    pm.active_processes[pid] = ProcessInfo(
        pid=pid,
        command="small 50MB process",
        start_time=time.time(),
        log_file=Path("/tmp/fake_small.log"),
        process=None,
        status="running",
        last_access_time=time.time() - 60,
        backend=backend,
    )

    try:
        await pm._watchdog_check_once()
        await asyncio.sleep(0.3)

        assert pid not in killed_pids, (
            f"进程 {pid}(50MB<160MB)不应被杀,实际杀了 {killed_pids}"
        )
        assert proc.poll() is None, "小内存进程不该被杀"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        pm.active_processes.clear()
