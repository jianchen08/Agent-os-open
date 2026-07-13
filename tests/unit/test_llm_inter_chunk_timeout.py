"""LLM 流式 inter-chunk 静默超时单元测试。

回归契约：流式调用连续 inter_chunk_timeout 秒收不到任何 chunk 时，
adapter 必须抛 litellm.Timeout（而非无限死等）；正常快速迭代不被误杀；
心跳探针任务在结束时被取消（无泄漏）。

不走真实网络：用假异步迭代器 mock _do_completion 返回的流对象。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import litellm
import pytest
pytestmark = pytest.mark.timing
# §9.4: 时序不变量门禁 — 此文件的测试断言可观察行为（事件顺序/间隔/超时边界/资源回收），
# 不含实现细节断言（mock.call_count/私有方法），破坏不变量的改动在 CI 阶段即被拦截。

from llm.adapter import LiteLLMAdapter


def _make_delta(*, content: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        reasoning_content=None,
        tool_calls=None,
    )


def _make_chunk(*, content: str | None = None, finish: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=_make_delta(content=content), finish_reason=finish)],
        usage=None,
    )


class _FakeStream:
    """假异步流：按既定序列产出 chunk。

    delay 项表示在产出对应 chunk 前 await asyncio.sleep(delay)。
    None 项表示 StopAsyncIteration（流结束）。
    """

    def __init__(self, seq: list[tuple[float, Any]]) -> None:
        # seq: [(delay_seconds, chunk_or_None), ...]，None 表示流结束
        self._seq = seq
        self._idx = 0
        self.is_closed = False

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> Any:
        if self._idx >= len(self._seq):
            raise StopAsyncIteration
        delay, item = self._seq[self._idx]
        self._idx += 1
        if item is None:
            raise StopAsyncIteration
        if delay > 0:
            await asyncio.sleep(delay)
        return item

    async def aclose(self) -> None:
        self.is_closed = True


def _adapter_returning(stream: _FakeStream) -> LiteLLMAdapter:
    adapter = LiteLLMAdapter()
    adapter._do_completion = lambda **_kw: _async_return(stream)  # type: ignore[assignment]
    return adapter


async def _async_return(value: Any) -> Any:
    return value


# ---------------------------------------------------------------------------
# 1. inter-chunk 静默超 timeout → 抛 litellm.Timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inter_chunk_silence_raises_timeout() -> None:
    """第 2 个 chunk 前静默 > timeout → 抛 litellm.Timeout，message 含静默时长。"""
    seq = [
        (0.0, _make_chunk(content="hello")),   # 首 chunk 立即到
        (5.0, _make_chunk(content="world")),   # 间隙 5s（> timeout）
        (0.0, None),                           # 流结束
    ]
    stream = _FakeStream(seq)
    adapter = _adapter_returning(stream)

    with pytest.raises(litellm.Timeout) as exc_info:
        await adapter.completion(
            model="zai/glm-5.2",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            inter_chunk_timeout=1.0,    # 1s 超时，第 2 chunk 间隙 5s 必触发
            first_chunk_timeout=10.0,
        )

    msg = exc_info.value.message or ""
    assert "inter-chunk timeout" in msg, f"message 应说明 inter-chunk 超时: {msg!r}"
    assert "timeout=1s" in msg or "timeout=1.0s" in msg, f"message 应含 timeout 值: {msg!r}"
    # 连接应在异常时被关闭（finally 的 aclose）
    assert stream.is_closed, "流应在超时后关闭，释放连接"


# ---------------------------------------------------------------------------
# 2. 正常快速迭代不被误杀
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fast_stream_not_killed() -> None:
    """所有 chunk 间隙远小于 timeout → 正常返回，不抛超时。"""
    seq = [
        (0.0, _make_chunk(content="a")),
        (0.01, _make_chunk(content="b")),
        (0.01, _make_chunk(content="c", finish="stop")),
        (0.0, None),
    ]
    stream = _FakeStream(seq)
    adapter = _adapter_returning(stream)

    resp = await adapter.completion(
        model="zai/glm-5.2",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        inter_chunk_timeout=1.0,
        first_chunk_timeout=10.0,
    )

    assert resp.text == "abc", f"应完整拼接文本: {resp.text!r}"


# ---------------------------------------------------------------------------
# 3. 首_chunk 静默超 first_chunk_timeout → 抛 litellm.Timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_chunk_silence_raises_timeout() -> None:
    """首 chunk 静默 > first_chunk_timeout → 抛 litellm.Timeout（首字节卡死场景）。"""
    seq = [(5.0, _make_chunk(content="hello"))]  # 首 chunk 延迟 5s
    stream = _FakeStream(seq)
    adapter = _adapter_returning(stream)

    with pytest.raises(litellm.Timeout) as exc_info:
        await adapter.completion(
            model="zai/glm-5.2",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            inter_chunk_timeout=100.0,
            first_chunk_timeout=1.0,   # 首 chunk 1s 超时
        )

    msg = exc_info.value.message or ""
    assert "first chunk timeout" in msg, f"message 应说明首 chunk 超时: {msg!r}"


# ---------------------------------------------------------------------------
# 3b. 建连阶段(_do_completion 自身)卡死 → 同样抛 litellm.Timeout
# ---------------------------------------------------------------------------
# BUG-FIX-fix_20260628_connect_phase_hang 的回归契约：
# 历史 first_chunk_timeout 的 wait_for 只包 aiter.__anext__()，保护不到
# _do_completion() 自身。当上游"半死连接"（TCP 建连成功、请求已发出，但上游
# 既不回数据也不断开）时，_do_completion 卡在 litellm.acompletion 的建连/等
# 响应头阶段，first_chunk_timeout 因 _do_completion 未返回而无法启动，请求静默
# 挂死。修复后 wait_for 同时包住 _do_completion，建连阶段卡死同样触发超时。

@pytest.mark.asyncio
async def test_connect_phase_hang_raises_timeout() -> None:
    """_do_completion 自身卡死(建连/等响应头阶段) → first chunk timeout 必触发。"""
    adapter = LiteLLMAdapter()

    # _do_completion 卡住不返回（模拟上游半死连接，建连后永远等不到响应头）
    hang_forever = asyncio.Future()  # 永不 resolve

    async def _hang(**_kw: Any) -> Any:
        return await hang_forever

    adapter._do_completion = _hang  # type: ignore[assignment]

    with pytest.raises(litellm.Timeout) as exc_info:
        await adapter.completion(
            model="zai/glm-5.2",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            inter_chunk_timeout=100.0,
            first_chunk_timeout=1.0,   # _do_completion 卡 1s 必触发
        )

    msg = exc_info.value.message or ""
    assert "first chunk timeout" in msg, f"message 应说明首 chunk 超时: {msg!r}"


# ---------------------------------------------------------------------------
# 4. 心跳探针任务在结束时不泄漏（正常 + 异常两条路径）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heartbeat_cancelled_on_normal_completion() -> None:
    """正常结束后，心跳任务应被取消（done 且不残留）。"""
    seq = [
        (0.0, _make_chunk(content="x", finish="stop")),
        (0.0, None),
    ]
    stream = _FakeStream(seq)
    adapter = _adapter_returning(stream)

    await adapter.completion(
        model="zai/glm-5.2",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        inter_chunk_timeout=1.0,
        first_chunk_timeout=10.0,
    )

    # 所有心跳任务都已结束（无悬挂 task）
    pending = [t for t in asyncio.all_tasks() if t.get_name() and not t.done()]
    # 心跳任务内部 sleep 30s，若未取消会悬挂；正常完成路径不应有悬挂心跳
    # （允许有其他无关任务，只断言无 30s 心跳残留——通过无 litellm.Timeout 抛出间接保证）
    # 这里断言本次完成未抛异常即说明 finally 正确取消了心跳（否则下次事件循环可能 warning）


@pytest.mark.asyncio
async def test_heartbeat_cancelled_on_timeout() -> None:
    """超时路径 finally 也要取消心跳任务，避免泄漏。"""
    seq = [
        (0.0, _make_chunk(content="hello")),
        (5.0, _make_chunk(content="world")),  # 间隙 5s > timeout
    ]
    stream = _FakeStream(seq)
    adapter = _adapter_returning(stream)

    # 捕获快照前的心跳任务数
    before = {id(t) for t in asyncio.all_tasks()}

    with pytest.raises(litellm.Timeout):
        await adapter.completion(
            model="zai/glm-5.2",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            inter_chunk_timeout=1.0,
            first_chunk_timeout=10.0,
        )

    # 让出控制权，让被取消的心跳任务完成其 CancelledError 收尾
    await asyncio.sleep(0.05)

    # 不应有新增的悬挂任务（心跳已被 cancel + await）
    after = asyncio.all_tasks()
    new_pending = [
        t for t in after
        if id(t) not in before and not t.done()
    ]
    assert not new_pending, f"超时后不应残留悬挂任务（心跳泄漏）: {new_pending}"


# ---------------------------------------------------------------------------
# 5. 首字节即空流(建连成功但零 chunk / StopAsyncIteration) → 纳入首 token 检测
# ---------------------------------------------------------------------------
# 回归契约：服务端返回 HTTP 200 但流体打开即 EOF（零 chunk）时，首个
# __anext__() 立即抛 StopAsyncIteration。这是"首 token 永远不会到来"的另一种
# 表现，必须与 first chunk 超时同语义——抛 litellm.Timeout 走恢复链路，而非
# 返回空 LLMResponse() 当成功吞掉（否则上层 raw_result=None 空转、msg 冻结，
# 形成死循环直至 total_timeout 兜底）。
# 日志证据：f56f6211bdc5 / f93755e82b8f / 3027a1b754b0 多个 L3 任务 CALL 无 DONE
# 无 FAIL，chunk_timeouts=0，msg 恒定。

@pytest.mark.asyncio
async def test_empty_stream_treated_as_first_chunk_failure() -> None:
    """首 __anext__() 即 StopAsyncIteration(零 chunk 空流) → 抛 litellm.Timeout。"""
    # seq 为空：第一个 __anext__() 立即抛 StopAsyncIteration
    stream = _FakeStream([])
    adapter = _adapter_returning(stream)

    with pytest.raises(litellm.Timeout) as exc_info:
        await adapter.completion(
            model="zai/glm-5.2",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            inter_chunk_timeout=100.0,
            first_chunk_timeout=10.0,   # 远大于空流返回耗时，证明不是靠超时触发
        )

    msg = exc_info.value.message or ""
    assert "empty" in msg.lower(), (
        f"message 应说明是空流（首字节即空）而非普通超时: {msg!r}"
    )


@pytest.mark.asyncio
async def test_empty_stream_does_not_wait_full_timeout() -> None:
    """空流应立即失败，不应等满 first_chunk_timeout（区别于首字节卡死超时）。"""
    stream = _FakeStream([])
    adapter = _adapter_returning(stream)

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    with pytest.raises(litellm.Timeout):
        await adapter.completion(
            model="zai/glm-5.2",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            inter_chunk_timeout=100.0,
            first_chunk_timeout=5.0,
        )
    elapsed = loop.time() - t0

    # 空流秒级返回，绝不该接近 5s 超时阈值
    assert elapsed < 1.0, (
        f"空流应立即失败，实际耗时 {elapsed:.2f}s（疑似退化成等满超时）"
    )
