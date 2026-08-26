# @feature: FP-0.2.LLM 流式服务契约 | @vision: V3 可嵌入 | @ci: python-coverage
"""llm.complete_stream 服务端行为测试（断流兜底 / 信封 / 返回值）。

验证：
- stream=True 调用 adapter，chunk 经 event-bus.emit 推送，信封含
  thread_id/pipeline_id/message_id/sequence
- 正常结束：usage → finish{reason:stop} 由服务端收尾；返回 {"status":"streamed","stream_id":...}
- 异常/断流：finish 前抛错 → 兜底补发 finish{reason:error}，异常不吞
- event-bus 未注入（KeyError）→ 服务仍可调用（流式推送降级，返回值照常）
- 流式推送失败（notify 抛错）不阻断主流程（fire-and-forget 语义）
- keepalive：流静默超过阈值时发心跳（不打断块序列）
- llm.complete 已退役：注册表无该工具、模块无处理器

测试断行为不断实现：断言事件序列/载荷/返回值，不断言内部队列等私有细节。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_server() -> Any:
    """按显式路径加载 llm 插件 server 模块（唯一模块名隔离同名 server.py）。"""
    mod_name = "llm_server_streaming_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None, "cannot load llm plugin server.py"
    assert spec.loader is not None, "cannot load llm plugin server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class FakeBus:
    """记录 emit 调用的伪 event-bus capability handle。"""

    def __init__(self, fail: bool = False) -> None:
        self.emits: list[tuple[str, dict[str, Any]]] = []
        self._fail = fail

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if self._fail:
            raise RuntimeError("bus down")
        self.emits.append((method, params))


class FakeAdapter:
    """伪 adapter：按 on_chunk 契约投递归一化 chunk，返回伪 LLMResponse。

    与真实 adapter（_BaseLiteLLMAdapter._call_streaming）的行为对齐：
    completion 内部迭代并把归一化 chunk 逐条调 on_chunk，随后返回
    usage/finish_reason。``connect_exc`` 在首 chunk 前抛（建连失败）；
    ``mid_stream_exc`` 在发送 ``mid_stream_after`` 个 chunk 后抛（流中断）。
    """

    def __init__(
        self,
        chunks: list[dict[str, Any]] | None = None,
        connect_exc: BaseException | None = None,
        mid_stream_exc: BaseException | None = None,
        mid_stream_after: int = 0,
        usage: dict[str, Any] | None = None,
        finish_reason: str | None = "stop",
        chunk_delay: float = 0.0,
        text: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        thinking_text: str | None = None,
    ) -> None:
        self._chunks = list(chunks or [])
        self._connect_exc = connect_exc
        self._mid_stream_exc = mid_stream_exc
        self._mid_stream_after = mid_stream_after
        self._usage = usage
        self._finish_reason = finish_reason
        self._chunk_delay = chunk_delay
        self._text = text
        self._tool_calls = tool_calls
        self._thinking_text = thinking_text
        self.calls: list[dict[str, Any]] = []

    async def completion(self, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        self.calls.append(kwargs)
        if self._connect_exc is not None:
            raise self._connect_exc
        on_chunk = kwargs.get("on_chunk")
        for i, c in enumerate(self._chunks):
            if self._mid_stream_exc is not None and i >= self._mid_stream_after:
                raise self._mid_stream_exc
            if on_chunk is not None:
                on_chunk(c)
            if self._chunk_delay:
                await asyncio.sleep(self._chunk_delay)
        if self._mid_stream_exc is not None:
            raise self._mid_stream_exc
        return SimpleNamespace(
            text=self._text,
            tool_calls=self._tool_calls or [],
            thinking_text=self._thinking_text,
            usage=self._usage,
            finish_reason=self._finish_reason,
        )

    async def health_check(self, model: str) -> bool:
        return True


def _text(content: str) -> dict[str, Any]:
    return {"type": "text", "content": content}


def _inject(module: Any, name: str, handle: Any) -> None:
    """把伪 capability 注入插件（覆盖 get_capability 的返回）。"""
    original = module.plugin.get_capability
    module.plugin.get_capability = lambda n: handle if n == name else original(n)  # type: ignore[method-assign]


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_ENVELOPE_KEYS = {"thread_id", "pipeline_id", "message_id", "sequence"}
_STREAM_EVENTS = {
    "block_start",
    "text_delta",
    "reasoning_delta",
    "tool_call_delta",
    "block_end",
    "usage",
    "finish",
    "keepalive",
}


class TestCompleteStream:
    """llm.complete_stream 行为测试。"""

    def _call(
        self,
        module: Any,
        bus: FakeBus,
        adapter: FakeAdapter,
        *,
        model: str = "glm-5.2",
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        _inject(module, "event-bus", bus)
        module._adapter = adapter
        return _run(
            module.llm_complete_stream(
                model=model,
                messages=[{"role": "user", "content": "hi"}],
                tools=tools,
            )
        )

    def test_streams_via_event_bus_with_envelope(self) -> None:
        """text 流：chunk 经 event-bus.emit 推送，信封含 thread_id/pipeline_id/message_id/sequence。"""
        mod = _load_server()
        bus = FakeBus()
        result = self._call(mod, bus, FakeAdapter(chunks=[_text("He"), _text("llo")]))

        assert result["status"] == "streamed"
        assert result["stream_id"]

        # 事件序列：block_start → text_delta×2 → block_end → finish
        events = [p["event"] for _, p in bus.emits]
        assert events == ["block_start", "text_delta", "text_delta", "block_end", "finish"]

        # 信封：thread_id/pipeline_id/message_id/sequence 全量存在
        assert len(bus.emits) == 5
        for _, params in bus.emits:
            assert params["event"] in _STREAM_EVENTS
            payload = params["payload"]
            assert payload.keys() >= _ENVELOPE_KEYS
            assert payload["thread_id"] == ""
            assert payload["pipeline_id"] == ""
            assert payload["message_id"] == ""
        sequences = [params["payload"]["sequence"] for _, params in bus.emits]
        assert sequences == list(range(5))

        # stream=True 传给 adapter
        assert mod._adapter.calls[0]["stream"] is True
        assert mod._adapter.calls[0]["on_chunk"] is not None

    def test_text_deltas_concatenate_to_full_content(self) -> None:
        """性质断言：同块 text 增量拼接 == 完整正文（事件载荷带 index）。"""
        mod = _load_server()
        bus = FakeBus()
        content = "a" * 50
        self._call(mod, bus, FakeAdapter(chunks=[_text(content) for _ in range(4)]))
        text = "".join(
            params["payload"].get("text", "")
            for _, params in bus.emits
            if params["event"] == "text_delta"
        )
        assert text == content * 4
        indices = {params["payload"]["index"] for _, params in bus.emits if params["event"] == "text_delta"}
        assert indices == {0}

    def test_finish_reason_error_on_mid_stream_exception(self) -> None:
        """断流兜底：流中途抛异常 → 已推 chunk 照常 + 补发 finish{reason:error}，异常不吞。"""
        mod = _load_server()
        bus = FakeBus()

        class Boom(Exception):
            pass

        adapter = FakeAdapter(chunks=[_text("par"), _text("tial")], mid_stream_exc=Boom("boom"), mid_stream_after=1)
        _inject(mod, "event-bus", bus)
        mod._adapter = adapter

        with pytest.raises(Boom):
            _run(
                mod.llm_complete_stream(
                    model="glm-5.2",
                    messages=[{"role": "user", "content": "hi"}],
                )
            )

        events = [p["event"] for _, p in bus.emits]
        assert "block_start" in events
        assert events[-1] == "finish"
        assert bus.emits[-1][1]["payload"]["reason"] == "error"
        assert events.count("finish") == 1

    def test_finish_reason_error_on_connect_exception(self) -> None:
        """断流兜底（第二组）：adapter.completion 直接抛错 → 兜底 finish{reason:error}。"""
        mod = _load_server()
        bus = FakeBus()

        class Boom(Exception):
            pass

        adapter = FakeAdapter(connect_exc=Boom("conn lost"))
        _inject(mod, "event-bus", bus)
        mod._adapter = adapter

        with pytest.raises(Boom):
            _run(
                mod.llm_complete_stream(
                    model="glm-5.2",
                    messages=[{"role": "user", "content": "hi"}],
                )
            )
        events = [p["event"] for _, p in bus.emits]
        assert events == ["finish"]
        assert bus.emits[0][1]["payload"]["reason"] == "error"

    def test_usage_emitted_before_finish(self) -> None:
        """usage 事件在 finish 前发出（token 字段映射为 input/output_tokens）。"""
        mod = _load_server()
        bus = FakeBus()
        usage = {
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "total_tokens": 10,
            "cached_tokens": 2,
        }
        self._call(mod, bus, FakeAdapter(chunks=[_text("hi")], usage=usage))
        events = [p["event"] for _, p in bus.emits]
        assert events == ["block_start", "text_delta", "block_end", "usage", "finish"]
        usage_payload = bus.emits[-2][1]["payload"]
        assert usage_payload["input_tokens"] == 7
        assert usage_payload["output_tokens"] == 3
        assert bus.emits[-1][1]["payload"]["reason"] == "stop"

    def test_keepalive_during_slow_stream(self) -> None:
        """心跳：chunk 间隔超过阈值时发 keepalive（不打断块序列）。"""
        mod = _load_server()
        bus = FakeBus()
        adapter = FakeAdapter(chunks=[_text("x") for _ in range(3)], chunk_delay=0.08)
        _inject(mod, "event-bus", bus)
        mod._adapter = adapter
        old_interval = mod.KEEPALIVE_INTERVAL_SECONDS
        mod.KEEPALIVE_INTERVAL_SECONDS = 0.05
        try:
            result = _run(
                mod.llm_complete_stream(
                    model="glm-5.2",
                    messages=[{"role": "user", "content": "hi"}],
                )
            )
        finally:
            mod.KEEPALIVE_INTERVAL_SECONDS = old_interval
        assert result["status"] == "streamed"
        events = [p["event"] for _, p in bus.emits]
        assert "keepalive" in events
        assert events.index("block_start") < events.index("finish")
        # keepalive 无业务载荷（仅信封）
        ka = next(params for _, params in bus.emits if params["event"] == "keepalive")
        assert ka["payload"].keys() == _ENVELOPE_KEYS

    def test_stream_without_event_bus_still_returns(self) -> None:
        """event-bus 未注入 → 流式推送降级，服务仍返回 streamed（不抛 KeyError）。"""
        mod = _load_server()
        # 不注入 event-bus（get_capability 抛 KeyError → 内部降级）
        mod._adapter = FakeAdapter(chunks=[_text("hi")])
        result = _run(
            mod.llm_complete_stream(
                model="glm-5.2",
                messages=[{"role": "user", "content": "hi"}],
            )
        )
        assert result["status"] == "streamed"
        assert result["stream_id"]

    def test_bus_notify_failure_does_not_break_stream(self) -> None:
        """notify 抛错 → fire-and-forget 语义，不阻断流式调用与返回。"""
        mod = _load_server()
        bus = FakeBus(fail=True)
        result = self._call(mod, bus, FakeAdapter(chunks=[_text("hi")]))
        assert result["status"] == "streamed"
        assert mod._adapter.calls[0]["stream"] is True

    # ── 返回值聚合契约：完整 LLMResponse 同构字段 ────────────────

    def test_return_carries_full_text_response(self) -> None:
        """纯文本响应：返回 dict 含 text/tool_calls/thinking_text/usage/finish_reason。"""
        mod = _load_server()
        bus = FakeBus()
        usage = {"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14, "cached_tokens": 0}
        result = self._call(
            mod,
            bus,
            FakeAdapter(
                chunks=[_text("He"), _text("llo")],
                text="Hello",
                thinking_text="plan",
                usage=usage,
                finish_reason="stop",
            ),
        )

        assert result["status"] == "streamed"
        assert result["stream_id"].startswith("stream_")
        assert result["text"] == "Hello"
        assert result["tool_calls"] == []
        assert result["thinking_text"] == "plan"
        assert result["usage"] == usage
        assert result["finish_reason"] == "stop"
        # 流式推送保留（逐字事件不因聚合返回而消失；usage 在 finish 前）
        events = [p["event"] for _, p in bus.emits]
        assert events == ["block_start", "text_delta", "text_delta", "block_end", "usage", "finish"]

    def test_return_carries_full_tool_call_response(self) -> None:
        """工具调用响应（区分度输入）：tool_calls/finish_reason=tool_calls 如实回传。"""
        mod = _load_server()
        bus = FakeBus()
        tool_calls = [
            {
                "id": "call_001",
                "name": "bash",
                "arguments": '{"cmd": "ls"}',
            }
        ]
        result = self._call(
            mod,
            bus,
            FakeAdapter(
                chunks=[_text("x")],
                text=None,
                tool_calls=tool_calls,
                thinking_text=None,
                usage=None,
                finish_reason="tool_calls",
            ),
        )

        assert result["text"] is None
        assert result["tool_calls"] == tool_calls
        assert result["thinking_text"] is None
        assert result["usage"] == {}
        assert result["finish_reason"] == "tool_calls"

    def test_return_maps_unknown_finish_reason_and_none_usage(self) -> None:
        """未知 finish_reason 收敛为 stop、无 usage 空 dict（协议四值契约）。"""
        mod = _load_server()
        bus = FakeBus()
        result = self._call(
            mod,
            bus,
            FakeAdapter(
                chunks=[_text("hi")],
                text="hi",
                tool_calls=[],
                thinking_text=None,
                usage=None,
                finish_reason="content_filter",
            ),
        )
        assert result["finish_reason"] == "stop"
        assert result["usage"] == {}

    def test_agent_level_passthrough_sets_priority(self) -> None:
        """agent_level 经 kwargs 透传 → 本进程 KeyPool 优先级落位（跨进程透传契约）。"""
        mod = _load_server()
        bus = FakeBus()

        from key_pool import get_agent_priority

        # 在 adapter 调用时（与 complete_stream 同一协程上下文）读优先级：
        # contextvar 只在任务上下文内可见，测试侧读不到跨任务写入。
        captured: dict[str, int] = {}

        class _RecordingAdapter(FakeAdapter):
            async def completion(self, **kwargs: Any) -> Any:
                captured["priority"] = get_agent_priority()
                return await super().completion(**kwargs)

        _inject(mod, "event-bus", bus)
        mod._adapter = _RecordingAdapter(chunks=[_text("hi")], text="hi")

        result = _run(
            mod.llm_complete_stream(
                model="glm-5.2",
                messages=[{"role": "user", "content": "hi"}],
                agent_level="L1",
            )
        )
        assert result["status"] == "streamed"
        assert captured["priority"] == 1  # L1 → 1
        # 透传键不得泄漏给 adapter（litellm 不认 agent_level）
        assert "agent_level" not in mod._adapter.calls[0]


class TestCompleteRetired:
    """llm.complete 退役：注册表无该工具、模块无处理器。"""

    def test_complete_not_registered(self) -> None:
        mod = _load_server()
        assert "llm.complete" not in mod.plugin._tools
        assert "llm.complete_stream" in mod.plugin._tools
        assert "llm.health_check" in mod.plugin._tools
        assert "http.handle" in mod.plugin._tools

    def test_complete_handler_removed(self) -> None:
        """server.py 不再导出 llm_complete 处理器（服务方法已退役）。"""
        mod = _load_server()
        assert not hasattr(mod, "llm_complete")
        assert hasattr(mod, "llm_complete_stream")

    def test_health_check_still_registered(self) -> None:
        """health_check 保留（不受退役影响）。"""
        mod = _load_server()
        assert "llm.health_check" in mod.plugin._tools


class TestManifestServices:
    """plugin.json 服务声明与实现一致（G2 双写对照）。"""

    def test_manifest_services_names(self) -> None:
        """plugin.json capabilities.services 名单：complete_stream 在、complete 不在。"""
        manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        services = [s["name"] for s in manifest["capabilities"]["services"]]
        assert "llm.complete_stream" in services
        assert "llm.complete" not in services
        assert "llm.health_check" in services
        assert "http.handle" in services

    def test_manifest_schema_matches_complete_old_schema(self) -> None:
        """complete_stream 的 input_schema 与退役前 llm.complete 相同（model/messages/tools/...）。"""
        manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        svc = next(s for s in manifest["capabilities"]["services"] if s["name"] == "llm.complete_stream")
        schema = svc["input_schema"]
        props = schema["properties"]
        assert set(props.keys()) == {"model", "messages", "tools", "temperature", "max_tokens"}
        assert schema["required"] == ["model", "messages"]
        assert props["model"]["type"] == "string"
        assert props["messages"]["type"] == "array"
        assert props["tools"]["type"] == "array"
        assert props["temperature"]["type"] == "number"
        assert props["max_tokens"]["type"] == "integer"
