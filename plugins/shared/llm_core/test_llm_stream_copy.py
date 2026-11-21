# @feature: FP-T07 llm api | @ci: python-coverage
"""适配服务面流式消费机（复制改造件）行为测试。

覆盖 ``plugins/shared/llm_core/llm_stream.py``（源 =
``plugins/shared/llm/adapter.py`` 的 ``_BaseLLMAdapter`` 消费路径 @ c0fe6922fc）：

- 归一 chunk 消费：正文/思考/<think> 标签状态机/工具调用增量合并/usage/
  finish_reason 观察/心跳与硬超时守卫；
- 超时与取消：首块空流/首块超时/块间静默超时/停止信号中断（StreamCancelledError）；
- 半死连接防护：限时 aclose（超时放弃关闭、异常不掩盖主异常）；
- 输出完即清（§8 条款 2）：``_StreamState.release_buffers`` 释放全部缓冲；
- 非流式响应解析与 MiniMax 正文工具调用恢复；decode/health_check 契约。

时序一律用显式小超时 + 假时钟替代真实等待（无长 sleep）。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LLM_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "llm_core"
_SYSTEM_LLM_DIR = _REPO_ROOT / "plugins" / "shared" / "llm"
for _d in (_SYSTEM_LLM_DIR, _LLM_CORE_DIR):
    if str(_d) in sys.path:
        sys.path.remove(str(_d))
sys.path.insert(0, str(_SYSTEM_LLM_DIR))
sys.path.insert(0, str(_LLM_CORE_DIR))

import llm_stream  # noqa: E402
import llm_wire  # noqa: E402


def _chunk(
    *,
    content: str | None = None,
    reasoning: str | None = None,
    tool: tuple[str | None, str, str] | None = None,
    finish: str | None = None,
    usage: Any = None,
) -> Any:
    delta = SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=[])
    if tool is not None:
        tool_id, name, args = tool
        delta.tool_calls = [
            SimpleNamespace(index=0, id=tool_id, function=SimpleNamespace(name=name, arguments=args))
        ]
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)], usage=usage)


class _StubStream:
    """脚本化 async 流：逐 chunk 返回，可选逐 chunk 延迟 / 中途异常。"""

    def __init__(
        self,
        chunks: list[Any],
        *,
        delays: list[float] | None = None,
        error: BaseException | None = None,
        error_after: int | None = None,
        aclose_error: BaseException | None = None,
        aclose_delay: float = 0.0,
        prefetched: Any = None,
    ) -> None:
        self._chunks = list(chunks)
        self._delays = list(delays or [])
        self._error = error
        self._error_after = error_after
        self._aclose_error = aclose_error
        self._aclose_delay = aclose_delay
        self._idx = 0
        self.iter_calls = 0
        self.closed = False
        if prefetched is not None:
            self._prefetched_first_chunk = prefetched

    def __aiter__(self) -> _StubStream:
        return self

    async def __anext__(self) -> Any:
        self.iter_calls += 1
        if self._idx < len(self._delays):
            await asyncio.sleep(self._delays[self._idx])
        if self._error is not None and self._error_after is not None and self._idx >= self._error_after:
            raise self._error
        if self._idx >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._idx]
        self._idx += 1
        return chunk

    async def aclose(self) -> None:
        if self._aclose_delay:
            await asyncio.sleep(self._aclose_delay)
        self.closed = True
        if self._aclose_error is not None:
            raise self._aclose_error


class _Adapter(llm_stream.WireStreamAdapter):
    """测试适配器：``_do_completion`` 回放脚本流 / 抛预设异常。"""

    def __init__(self, stream: Any = None, *, exc: BaseException | None = None) -> None:
        self._stream = stream
        self._exc = exc
        self.calls: list[dict[str, Any]] = []

    async def _do_completion(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return self._stream


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ─────────────────────────── 流式聚合主路径 ───────────────────────────


def test_stream_aggregates_and_emits_conduit_chunks() -> None:
    """正文/思考/工具调用/usage 聚合 + conduit 事件序列（thinking_end 收口）。"""
    stream = _StubStream(
        [
            _chunk(reasoning="想一想"),
            _chunk(content="答"),
            _chunk(tool=(None, "bash", '{"cmd"')),
            _chunk(tool=("call_real", "", ': "ls"}')),
            _chunk(usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2, total_tokens=6, prompt_tokens_details=SimpleNamespace(cached_tokens=1))),
        ]
    )
    chunks: list[dict[str, Any]] = []
    response = _run(
        _Adapter(stream).completion(
            model="gpt-test",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            on_chunk=chunks.append,
        )
    )
    assert response.text == "答"
    assert response.thinking_text == "想一想"
    assert response.tool_calls == [{"id": "call_real", "name": "bash", "arguments": '{"cmd": "ls"}'}]
    assert response.usage == {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6, "cached_tokens": 1}
    types = [c["type"] for c in chunks]
    # 源契约：thinking_end 在每个正文/工具 chunk 前补发（不清 thinking_parts）；
    # 首块 stop 返回值不参与中断判定（见 stop 测试注释）。
    assert types == [
        "thinking",
        "thinking_end",
        "text",
        "thinking_end",
        "tool_call",
        "thinking_end",
        "tool_call",
    ]
    assert chunks[2] == {"type": "text", "content": "答"}
    assert stream.closed is True


def test_stream_first_chunk_finish_reason_observed() -> None:
    """首 chunk 的 finish_reason 被观察并透出（length=截断语义）。"""
    stream = _StubStream([_chunk(content="截断", finish="length")])
    response = _run(_Adapter(stream).completion(model="m", messages=[], stream=True))
    assert response.finish_reason == "length"
    assert response.stream_repetition is False


def test_stream_without_on_chunk_still_builds_response() -> None:
    """on_chunk 缺席：不产出事件但聚合结果不变（无回调分支）。"""
    stream = _StubStream([_chunk(content="a"), _chunk(content="b")])
    response = _run(_Adapter(stream).completion(model="m", messages=[], stream=True))
    assert response.text == "ab"


def test_stream_stop_signal_truncates_with_repetition_flag() -> None:
    """on_chunk 返回 "stop" → 截断 + stream_repetition（finish 收敛 length）。

    源契约保持：首块在进入循环前处理、其返回值不参与中断判定——首块触发
    stop 时后续仍会再消费一个 chunk 后才截断（复制件不修该不对称）。
    """
    chunks: list[dict[str, Any]] = []

    def _hook(chunk: dict[str, Any]) -> str | None:
        chunks.append(chunk)
        return "stop" if chunk.get("type") == "text" else None

    stream = _StubStream([_chunk(content="重复"), _chunk(content="重复")])
    response = _run(_Adapter(stream).completion(model="m", messages=[], stream=True, on_chunk=_hook))
    assert response.stream_repetition is True
    assert response.finish_reason == "length"
    assert response.text == "重复重复"


def test_thinking_limit_truncates_stream() -> None:
    """思考超长：thinking_truncated 置位；首块中断返回值不参与判定（源契约）。"""
    stream = _StubStream([_chunk(reasoning="abcdef"), _chunk(content="正文")])
    response = _run(
        _Adapter(stream).completion(model="m", messages=[], stream=True, max_thinking_chars=3)
    )
    assert response.thinking_truncated is True
    assert response.thinking_text == "abcdef"
    assert response.text == "正文"


def test_think_tag_state_machine_routes_thinking_and_text() -> None:
    """MiniMax 形态 <think/> 标签跨 chunk 切分：思考/正文分通道。"""
    stream = _StubStream(
        [
            _chunk(content="<think>想"),
            _chunk(content="一想</think>正"),
            _chunk(content="文"),
        ]
    )
    chunks: list[dict[str, Any]] = []
    response = _run(_Adapter(stream).completion(model="m", messages=[], stream=True, on_chunk=chunks.append))
    assert response.thinking_text == "想一想"
    assert response.text == "正文"
    assert {"type": "thinking_end", "content": ""} in chunks


def test_think_tag_without_closing_gt_keeps_thinking() -> None:
    """闭标签缺 '>'（截断形态）：思考片段不泄漏为正文，流正常收尾。"""
    stream = _StubStream([_chunk(content="<think>想</think")])
    response = _run(_Adapter(stream).completion(model="m", messages=[], stream=True))
    assert response.thinking_text == "想"
    assert response.text is None


def test_process_chunk_ignores_empty_choices() -> None:
    """usage-only / 空 choices chunk 不计正文（只收 usage）。"""
    adapter = _Adapter(_StubStream([]))
    state = llm_stream._StreamState()
    usage_only = SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3, prompt_tokens_details=None),
    )
    assert _run(adapter._process_chunk(usage_only, state)) is False  # noqa: SLF001
    assert state.stream_usage == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "cached_tokens": 0}


# ─────────────────────────── 超时 / 取消 ───────────────────────────


def test_empty_first_chunk_maps_to_wire_timeout() -> None:
    """建连成功但零 chunk（过早 EOF）→ WireTimeoutError（首 token 失败）。"""
    adapter = _Adapter(_StubStream([]))
    with pytest.raises(llm_wire.WireTimeoutError, match="empty"):
        _run(adapter.completion(model="m", messages=[], stream=True, first_chunk_timeout=1.0))


def test_first_chunk_timeout_maps_to_wire_timeout() -> None:
    """首块预算到点（含建连）→ WireTimeoutError。"""
    adapter = _Adapter(_StubStream([_chunk(content="迟到")], delays=[0.3]))
    with pytest.raises(llm_wire.WireTimeoutError, match="first chunk timeout"):
        _run(adapter.completion(model="m", messages=[], stream=True, first_chunk_timeout=0.02))


def test_inter_chunk_silence_timeout_maps_to_wire_timeout() -> None:
    """块间静默超时 → WireTimeoutError（上游冻结不干等）。"""
    adapter = _Adapter(_StubStream([_chunk(content="a"), _chunk(content="b")], delays=[0.0, 0.3]))
    with pytest.raises(llm_wire.WireTimeoutError, match="inter-chunk timeout"):
        _run(adapter.completion(model="m", messages=[], stream=True, inter_chunk_timeout=0.02))


def test_cancel_event_interrupts_silent_stream() -> None:
    """上游静默期停止信号 → StreamCancelledError（停止延迟=信号传播）。"""
    cancel_event = asyncio.Event()

    async def _run_async() -> None:
        async def _cancel_soon() -> None:
            await asyncio.sleep(0.05)
            cancel_event.set()

        task = asyncio.create_task(_cancel_soon())
        try:
            adapter = _Adapter(_StubStream([_chunk(content="a"), _chunk(content="b")], delays=[0.0, 0.3]))
            with pytest.raises(llm_stream.StreamCancelledError):
                await adapter.completion(
                    model="m", messages=[], stream=True, cancel_event=cancel_event, inter_chunk_timeout=5.0
                )
        finally:
            await task

    asyncio.run(_run_async())


def test_await_with_escape_timeout_and_abort() -> None:
    """通用等待器：超时抛 TimeoutError；停止信号胜出抛 StreamCancelledError。"""

    async def _slow() -> None:
        await asyncio.sleep(0.3)

    async def _run_async() -> None:
        with pytest.raises(TimeoutError, match="probe"):
            await llm_stream._await_with_escape(_slow(), 0.02, what="probe")  # noqa: SLF001

        # 超时 + 未触发的停止信号：两条后台等待都取消（含 abort_task 分支）
        idle_abort = asyncio.Event()
        with pytest.raises(TimeoutError, match="probe-idle"):
            await llm_stream._await_with_escape(  # noqa: SLF001
                _slow(), 0.02, what="probe-idle", abort_event=idle_abort
            )

        abort = asyncio.Event()
        abort.set()
        with pytest.raises(llm_stream.StreamCancelledError):
            await llm_stream._await_with_escape(_slow(), 5.0, what="probe2", abort_event=abort)  # noqa: SLF001

    asyncio.run(_run_async())


def test_await_with_escape_diag_timer_logs_when_loop_frozen(caplog: pytest.LogCaptureFixture) -> None:
    """事件循环被同步阻塞冻住时，独立线程诊断到点打日志（不依赖循环调度）。"""

    async def _freeze() -> str:
        time.sleep(0.3)  # 同步阻塞冻住事件循环，asyncio 超时回调无法执行
        return "ok"

    async def _run_async() -> None:
        assert await llm_stream._await_with_escape(_freeze(), 0.02, what="frozen") == "ok"  # noqa: SLF001

    with caplog.at_level(logging.ERROR, logger=llm_stream.logger.name):
        asyncio.run(_run_async())
    # 诊断线程 0.02s 到点打日志，事件循环 0.3s 才解冻——返回时日志必然已落
    assert "独立线程诊断" in caplog.text
    assert "what=frozen" in caplog.text


def test_reraise_if_outer_cancel_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """外层任务级取消在飞 → 重抛 CancelledError；无取消/无任务时静默。"""

    class _CancelledTask:
        def cancelling(self) -> int:
            return 1

    def _cancelled_task() -> Any:
        return _CancelledTask()

    monkeypatch.setattr(llm_stream.asyncio, "current_task", lambda: None)
    llm_stream._reraise_if_outer_cancel_pending()  # noqa: SLF001

    monkeypatch.setattr(llm_stream.asyncio, "current_task", _cancelled_task)
    with pytest.raises(asyncio.CancelledError):
        llm_stream._reraise_if_outer_cancel_pending()  # noqa: SLF001


# ─────────────────────────── 半死连接防护 / 清缓存 ───────────────────────────


def test_aclose_timeout_in_finally_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """finally aclose 超时（半死 socket）→ 放弃关闭、不掩盖主路径。"""
    monkeypatch.setattr(llm_stream, "_ACLOSE_TIMEOUT_SECONDS", 0.02)
    stream = _StubStream([_chunk(content="ok")], aclose_delay=0.3)
    response = _run(_Adapter(stream).completion(model="m", messages=[], stream=True))
    assert response.text == "ok"
    assert stream.closed is False


def test_aclose_generic_error_in_finally_is_swallowed() -> None:
    """finally aclose 非超时异常 → 忽略并保留聚合结果。"""
    stream = _StubStream([_chunk(content="ok")], aclose_error=RuntimeError("close boom"))
    response = _run(_Adapter(stream).completion(model="m", messages=[], stream=True))
    assert response.text == "ok"


def test_close_stream_quietly_variants() -> None:
    """限时关闭：无 aclose 直接返回；异常吞掉；取消语义穿透。"""

    class _NoAclose:
        pass

    class _Boom:
        async def aclose(self) -> None:
            raise RuntimeError("boom")

    class _Cancelled:
        async def aclose(self) -> None:
            raise asyncio.CancelledError

    async def _run_async() -> None:
        await llm_stream._close_stream_quietly(_NoAclose())  # noqa: SLF001
        await llm_stream._close_stream_quietly(_Boom())  # noqa: SLF001
        with pytest.raises(asyncio.CancelledError):
            await llm_stream._close_stream_quietly(_Cancelled())  # noqa: SLF001

    asyncio.run(_run_async())


def test_prefetch_first_chunk_variants() -> None:
    """首块预读：成功返回首块；空流/超时归一 WireTimeoutError；其它异常关流上抛。"""

    async def _run_async() -> None:
        first = await llm_stream._prefetch_first_chunk(  # noqa: SLF001
            _StubStream([_chunk(content="first")]), 1.0, "m"
        )
        assert first.choices[0].delta.content == "first"

        with pytest.raises(llm_wire.WireTimeoutError, match="empty"):
            await llm_stream._prefetch_first_chunk(_StubStream([]), 1.0, "m")  # noqa: SLF001

        with pytest.raises(llm_wire.WireTimeoutError, match="timeout"):
            await llm_stream._prefetch_first_chunk(  # noqa: SLF001
                _StubStream([_chunk(content="late")], delays=[0.3]), 0.02, "m"
            )

        broken = _StubStream([_chunk(content="x")], error=RuntimeError("bad"), error_after=0)
        with pytest.raises(RuntimeError, match="bad"):
            await llm_stream._prefetch_first_chunk(broken, 1.0, "m")  # noqa: SLF001
        assert broken.closed is True

    asyncio.run(_run_async())


def test_stream_state_release_buffers_clears_everything() -> None:
    """输出完即清（§8 条款 2）：缓冲/映射/回调引用一并释放。"""
    state = llm_stream._StreamState()
    state.result_parts.append("正文")
    state.thinking_parts.append("思考")
    state.tool_calls_map[0] = {"id": "c", "name": "n", "arguments": "{}"}
    state.stream_usage = {"total_tokens": 1}
    state.on_chunk = lambda _chunk: None
    state.release_buffers()
    assert state.result_parts == []
    assert state.thinking_parts == []
    assert state.tool_calls_map == {}
    assert state.stream_usage is None
    assert state.on_chunk is None


def test_track_background_task_cleans_up_and_consumes_exception() -> None:
    """后台任务登记：完成即移除并消费异常（无 never-retrieved 告警）。"""

    async def _run_async() -> None:
        async def _boom() -> None:
            raise RuntimeError("boom")

        before = len(llm_stream._background_tasks)  # noqa: SLF001
        task = asyncio.ensure_future(_boom())
        llm_stream._track_background_task(task)  # noqa: SLF001
        assert len(llm_stream._background_tasks) == before + 1  # noqa: SLF001
        await asyncio.sleep(0.01)
        assert len(llm_stream._background_tasks) == before  # noqa: SLF001

    asyncio.run(_run_async())


# ─────────────────────────── 心跳 / 硬超时守卫 ───────────────────────────


def test_stream_heartbeat_exits_on_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    """心跳探针：周期日志（首轮执行）→ CancelledError 退出（不泄漏任务）。"""
    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        if sleeps:
            raise asyncio.CancelledError
        sleeps.append(seconds)

    monkeypatch.setattr(llm_stream.asyncio, "sleep", _fake_sleep)
    adapter = _Adapter(_StubStream([]))
    _run(adapter._stream_heartbeat("m", 90.0, lambda: 1.0, lambda: 2, SimpleNamespace(is_closed=False)))  # noqa: SLF001
    assert sleeps == [30.0]


def test_hard_timeout_armed_reset_and_disarmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """独立线程硬超时：每个 chunk 重置、收尾必 disarm（chunk 间隔语义）。"""
    created: list[Any] = []

    class _FakeHardTimeout:
        def __init__(self, response: Any, loop: Any, timeout: float) -> None:
            self.armed = False
            self.disarmed = False
            self.resets = 0
            created.append(self)

        def arm(self) -> None:
            self.armed = True

        def reset(self) -> None:
            self.resets += 1

        def disarm(self) -> None:
            self.disarmed = True

    monkeypatch.setattr(llm_stream, "StreamHardTimeout", _FakeHardTimeout)
    stream = _StubStream([_chunk(content="a"), _chunk(content="b"), _chunk(content="c")])
    response = _run(_Adapter(stream).completion(model="m", messages=[], stream=True))
    assert response.text == "abc"
    assert len(created) == 1
    assert created[0].armed is True
    assert created[0].resets == 2
    assert created[0].disarmed is True


def test_prefetched_first_chunk_skips_extra_read() -> None:
    """首块预读面：已预读的流不再从迭代器取首块（KeyPool 契约兼容）。

    迭代器只服务第二块 + EOF 两次取值（未预读时为三次：首块/第二块/EOF）。
    """
    first = _chunk(content="first")
    stream = _StubStream([_chunk(content="second")], prefetched=first)
    response = _run(_Adapter(stream).completion(model="m", messages=[], stream=True))
    assert response.text == "firstsecond"
    assert stream.iter_calls == 2


# ─────────────────────────── 非流式 / 健康检查 / 参数归一 ───────────────────────────


def _nonstream(content: str | None, *, reasoning: str | None = None, tool_calls: Any = None, usage: Any = None) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=tool_calls),
                finish_reason="stop",
            )
        ],
        usage=usage,
    )


def test_non_stream_reasoning_fallback_and_tools() -> None:
    """非流式：reasoning_content 充当正文兜底；结构化 tool_calls 解析。"""
    tool_calls = [SimpleNamespace(id="call_9", function=SimpleNamespace(name="f", arguments="{}"))]
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3, prompt_tokens_details=None)
    adapter = _Adapter(_nonstream(None, reasoning="只有思考", tool_calls=tool_calls, usage=usage))
    result = _run(adapter.completion(model="m", messages=[], stream=False, inter_chunk_timeout=5))
    assert result.text == "只有思考"
    assert result.thinking_text == "只有思考"
    assert result.tool_calls == [{"id": "call_9", "name": "f", "arguments": "{}"}]
    assert result.usage == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "cached_tokens": 0}
    assert adapter.calls[0]["timeout"] == 5.0
    assert adapter.calls[0]["stream"] is False


def test_non_stream_extracts_think_tags_from_content() -> None:
    """非流式：content 内 <think/> 标签提取为 thinking，正文剥离标签。"""
    adapter = _Adapter(_nonstream("<think>想</think>答"))
    result = _run(adapter.completion(model="m", messages=[], stream=False))
    assert result.thinking_text == "想"
    assert result.text == "答"


def test_non_stream_minimax_native_tool_call_recovery() -> None:
    """MiniMax 正文原生工具调用：结构化缺席时从正文恢复并剥离调用块。"""
    text = '<minimax:tool_call><invoke name="bash"><parameter name="command">ls</parameter></invoke></minimax:tool_call>'
    adapter = _Adapter(_nonstream(text))
    result = _run(adapter.completion(model="minimax-m3", messages=[], stream=False))
    assert result.tool_calls
    assert result.tool_calls[0]["name"] == "bash"
    assert result.text is None


def test_non_stream_minimax_recovery_early_returns() -> None:
    """恢复兜底不越权：已有结构化调用/非 minimax/无可识别块均不改写。"""
    tool_calls = [SimpleNamespace(id="c1", function=SimpleNamespace(name="f", arguments="{}"))]
    with_tools = _run(_Adapter(_nonstream("正文", tool_calls=tool_calls)).completion(model="minimax", messages=[], stream=False))
    assert with_tools.text == "正文"
    assert len(with_tools.tool_calls) == 1

    non_minimax = _run(_Adapter(_nonstream("<tool_call><invoke name=\"x\"></invoke></tool_call>")).completion(model="glm", messages=[], stream=False))
    assert non_minimax.tool_calls == []


def test_health_check_true_and_false() -> None:
    """health_check：有 choices → True；异常 → False（不抛）。"""
    ok = _Adapter(SimpleNamespace(choices=[object()]))
    assert _run(ok.health_check("m")) is True
    bad = _Adapter(exc=RuntimeError("down"))
    assert _run(bad.health_check("m")) is False


def test_do_completion_abstract_raises() -> None:
    with pytest.raises(NotImplementedError):
        _run(llm_stream.WireStreamAdapter()._do_completion())  # noqa: SLF001


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"first_token_timeout": 7}, {"first_chunk_timeout": 7}),
        ({"first_chunk_timeout": 3, "first_token_timeout": 7}, {"first_chunk_timeout": 3}),
        ({"stream_idle_timeout": 9}, {"inter_chunk_timeout": 9}),
        ({"inter_chunk_timeout": 4, "stream_idle_timeout": 9}, {"inter_chunk_timeout": 4}),
    ],
)
def test_normalize_timeout_keys(kwargs: dict[str, Any], expected: dict[str, Any]) -> None:
    """llm.yaml 配置键 → 内部超时键归一；内部键优先，残留键弹出（不外发）。"""
    actual = dict(kwargs)
    llm_stream._normalize_timeout_keys(actual)  # noqa: SLF001
    assert actual == expected


def test_ensure_minimax_role_safety_fixes_non_first_system() -> None:
    """minimax 最后防线：非首位 system → user（name 移除）；非 minimax 不动。"""
    messages = [{"role": "system", "content": "s1"}, {"role": "user", "content": "u"}, {"role": "system", "content": "s2", "name": "n"}]
    out = llm_stream.WireStreamAdapter._ensure_minimax_role_safety("MiniMax-M3", messages)  # noqa: SLF001
    assert out[2]["role"] == "user"
    assert "name" not in out[2]
    other = [{"role": "user", "content": "u"}, {"role": "system", "content": "s2"}]
    assert llm_stream.WireStreamAdapter._ensure_minimax_role_safety("glm", other) is other  # noqa: SLF001


# ─────────────────── 状态机定向补测（区分度分支） ───────────────────


def _state(hook: Any = None) -> Any:
    return llm_stream._StreamState(on_chunk=hook)  # noqa: SLF001


def test_handle_delta_in_think_thinking_stop_interrupts() -> None:
    """标签内 + 闭标签：思考段触发 stop → 中断且不再发正文（None 语义）。"""
    disabled = _state(lambda chunk: "stop" if chunk["type"] == "thinking" else None)
    disabled.in_think_tag = True
    assert _Adapter(_StubStream([]))._handle_delta_content("前</think>后", disabled) is True  # noqa: SLF001
    assert disabled.in_think_tag is False
    assert disabled.result_parts == []


def test_handle_delta_in_think_text_stop_interrupts() -> None:
    """标签内 + 闭标签：残余正文触发 stop → 中断并置退化标记。"""
    stop_text = _state(lambda chunk: "stop" if chunk["type"] == "text" else None)
    stop_text.in_think_tag = True
    assert _Adapter(_StubStream([]))._handle_delta_content("前</think>后", stop_text) is True  # noqa: SLF001
    assert stop_text.result_parts == ["后"]
    assert stop_text.stream_repetition is True


def test_handle_delta_in_think_keeps_appending_without_close() -> None:
    """标签内无闭标签：继续累积思考（enforce_limit 档位）。"""
    state = _state()
    state.in_think_tag = True
    assert _Adapter(_StubStream([]))._handle_delta_content("继续想", state) is False  # noqa: SLF001
    assert state.thinking_parts == ["继续想"]


def test_handle_delta_open_tag_with_prefix_text() -> None:
    """标签外开标签：前缀发正文事件、标签内文本入思考通道。"""
    state = _state()
    assert _Adapter(_StubStream([]))._handle_delta_content("前缀<think>想", state) is False  # noqa: SLF001
    assert state.result_parts == ["前缀"]
    assert state.thinking_parts == ["想"]
    assert state.in_think_tag is True


def test_handle_delta_open_and_close_same_chunk_thinking_stop() -> None:
    """开/闭标签同 chunk：思考段 stop → 中断（残留正文不发）。"""
    state = _state(lambda chunk: "stop" if chunk["type"] == "thinking" else None)
    assert _Adapter(_StubStream([]))._handle_delta_content("<think>前</think>后", state) is True  # noqa: SLF001
    assert state.result_parts == []


def test_handle_delta_open_and_close_same_chunk_text_stop() -> None:
    """开/闭标签同 chunk：残余正文 stop → 中断并置退化标记。"""
    state = _state(lambda chunk: "stop" if chunk["type"] == "text" else None)
    assert _Adapter(_StubStream([]))._handle_delta_content("<think>前</think>后", state) is True  # noqa: SLF001
    assert state.result_parts == ["后"]
    assert state.stream_repetition is True


def test_handle_delta_think_tags_without_on_chunk_stays_quiet() -> None:
    """无 on_chunk：<think/> 文本分通道且不发事件（无回调分支）。"""
    stream = _StubStream([_chunk(content="<think>想</think>正文")])
    response = _run(_Adapter(stream).completion(model="m", messages=[], stream=True))
    assert response.thinking_text == "想"
    assert response.text == "正文"


def test_thinking_stop_signal_sets_repetition_flag() -> None:
    """思考通道 on_chunk 返回 stop → stream_repetition（与正文通同等对待）。"""
    stream = _StubStream([_chunk(reasoning="一"), _chunk(reasoning="二")])
    response = _run(
        _Adapter(stream).completion(
            model="m",
            messages=[],
            stream=True,
            on_chunk=lambda chunk: "stop" if chunk["type"] == "thinking" else None,
        )
    )
    assert response.stream_repetition is True
    assert response.thinking_text == "一二"


def test_non_stream_forwards_tools_schema() -> None:
    """非流式同样透传 tools（wire body 携带工具面）。"""
    adapter = _Adapter(_nonstream("ok"))
    tools = [{"type": "function", "function": {"name": "bash"}}]
    _run(adapter.completion(model="m", messages=[], stream=False, tools=tools))
    assert adapter.calls[0]["tools"] is tools


def test_fallback_notice_precedes_content_chunk() -> None:
    """降级通知先于内容 chunk 投递（fallback_notice 通道）。"""
    stream = _StubStream([_chunk(content="x")])
    stream.keypool_fallback_notice = {"provider": "p", "model": "m", "message": "fallback"}
    chunks: list[dict[str, Any]] = []
    _run(_Adapter(stream).completion(model="m", messages=[], stream=True, on_chunk=chunks.append))
    assert chunks[0]["type"] == "fallback_notice"
    assert chunks[0]["message"] == "fallback"


def test_first_chunk_error_with_failing_aclose_preserves_error() -> None:
    """首块异常 + aclose 也异常：原异常优先传播（关闭失败被抑制）。"""
    stream = _StubStream(
        [_chunk(content="x")],
        error=RuntimeError("bad first"),
        error_after=0,
        aclose_error=RuntimeError("close boom"),
    )
    with pytest.raises(RuntimeError, match="bad first"):
        _run(_Adapter(stream).completion(model="m", messages=[], stream=True))
    assert stream.closed is True


def test_diag_recv_chunk_swallows_structural_errors() -> None:
    """接收端点观察：结构异常吞掉但计数保留（不影响流处理的尽力而为面）。"""

    class _BadChunk:
        @property
        def choices(self) -> Any:
            raise RuntimeError("bad shape")

    state = _state()
    llm_stream.WireStreamAdapter()._diag_recv_chunk(_BadChunk(), state, first=True)  # noqa: SLF001
    assert state.recv_seq == 1
