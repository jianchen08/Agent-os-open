# @feature: FP-T07 llm api | @ci: python-coverage
"""适配服务面 wire 机器（复制改造件）行为测试——SQLite 无关、零真实外呼。

覆盖复制件 ``plugins/shared/llm_core/llm_wire.py``（源 =
``plugins/shared/llm/wire_client.py`` @ c0fe6922fc，见文件头复制锚点）：

- build_body 词汇（stream/stream_options/tools/extra None 过滤、字段序）；
- SSE 行解析（data:/注释/空行/非 JSON/[DONE]/空尾 usage 块/远端断流）；
- 连接池单例语义（32/16 上限、trust_env=False 直连、关闭后可重建）；
- HTTP 状态 → 失败契约 code 映射（rate_limit/auth/service_down 等）；
- post_chat 非流式归一（strip stream 键、choices/message/usage）；
- 输出完即清：aclose 后释放响应与行迭代器引用（内存定案 §8 条款 2）。

真实依赖：httpx（MockTransport 走真实 httpx 序列化/解析栈，不触网）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
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

import llm_wire  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_shared_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """每个用例独立连接池槽位；旧池关闭，避免跨用例串用 transport。"""
    monkeypatch.setattr(llm_wire, "_shared_client", None)
    yield
    client = llm_wire._shared_client
    if client is not None:
        asyncio.run(client.aclose())
    llm_wire._shared_client = None


def _sse(payload: dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False)


def _chunk(content: str | None = None, *, reasoning: str | None = None, tool: tuple[str, str] | None = None) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if tool is not None:
        name, args = tool
        delta["tool_calls"] = [
            {"index": 0, "id": "call_1", "function": {"name": name, "arguments": args}}
        ]
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}


class _LineResponse:
    """httpx Response 替身：只提供 _WireStream 消费的行源与关闭面。"""

    def __init__(self, lines: list[str], *, with_aclose: bool = True) -> None:
        self._lines = lines
        self.create_count = 0
        self.closed = False
        self._with_aclose = with_aclose

    def aiter_lines(self) -> Any:
        self.create_count += 1
        if self.create_count > 1:
            raise AssertionError("SSE 行迭代器被重复创建（半消费流挂死点）")

        async def _gen() -> Any:
            for line in self._lines:
                yield line

        return _gen()

    async def aclose(self) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True


class _CloseLessResponse:
    """无 aclose 的响应形态（close 兜底分支）。"""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _collect(stream: Any) -> list[Any]:
    async def _run() -> list[Any]:
        out: list[Any] = []
        aiter = stream.__aiter__()
        for _ in range(100):
            try:
                out.append(await asyncio.wait_for(aiter.__anext__(), timeout=2))
            except StopAsyncIteration:
                break
        return out

    return asyncio.run(_run())


# ─────────────────────────── build_body ───────────────────────────


def test_build_body_stream_default_includes_usage_options() -> None:
    """默认流式 body：model/messages 在前，stream + include_usage 齐备。"""
    messages = [{"role": "user", "content": "hi"}]
    body = llm_wire.build_body("up-model", messages)
    assert list(body) == ["model", "messages", "stream", "stream_options"]
    assert body["model"] == "up-model"
    assert body["messages"] is messages
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}


def test_build_body_non_stream_omits_stream_keys_and_honours_tools_extra() -> None:
    """非流式：不携带 stream 键；tools 空不出现；extra None 值过滤、其余平铺。"""
    body = llm_wire.build_body(
        "m",
        [{"role": "user", "content": "x"}],
        tools=[],
        stream=False,
        extra={"temperature": 0.0, "max_tokens": None, "thinking": {"type": "disabled"}},
    )
    assert "stream" not in body
    assert "stream_options" not in body
    assert "tools" not in body
    assert body["temperature"] == 0.0  # 0.0 不是 None——不得被过滤
    assert "max_tokens" not in body
    assert body["thinking"] == {"type": "disabled"}


def test_build_body_tools_present_when_non_empty() -> None:
    tools = [{"type": "function", "function": {"name": "bash"}}]
    body = llm_wire.build_body("m", [], tools=tools, stream=True)
    assert body["tools"] is tools


# ─────────────────────────── SSE 解析 ───────────────────────────


def test_wire_stream_parses_text_reasoning_tool_calls_and_usage_tail() -> None:
    """SSE 行解析：注释/空行/非 data 行跳过，非 JSON 忽略，usage 空尾块保留。"""
    source = _LineResponse(
        [
            _sse(_chunk(reasoning="想一想")),
            "",
            ": keep-alive",
            "event: ping",
            _sse(_chunk(content="你")),
            "data: [NOT-JSON]",
            _sse(_chunk(tool=("bash", '{"cmd": "ls"}'))),
            _sse({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8,
                                           "prompt_tokens_details": {"cached_tokens": 2}}}),
            "data: [DONE]",
        ]
    )
    chunks = _collect(llm_wire._WireStream(cast(Any, source), "stub"))
    assert source.create_count == 1
    assert chunks[0].choices[0].delta.reasoning_content == "想一想"
    assert chunks[1].choices[0].delta.content == "你"
    tc = chunks[2].choices[0].delta.tool_calls[0]
    assert tc.id == "call_1"
    assert tc.function.name == "bash"
    assert tc.function.arguments == '{"cmd": "ls"}'
    usage = chunks[-1].usage
    assert chunks[-1].choices == []
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (3, 5, 8)
    assert usage.prompt_tokens_details.cached_tokens == 2


def test_wire_stream_remote_protocol_error_ends_stream() -> None:
    """上游中途断流（RemoteProtocolError）→ 归一为流结束（半截语义）。"""

    class _BrokenLineResponse(_LineResponse):
        def aiter_lines(self) -> Any:
            async def _gen() -> Any:
                yield _sse(_chunk(content="half"))
                raise httpx.RemoteProtocolError("upstream reset")

            return _gen()

    chunks = _collect(llm_wire._WireStream(cast(Any, _BrokenLineResponse([])), "stub"))
    assert [c.choices[0].delta.content for c in chunks] == ["half"]


def test_wire_stream_aclose_releases_buffers_and_stops_iteration() -> None:
    """输出完即清：aclose 关闭响应并释放响应/行迭代器引用。"""
    source = _LineResponse([_sse(_chunk(content="late"))])
    stream = llm_wire._WireStream(cast(Any, source), "stub")
    asyncio.run(stream.aclose())
    assert source.closed is True
    assert stream._response is None
    assert stream._lines is None
    with pytest.raises(StopAsyncIteration):
        asyncio.run(stream.__anext__())
    # 幂等：重复 aclose 不重复关闭底层
    asyncio.run(stream.aclose())


def test_wire_stream_aclose_falls_back_to_close() -> None:
    """底层无 aclose（同步 close 形态）→ close 兜底。"""
    response = _CloseLessResponse()
    stream = llm_wire._WireStream(cast(Any, response), "stub")
    asyncio.run(stream.aclose())
    assert response.closed is True


# ─────────────────────────── 连接池 ───────────────────────────


def test_shared_client_is_singleton_with_bounded_stateless_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """连接池无状态（§8 条款 4）：单例、有界 32/16、trust_env=False 出口直连。"""
    captured: dict[str, Any] = {}
    real_client = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        captured.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_wire.httpx, "AsyncClient", _factory)
    client = llm_wire._get_client()
    assert llm_wire._get_client() is client
    assert client.trust_env is False
    limits = captured["limits"]
    assert limits.max_connections == 32
    assert limits.max_keepalive_connections == 16


def test_aclose_shared_client_is_idempotent_and_rebuilds() -> None:
    """关闭幂等；关闭后再次取用重建活池（无残留会话）。"""
    llm_wire._get_client()
    asyncio.run(llm_wire.aclose_shared_client())
    assert llm_wire._shared_client is None
    asyncio.run(llm_wire.aclose_shared_client())  # 无池可关也不报错
    rebuilt = llm_wire._get_client()
    assert rebuilt.is_closed is False


# ─────────────────────────── stream_chat / post_chat ───────────────────────────


def _mock_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("status", "kind"),
    [(400, "bad_request"), (401, "auth"), (403, "auth"), (404, "bad_request"), (429, "rate_limit"), (500, "service_down")],
)
def test_stream_chat_http_status_maps_failure_code(
    monkeypatch: pytest.MonkeyPatch, status: int, kind: str
) -> None:
    """HTTP 非 200 → WireHTTPError（状态码译失败契约 code，冷却机直接消费）。"""

    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="upstream says no", request=request)

    monkeypatch.setattr(llm_wire, "_shared_client", _mock_client(_handler))

    async def _run() -> None:
        with pytest.raises(llm_wire.WireHTTPError) as ei:
            await llm_wire.stream_chat("http://gw.test/v1", "sk-1", {"model": "m"}, timeout=5.0)
        assert ei.value.status_code == status
        assert ei.value.code == kind

    asyncio.run(_run())


def test_stream_chat_sends_authorized_openai_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """stream_chat 发 POST {base}/chat/completions，Bearer 鉴权，body 原样。"""
    seen: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = json.loads(request.content.decode("utf-8"))

        async def _body() -> Any:
            yield (_sse(_chunk(content="ok")) + "\n\n").encode()
            yield b"data: [DONE]\n\n"

        return httpx.Response(200, content=_body(), request=request)

    monkeypatch.setattr(llm_wire, "_shared_client", _mock_client(_handler))

    body = {"model": "up", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    async def _run() -> Any:
        stream = await llm_wire.stream_chat("http://gw.test/v1", "sk-1", body, timeout=5.0, model_label="m")
        first = await stream.__anext__()
        await stream.aclose()
        return first

    first = asyncio.run(_run())
    assert first.choices[0].delta.content == "ok"
    assert seen["method"] == "POST"
    assert seen["url"] == "http://gw.test/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-1"
    assert seen["content_type"] == "application/json"
    assert seen["body"] == body


def test_post_chat_strips_stream_keys_and_normalizes_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非流式：wire body 去掉 stream/stream_options；响应归一 message/usage。"""
    seen: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "model": "up",
                "choices": [
                    {
                        "message": {
                            "content": "正文",
                            "reasoning_content": "思考",
                            "tool_calls": [{"id": "call_9", "function": {"name": "f", "arguments": "{}"}}],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            },
            request=request,
        )

    monkeypatch.setattr(llm_wire, "_shared_client", _mock_client(_handler))
    response = asyncio.run(
        llm_wire.post_chat(
            "http://gw.test/v1/",
            "sk-2",
            {"model": "up", "messages": [], "stream": True, "stream_options": {"include_usage": True}},
            timeout=5.0,
        )
    )
    assert "stream" not in seen["body"]
    assert "stream_options" not in seen["body"]
    choice = response.choices[0]
    assert choice.message.content == "正文"
    assert choice.message.reasoning_content == "思考"
    assert choice.message.tool_calls[0].function.name == "f"
    assert choice.finish_reason == "tool_calls"
    assert response.usage.total_tokens == 3
    assert response.model == "up"


def test_post_chat_http_error_maps_service_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down", request=request)

    monkeypatch.setattr(llm_wire, "_shared_client", _mock_client(_handler))
    with pytest.raises(llm_wire.WireHTTPError) as ei:
        asyncio.run(llm_wire.post_chat("http://gw.test/v1", "k", {"model": "m"}, timeout=1.0))
    assert ei.value.code == "service_down"


def test_wire_timeout_error_is_runtime_error() -> None:
    """WireTimeoutError 是 RuntimeError 子类（上层错误链按传输类判别）。"""
    assert issubclass(llm_wire.WireTimeoutError, RuntimeError)
    ns = SimpleNamespace(a=1)
    assert ns.a == 1
