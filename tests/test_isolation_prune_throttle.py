# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入
import tests._isolation_path  # noqa: F401

"""镜像清理限频与 docker 调用并发信号量回归测试。

背景：IsolationManager.start() 每次都触发全局 image/builder prune
（持锁重操作），频繁启动管理器时反复对 daemon 施压，加剧 WSL2 后端的
ext4.vhdx 锁死与 daemon 假死。修复：用持久化标记文件限频（24h 一次）。

DockerProvider._run_cmd 原本用裸线程池无并发上限，多任务同时
create/start/exec 会打爆 daemon。修复：全局信号量限制并发 docker 调用。

本测试锁定核心契约：
1. prune 限频：距上次清理不足 24h → 跳过；超 24h → 执行。
2. 并发信号量：并发 docker 调用数不超过上限。
"""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from manager import IsolationManager
from providers.docker_provider import DockerProvider

# ---------------------------------------------------------------------------
# 1. prune 限频：_should_prune 按标记文件判断
# ---------------------------------------------------------------------------


def _make_manager(tmp_path, monkeypatch) -> IsolationManager:
    """构造 manager，标记文件指向 tmp_path。"""
    manager = IsolationManager(providers={})
    monkeypatch.setattr(IsolationManager, "_PRUNE_MARK_FILE", str(tmp_path / ".prune_mark"))
    return manager


def test_should_prune_true_when_no_mark(tmp_path, monkeypatch):
    """无标记文件（首次运行）→ 应该清理。"""
    manager = _make_manager(tmp_path, monkeypatch)
    assert manager._should_prune() is True


def test_should_prune_false_within_24h(tmp_path, monkeypatch):
    """标记时间在 24h 内 → 跳过清理。"""
    manager = _make_manager(tmp_path, monkeypatch)
    manager._mark_prune_done()
    assert manager._should_prune() is False


def test_should_prune_true_after_24h(tmp_path, monkeypatch):
    """标记时间超过 24h → 应该清理。"""
    manager = _make_manager(tmp_path, monkeypatch)
    # 写入 25h 前的时间戳
    mark_file = manager._PRUNE_MARK_FILE
    with open(mark_file, "w", encoding="utf-8") as f:
        f.write(str(time.time() - 25 * 3600))
    assert manager._should_prune() is True


def test_should_prune_recovers_from_corrupt_mark(tmp_path, monkeypatch):
    """标记文件损坏（非数字）→ 当作首次运行，执行清理。"""
    manager = _make_manager(tmp_path, monkeypatch)
    with open(manager._PRUNE_MARK_FILE, "w", encoding="utf-8") as f:
        f.write("not-a-number")
    assert manager._should_prune() is True


# ---------------------------------------------------------------------------
# 2. start() 不再每次都触发 prune
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_skips_prune_when_within_cooldown(tmp_path, monkeypatch):
    """24h 内已清理过 → start() 不调度 prune。"""
    manager = _make_manager(tmp_path, monkeypatch)
    manager._mark_prune_done()

    pruned = False
    original = manager._prune_docker_images

    async def spy_prune():
        nonlocal pruned
        pruned = True

    manager._prune_docker_images = spy_prune
    # _resume_containers 会访问 docker，mock 掉
    manager._resume_containers = AsyncMock()

    await manager.start()

    assert pruned is False


@pytest.mark.asyncio
async def test_start_triggers_prune_when_overdue(tmp_path, monkeypatch):
    """距上次清理超 24h → start() 调度 prune。"""
    manager = _make_manager(tmp_path, monkeypatch)
    # 写入 25h 前的时间戳
    with open(manager._PRUNE_MARK_FILE, "w", encoding="utf-8") as f:
        f.write(str(time.time() - 25 * 3600))

    pruned = False

    async def spy_prune():
        nonlocal pruned
        pruned = True

    manager._prune_docker_images = spy_prune
    manager._resume_containers = AsyncMock()

    await manager.start()
    # start() 用 ensure_future 异步调度 prune，需让事件循环跑一轮让其完成
    await asyncio.sleep(0.05)

    assert pruned is True


# ---------------------------------------------------------------------------
# 3. 并发信号量：docker 调用不超过上限
# ---------------------------------------------------------------------------


async def _measure_peak_docker_concurrency(provider: DockerProvider, n_calls: int) -> int:
    """并发发起 n_calls 次 _run_cmd，返回 docker 调用层观测到的峰值并发。

    subprocess.run 是与外部 docker daemon 的边界：替换为带 50ms 真实耗时
    的桩制造重叠窗口。峰值并发即限流信号量的可观察行为——配置的
    max_docker_concurrency 生效与否只看这个峰值，不看内部属性。
    """
    current_concurrent = 0
    peak_concurrent = 0

    class FakeResult:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(args, capture_output=True, timeout=30, **kwargs):
        nonlocal current_concurrent, peak_concurrent
        current_concurrent += 1
        peak_concurrent = max(peak_concurrent, current_concurrent)
        time.sleep(0.05)  # 模拟 IO 耗时，让多个协程重叠
        current_concurrent -= 1
        return FakeResult()

    import subprocess as real_sp

    original_run = real_sp.run
    real_sp.run = fake_run
    try:
        await asyncio.gather(
            *[provider._run_cmd(["docker", "version"]) for _ in range(n_calls)],
        )
    finally:
        real_sp.run = original_run

    return peak_concurrent


@pytest.mark.asyncio
async def test_docker_concurrency_limited_by_semaphore():
    """并发 _run_cmd 调用数不超过 max_docker_concurrency（config=3）。"""
    provider = DockerProvider(config={"max_docker_concurrency": 3})

    peak_concurrent = await _measure_peak_docker_concurrency(provider, 10)

    assert peak_concurrent <= 3, f"并发数 {peak_concurrent} 超过上限 3"
    assert peak_concurrent >= 2, "信号量未真正限流（应有重叠）"


@pytest.mark.asyncio
async def test_docker_concurrency_configurable():
    """默认并发上限为 4（第 5 个并发调用被限流）；config 调高后真实生效。"""
    # 默认配置：10 个并发调用，峰值不得超过默认上限 4
    p_default = DockerProvider()
    peak_default = await _measure_peak_docker_concurrency(p_default, 10)
    assert peak_default <= 4, f"默认配置下并发数 {peak_default} 超过默认上限 4"

    # 调高到 8：12 个并发调用，峰值既不超过 8，也确实超过默认上限 4
    # （后者证明配置被真实消费，而非固定 4）
    p_custom = DockerProvider(config={"max_docker_concurrency": 8})
    peak_custom = await _measure_peak_docker_concurrency(p_custom, 12)
    assert peak_custom <= 8, f"自定义配置下并发数 {peak_custom} 超过上限 8"
    assert peak_custom > 4, f"调高配置未生效（峰值 {peak_custom} 未超过默认 4）"
