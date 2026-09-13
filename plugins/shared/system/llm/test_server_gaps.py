# @feature: FP-0.2.二 内部模块统一 manifest 化 | @ci: python-coverage
"""llm server.py 未覆盖分支补测（生命周期 / 部分内容累积 / 取消轮询 / 健康检查 / HTTP 500 兜底）。

行为契约（断输入→输出/副作用，不钉实现）：
- on_load：注入配置到 _config_models shim（get_config 可读回）、重置
  router_factory 模块级单例（旧模型映射被清空）、清空 adapter 惰性单例；
  on_unload 清空 adapter
- _StreamOutbound.on_chunk：thinking/tool_call 归一化 chunk 累积进 partial
  快照（id 首个非空定格、name/arguments 增量拼接、负 index 收敛 0、无 id
  回退 call_{idx}）；accumulator 属性可取
- _poll_run_cancel：轮询通道故障 best-effort——异常不外抛继续轮询，见
  suspended 置取消事件收口
- llm.complete_stream：pipeline-executor 能力缺失 → 跳过取消轮询照常流式；
  任务级取消（CancelledError）原样传播不转业务返回
- llm.health_check：健康/异常两态返回契约
- http.handle：业务 handler 未预期异常 → HTTP 500 内部错误兜底（非
  status_code 业务异常路径）

外部依赖全桩（adapter / event-bus / capability handle / routes 模块），绝不触网。
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_server() -> Any:
    """按显式路径加载 llm 插件 server 模块（唯一模块名隔离同名 server.py）。"""
    mod_name = "llm_server_gaps_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class FakeBus:
    """记录 emit 调用的伪 event-bus capability handle。"""

    def __init__(self) -> None:
        self.emits: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.emits.append((method, params))


class FakeAdapter:
    """伪 adapter：按 on_chunk 契约投递 chunk，返回伪 LLMResponse。"""

    def __init__(
        self,
        *,
        chunks: list[dict[str, Any]] | None = None,
        connect_exc: BaseException | None = None,
        health_result: bool = True,
        health_exc: Exception | None = None,
    ) -> None:
        self._chunks = list(chunks or [])
        self._connect_exc = connect_exc
        self._health_result = health_result
        self._health_exc = health_exc
        self.calls: list[dict[str, Any]] = []

    async def completion(self, **kwargs: Any) -> Any:
        from types import SimpleNamespace as _NS

        self.calls.append(kwargs)
        if self._connect_exc is not None:
            raise self._connect_exc
        on_chunk = kwargs.get("on_chunk")
        for c in self._chunks:
            if on_chunk is not None:
                on_chunk(c)
        return _NS(text="ok", tool_calls=[], thinking_text=None, usage=None, finish_reason="stop")

    async def health_check(self, model: str) -> bool:
        if self._health_exc is not None:
            raise self._health_exc
        return self._health_result


def _text(content: str) -> dict[str, Any]:
    return {"type": "text", "content": content}


def _inject_caps(module: Any, mapping: dict[str, Any]) -> None:
    """按名字表注入 capability：表内返回句柄，表外抛 KeyError（能力未注册语义）。"""
    module.plugin.get_capability = lambda n: mapping[n]  # type: ignore[method-assign]


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _decode(data: dict[str, Any]) -> dict[str, Any]:
    """HttpHandleResponse body base64 → dict。"""
    assert data["body_encoding"] == "base64"
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


# ─────────────────────────── 生命周期：on_load / on_unload ───────────────────────────


def test_on_load_injects_config_resets_router_and_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """on_load：配置注入 shim 可读回、router_factory 旧单例清空、adapter 单例清空。"""
    mod = _load_server()
    import router_factory as rf

    mod.plugin._injected_config = {"llm": {"providers": {"deepseek": {"keys": []}}}}
    mod._adapter = object()  # 预置旧单例：on_load 后必须清空
    rf._model_to_provider["stale-model"] = "stale-provider"  # 旧模型映射残留

    _run(mod._on_load({}))

    assert "stale-model" not in rf._model_to_provider  # 单例重置：旧配置不残留
    assert mod.get_config()["llm"]["providers"]["deepseek"]["keys"] == []  # shim 可读回
    assert mod._adapter is None  # 惰性 adapter 清空，下次调用重建


def test_on_unload_clears_adapter() -> None:
    """on_unload：adapter 单例清空（进程内资源随卸载释放）。"""
    mod = _load_server()
    mod._adapter = object()

    _run(mod._on_unload({}))

    assert mod._adapter is None


# ─────────────────────────── _StreamOutbound：partial 累积 ───────────────────────────


def test_outbound_accumulates_thinking_chunks_into_snapshot() -> None:
    """thinking chunk 累积进快照 thinking_text；空内容不累积。"""
    mod = _load_server()
    outbound = mod._StreamOutbound(publisher=None, cancel_event=asyncio.Event())

    assert outbound.on_chunk({"type": "thinking", "content": ""}) is None  # 空内容跳过
    outbound.on_chunk({"type": "thinking", "content": "plan"})
    outbound.on_chunk({"type": "thinking", "content": "-hard"})

    assert outbound.accumulator is not None  # accumulator 属性可取
    assert outbound.has_content() is True
    snapshot = outbound.snapshot()
    assert snapshot["thinking_text"] == "plan-hard"
    assert snapshot["text"] is None
    assert snapshot["tool_calls"] == []
    assert snapshot["usage"] is None  # 流中断时 usage 不可达（契约）


def test_outbound_merges_tool_call_deltas_by_index() -> None:
    """tool_call 增量按 index 归组：id 首个非空定格，name/arguments 拼接，负 index 收敛 0。"""
    mod = _load_server()
    outbound = mod._StreamOutbound(publisher=None, cancel_event=asyncio.Event())

    outbound.on_chunk({"type": "tool_call", "tool_calls": [
        SimpleNamespace(index=0, id="call_a", function=SimpleNamespace(name="get", arguments='{"x"')),
    ]})
    outbound.on_chunk({"type": "tool_call", "tool_calls": [
        # 同 index 第二段：id 为空不覆盖首段；name/arguments 续拼
        SimpleNamespace(index=0, id=None, function=SimpleNamespace(name="_wx", arguments=':1}')),
        # 负 index 收敛为 0 并入同块；无 id → 快照回退 call_{idx}
        SimpleNamespace(index=-3, id="", function=SimpleNamespace(name=None, arguments=None)),
    ]})

    snapshot = outbound.snapshot()
    assert snapshot["tool_calls"] == [
        {"id": "call_a", "name": "get_wx", "arguments": '{"x":1}'},
    ]


def test_outbound_tool_call_snapshot_falls_back_to_call_idx() -> None:
    """独立 index 的 tool_call 从未收到 id → 快照 id 回退 call_{index}。"""
    mod = _load_server()
    outbound = mod._StreamOutbound(publisher=None, cancel_event=asyncio.Event())

    outbound.on_chunk({"type": "tool_call", "tool_calls": [
        SimpleNamespace(index=2, id=None, function=SimpleNamespace(name="f", arguments="{}")),
    ]})

    assert outbound.snapshot()["tool_calls"] == [
        {"id": "call_2", "name": "f", "arguments": "{}"},
    ]


# ─────────────────────────── _poll_run_cancel：轮询容错 ───────────────────────────


def test_poll_run_cancel_survives_poll_errors_until_external_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """轮询通道持续故障 → 异常被吞继续轮询（best-effort），仅外部取消能收口。"""
    mod = _load_server()
    monkeypatch.setattr(mod, "CANCEL_POLL_INTERVAL_SECONDS", 0.01)

    class _DownHandle:
        async def call(self, _method: str, _params: dict[str, Any]) -> Any:
            raise RuntimeError("executor channel down")

    cancel_event = asyncio.Event()

    async def _scenario() -> None:
        task = asyncio.ensure_future(
            mod._poll_run_cancel(_DownHandle(), "run-1", cancel_event)
        )
        await asyncio.sleep(0.05)  # 期间多轮轮询均失败
        assert not cancel_event.is_set()  # 轮询失败不误置取消
        cancel_event.set()
        await asyncio.wait_for(task, timeout=2)  # 正常返回，不抛轮询异常

    _run(_scenario())


def test_poll_run_cancel_sets_cancel_on_suspended_after_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首轮故障后恢复读取：见 suspended → 置取消事件并返回（停止信号感知锚）。"""
    mod = _load_server()
    monkeypatch.setattr(mod, "CANCEL_POLL_INTERVAL_SECONDS", 0.01)
    responses = [RuntimeError("transient"), {"status": "suspended"}]

    class _RecoveringHandle:
        async def call(self, _method: str, _params: dict[str, Any]) -> Any:
            r = responses.pop(0)
            if isinstance(r, BaseException):
                raise r
            return r

    cancel_event = asyncio.Event()

    async def _scenario() -> None:
        await asyncio.wait_for(
            mod._poll_run_cancel(_RecoveringHandle(), "run-2", cancel_event), timeout=2
        )
        assert cancel_event.is_set()  # suspended → 取消事件置位

    _run(_scenario())


