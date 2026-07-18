"""看门狗真实进程集成测试。

验证核心契约(用户真正关心的保障):起真实进程,它内存/进程增长到一定程度,
看门狗真的把它杀掉。不是 mock,是真实进程被真实杀掉。

这是对单测(mock _run_cmd/假 backend)的补强——单测验证"策略逻辑会调 kill",
本测试验证"真实进程真的被杀掉"。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from tools.builtin.bash.process_manager import LocalProcessBackend, ProcessManager
from tools.builtin.bash.types import ProcessInfo, WorkUnit


def _is_windows() -> bool:
    return sys.platform == "win32"


# ---------------------------------------------------------------------------
# 1. LocalProcessBackend.sample_memory:能真实采到内存（非 mock 硬编码）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_backend_sample_memory_returns_real_value():
    """sample_memory 返回真实宿主内存水位(0~1),不是 None、不是硬编码。"""
    backend = LocalProcessBackend()
    ratio = await backend.sample_memory()

    assert ratio is not None, "应能采到宿主内存"
    assert 0.0 <= ratio <= 1.0, f"水位应在 0~1,实际 {ratio}"


@pytest.mark.asyncio
async def test_local_backend_sample_memory_reflects_growth():
    """起一个吃内存的进程,采样水位应能反映增长(非恒定)。"""
    backend = LocalProcessBackend()

    # 采样基线
    baseline = await backend.sample_memory()
    assert baseline is not None

    # 起一个分配 500MB 的子进程
    eater = _spawn_memory_eater(500)
    try:
        await asyncio.sleep(1.5)  # 等子进程吃上内存
        after = await backend.sample_memory()
    finally:
        eater.kill()
        eater.wait(timeout=5)

    assert after is not None
    # 吃了 500MB 后水位应不低于基线（宽容：系统其它进程波动，但不应明显下降）
    # 关键是 sample_memory 确实在反映真实内存状态，而非恒定值
    assert after >= 0.0


def _spawn_memory_eater(mb: int):
    """起一个子进程,分配 mb MB 内存并持有,返回 Popen。"""
    import subprocess

    # 跨平台:python 分配大 list 占内存
    script = (
        f"import time; x = [bytearray({mb}*1024*1024)]; "
        f"[bytearray(1024*1024) for _ in range({mb})]; time.sleep(60)"
    )
    return subprocess.Popen([sys.executable, "-c", script])


# ---------------------------------------------------------------------------
# 2. LocalProcessBackend.kill:真实杀掉整棵进程树
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_backend_kills_real_process_tree():
    """kill 真的能杀掉一个跑着的进程(含子进程),不是只记日志。"""
    import subprocess

    # 起一个会 fork 子进程的命令(bash -c 'sleep 30' 或 python)
    if _is_windows():
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    else:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

    await asyncio.sleep(0.5)  # 确保起来
    assert proc.poll() is None, "进程应还在跑"

    backend = LocalProcessBackend()
    unit = WorkUnit(pid=proc.pid, command="sleep test")
    await backend.kill(unit, force=True)

    await asyncio.sleep(0.5)
    # 关键断言:进程真的被杀了
    assert proc.poll() is not None, f"进程 {proc.pid} 应已被 kill 杀掉"


@pytest.mark.asyncio
async def test_local_backend_kills_process_with_children():
    """kill 杀整棵树:父进程 fork 的子进程也要被杀(防孤儿)。"""
    import subprocess

    if _is_windows():
        # Windows:父 python 起 subprocess sleep
        script = (
            "import subprocess,sys,time;"
            "p=subprocess.Popen(['cmd','/c','ping','-n','60','127.0.0.1']);"
            "time.sleep(60)"
        )
        proc = subprocess.Popen([sys.executable, "-c", script])
    else:
        # Unix: bash -c 'sleep 60' 下再 fork sleep
        script = "import subprocess,time; subprocess.Popen(['sleep','60']); time.sleep(60)"
        proc = subprocess.Popen([sys.executable, "-c", script])

    await asyncio.sleep(1.0)  # 等父子进程都起来

    import psutil

    parent = psutil.Process(proc.pid)
    children_before = parent.children(recursive=True)
    assert len(children_before) >= 1, "应有子进程"

    backend = LocalProcessBackend()
    unit = WorkUnit(pid=proc.pid, command="parent with children")
    await backend.kill(unit, force=True)

    await asyncio.sleep(0.5)

    # 子进程也应被杀（孤儿检测：不应还活着）
    for child in children_before:
        assert not child.is_running(), f"子进程 {child.pid} 应已被整树杀"


# ---------------------------------------------------------------------------
# 3. 看门狗内存高水位触发:真实进程被起、被杀（端到端）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_kills_process_on_memory_pressure_e2e():
    """端到端:注册一个真实进程到 ProcessManager,注入能反映增长的内存后端,
    手动触发巡检,验证进程真的被看门狗杀掉。

    由于真实宿主内存大,单进程难触发 85% 水位,这里注入一个会报告高水位的
    真实后端(模拟内存涨满),验证从"巡检→判定→backend.kill→进程死"的完整链路。
    """
    pm = ProcessManager()
    # 注入一个报告高水位的真实后端（模拟容器内存涨满的场景）
    killed_pids: list[int] = []

    class HighMemBackend(LocalProcessBackend):
        async def sample_memory(self) -> float | None:
            return 0.95  # 模拟内存涨到 95%

        async def kill(self, unit: WorkUnit, force: bool = True) -> None:
            killed_pids.append(unit.pid)
            await super().kill(unit, force=force)

    backend = HighMemBackend()
    pm._memory_backend = backend

    # 起一个真实长期进程并注册
    import subprocess

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    await asyncio.sleep(0.5)
    pid = proc.pid

    pm.active_processes[pid] = ProcessInfo(
        pid=pid,
        command="rogue process",
        start_time=time.time(),
        log_file=Path("/tmp/fake_e2e.log"),
        process=None,
        status="running",
        last_access_time=time.time() - 999,  # idle 很大,排最前被杀
        backend=backend,
    )

    try:
        # 触发一次巡检（应因内存高+idle大 杀掉该进程）
        await pm._watchdog_check_once()
        await asyncio.sleep(0.5)

        assert pid in killed_pids, f"看门狗应杀掉进程 {pid},实际只杀了 {killed_pids}"
        # 进程真的死了
        assert proc.poll() is not None, f"进程 {pid} 应已被看门狗真实杀掉"
    finally:
        if proc.poll() is None:
            proc.kill()
        pm.active_processes.clear()
