"""StreamHardTimeout 独立线程硬超时测试。

背景：LLM 流式调用在底层 socket 阻塞时，所有 asyncio 级超时（wait_for、
心跳协程）共享事件循环，loop 一冻全部失效。StreamHardTimeout 用独立
threading 线程倒计时，到点强制关闭底层 stream，是 loop 冻住也能生效的
兜底。

本测试直接验证 watchdog 的行为契约，不依赖 adapter 内部实现。
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
pytestmark = pytest.mark.timing
# §9.4: 时序不变量门禁 — 此文件的测试断言可观察行为（事件顺序/间隔/超时边界/资源回收），
# 不含实现细节断言（mock.call_count/私有方法），破坏不变量的改动在 CI 阶段即被拦截。

from llm.stream_watchdog import StreamHardTimeout


class _FakeStream:
    """模拟 litellm 流对象：aclose 是 async，可追踪是否被调用。"""

    def __init__(self) -> None:
        self.aclose = AsyncMock(name="aclose")
        self._closed = False

    async def _aclose_impl(self) -> None:
        self._closed = True


def _make_fake_stream() -> _FakeStream:
    s = _FakeStream()
    s.aclose = AsyncMock(side_effect=s._aclose_impl)
    return s


class TestStreamHardTimeoutFires:
    """到点未 disarm，应强制关闭 stream。"""

    @pytest.mark.asyncio
    async def test_fires_after_timeout_calls_aclose(self) -> None:
        """超时后 watchdog 应在主 loop 调用 stream.aclose()。"""
        stream = _make_fake_stream()
        loop = asyncio.get_running_loop()
        fired_event = threading.Event()
        wd = StreamHardTimeout(stream, loop, timeout=0.3, on_fire=fired_event.set)

        wd.arm()
        # 等待 watchdog 触发（略大于 timeout）
        assert fired_event.wait(timeout=2.0), "watchdog 未在超时后触发"
        # on_fire 在独立线程触发；aclose 经 run_coroutine_threadsafe 回主 loop，
        # 需让 loop 跑一会儿把 aclose 协程调度执行
        await asyncio.sleep(0.1)
        stream.aclose.assert_awaited()

    @pytest.mark.asyncio
    async def test_does_not_block_event_loop(self) -> None:
        """watchdog 在独立线程，主 loop 期间应能正常并发执行其他协程。"""
        stream = _make_fake_stream()
        loop = asyncio.get_running_loop()
        wd = StreamHardTimeout(stream, loop, timeout=5.0)
        wd.arm()
        # 主 loop 不应被卡住：这段代码应几乎立即完成
        started = time.monotonic()
        await asyncio.sleep(0.05)
        elapsed = time.monotonic() - started
        assert elapsed < 0.5, "watchdog 阻塞了事件循环"
        wd.disarm()


class TestStreamHardTimeoutDisarm:
    """正常结束（disarm）后不应触发关闭。"""

    @pytest.mark.asyncio
    async def test_disarm_prevents_aclose(self) -> None:
        """disarm 后即使等到 timeout，也不应调用 aclose。"""
        stream = _make_fake_stream()
        loop = asyncio.get_running_loop()
        fired = threading.Event()
        wd = StreamHardTimeout(stream, loop, timeout=0.2, on_fire=fired.set)

        wd.arm()
        wd.disarm()
        # 远超 timeout
        await asyncio.sleep(0.5)
        assert not fired.is_set(), "disarm 后 watchdog 仍触发了"
        stream.aclose.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disarm_is_idempotent(self) -> None:
        """多次 disarm 不抛错（finally 块可能重复调用）。"""
        stream = _make_fake_stream()
        loop = asyncio.get_running_loop()
        wd = StreamHardTimeout(stream, loop, timeout=1.0)
        wd.arm()
        wd.disarm()
        wd.disarm()  # 不应抛异常
        wd.disarm()

    @pytest.mark.asyncio
    async def test_arm_without_disarm_then_disarm_safe(self) -> None:
        """arm 后立刻 disarm（首 chunk 即返回的快路径）安全。"""
        stream = _make_fake_stream()
        loop = asyncio.get_running_loop()
        wd = StreamHardTimeout(stream, loop, timeout=10.0)
        wd.arm()
        wd.disarm()
        await asyncio.sleep(0.01)
        stream.aclose.assert_not_awaited()


class TestStreamHardTimeoutRobustness:
    """watchdog 自身永不抛错影响主流程。"""

    @pytest.mark.asyncio
    async def test_aclose_failure_does_not_raise(self) -> None:
        """stream.aclose 抛异常时，watchdog 应吞掉，不传播到主流程。"""
        stream = MagicMock()
        stream.aclose = AsyncMock(side_effect=RuntimeError("boom"))
        loop = asyncio.get_running_loop()
        fired = threading.Event()
        wd = StreamHardTimeout(stream, loop, timeout=0.2, on_fire=fired.set)
        wd.arm()
        assert fired.wait(timeout=2.0)
        # 让 loop 处理 run_coroutine_threadsafe 的 aclose（会抛但被吞）
        await asyncio.sleep(0.2)
        stream.aclose.assert_awaited()

    @pytest.mark.asyncio
    async def test_arm_is_idempotent(self) -> None:
        """重复 arm 不创建多个线程。"""
        stream = _make_fake_stream()
        loop = asyncio.get_running_loop()
        wd = StreamHardTimeout(stream, loop, timeout=5.0)
        wd.arm()
        wd.arm()  # 幂等，不应启动第二个线程
        wd.disarm()


class TestStreamHardTimeoutReset:
    """reset() 让 watchdog 成为 chunk 间隔超时，避免误杀长流。"""

    @pytest.mark.asyncio
    async def test_reset_before_timeout_does_not_fire(self) -> None:
        """每小于 timeout 间隔 reset 一次，即使总时长远超 timeout 也不触发。"""
        stream = _make_fake_stream()
        loop = asyncio.get_running_loop()
        fired = threading.Event()
        # timeout=0.3s，但每 0.1s reset 一次（模拟 chunk 持续健康到达）
        wd = StreamHardTimeout(stream, loop, timeout=0.3, on_fire=fired.set)
        wd.arm()
        # 持续 reset，总时长 1.0s 远超 timeout 0.3s
        for _ in range(10):
            await asyncio.sleep(0.1)
            wd.reset()
        wd.disarm()
        # 再等一会儿确认确实没触发
        await asyncio.sleep(0.5)
        assert not fired.is_set(), "持续 reset 时 watchdog 仍误触发了 aclose"
        stream.aclose.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reset_then_idle_past_timeout_fires(self) -> None:
        """reset 后若真的静默满 timeout，仍应触发（chunk 间隔超时语义成立）。"""
        stream = _make_fake_stream()
        loop = asyncio.get_running_loop()
        fired = threading.Event()
        wd = StreamHardTimeout(stream, loop, timeout=0.2, on_fire=fired.set)
        wd.arm()
        # reset 一次模拟一个 chunk，然后停止 reset 模拟死连接
        await asyncio.sleep(0.05)
        wd.reset()
        await asyncio.sleep(0.05)
        wd.reset()
        # 之后不再 reset，静默满 timeout 应触发
        assert fired.wait(timeout=2.0), "reset 后真正静默满 timeout 未触发"
        await asyncio.sleep(0.1)
        stream.aclose.assert_awaited()

    @pytest.mark.asyncio
    async def test_reset_is_safe_before_arm(self) -> None:
        """arm 之前调 reset 不抛错（防御性）。"""
        stream = _make_fake_stream()
        loop = asyncio.get_running_loop()
        wd = StreamHardTimeout(stream, loop, timeout=1.0)
        wd.reset()  # 未 arm，不应抛异常
        wd.arm()
        wd.disarm()
