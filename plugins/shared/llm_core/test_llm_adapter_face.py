# @feature: FP-T07 llm api | @ci: python-coverage
"""llm 适配服务面（``llm_adapter.execute``）行为测试。

契约（主方案 §6.2 / ADR 2026-11-18-llm-two-tier-plugin-convergence）：
- 一个 manifest / 一个进程两个服务入口：适配面 = wire 机器 + 方言路由所有者；
- 输入 = 模型（yaml id）+ messages + tools + 采样参数；输出 = 与
  ``llm.complete_stream`` 同构的聚合 dict（+ dialect/partial）；
- 配置面读取 = 注入 llm.yaml 视图（providers.<name>.type 方言键 /
  api_base / api_key）；缺模型 / 缺 api_base / 未解析占位符 / 未注册方言
  全部 fail-closed；
- 内存定案 §8：连接池无状态复用、输出完即清（半截累积器归零）、
  适配面实例零 per-request 状态、无跨请求累积；
- 半截快照 / 取消 / 退化停止与网关契约同形。

真实依赖：httpx（MockTransport 走真实 httpx 序列化/SSE 解析栈，不触网）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

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

import llm_adapter  # noqa: E402
import llm_dialects  # noqa: E402
import llm_wire  # noqa: E402

_LLM_DATA: dict[str, Any] = {
    "providers": {
        "openai_prov": {"type": "openai", "api_base": "http://gw.test/v1", "api_key": "sk-openai"},
        "zai_prov": {
            "type": "zai",
            "api_base": "http://zai.test/v4",
            "keys": [{"id": "zai_main", "api_key": "sk-zai"}],
        },
        "no_base": {"type": "openai", "api_key": "sk-x"},
        "placeholder": {"type": "openai", "api_base": "http://p.test/v1", "api_key": "${MISSING_KEY_FOR_TEST}"},
        "native_proto": {"type": "anthropic", "api_base": "http://anthropic.test/v1", "api_key": "sk-a"},
    },
    "models": {
        "chat-model": {
            "provider": "openai_prov",
            "model_name": "up-chat",
            "default_params": {"temperature": 0.1, "max_tokens": 256},
        },
        "zai-model": {"provider": "zai_prov", "model_name": "glm-x"},
        "no-base-model": {"provider": "no_base", "model_name": "nb"},
        "placeholder-model": {"provider": "placeholder", "model_name": "ph"},
        "native-model": {"provider": "native_proto", "model_name": "native"},
    },
}


def _sse(payload: dict[str, Any]) -> bytes:
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()


def _chunk(content: str | None = None, *, reasoning: str | None = None, tool: tuple[str, str, str] | None = None) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if tool is not None:
        tool_id, name, args = tool
        delta["tool_calls"] = [{"index": 0, "id": tool_id, "function": {"name": name, "arguments": args}}]
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}


def _sse_response(lines: list[bytes], request: httpx.Request) -> httpx.Response:
    async def _body() -> Any:
        for line in lines:
            yield line

    return httpx.Response(200, content=_body(), request=request)


def _face(data: dict[str, Any] | None = None) -> llm_adapter.LLMAdapterFace:
    payload = _LLM_DATA if data is None else data
    return llm_adapter.LLMAdapterFace(config_provider=lambda: {"llm": payload})


@pytest.fixture(autouse=True)
def _isolated_pool(monkeypatch: pytest.MonkeyPatch) -> Any:
    """每用例独立连接池槽位（关旧池），池语义自身在 wire 测试里单测。"""
    monkeypatch.setattr(llm_wire, "_shared_client", None)
    yield
    client = llm_wire._shared_client
    if client is not None:
        asyncio.run(client.aclose())
    llm_wire._shared_client = None


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    monkeypatch.setattr(llm_wire, "_shared_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))


# ─────────────────────────── 流式聚合 ───────────────────────────


def test_stream_aggregates_text_thinking_tool_calls_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """流式：归一 chunk 聚合为与 llm.complete_stream 同构的响应 + conduit 钩子事件。"""
    seen: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode("utf-8"))
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return _sse_response(
            [
                _sse(_chunk(reasoning="想一想")),
                _sse(_chunk(content="答")),
                _sse(_chunk(tool=("call_1", "bash", '{"cmd": "ls"}'))),
                _sse({"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}),
                b"data: [DONE]\n\n",
            ],
            request,
        )

    _install_transport(monkeypatch, _handler)
    chunks: list[dict[str, Any]] = []
    result = asyncio.run(
        _face().execute(
            model="chat-model",
            messages=[{"role": "user", "content": "hi"}],
            on_chunk=chunks.append,
        )
    )

    assert seen["url"] == "http://gw.test/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-openai"
    body = seen["body"]
    assert body["model"] == "up-chat"
    # default_params 回填：entries 缺省参数下发，显式 params 优先
    assert body["temperature"] == 0.1
    assert body["max_tokens"] == 256
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}

    assert result["status"] == "streamed"
    assert result["dialect"] == "openai-compat"
    assert result["text"] == "答"
    assert result["thinking_text"] == "想一想"
    assert result["tool_calls"] == [{"id": "call_1", "name": "bash", "arguments": '{"cmd": "ls"}'}]
    assert result["usage"] == {
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "total_tokens": 10,
        "cached_tokens": 0,
    }
    assert result["stream_repetition"] is False
    assert result["partial"] is None
    assert result["stream_id"].startswith("stream_")

    types = [c["type"] for c in chunks]
    assert types == ["thinking", "thinking_end", "text", "thinking_end", "tool_call"]
    assert chunks[2] == {"type": "text", "content": "答"}


def test_explicit_params_override_model_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式 params > llm.yaml default_params（setdefault 语义）。"""
    seen: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return _sse_response([_sse(_chunk(content="ok")), b"data: [DONE]\n\n"], request)

    _install_transport(monkeypatch, _handler)
    asyncio.run(
        _face().execute(
            model="chat-model",
            messages=[{"role": "user", "content": "hi"}],
            params={"temperature": 0.9, "thinking": {"type": "disabled"}},
        )
    )
    assert seen["body"]["temperature"] == 0.9
    assert seen["body"]["max_tokens"] == 256
    assert seen["body"]["thinking"] == {"type": "disabled"}


