"""Docker daemon 假死防阻塞回归测试。

背景 BUG：IsolationManager 的 4 个容器管理方法（_find_existing_container 等）
直接同步调用 docker Python SDK（docker.from_env / containers.get / container.start），
既无 socket 超时，又跑在事件循环线程。daemon 假死时这些 HTTP 调用永久阻塞、
不抛异常，直接冻死整个 asyncio 事件循环。

修复（见 src/isolation/manager.py）：
- 新增 _run_docker_sync：同步逻辑搬进线程池（run_in_executor）+ wait_for 硬超时兜底。
- 4 个方法拆 _xxx_sync 实现 + async 外壳走 _run_docker_sync。
- client 统一 from_env(timeout=10)，daemon 假死时 HTTP 最多等 10s 后抛异常。

本测试锁定的核心契约：daemon 假死时这些方法必须在截止时间内返回（None），
而不是永久阻塞冻死进程。
"""
import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from isolation.manager import IsolationManager

# _run_docker_sync 超时上限（秒）。测试里把硬超时压到这个值，让"挂起"场景
# 快速收敛——这验证的是超时机制本身，生产默认是 15s/30s。
_TEST_TIMEOUT = 0.5


def _make_manager() -> IsolationManager:
    """构造一个轻量 IsolationManager（不拉起 provider，避免外部依赖）。

    _find_existing_container / _destroy_container_by_name 不依赖 self._providers，
    只用 self.CONTAINER_NAME_PREFIX，故 providers={} 即可。
    """
    return IsolationManager(providers={})


def _patch_fast_timeout(monkeypatch, manager: IsolationManager):
    """把 manager 的 _run_docker_sync 硬超时压到 _TEST_TIMEOUT，让挂起场景快速收敛。"""
    orig = manager._run_docker_sync

    async def fast(sync_fn, *, timeout, op_name):
        return await orig(sync_fn, timeout=_TEST_TIMEOUT, op_name=op_name)

    monkeypatch.setattr(manager, "_run_docker_sync", fast)


# ---------------------------------------------------------------------------
# 1. _run_docker_sync：防阻塞的命脉（直接、确定性验证）
# ---------------------------------------------------------------------------


async def test_run_docker_sync_returns_none_on_timeout():
    """同步操作永久阻塞时，_run_docker_sync 必须在超时后返回 None，不卡死。"""
    manager = _make_manager()

    def hang_forever():
        # 模拟 daemon 假死：SDK 调用永不返回
        time.sleep(30)

    start = time.perf_counter()
    result = await manager._run_docker_sync(
        hang_forever, timeout=_TEST_TIMEOUT, op_name="test-hang"
    )
    elapsed = time.perf_counter() - start

    assert result is None  # 超时返回 None（不抛异常、不卡死）
    assert elapsed < 3.0  # 远小于 sleep(30)，证明是超时中断而非等满


async def test_run_docker_sync_returns_value_on_success():
    """同步操作正常返回时，结果原样透传。"""
    manager = _make_manager()

    result = await manager._run_docker_sync(
        lambda: "ok-value", timeout=5.0, op_name="test-ok"
    )

    assert result == "ok-value"


async def test_run_docker_sync_propagates_exception():
    """同步操作抛异常时，异常透传（不在 helper 层吞掉，由调用方按语义处理）。"""
    manager = _make_manager()

    def boom():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await manager._run_docker_sync(boom, timeout=5.0, op_name="test-err")


# ---------------------------------------------------------------------------
# 2. _find_existing_container（每个任务执行必走的热路径）
# ---------------------------------------------------------------------------


async def test_find_existing_container_returns_none_when_daemon_raises():
    """daemon 连接失败（client 抛 ConnectionError）时优雅返回 None，不冒泡。"""
    manager = _make_manager()

    with patch("docker.from_env", side_effect=ConnectionError("daemon 拒绝连接")):
        result = await manager._find_existing_container("cua-test-ws")

    assert result is None


async def test_find_existing_container_does_not_hang_when_daemon_hangs(monkeypatch):
    """daemon 假死（containers.get 永久阻塞）时不冻死进程，超时返回 None。

    这是本次修复的核心回归用例。压低硬超时让测试快速收敛。
    """
    manager = _make_manager()
    _patch_fast_timeout(monkeypatch, manager)

    # 模拟 daemon 假死：containers.get 永久阻塞
    fake_client = MagicMock()
    fake_client.containers.get = MagicMock(side_effect=lambda name: time.sleep(30))
    fake_client.close = MagicMock()

    start = time.perf_counter()
    with patch("docker.from_env", return_value=fake_client):
        result = await manager._find_existing_container("cua-test-ws")
    elapsed = time.perf_counter() - start

    assert result is None  # 超时返回 None，而非永久阻塞
    assert elapsed < 3.0  # 远小于 sleep(30)，证明被超时机制中断


async def test_find_existing_container_returns_none_on_not_found():
    """容器不存在（NotFound）时返回 None，正常路径不受影响。"""
    manager = _make_manager()

    from docker.errors import NotFound

    fake_client = MagicMock()
    fake_client.containers.get = MagicMock(side_effect=NotFound("no such container"))
    fake_client.close = MagicMock()

    with patch("docker.from_env", return_value=fake_client):
        result = await manager._find_existing_container("cua-test-ws")

    assert result is None


# ---------------------------------------------------------------------------
# 3. _destroy_container_by_name
# ---------------------------------------------------------------------------


async def test_destroy_container_does_not_raise_on_connection_error():
    """daemon 不可用时 _destroy_container_by_name 不抛异常（销毁失败仅记日志）。"""
    manager = _make_manager()

    with patch("docker.from_env", side_effect=ConnectionError("daemon 拒绝连接")):
        # 不应抛异常
        result = await manager._destroy_container_by_name("test-ws")

    assert result is None


async def test_destroy_container_does_not_hang_when_daemon_hangs(monkeypatch):
    """daemon 假死时 _destroy_container_by_name 超时返回，不卡死。"""
    manager = _make_manager()
    _patch_fast_timeout(monkeypatch, manager)

    fake_client = MagicMock()
    fake_client.containers.get = MagicMock(side_effect=lambda name: time.sleep(30))
    fake_client.close = MagicMock()

    start = time.perf_counter()
    with patch("docker.from_env", return_value=fake_client):
        result = await manager._destroy_container_by_name("test-ws")
    elapsed = time.perf_counter() - start

    assert result is None
    assert elapsed < 3.0


async def test_destroy_container_swallows_sync_exception():
    """sync 实现抛 NotFound/其它异常时，async 外壳的 except 兜底，不冒泡。"""
    manager = _make_manager()

    from docker.errors import NotFound

    fake_client = MagicMock()
    fake_client.containers.get = MagicMock(side_effect=NotFound("no such"))
    fake_client.close = MagicMock()

    with patch("docker.from_env", return_value=fake_client):
        result = await manager._destroy_container_by_name("test-ws")

    assert result is None
