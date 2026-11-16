# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""_WireStream 迭代契约：SSE 行源只建一次、跨 __anext__ 连续推进。

锁定 2026-11-04 实测缺陷：旧实现每次 `__anext__` 都新建一条
`response.aiter_lines()`——首个 chunk 从旧生成器返回后，第二个生成器在
httpx 半消费流上取行永久阻塞（tool_call 类 chunk 首 chunk 后挂死；MiniMax
真机同症状）。修复=惰性单例行迭代器，解析状态跨调用保留。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, cast

import httpx  # noqa: E402
import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import wire_client  # noqa: E402


def _sse(payload: dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False)


def _chunk(content: str | None = None, tool_name: str | None = None) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if tool_name is not None:
        delta["tool_calls"] = [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": tool_name, "arguments": "{}"}}
        ]
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}


class _StatefulLineSource:
    """模拟 httpx Response.aiter_lines：**只允许创建一次**行迭代器。

    旧实现第二次 __anext__ 会重新调用 aiter_lines() → 直接断言失败；
    修复后行迭代器单例复用，全程只建一次。
    """

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.create_count = 0
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def aiter_lines(self):  # type: ignore[no-untyped-def]
        self.create_count += 1
        if self.create_count > 1:
            raise AssertionError("SSE 行迭代器被重复创建（旧实现挂死点）")

        async def _gen():
            for line in self._lines:
                yield line

        return _gen()


def _collect(stream: Any, limit: int = 50) -> list[Any]:
    async def _run() -> list[Any]:
        out: list[Any] = []
        aiter = stream.__aiter__()
        for _ in range(limit):
            try:
                out.append(await asyncio.wait_for(aiter.__anext__(), timeout=2))
            except StopAsyncIteration:
                break
        return out

    return asyncio.run(_run())


def test_tool_call_chunks_iterate_after_first_chunk() -> None:
    source = _StatefulLineSource([
        _sse(_chunk(tool_name="bash_execute")),
        "",
        ": keep-alive comment",
        _sse(_chunk(content="first-token")),
        _sse(_chunk(content="second-token")),
        "event: ping",
        "",
        "data: [DONE]",
    ])
    stream = wire_client._WireStream(cast(Any, source), "stub-llm")  # noqa: SLF001
    chunks = _collect(stream)
    # 首 chunk（tool_call）+ 两段正文，共 3 个产出；[DONE] 正常终止不抛
    assert len(chunks) == 3
    assert source.create_count == 1
    assert chunks[0].choices[0].delta.tool_calls
    assert chunks[1].choices[0].delta.content == "first-token"


def test_empty_choices_without_usage_are_skipped() -> None:
    source = _StatefulLineSource([
        _sse({"choices": []}),
        _sse(_chunk(content="only")),
        _sse({"choices": [], "usage": {"total_tokens": 3}}),
        "data: [NOT-JSON]",
        "data: [DONE]",
    ])
    stream = wire_client._WireStream(cast(Any, source), "stub-llm")  # noqa: SLF001
    chunks = _collect(stream)
    assert [c.choices[0].delta.content for c in chunks if c.choices] == ["only"]
    assert chunks[-1].usage.total_tokens == 3
    assert chunks[-1].choices == []  # usage-only 尾块保留（choices 空但有 usage）


def test_anext_after_aclose_stops() -> None:
    """aclose 后 __anext__ 立即 StopAsyncIteration（关闭态守卫）。"""
    source = _StatefulLineSource([_sse(_chunk(content="late"))])
    stream = wire_client._WireStream(cast(Any, source), "stub-llm")  # noqa: SLF001
    asyncio.run(stream.aclose())
    assert source.closed is True
    with pytest.raises(StopAsyncIteration):
        asyncio.run(stream.__anext__())


class _GatedSseTransport(httpx.AsyncBaseTransport):
    """分批发 SSE 的传输替身：首批发出后挂起，等放行再续发。

    锁定 2026-11-06 dev 实测缺陷（装机同源）：stream_chat 若用
    ``client.post()``（非流式 send），httpx 会把响应体读到 EOF 才返回——SSE
    要到流结束才 EOF，全部 chunk 攒到生成结束一次性交付（前端「转圈后回复
    与工具卡一起弹出」的根因）。真流式判据：stream_chat 返回时响应体**尚未
    读完**，首 chunk 可在续批未产出的情况下迭代出来。
    """

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.exhausted = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        async def body() -> Any:
            try:
                yield (_sse(_chunk(content="你")) + "\n\n").encode()
                yield (_sse(_chunk(content="好")) + "\n\n").encode()
                await self.release.wait()
                yield b"data: [DONE]\n\n"
            finally:
                # aclose（GeneratorExit）也计入：体读完/关闭即置位
                self.exhausted = True

        return httpx.Response(200, content=body(), request=request)


def test_stream_chat_delivers_chunks_before_body_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    """stream_chat 必须真流式：响应对象先于响应体 EOF 可用，chunk 分批交付。"""
    transport = _GatedSseTransport()
    client = httpx.AsyncClient(transport=transport)
    monkeypatch.setattr(wire_client, "_shared_client", client)

    async def _run() -> None:
        ws = await asyncio.wait_for(
            wire_client.stream_chat(
                "http://gateway.test/v1", "sk-test",
                {"model": "m", "stream": True,
                 "messages": [{"role": "user", "content": "hi"}]},
                timeout=5.0, model_label="probe",
            ),
            timeout=2.0,
        )
        # 判据A：stream_chat 返回时正文未读完（首批已到、[DONE] 仍未产出）
        assert not transport.exhausted
        # 判据B：首批 chunk 在放行前即可迭代出来
        first = await ws.__anext__()
        assert first.choices[0].delta.content == "你"
        assert not transport.exhausted
        # 放行续批 → 后续 chunk 正常到、[DONE] 正常终止
        transport.release.set()
        second = await ws.__anext__()
        assert second.choices[0].delta.content == "好"
        with pytest.raises(StopAsyncIteration):
            await ws.__anext__()
        await ws.aclose()

    try:
        asyncio.run(asyncio.wait_for(_run(), timeout=5.0))
    finally:
        asyncio.run(client.aclose())
