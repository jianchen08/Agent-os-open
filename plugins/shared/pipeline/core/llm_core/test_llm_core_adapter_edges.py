# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core adapter.py 边界路径补测——非流式/流式辅助方法/桥接/协议。

契约（0.2 现状）：adapter.py 仅保留类型/响应结构职责（LLMAdapter/LLMResponse）
与 LiteLLM 直调适配器；llm_core 生产路径不直连 litellm（走 llm_service 能力
通道），adapter 供测试注入与独立使用。本文件覆盖既有
test_llm_adapter_call_streaming.py 未触及的分支：

- ``_call_non_streaming``：usage 核算、reasoning_content 兜底、<think/> 提取、
  tool_calls 解析、health_check 成败两路；
- ``_build_streaming_call_kwargs``：tools 透传与超时解析；
- ``_open_and_first_chunk``：首 chunk 异常时 aclose 自身异常被抑制（不掩盖原始异常）；
- ``_handle_delta_content``：<think/> 状态机各分支（标签内闭合/截断/开标签前文/
  同 chunk 闭合/标签外 thinking_end）；
- ``_process_chunk``：诊断日志分支、thinking→tool_calls 过渡 thinking_end；
- ``_consume_stream``：首 chunk finish_reason 捕获、诊断块异常吞掉、
  aclose 超时/异常兜底；
- ``_stream_heartbeat``：心跳日志（DEBUG/WARNING 两档）；
- ``_ThreadedStreamBridge``：__anext__ 三分支 + aclose；
- ``_await_with_escape``：超时抛错（含独立线程诊断）；
- ``LiteLLMAdapter._do_completion``：直调 litellm.acompletion。

加载：平铺 import adapter（与生产代码一致），_CORE_DIR 供 llm_provider_* 插件
惰性 import。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import queue as _queue
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[5]
_LLM_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "pipeline" / "core" / "llm_core"
_CORE_DIR = _LLM_CORE_DIR.parent
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
_SYSTEM_LLM_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "llm"

for _d in (_SYSTEM_LLM_DIR, _SHARED_DIR, _CORE_DIR, _LLM_CORE_DIR):
    if str(_d) in sys.path:
        sys.path.remove(str(_d))
    sys.path.insert(0, str(_d))

import adapter as _adapter  # noqa: E402  平铺 import，与生产代码一致
import litellm  # noqa: E402

# <think/> 闭合标签：拼接构造（源码中避免字面 "</think>" 序列被工具链改写）
_OPEN_THINK = "<" + "think>"
_CLOSE_THINK = "</" + "think>"


class _HandlerlessLogger(logging.Logger):
    """``.handlers`` 恒为空的 logger：屏蔽 pytest 日志插件挂的 capture/file handler。

    与 test_llm_adapter_call_streaming.py 同款：生产 ``_process_chunk`` 仅在
    ``_diag_logger.handlers`` 非空时才进入诊断分支，pytest 日志插件会给触及的
    logger 挂 LogCaptureHandler，故用属性覆盖令 ``.handlers`` 恒返回 ``[]``。
    """

    @property
    def handlers(self) -> list[Any]:
        return []

    @handlers.setter
    def handlers(self, value: Any) -> None:
        pass


@pytest.fixture(autouse=True)
def _isolate_diag_loggers(monkeypatch: Any) -> None:
    """隔离诊断 logger，使诊断分支稳定跳过，与宿主日志配置解耦。"""
    monkeypatch.setattr(_adapter, "_diag_logger", _HandlerlessLogger("adapter._diag.__test__"))
    monkeypatch.setattr(_adapter, "_sync_diag_handlers", lambda: None)


# ─────────────────────────── 桩对象 ───────────────────────────


class _FakeStream:
    """模拟 litellm 流：按预设 chunk 序列异步迭代，可观测 aclose。"""

    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = list(chunks)
        self._it = iter(self._chunks)
        self.aclose_called = False
        self.completion_stream = SimpleNamespace(is_closed=False)

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration

    async def aclose(self) -> None:
        self.aclose_called = True


class _SlowCloseStream(_FakeStream):
    """aclose 抛 asyncio.TimeoutError（模拟半死 socket 放弃优雅关闭）。"""

    async def aclose(self) -> None:
        raise asyncio.TimeoutError("close timeout")


