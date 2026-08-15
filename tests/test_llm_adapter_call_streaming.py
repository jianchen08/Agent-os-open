"""``_call_streaming`` 行为等价回归测试（AsyncMock 桩）。

背景：``adapter._call_streaming`` 是流式 LLM 调用核心（首 chunk 超时检测建连 +
inter-chunk 静默超时防僵死 + usage 核算 + chunk 处理 + <think/> 状态机）。
本测试用桩流（``_FakeStream``）验证主流程编排（建连 → 消费 → 收尾）与各类 chunk
解析行为，不依赖真实 LLM / 网络。

WHY：``_call_streaming`` 即将按职责拆分为多个私有辅助方法（保持对外签名与返回
``LLMResponse`` 不变）。本测试在拆分前固化行为，拆分后须仍然全绿——任一用例变红
即说明行为发生漂移。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LLM_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "pipeline" / "core" / "llm_core"
if str(_LLM_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_LLM_CORE_DIR))

import adapter as _adapter  # noqa: E402  平铺 import，与生产代码一致
import litellm  # noqa: E402


class _HandlerlessLogger(logging.Logger):
    """``.handlers`` 恒为空的 logger：屏蔽 pytest 日志插件挂的 capture/file handler。

    生产 ``_process_chunk`` 仅在 ``_diag_logger.handlers`` 非空时才进入会崩溃的
    诊断分支（usage-only chunk 的 ``choices=[]`` 索引越界）。pytest 的日志插件会
    给所有触及的 logger 挂 ``LogCaptureHandler``，普通清空/换名都会被重新挂上，
    故用属性覆盖令 ``.handlers`` 恒返回 ``[]``。该诊断分支与本次拆分正交，生产
    代码原样保留、行为一致。
    """

    @property
    def handlers(self) -> list[Any]:
        return []

    @handlers.setter
    def handlers(self, value: Any) -> None:
        pass


@pytest.fixture(autouse=True)
def _isolate_diag_loggers(monkeypatch) -> Any:  # noqa: ANN001
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
        # adapter 读取底层流对象（心跳日志用 is_closed）
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


class _HangingStream(_FakeStream):
    """首个 __anext__ 永久挂起的流（模拟建连后半死连接）。"""

    async def __anext__(self) -> Any:
        await asyncio.sleep(1000)
        raise StopAsyncIteration


class _FirstEmptyStream(_FakeStream):
    """首个 __anext__ 即抛 StopAsyncIteration（空流）。"""

    def __init__(self) -> None:
        super().__init__(chunks=[])


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


class _RaisingAdapter(_adapter._BaseLiteLLMAdapter):
    """``_do_completion`` 直接抛指定异常（模拟建连失败/超时）。"""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def _do_completion(self, **kwargs: Any) -> Any:
        raise self._exc


# ─────────────────────────── 主流程编排 ───────────────────────────


async def test_happy_path_text_and_usage() -> None:
    """文本 chunk 累积 + usage 核算 + 正常收尾 aclose。"""
    chunks = [
        _chunk([_choice(_delta(content="Hello"))]),
        _chunk([_choice(_delta(content=" world"))]),
        _chunk(usage=_usage(prompt=10, completion=5, total=15, cached=2)),
    ]
    stream = _FakeStream(chunks)
    ad = _StubAdapter(stream)

    resp = await ad._call_streaming(
        "zai/glm-test", [{"role": "user", "content": "hi"}],
        inter_chunk_timeout=600, first_chunk_timeout=10,
    )

    assert resp.text == "Hello world"
    assert resp.thinking_text is None
    assert resp.tool_calls == []
    assert resp.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "cached_tokens": 2,
    }
    assert resp.stream_repetition is False
    assert resp.thinking_truncated is False
    # 建连参数：stream=True + include_usage + drop_params + timeout
    assert ad.captured_kwargs["stream"] is True
    assert ad.captured_kwargs["stream_options"] == {"include_usage": True}
    assert ad.captured_kwargs["drop_params"] is True
    assert ad.captured_kwargs["timeout"] == 10
    # 正常收尾关闭底层流
    assert stream.aclose_called is True


async def test_on_chunk_text_sequence() -> None:
    """on_chunk 按 text 顺序回调；thinking_end 仅在有 thinking 后触发。"""
    received: list[dict[str, Any]] = []

    def _on_chunk(evt: dict[str, Any]) -> Any:
        received.append(evt)

    chunks = [
        _chunk([_choice(_delta(content="A"))]),
        _chunk([_choice(_delta(content="B"))]),
        _chunk(usage=_usage()),
    ]
    ad = _StubAdapter(_FakeStream(chunks))
    await ad._call_streaming(
        "m", [{"role": "user", "content": "x"}],
        on_chunk=_on_chunk, inter_chunk_timeout=600, first_chunk_timeout=10,
    )
    assert received == [
        {"type": "text", "content": "A"},
        {"type": "text", "content": "B"},
    ]


async def test_reasoning_content_routes_to_thinking() -> None:
    """delta.reasoning_content → thinking_text + on_chunk thinking。"""
    received: list[dict[str, Any]] = []

    def _on_chunk(evt: dict[str, Any]) -> Any:
        received.append(evt)

    chunks = [
        _chunk([_choice(_delta(reasoning="plan"))]),
        _chunk([_choice(_delta(content="answer"))]),
        _chunk(usage=_usage()),
    ]
    ad = _StubAdapter(_FakeStream(chunks))
    resp = await ad._call_streaming(
        "m", [{"role": "user", "content": "x"}],
        on_chunk=_on_chunk, inter_chunk_timeout=600, first_chunk_timeout=10,
    )
    assert resp.thinking_text == "plan"
    assert resp.text == "answer"
    # reasoning 先到 → thinking_end 在首个 text 前补发（关闭 thinking 通道）
    assert received == [
        {"type": "thinking", "content": "plan"},
        {"type": "thinking_end", "content": ""},
        {"type": "text", "content": "answer"},
    ]


async def test_think_tag_state_machine_split_across_chunks() -> None:
    """<think/> 标签跨 chunk：开标签后的正文路由到 thinking，闭标签后回 text。"""
    chunks = [
        _chunk([_choice(_delta(content="<think>sec"))]),
        _chunk([_choice(_delta(content="ret</think>done"))]),
        _chunk(usage=_usage()),
    ]
    ad = _StubAdapter(_FakeStream(chunks))
    resp = await ad._call_streaming(
        "m", [{"role": "user", "content": "x"}], inter_chunk_timeout=600, first_chunk_timeout=10,
    )
    assert resp.thinking_text == "secret"
    assert resp.text == "done"


async def test_tool_calls_streaming_accumulation_and_finish_reason() -> None:
    """流式 tool_calls 增量合并 + finish_reason 透传。"""
    chunks = [
        _chunk([_choice(_delta(tool_calls=[_tc(0, id_="call_1", name="get_weather", arguments='{"a":')]))]),
        _chunk([_choice(
            _delta(tool_calls=[_tc(0, id_=None, name=None, arguments='1}')]),
            finish_reason="tool_calls",
        )]),
        _chunk(usage=_usage()),
    ]
    ad = _StubAdapter(_FakeStream(chunks))
    resp = await ad._call_streaming(
        "m", [{"role": "user", "content": "x"}], inter_chunk_timeout=600, first_chunk_timeout=10,
    )
    assert resp.tool_calls == [
        {"id": "call_1", "name": "get_weather", "arguments": '{"a":1}'},
    ]
    assert resp.finish_reason == "tool_calls"


async def test_reasoning_truncation_breaks_loop() -> None:
    """思考超 max_thinking_chars → 截断标志置位并中断后续 chunk 处理。"""
    seen: list[str] = []

    def _on_chunk(evt: dict[str, Any]) -> Any:
        seen.append(evt["type"])

    chunks = [
        _chunk([_choice(_delta(content="ok"))]),
        _chunk([_choice(_delta(reasoning="x" * 50))]),
        _chunk([_choice(_delta(content="never"))]),  # 截断后不应处理
    ]
    ad = _StubAdapter(_FakeStream(chunks))
    resp = await ad._call_streaming(
        "m", [{"role": "user", "content": "x"}],
        on_chunk=_on_chunk, max_thinking_chars=10,
        inter_chunk_timeout=600, first_chunk_timeout=10,
    )
    assert resp.thinking_truncated is True
    assert resp.thinking_text == "x" * 50
    # 截断发生在第二个 chunk（loop 内 break），第三个 chunk 未处理
    assert "never" not in resp.text
    assert resp.text == "ok"


async def test_on_chunk_stop_signal_sets_stream_repetition() -> None:
    """on_chunk 返回 'stop' → stream_repetition=True 并截断流。"""
    calls = {"n": 0}

    def _on_chunk(evt: dict[str, Any]) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            return "stop"
        return None

    chunks = [
        _chunk([_choice(_delta(content="A"))]),
        _chunk([_choice(_delta(content="B"))]),
        _chunk([_choice(_delta(content="C"))]),  # stop 后不应处理
    ]
    ad = _StubAdapter(_FakeStream(chunks))
    resp = await ad._call_streaming(
        "m", [{"role": "user", "content": "x"}],
        on_chunk=_on_chunk, inter_chunk_timeout=600, first_chunk_timeout=10,
    )
    assert resp.stream_repetition is True
    assert resp.text == "AB"


# ─────────────────────────── 超时 / 空流路径 ───────────────────────────


async def test_empty_stream_raises_timeout_and_closes() -> None:
    """首字节即空流（建连成功零 chunk）→ litellm.Timeout，且 stream 被 aclose。"""
    stream = _FirstEmptyStream()
    ad = _StubAdapter(stream)
    with pytest.raises(litellm.Timeout):
        await ad._call_streaming(
            "m", [{"role": "user", "content": "x"}],
            inter_chunk_timeout=600, first_chunk_timeout=10,
        )
    # _open_and_first_chunk 错误路径必须 aclose 释放连接
    assert stream.aclose_called is True


async def test_first_chunk_timeout_raises_litellm_timeout() -> None:
    """建连阶段超时（_do_completion 抛 asyncio.TimeoutError）→ litellm.Timeout。"""
    ad = _RaisingAdapter(asyncio.TimeoutError())
    with pytest.raises(litellm.Timeout):
        await ad._call_streaming(
            "m", [{"role": "user", "content": "x"}],
            inter_chunk_timeout=600, first_chunk_timeout=10,
        )


async def test_inter_chunk_timeout_raises_litellm_timeout() -> None:
    """inter-chunk 静默超时（后续 __anext__ 抛 asyncio.TimeoutError）→ litellm.Timeout。"""

    class _OneThenTimeout(_FakeStream):
        def __init__(self) -> None:
            super().__init__([_chunk([_choice(_delta(content="first"))])])
            self._served = False

        async def __anext__(self) -> Any:
            if not self._served:
                self._served = True
                return next(self._it)
            raise asyncio.TimeoutError()

    ad = _StubAdapter(_OneThenTimeout())
    with pytest.raises(litellm.Timeout):
        await ad._call_streaming(
            "m", [{"role": "user", "content": "x"}],
            inter_chunk_timeout=600, first_chunk_timeout=10,
        )