def test_non_stream_uses_post_and_omits_stream_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """非流式：wire body 无 stream 键，响应归一为聚合 dict。"""
    seen: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "非流式答"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
            request=request,
        )

    _install_transport(monkeypatch, _handler)
    result = asyncio.run(
        _face().execute(
            model="chat-model",
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
        )
    )
    assert "stream" not in seen["body"]
    assert "stream_options" not in seen["body"]
    assert result["status"] == "streamed"
    assert result["text"] == "非流式答"
    assert result["finish_reason"] == "stop"
    assert result["usage"]["total_tokens"] == 2


def test_on_chunk_stop_truncates_and_flags_repetition(monkeypatch: pytest.MonkeyPatch) -> None:
    """conduit 钩子返回 "stop" → 截断输出并置退化标记（finish_reason 收敛 length）。

    源契约保持：首块返回值不参与中断判定（下一 chunk 到达时才截断）。
    """

    async def _handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [_sse(_chunk(content="重复")), _sse(_chunk(content="重复")), b"data: [DONE]\n\n"],
            request,
        )

    _install_transport(monkeypatch, _handler)
    result = asyncio.run(
        _face().execute(
            model="chat-model",
            messages=[{"role": "user", "content": "hi"}],
            on_chunk=lambda chunk: "stop" if chunk.get("type") == "text" else None,
        )
    )
    assert result["stream_repetition"] is True
    assert result["finish_reason"] == "length"
    assert result["text"] == "重复重复"


# ─────────────────────────── 半截快照 / 取消 ───────────────────────────


def test_mid_stream_error_returns_partial_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """流已开始且有内容 → 中途异常以 partial 交付（异常不跨服务边界）。"""

    async def _handler(request: httpx.Request) -> httpx.Response:
        async def _body() -> Any:
            yield _sse(_chunk(content="半截"))
            raise RuntimeError("upstream exploded")

        return httpx.Response(200, content=_body(), request=request)

    _install_transport(monkeypatch, _handler)
    result = asyncio.run(
        _face().execute(model="chat-model", messages=[{"role": "user", "content": "hi"}])
    )
    assert result["status"] == "error"
    assert result["partial"]["text"] == "半截"
    assert result["llm_error_info"]["error_type"] == "RuntimeError"
    assert "exploded" in result["llm_error_info"]["error_message"]