class _BadCloseStream(_FakeStream):
    """aclose 抛普通异常（finally 兜底吞掉并记 debug）。"""

    async def aclose(self) -> None:
        raise RuntimeError("close boom")


def _delta(
    *,
    content: str | None = None,
    reasoning: str | None = None,
    tool_calls: list[Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
    )


def _choice(delta: SimpleNamespace, finish_reason: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(delta=delta, finish_reason=finish_reason)


def _tc(
    index: int = 0,
    *,
    id_: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> SimpleNamespace:
    fn = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=id_, function=fn)


def _chunk(
    choices: list[Any] | None = None,
    *,
    usage: SimpleNamespace | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(choices=choices if choices is not None else [], usage=usage)


def _usage(
    *,
    prompt: int = 10,
    completion: int = 5,
    total: int = 15,
    cached: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )


class _StubAdapter(_adapter._BaseLiteLLMAdapter):
    """桩 adapter：``_do_completion`` 返回预设流并记录调用 kwargs。"""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self.captured_kwargs: dict[str, Any] = {}

    async def _do_completion(self, **kwargs: Any) -> Any:
        self.captured_kwargs = dict(kwargs)
        return self._stream


class _NonStreamingAdapter(_adapter._BaseLiteLLMAdapter):
    """非流式桩 adapter：``_do_completion`` 返回预设响应或抛预设异常。"""

    def __init__(self, response: Any = None, exc: BaseException | None = None) -> None:
        self._response = response
        self._exc = exc
        self.captured_kwargs: dict[str, Any] = {}

    async def _do_completion(self, **kwargs: Any) -> Any:
        self.captured_kwargs = dict(kwargs)
        if self._exc is not None:
            raise self._exc
        return self._response


def _ns_response(
    *,
    content: str | None = None,
    reasoning: str | None = None,
    tool_calls: list[Any] | None = None,
    finish_reason: str | None = None,
    usage: SimpleNamespace | None = None,
) -> SimpleNamespace:
    msg = SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


def _ns_tc(id_: str | None, name: str, arguments: str) -> SimpleNamespace:
    return SimpleNamespace(id=id_, function=SimpleNamespace(name=name, arguments=arguments))


# ─────────────────── 非流式路径（_call_non_streaming） ───────────────────


async def test_non_streaming_usage_and_tool_calls() -> None:
    """非流式：usage 核算 + tool_calls 解析 + finish_reason 透传。"""
    ad = _NonStreamingAdapter(
        _ns_response(
            content="answer",
            tool_calls=[_ns_tc("call_1", "bash", '{"cmd":"ls"}')],
            finish_reason="tool_calls",
            usage=_usage(prompt=7, completion=3, total=10, cached=1),
        )
    )
    resp = await ad.completion("m", [{"role": "user", "content": "q"}], stream=False)
    assert resp.text == "answer"
    assert resp.tool_calls == [{"id": "call_1", "name": "bash", "arguments": '{"cmd":"ls"}'}]
    assert resp.finish_reason == "tool_calls"
    assert resp.usage == {
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "total_tokens": 10,
        "cached_tokens": 1,
    }
    # 非流式显式 float timeout + drop_params
    assert ad.captured_kwargs["timeout"] == 300.0
    assert ad.captured_kwargs["drop_params"] is True


async def test_non_streaming_tool_call_id_fallback() -> None:
    """非流式 tool_call 无 id → 回退 call_<index>。"""
    ad = _NonStreamingAdapter(
        _ns_response(content="x", tool_calls=[_ns_tc(None, "f", "{}")])
    )
    resp = await ad.completion("m", [{"role": "user", "content": "q"}], stream=False)
    assert resp.tool_calls == [{"id": "call_0", "name": "f", "arguments": "{}"}]


async def test_non_streaming_reasoning_content_fallback_to_text() -> None:
    """reasoning_content 非空且正文为空 → 用思考内容作为 result_text。"""
    ad = _NonStreamingAdapter(_ns_response(content=None, reasoning="plan"))
    resp = await ad.completion("m", [{"role": "user", "content": "q"}], stream=False)
    assert resp.thinking_text == "plan"
    assert resp.text == "plan"


async def test_non_streaming_think_tag_extraction() -> None:
    """reasoning_content 为空 → 从 content 提取 <think/> 标签（分发到 provider 插件）。"""
    ad = _NonStreamingAdapter(_ns_response(content=" " + _OPEN_THINK + "plan" + _CLOSE_THINK + "answer"))
    resp = await ad.completion("m", [{"role": "user", "content": "q"}], stream=False)
    assert resp.thinking_text == "plan"
    assert resp.text == "answer"


async def test_non_streaming_no_usage_no_thinking() -> None:
    """无 usage/无 thinking → 空 usage 与 None thinking（不误报）。"""
    ad = _NonStreamingAdapter(_ns_response(content="plain"))
    resp = await ad.completion("m", [{"role": "user", "content": "q"}], stream=False)
    assert resp.usage is None
    assert resp.thinking_text is None
    assert resp.text == "plain"


async def test_non_streaming_tools_passed_through() -> None:
    """非流式 tools 参数透传（call_kwargs 含 tools）。"""
    ad = _NonStreamingAdapter(_ns_response(content="x"))
    await ad.completion(
        "m", [{"role": "user", "content": "q"}],
        stream=False,
        tools=[{"type": "function", "function": {"name": "f"}}],
    )
    assert ad.captured_kwargs["tools"] == [{"type": "function", "function": {"name": "f"}}]


async def test_completion_stream_true_routes_to_streaming() -> None:
    """completion(stream=True) → 走流式路径（_do_completion 收到 stream=True）。"""
    stream = _FakeStream([_chunk([_choice(_delta(content="a"))]), _chunk(usage=_usage())])
    ad = _StubAdapter(stream)
    resp = await ad.completion("m", [{"role": "user", "content": "q"}], stream=True)
    assert resp.text == "a"
    assert ad.captured_kwargs["stream"] is True


async def test_health_check_success_and_failure() -> None:
    """health_check：_do_completion 成功（有 choices）→ True；异常 → False。"""
    ok = _NonStreamingAdapter(_ns_response(content="pong"))
    assert await ok.health_check("m") is True

    bad = _NonStreamingAdapter(exc=RuntimeError("boom"))
    assert await bad.health_check("m") is False


# ─────────────────── 流式辅助方法 ───────────────────


async def test_build_streaming_call_kwargs_tools_and_timeouts() -> None:
    """流式参数构造：tools 透传 + 超时解析（字符串转 float）+ 专属参数弹出。"""
    ad = _StubAdapter(_FakeStream([]))
    kwargs = {
        "first_chunk_timeout": "5",
        "inter_chunk_timeout": "7",
        "max_thinking_chars": "100",
        "temperature": 0.5,
    }
    call_kwargs, fct, ict, mtc = ad._build_streaming_call_kwargs(  # noqa: SLF001
        "m", [{"role": "user", "content": "x"}], tools=[{"type": "function"}], kwargs=kwargs
    )
    assert call_kwargs["tools"] == [{"type": "function"}]
    assert call_kwargs["timeout"] == 5.0
    assert fct == 5.0 and ict == 7.0 and mtc == 100
    assert "first_chunk_timeout" not in call_kwargs
    assert "inter_chunk_timeout" not in call_kwargs
    # max_thinking_chars 在 call_kwargs 构造后 pop（已随 **kwargs 进入，由
    # drop_params=True 丢弃）——与生产注释契约一致，值保持原样（字符串）
    assert call_kwargs["max_thinking_chars"] == "100"


async def test_open_and_first_chunk_aclose_exception_suppressed() -> None:
    """首 chunk 异常 + aclose 自身也异常 → 原始异常透传，aclose 异常被抑制。"""

    class _Stream(_FakeStream):
        def __init__(self) -> None:
            super().__init__(chunks=[])

        async def __anext__(self) -> Any:
            raise RuntimeError("first chunk failed")

        async def aclose(self) -> None:
            self.aclose_called = True
            raise RuntimeError("aclose also fails")

    stream = _Stream()
    ad = _StubAdapter(stream)
    with pytest.raises(RuntimeError, match="first chunk failed"):
        await ad._open_and_first_chunk({}, "m")  # noqa: SLF001
    assert stream.aclose_called is True


def _state(**kw: Any) -> _adapter._StreamState:
    s = _adapter._StreamState()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


async def test_handle_delta_content_in_think_tag_close() -> None:
    """标签内收到闭合标签：思考路由 thinking、闭合后正文路由 text。"""
    received: list[dict[str, Any]] = []
    ad = _StubAdapter(_FakeStream([]))
    state = _state(in_think_tag=True, on_chunk=received.append)
    ret = ad._handle_delta_content(" rest" + _CLOSE_THINK + "done", state)  # noqa: SLF001
    assert ret is False
    assert state.thinking_parts == [" rest"]
    assert state.result_parts == ["done"]
    assert state.in_think_tag is False
    assert received == [
        {"type": "thinking", "content": " rest"},
        {"type": "text", "content": "done"},
    ]


async def test_handle_delta_content_in_think_tag_stop_signal() -> None:
    """标签内闭合后 on_chunk 返回 stop → stream_repetition 置位并中断。"""
    calls = {"n": 0}

    def _cb(evt: dict[str, Any]) -> Any:
        calls["n"] += 1
        return "stop" if calls["n"] == 2 else None

    ad = _StubAdapter(_FakeStream([]))
    state = _state(in_think_tag=True, on_chunk=_cb)
    ret = ad._handle_delta_content(" rest" + _CLOSE_THINK + "done", state)  # noqa: SLF001
    assert ret is True
    assert state.stream_repetition is True


async def test_handle_delta_content_in_think_tag_accumulate_and_truncate() -> None:
    """标签内无闭合：思考累积；超 max_thinking_chars → 截断置位并中断。"""
    received: list[dict[str, Any]] = []
    ad = _StubAdapter(_FakeStream([]))
    state = _state(in_think_tag=True, on_chunk=received.append, max_thinking_chars=10)
    ret = ad._handle_delta_content("more thinking", state)  # noqa: SLF001
    assert ret is True
    assert state.thinking_truncated is True
    assert state.thinking_parts == ["more thinking"]
    assert received == [{"type": "thinking", "content": "more thinking"}]


async def test_handle_delta_content_open_tag_with_prefix_and_same_chunk_close() -> None:
    """开标签前有正文 + 同 chunk 内闭合：前文进 text、思考进 thinking、后文进 text。"""
    received: list[dict[str, Any]] = []
    ad = _StubAdapter(_FakeStream([]))
    state = _state(on_chunk=received.append)
    ret = ad._handle_delta_content("pre" + _OPEN_THINK + "inner" + _CLOSE_THINK + "rest", state)  # noqa: SLF001
    assert ret is False
    assert state.result_parts == ["pre", "rest"]
    assert state.thinking_parts == ["inner"]
    assert state.in_think_tag is False
    assert received == [
        {"type": "text", "content": "pre"},
        {"type": "thinking", "content": "inner"},
        {"type": "text", "content": "rest"},
    ]


async def test_handle_delta_content_open_tag_inner_without_close() -> None:
    """开标签后 inner 无闭合标签：inner 进 thinking、in_think_tag 保持 True。"""
    received: list[dict[str, Any]] = []
    ad = _StubAdapter(_FakeStream([]))
    state = _state(on_chunk=received.append)
    ret = ad._handle_delta_content("pre" + _OPEN_THINK + "inner", state)  # noqa: SLF001
    assert ret is False
    assert state.result_parts == ["pre"]
    assert state.thinking_parts == ["inner"]
    assert state.in_think_tag is True
    assert received == [
        {"type": "text", "content": "pre"},
        {"type": "thinking", "content": "inner"},
    ]


async def test_handle_delta_content_same_chunk_close_stop_signal() -> None:
    """同 chunk 闭合后 on_chunk 返回 stop → stream_repetition 置位并中断。"""
    calls = {"n": 0}

    def _cb(evt: dict[str, Any]) -> Any:
        calls["n"] += 1
        return "stop" if calls["n"] == 3 else None  # 第 3 个事件 = 闭合后正文

    ad = _StubAdapter(_FakeStream([]))
    state = _state(on_chunk=_cb)
    ret = ad._handle_delta_content("pre" + _OPEN_THINK + "inner" + _CLOSE_THINK + "rest", state)  # noqa: SLF001
    assert ret is True
    assert state.stream_repetition is True
    assert state.result_parts == ["pre", "rest"]


async def test_handle_delta_content_plain_text_after_thinking_emits_thinking_end() -> None:
    """标签外普通文本且已有思考内容 → 先发 thinking_end 再进 text。"""
    received: list[dict[str, Any]] = []
    ad = _StubAdapter(_FakeStream([]))
    state = _state(thinking_parts=["plan"], on_chunk=received.append)
    ret = ad._handle_delta_content("plain", state)  # noqa: SLF001
    assert ret is False
    assert state.result_parts == ["plain"]
    assert received == [
        {"type": "thinking_end", "content": ""},
        {"type": "text", "content": "plain"},
    ]


async def test_process_chunk_diag_branch_logs(monkeypatch: Any) -> None:
    """_diag_logger.handlers 非空 → 诊断分支记录 chunk 摘要。"""
    messages: list[tuple[Any, ...]] = []

    class _FakeDiagLogger:
        handlers = [object()]

        def debug(self, *args: Any, **kwargs: Any) -> None:
            messages.append(args)

    monkeypatch.setattr(_adapter, "_diag_logger", _FakeDiagLogger())
    ad = _StubAdapter(_FakeStream([]))
    ret = await ad._process_chunk(  # noqa: SLF001
        _chunk([_choice(_delta(content="hi"))]), _state()
    )
    assert ret is False
    assert messages, "诊断分支应记录 chunk 摘要"


async def test_process_chunk_thinking_end_before_tool_calls() -> None:
    """thinking→tool_calls 过渡：先发 thinking_end 再发 tool_call 事件。"""
    received: list[dict[str, Any]] = []
    ad = _StubAdapter(_FakeStream([]))
    state = _state(thinking_parts=["plan"], on_chunk=received.append)
    ret = await ad._process_chunk(  # noqa: SLF001
        _chunk([_choice(_delta(tool_calls=[_tc(0, id_="call_1", name="f", arguments="{}")]))]),
        state,
    )
    assert ret is False
    assert received == [
        {"type": "thinking_end", "content": ""},
        {"type": "tool_call", "tool_calls": [_tc(0, id_="call_1", name="f", arguments="{}")]},
    ]


# ─────────────────── _consume_stream 收尾/诊断兜底 ───────────────────


async def test_consume_stream_first_chunk_finish_reason_captured() -> None:
    """首 chunk 携带 finish_reason → 捕获进 state（接收端点诊断）。"""
    chunks = [
        _chunk([_choice(_delta(content="a"), finish_reason="stop")]),
        _chunk(usage=_usage()),
    ]
    ad = _StubAdapter(_FakeStream(chunks))
    resp = await ad._call_streaming(  # noqa: SLF001
        "m", [{"role": "user", "content": "x"}], inter_chunk_timeout=600, first_chunk_timeout=10
    )
    assert resp.finish_reason == "stop"
    assert resp.text == "a"


async def test_consume_stream_later_chunk_finish_reason_without_tool_calls() -> None:
    """后续 chunk 携带 finish_reason 但无 tool_calls（终止 stop/length chunk）→ 同样捕获。

    终止 chunk 通常只带 finish_reason 不带 tool_calls；漏记会让
    output_truncated（finish_reason=="length"）截断检测失效。
    """
    chunks = [
        _chunk([_choice(_delta(content="半截"))]),
        _chunk([_choice(_delta(), finish_reason="length")]),
        _chunk(usage=_usage()),
    ]
    ad = _StubAdapter(_FakeStream(chunks))
    resp = await ad._call_streaming(  # noqa: SLF001
        "m", [{"role": "user", "content": "x"}], inter_chunk_timeout=600, first_chunk_timeout=10
    )
    assert resp.finish_reason == "length"
    assert resp.text == "半截"


async def test_consume_stream_diag_block_exception_swallowed() -> None:
    """接收端点诊断块异常（arguments=None → len 失败）→ 吞掉不阻断流。"""
    chunks = [
        _chunk([_choice(_delta(tool_calls=[_tc(0, id_="call_1", name="f", arguments=None)]))]),
        _chunk([_choice(_delta(content="a"))]),
        _chunk(usage=_usage()),
    ]
    ad = _StubAdapter(_FakeStream(chunks))
    resp = await ad._call_streaming(  # noqa: SLF001
        "m", [{"role": "user", "content": "x"}], inter_chunk_timeout=600, first_chunk_timeout=10
    )
    assert resp.text == "a"
    assert resp.tool_calls == [{"id": "call_1", "name": "f", "arguments": ""}]


async def test_consume_stream_second_chunk_diag_exception_swallowed() -> None:
    """后续 chunk 诊断块异常（arguments=None → len 失败）→ 吞掉不阻断流。"""
    chunks = [
        _chunk([_choice(_delta(content="a"))]),
        _chunk([_choice(_delta(tool_calls=[_tc(0, id_="call_1", name="f", arguments=None)]))]),
        _chunk(usage=_usage()),
    ]
    ad = _StubAdapter(_FakeStream(chunks))
    resp = await ad._call_streaming(  # noqa: SLF001
        "m", [{"role": "user", "content": "x"}], inter_chunk_timeout=600, first_chunk_timeout=10
    )
    assert resp.text == "a"
    assert resp.tool_calls == [{"id": "call_1", "name": "f", "arguments": ""}]


async def test_consume_stream_aclose_timeout_swallowed() -> None:
    """finally aclose 抛 asyncio.TimeoutError（半死 socket）→ 放弃关闭不阻断返回。"""
    stream = _SlowCloseStream([_chunk([_choice(_delta(content="a"))]), _chunk(usage=_usage())])
    ad = _StubAdapter(stream)
    resp = await ad._call_streaming(  # noqa: SLF001
        "m", [{"role": "user", "content": "x"}], inter_chunk_timeout=600, first_chunk_timeout=10
    )
    assert resp.text == "a"


async def test_consume_stream_aclose_exception_swallowed() -> None:
    """finally aclose 抛普通异常 → 记 debug 不阻断返回。"""
    stream = _BadCloseStream([_chunk([_choice(_delta(content="a"))]), _chunk(usage=_usage())])
    ad = _StubAdapter(stream)
    resp = await ad._call_streaming(  # noqa: SLF001
        "m", [{"role": "user", "content": "x"}], inter_chunk_timeout=600, first_chunk_timeout=10
    )
    assert resp.text == "a"


# ─────────────────── 心跳探针 ───────────────────


class _FakeStreamLogger:
    """记录 log() 调用的伪 logger（心跳日志断言用）。"""

    def __init__(self) -> None:
        self.records: list[tuple[int, str, tuple[Any, ...]]] = []

    def log(self, level: int, msg: str, *args: Any) -> None:
        self.records.append((level, msg, args))


async def _run_heartbeat_once(monkeypatch: Any, idle: float) -> _FakeStreamLogger:
    """把 asyncio.sleep 打成零延迟（仍让出事件循环），跑心跳若干轮后取消。"""
    real_sleep = asyncio.sleep

    async def _noop_sleep(*args: Any, **kwargs: Any) -> None:
        await real_sleep(0)  # 让出事件循环，避免心跳循环饿死测试协程

    monkeypatch.setattr(_adapter.asyncio, "sleep", _noop_sleep)
    fake = _FakeStreamLogger()
    monkeypatch.setattr(_adapter, "_stream_logger", fake)
    ad = _StubAdapter(_FakeStream([]))
    task = asyncio.create_task(
        ad._stream_heartbeat(  # noqa: SLF001
            "m", 600, lambda: idle, lambda: 3, SimpleNamespace(is_closed=False)
        )
    )
    await real_sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return fake


async def test_stream_heartbeat_logs_debug_level(monkeypatch: Any) -> None:
    """心跳：idle 未过半 → DEBUG 档日志。"""
    fake = await _run_heartbeat_once(monkeypatch, idle=5.0)
    assert fake.records
    assert all(level == logging.DEBUG for level, _, _ in fake.records)


async def test_stream_heartbeat_logs_warning_when_idle_past_half(monkeypatch: Any) -> None:
    """心跳：idle 过半（静默即将触发超时）→ WARNING 档日志。"""
    fake = await _run_heartbeat_once(monkeypatch, idle=400.0)
    assert fake.records
    assert any(level == logging.WARNING for level, _, _ in fake.records)


# ─────────────────── _ThreadedStreamBridge ───────────────────


async def test_threaded_stream_bridge_anext_branches() -> None:
    """桥接 __anext__：队列有值返回 / 异常盒非空上抛 / done+空队列 StopAsyncIteration。"""
    q: _queue.Queue[Any] = _queue.Queue()
    q.put("chunk1")
    done = threading.Event()
    bridge = _adapter._ThreadedStreamBridge(  # noqa: SLF001
        queue=q, done_evt=done, exc_box=[], close_evt=threading.Event()
    )
    assert bridge.__aiter__() is bridge
    assert await bridge.__anext__() == "chunk1"

    bridge2 = _adapter._ThreadedStreamBridge(  # noqa: SLF001
        queue=_queue.Queue(), done_evt=done, exc_box=[ValueError("boom")], close_evt=threading.Event()
    )
    with pytest.raises(ValueError, match="boom"):
        await bridge2.__anext__()

    done.set()
    bridge3 = _adapter._ThreadedStreamBridge(  # noqa: SLF001
        queue=_queue.Queue(), done_evt=done, exc_box=[], close_evt=threading.Event()
    )
    with pytest.raises(StopAsyncIteration):
        await bridge3.__anext__()


async def test_threaded_stream_bridge_anext_waits_for_queue() -> None:
    """队列空且未 done → 短轮询等待（不抛错）；入队后返回。"""
    q: _queue.Queue[Any] = _queue.Queue()
    done = threading.Event()
    bridge = _adapter._ThreadedStreamBridge(  # noqa: SLF001
        queue=q, done_evt=done, exc_box=[], close_evt=threading.Event()
    )
    task = asyncio.create_task(bridge.__anext__())
    await asyncio.sleep(0.05)  # 让 __anext__ 进入轮询等待
    assert not task.done()
    q.put("late-chunk")
    assert await task == "late-chunk"


async def test_threaded_stream_bridge_aclose_sets_event() -> None:
    """桥接 aclose：设 close_evt 后立即返回（不等待 worker）。"""
    close_evt = threading.Event()
    bridge = _adapter._ThreadedStreamBridge(  # noqa: SLF001
        queue=_queue.Queue(), done_evt=threading.Event(), exc_box=[], close_evt=close_evt
    )
    await bridge.aclose()
    assert close_evt.is_set()


# ─────────────────── _await_with_escape 超时 ───────────────────


async def test_await_with_escape_timeout_raises() -> None:
    """协程超时未完成 → 取消并抛 asyncio.TimeoutError（含独立线程诊断）。"""

    async def _slow() -> None:
        await asyncio.sleep(60)

    with pytest.raises(asyncio.TimeoutError, match="超时"):
        await _adapter._await_with_escape(_slow(), 0.2, what="test-timeout")  # noqa: SLF001


# ─────────────────── 协议与 LiteLLM 直调 ───────────────────


def test_llm_adapter_protocol_runtime_checkable() -> None:
    """LLMAdapter 是 runtime_checkable 协议：LiteLLMAdapter 满足，普通对象不满足。"""
    assert isinstance(_adapter.LiteLLMAdapter(), _adapter.LLMAdapter)
    assert not isinstance(object(), _adapter.LLMAdapter)


async def test_protocol_placeholder_bodies() -> None:
    """协议方法体（... 占位）直接调用以覆盖定义行（协议方法不会被实例调用）。"""
    dummy = object()
    assert await _adapter.LLMAdapter.completion(dummy, "m", []) is None  # type: ignore[arg-type]
    assert await _adapter.LLMAdapter.health_check(dummy, "m") is None  # type: ignore[arg-type]


async def test_litellm_adapter_do_completion(monkeypatch: Any) -> None:
    """LiteLLMAdapter._do_completion 直调 litellm.acompletion（monkeypatch 桩）。"""
    calls: dict[str, Any] = {}

    async def _fake_acompletion(**kwargs: Any) -> Any:
        calls.update(kwargs)
        return "resp"

    monkeypatch.setattr(_adapter.litellm, "acompletion", _fake_acompletion)
    ad = _adapter.LiteLLMAdapter()
    out = await ad._do_completion(model="m", messages=[])  # noqa: SLF001
    assert out == "resp"
    assert calls["model"] == "m"