# ─────────────────────────── llm.complete_stream：能力缺失 / 任务取消 ───────────────────────────


def test_complete_stream_without_pipeline_executor_capability_skips_polling() -> None:
    """run_id 携带但 pipeline-executor 能力未注册 → 跳过取消轮询，流式照常。"""
    mod = _load_server()
    bus = FakeBus()
    adapter = FakeAdapter(chunks=[_text("hi")])
    _inject_caps(mod, {"event-bus": bus})  # pipeline-executor 表外 → KeyError → 降级
    mod._adapter = adapter

    result = _run(
        mod.llm_complete_stream(
            model="glm-5.2",
            messages=[{"role": "user", "content": "hi"}],
            run_id="run-1",
        )
    )

    assert result["status"] == "streamed"
    assert [p["event"] for _, p in bus.emits][-1] == "finish"


def test_complete_stream_task_cancellation_propagates() -> None:
    """任务级取消（CancelledError）原样传播——不转 interrupted/error 业务返回。"""
    mod = _load_server()
    _inject_caps(mod, {"event-bus": FakeBus()})
    mod._adapter = FakeAdapter(connect_exc=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        _run(
            mod.llm_complete_stream(
                model="glm-5.2",
                messages=[{"role": "user", "content": "hi"}],
            )
        )


# ─────────────────────────── llm.health_check ───────────────────────────


@pytest.mark.parametrize("health_result", [True, False])
def test_health_check_returns_adapter_verdict(health_result: bool) -> None:
    """健康判定如实透传：healthy 布尔随 adapter 结论、model 回显。"""
    mod = _load_server()
    mod._adapter = FakeAdapter(health_result=health_result)

    result = _run(mod.llm_health_check("glm-5.2"))

    assert result == {"healthy": health_result, "model": "glm-5.2"}


def test_health_check_failure_returns_error_dict() -> None:
    """health_check 抛错 → 不外抛，返回 healthy=False + error 摘要。"""
    mod = _load_server()
    mod._adapter = FakeAdapter(health_exc=RuntimeError("upstream exploded"))

    result = _run(mod.llm_health_check("glm-5.2"))

    assert result["healthy"] is False
    assert result["model"] == "glm-5.2"
    assert "upstream exploded" in result["error"]


# ─────────────────────────── http.handle：未预期异常兜底 ───────────────────────────


def test_http_handle_unexpected_error_returns_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """业务 handler 非 status_code 异常 → HTTP 500 内部错误兜底（detail 携带摘要）。"""
    mod = _load_server()

    def _boom() -> dict[str, Any]:
        raise RuntimeError("disk gone")

    fake_routes = types.ModuleType("routes_llm_config")
    for name in (
        "get_llm_config", "get_providers", "add_provider", "get_provider_types",
        "get_llm_presets", "get_models", "add_model", "get_defaults", "save_defaults",
        "get_remote_models", "update_provider", "delete_provider", "update_model",
        "delete_model",
    ):
        setattr(fake_routes, name, _boom)
    monkeypatch.setitem(sys.modules, "routes_llm_config", fake_routes)

    result = _run(
        mod.http_handle(path="/ext/llm_service/config/llm", method="GET")
    )

    assert result["success"] is True  # 协议面成功信封，错误经 HTTP status 表达
    body = _decode(result["data"])
    assert result["data"]["status"] == 500
    assert body["error"] == "internal server error"
    assert "disk gone" in body["detail"]