def test_pre_started_cancel_returns_interrupted(monkeypatch: pytest.MonkeyPatch) -> None:
    """停止信号在首块前落地 → interrupted（空快照，不抛异常）。"""

    async def _handler(request: httpx.Request) -> httpx.Response:
        async def _body() -> Any:
            await asyncio.sleep(0.2)
            yield _sse(_chunk(content="迟到"))

        return httpx.Response(200, content=_body(), request=request)

    _install_transport(monkeypatch, _handler)
    cancel_event = asyncio.Event()

    async def _run() -> dict[str, Any]:
        cancel_event.set()
        return await _face().execute(
            model="chat-model",
            messages=[{"role": "user", "content": "hi"}],
            cancel_event=cancel_event,
        )

    result = asyncio.run(_run())
    assert result["status"] == "interrupted"
    assert result["finish_reason"] == "interrupted"
    assert result["partial"] is not None


def test_mid_stream_cancel_returns_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    """流静默期停止信号 → 已累积内容经 partial 交付（停止延迟=信号传播）。"""

    async def _handler(request: httpx.Request) -> httpx.Response:
        async def _body() -> Any:
            yield _sse(_chunk(content="半"))
            await asyncio.sleep(0.3)
            yield _sse(_chunk(content="尾"))

        return httpx.Response(200, content=_body(), request=request)

    _install_transport(monkeypatch, _handler)
    cancel_event = asyncio.Event()

    async def _run() -> dict[str, Any]:
        async def _cancel_soon() -> None:
            await asyncio.sleep(0.05)
            cancel_event.set()

        task = asyncio.create_task(_cancel_soon())
        try:
            return await _face().execute(
                model="chat-model",
                messages=[{"role": "user", "content": "hi"}],
                cancel_event=cancel_event,
            )
        finally:
            await task

    result = asyncio.run(_run())
    assert result["status"] == "interrupted"
    assert result["partial"]["text"] == "半"


# ─────────────────────────── fail-closed 配置面 ───────────────────────────


def test_unknown_model_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="nope-model"):
        asyncio.run(_face().execute(model="nope-model", messages=[{"role": "user", "content": "hi"}]))


def test_missing_api_base_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="api_base"):
        asyncio.run(_face().execute(model="no-base-model", messages=[{"role": "user", "content": "hi"}]))


def test_unresolved_key_placeholder_fails_closed_before_http() -> None:
    with pytest.raises(RuntimeError, match="占位符"):
        asyncio.run(_face().execute(model="placeholder-model", messages=[{"role": "user", "content": "hi"}]))


def test_unknown_dialect_fails_closed() -> None:
    with pytest.raises(llm_dialects.DialectNotFoundError, match="anthropic"):
        asyncio.run(_face().execute(model="native-model", messages=[{"role": "user", "content": "hi"}]))


