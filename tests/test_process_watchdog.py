"""进程看门狗策略层测试：内存水位驱动 + 按 idle 排序杀。

背景：
原看门狗用"固定 30 分钟无访问"判孤儿 + "句柄超阈值且持续增长"判资源失控。
问题：Rust 编译几分钟就能撑爆容器内存，30 分钟太晚；句柄指标不直接反映内存压力。
新判据（用户设计）：
1. 内存水位驱动：采样内存使用率，达高水位(85%)才触发清理。
2. 按 idle（最久没访问）排序杀：不是杀"最老"的，而是杀 last_access_time 最早的。
   活跃进程（agent 一直在 continue/input）idle≈0，永不被杀。
3. 杀到内存回落低水位(70%)即停，不一刀切，保 2-3G 容几个并发工作单元。
4. 兜底：idle 超 30 分钟无条件杀（进程被遗忘但内存没涨的情况）。

本测试用假 ProcessBackend 验证纯策略逻辑，不碰真实进程/docker。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tools.builtin.bash.process_manager import ProcessManager
from tools.builtin.bash.types import ProcessInfo


def _make_pm() -> ProcessManager:
    """构造一个不依赖真实进程的 ProcessManager，用于策略测试。"""
    pm = ProcessManager()
    # 禁用真实 watchdog 后台循环（测试手动调 _watchdog_check_once）
    pm._watchdog_interval = 0.01
    return pm


def _make_info(pid: int, idle_secs: float, command: str = "cargo build", backend=None) -> ProcessInfo:
    """构造一个 running 进程记录，idle_secs 表示距上次访问过了多久。"""
    return ProcessInfo(
        pid=pid,
        command=command,
        start_time=time.time() - 1000,  # 启动早，但判据看 idle 不看 age
        log_file=Path("/tmp/fake.log"),
        process=None,  # 策略测试不需要真实 process 对象
        status="running",
        last_access_time=time.time() - idle_secs,  # idle_secs 前被访问
        backend=backend,
    )


class _FakeBackend:
    """假进程后端：记录被杀的 pid，按预设内存序列返回采样值。"""

    def __init__(self, memory_sequence: list[float]):
        # 巡检时按顺序消费；不够则用最后一个值
        self._mem = list(memory_sequence)
        self.killed: list[int] = []  # 记录 kill 调用顺序

    async def sample_memory(self) -> float:
        if len(self._mem) > 1:
            return self._mem.pop(0)
        return self._mem[0]

    async def kill(self, unit, force: bool = True) -> None:  # noqa: ARG002
        self.killed.append(unit.pid)


# ---------------------------------------------------------------------------
# 判据核心：内存高水位触发，按 idle 排序杀最闲的
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_triggers_when_memory_high():
    """内存达 90%（高水位）→ 触发清理，按 idle 从最久没访问的开始杀。"""
    pm = _make_pm()
    backend = _FakeBackend(memory_sequence=[0.90, 0.50])  # 第一次采样高，杀一个后回落
    pm._memory_backend = backend

    # 三个 running 进程，idle 分别 5s / 120s / 600s
    pm.active_processes = {
        101: _make_info(101, idle_secs=5, backend=backend),
        102: _make_info(102, idle_secs=120, backend=backend),
        103: _make_info(103, idle_secs=600, backend=backend),
    }

    await pm._watchdog_check_once()

    # 最久没访问的 103 先被杀
    assert 103 in backend.killed
    # 杀一个后内存回落到 0.50（<低水位），停止，不杀第二个
    assert len(backend.killed) == 1


@pytest.mark.asyncio
async def test_no_cleanup_when_memory_low():
    """内存 50%（低于高水位）→ 不杀任何进程。"""
    pm = _make_pm()
    backend = _FakeBackend(memory_sequence=[0.50])
    pm._memory_backend = backend

    pm.active_processes = {
        101: _make_info(101, idle_secs=600),  # 即使很闲，内存够也不杀
    }

    await pm._watchdog_check_once()

    assert backend.killed == []


@pytest.mark.asyncio
async def test_stops_when_memory_drops_below_low_watermark():
    """杀一个后内存回落到低水位以下 → 停止，不继续杀。"""
    pm = _make_pm()
    # 内存持续高：0.90 → 杀一个 → 仍 0.88 → 杀第二个 → 0.65（回落）停
    backend = _FakeBackend(memory_sequence=[0.90, 0.88, 0.65])
    pm._memory_backend = backend

    pm.active_processes = {
        101: _make_info(101, idle_secs=5, backend=backend),
        102: _make_info(102, idle_secs=60, backend=backend),
        103: _make_info(103, idle_secs=600, backend=backend),
        104: _make_info(104, idle_secs=1200, backend=backend),
    }

    await pm._watchdog_check_once()

    # 按 idle 排序：103(600) 先杀，104(1200) 次之... 等等，104 idle 更长
    # 重新理解：104 idle=1200 > 103 idle=600 > 102 idle=60 > 101 idle=5
    # 杀 104 → 内存 0.88 仍高 → 杀 103 → 内存 0.65 回落 → 停
    assert 104 in backend.killed
    assert 103 in backend.killed
    # 102/101 不该被杀（已回落）
    assert 102 not in backend.killed
    assert 101 not in backend.killed


@pytest.mark.asyncio
async def test_kills_by_idle_not_age():
    """判据是 idle（最久没访问），不是 age（启动最早）。

    一个 dev server 启动很早但 agent 一直在访问（idle≈0）→ 不杀；
    一个 cargo build 刚启动但无人管（idle 大）→ 内存紧张时优先杀。
    """
    pm = _make_pm()
    backend = _FakeBackend(memory_sequence=[0.92, 0.50])
    pm._memory_backend = backend

    # dev_server：启动极早（start_time 很老），但一直被访问（idle≈0）
    dev_server = _make_info(201, idle_secs=0, command="npm run dev", backend=backend)
    dev_server.start_time = time.time() - 99999  # age 极大
    # cargo：刚启动（age 小），但无人管（idle 大）
    cargo = _make_info(202, idle_secs=300, command="cargo build", backend=backend)
    cargo.start_time = time.time() - 300  # age 小

    pm.active_processes = {201: dev_server, 202: cargo}

    await pm._watchdog_check_once()

    # 该杀 cargo（idle 大），不杀 dev_server（idle≈0，虽 age 大）
    assert 202 in backend.killed
    assert 201 not in backend.killed


# ---------------------------------------------------------------------------
# 兜底：idle 超 30 分钟无条件杀（即使内存没涨）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphan_timeout_fallback():
    """内存没超(50%)，但 idle > 30 分钟 → 兜底杀（进程被遗忘）。"""
    pm = _make_pm()
    backend = _FakeBackend(memory_sequence=[0.50])
    pm._memory_backend = backend

    pm.active_processes = {
        301: _make_info(301, idle_secs=2000, backend=backend),  # 33 分钟没访问 > 1800s 阈值
    }

    await pm._watchdog_check_once()

    assert 301 in backend.killed


# ---------------------------------------------------------------------------
# 清理原则：旧句柄/固定超时判据代码应已删除
# ---------------------------------------------------------------------------


def test_old_handle_judgment_removed():
    """抽取后旧代码必须清理：句柄采样/固定超时判据的属性应不存在。"""
    pm = ProcessManager()
    # 旧判据相关配置/方法应已被移除（清理原则，不并存两套）
    assert not hasattr(pm, "_handle_threshold"), "旧句柄阈值应已删除"
    assert not hasattr(pm, "_is_resource_out_of_control"), "旧资源失控判定应已删除"
    assert not hasattr(pm, "_sample_handles"), "旧句柄采样应已删除"
    # 新判据配置应存在
    assert hasattr(pm, "_cleanup_high_watermark"), "缺少新高水位配置"
    assert hasattr(pm, "_cleanup_low_watermark"), "缺少新低水位配置"
    assert hasattr(pm, "_memory_backend"), "缺少内存后端"