def test_explicit_dialect_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式 dialect 参数优先于 providers.type（供上层钉死方言）。"""

    async def _handler(request: httpx.Request) -> httpx.Response:
        return _sse_response([_sse(_chunk(content="ok")), b"data: [DONE]\n\n"], request)

    _install_transport(monkeypatch, _handler)
    result = asyncio.run(
        _face().execute(
            model="zai-model",
            messages=[{"role": "user", "content": "hi"}],
            dialect="openai-compat",
        )
    )
    assert result["dialect"] == "openai-compat"


def test_invalid_timeout_value_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """timeout 参数非数值 → 落 180s 兜底（不因配置脏值炸调用）。"""
    seen: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}},
            request=request,
        )

    _install_transport(monkeypatch, _handler)
    result = asyncio.run(
        _face().execute(
            model="chat-model",
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
            params={"timeout": "not-a-number"},
        )
    )
    assert result["text"] == "ok"
    assert seen["body"]["model"] == "up-chat"


def test_task_level_cancel_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """任务级取消原样传播（不转成业务返回）；半截累积器随 finally 清空。"""

    async def _handler(request: httpx.Request) -> httpx.Response:
        async def _body() -> Any:
            yield _sse(_chunk(content="半"))
            await asyncio.sleep(0.5)
            yield _sse(_chunk(content="尾"))

        return httpx.Response(200, content=_body(), request=request)

    _install_transport(monkeypatch, _handler)

    async def _run() -> None:
        task = asyncio.create_task(
            _face().execute(model="chat-model", messages=[{"role": "user", "content": "hi"}])
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())


# ─────────────────────────── 内存定案（§8） ───────────────────────────


def test_face_holds_no_per_request_state_and_reuses_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """无跨请求累积：实例字段不随请求增长；连接池句柄跨请求复用（无状态）。"""

    async def _handler(request: httpx.Request) -> httpx.Response:
        return _sse_response([_sse(_chunk(content="ok")), b"data: [DONE]\n\n"], request)

    _install_transport(monkeypatch, _handler)
    face = _face()
    face_attrs = set(vars(face))
    adapter_attrs = set(vars(face._adapter))  # noqa: SLF001 —— 内部适配器无 per-request 槽位

    asyncio.run(face.execute(model="chat-model", messages=[{"role": "user", "content": "a"}]))
    pool_before = llm_wire._shared_client
    asyncio.run(face.execute(model="chat-model", messages=[{"role": "user", "content": "b"}]))
    pool_after = llm_wire._shared_client

    assert set(vars(face)) == face_attrs
    assert set(vars(face._adapter)) == adapter_attrs  # noqa: SLF001
    assert pool_before is pool_after


def test_get_face_is_process_singleton() -> None:
    """服务入口单例：同一进程复用同一适配面；reset 后可重建（on_unload 用）。"""
    first = llm_adapter.get_face()
    assert llm_adapter.get_face() is first
    llm_adapter.reset_face()
    assert llm_adapter.get_face() is not first


def test_default_config_provider_reads_injected_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺省配置源 = _config_models 注入视图（server on_load 注入同源）。"""
    import _config_models

    monkeypatch.setattr(_config_models, "_config", {"llm": _LLM_DATA})

    async def _handler(request: httpx.Request) -> httpx.Response:
        return _sse_response([_sse(_chunk(content="ok")), b"data: [DONE]\n\n"], request)

    _install_transport(monkeypatch, _handler)
    result = asyncio.run(llm_adapter.execute(model="zai-model", messages=[{"role": "user", "content": "hi"}]))
    assert result["status"] == "streamed"
    assert result["text"] == "ok"


# ─────────────────────────── 服务声明接线 ───────────────────────────


def test_server_registers_adapter_service_matching_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    """server.py 注册 ``llm_adapter.execute`` 工具 + plugin.json 声明同形（G2 无漂移）。"""
    name = "llm_core_server_adapter_face_test"
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _LLM_CORE_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)

    tool = module.plugin._tools["llm_adapter.execute"]  # noqa: SLF001
    manifest = json.loads((_LLM_CORE_DIR / "plugin.json").read_text(encoding="utf-8"))
    declared = {svc["name"]: svc for svc in manifest["capabilities"]["services"]}
    assert "llm_adapter.execute" in declared
    assert declared["llm_adapter.execute"]["input_schema"] == tool.schema
    assert declared["llm_adapter.execute"]["description"] == tool.description

    # 入口 handler 委托适配面（service 面 → face 单例接线在位）
    calls: list[dict[str, Any]] = []

    async def _stub(model: str, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        calls.append({"model": model, "messages": messages, "kwargs": kwargs})
        return {"status": "streamed", "text": "ok"}

    monkeypatch.setattr(module.llm_adapter, "execute", _stub)
    out = asyncio.run(
        module.llm_adapter_execute("chat-model", [{"role": "user", "content": "hi"}], stream=True)
    )
    assert out == {"status": "streamed", "text": "ok"}
    assert calls[0]["model"] == "chat-model"
    assert calls[0]["kwargs"]["stream"] is True


def test_on_unload_resets_face_and_closes_wire_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """卸载钩子：适配面单例槽位清空 + wire 连接池整池关闭（不留半开会话）。"""
    name = "llm_core_server_unload_face_test"
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _LLM_CORE_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)

    llm_adapter.get_face()  # 填单例槽位（构造无副作用）
    assert llm_adapter.get_face.cache_info().currsize == 1

    closed: list[bool] = []

    class _FakeClient:
        is_closed = False

        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr(llm_wire, "_shared_client", _FakeClient())
    asyncio.run(module._on_unload({}))

    assert llm_adapter.get_face.cache_info().currsize == 0
    assert closed == [True]
    assert llm_wire._shared_client is None
