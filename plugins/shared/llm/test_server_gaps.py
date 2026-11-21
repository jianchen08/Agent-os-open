# @feature: FP-0.2.二 内部模块统一 manifest 化 | @ci: python-coverage
"""llm server.py 未覆盖分支补测（生命周期 / 部分内容累积 / 取消轮询 / 健康检查 / HTTP 500 兜底）。

行为契约（断输入→输出/副作用，不钉实现）：
- on_load：注入配置到 _config_models shim（get_config 可读回）、重置
  router_factory 模块级单例（旧模型映射被清空）、清空 adapter 惰性单例；
  on_unload 清空 adapter
- _StreamOutbound.on_chunk：thinking/tool_call 归一化 chunk 累积进 partial
  快照（id 首个非空定格、name/arguments 增量拼接、负 index 收敛 0、无 id
  回退 call_{idx}）；accumulator 属性可取
- _watch_run_cancel：事件驱动取消感知（wait_run_stop 长轮询主路径/窗口重挂/
  通道故障降级回轮询/双通道故障 best-effort），见 suspended 置取消事件收口
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
import logging
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
    assert spec is not None
    assert spec.loader is not None
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

        self.calls.append(kwargs)
        if self._connect_exc is not None:
            raise self._connect_exc
        on_chunk = kwargs.get("on_chunk")
        for c in self._chunks:
            if on_chunk is not None:
                on_chunk(c)
        return SimpleNamespace(text="ok", tool_calls=[], thinking_text=None, usage=None, finish_reason="stop")

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
    """on_load：配置注入 shim 可读回、router 单例槽位重置、adapter 惰性单例清空。

    重置不清表（读侧无空窗，审计 2026-11-12 P2-12）：旧映射由下一次构建整体
    替换——此处显式触发一次懒重建，断言旧配置不残留。
    """
    mod = _load_server()
    import router_factory as rf

    mod.plugin._injected_config = {"llm": {"providers": {"deepseek": {"keys": []}}}}
    mod._adapter = object()  # 预置旧单例：on_load 后必须清空
    rf._model_to_provider["stale-model"] = "stale-provider"  # 旧模型映射残留

    _run(mod._on_load({}))

    assert mod.get_config()["llm"]["providers"]["deepseek"]["keys"] == []  # shim 可读回
    assert mod._adapter is None  # 惰性 adapter 清空，下次调用重建

    mod._ensure_adapter()  # 下次调用懒重建：下一代表整体替换
    assert "stale-model" not in rf._model_to_provider  # 旧配置不残留


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


# ─────────────────────── _watch_run_cancel：事件驱动取消感知 ───────────────────────


def test_watch_run_cancel_long_poll_suspended_sets_cancel() -> None:
    """主路径：wait_run_stop 长轮询首窗即返回 suspended → 置取消事件返回。"""
    mod = _load_server()
    calls: list[str] = []

    class _SuspendedHandle:
        async def call(self, method: str, _params: dict[str, Any], *_args: Any) -> Any:
            calls.append(method)
            return {"status": "suspended"}

    cancel_event = asyncio.Event()

    async def _scenario() -> None:
        await asyncio.wait_for(
            mod._watch_run_cancel(_SuspendedHandle(), "run-w1", cancel_event), timeout=2
        )
        assert cancel_event.is_set()

    _run(_scenario())
    assert calls == ["wait_run_stop"], "主路径不得回退 get_run_status 轮询"


def test_watch_run_cancel_running_window_rearms_until_suspended() -> None:
    """窗口超时（running）→ 重挂下一窗；后续窗见 suspended 收口。"""
    mod = _load_server()
    responses = [{"status": "running"}, {"status": "running"}, {"status": "suspended"}]

    class _WindowedHandle:
        async def call(self, method: str, _params: dict[str, Any], *_args: Any) -> Any:
            assert method == "wait_run_stop"
            return responses.pop(0)

    cancel_event = asyncio.Event()

    async def _scenario() -> None:
        await asyncio.wait_for(
            mod._watch_run_cancel(_WindowedHandle(), "run-w2", cancel_event), timeout=2
        )
        assert cancel_event.is_set()
        assert not responses, "suspended 前的每一窗都应被消费（重挂语义）"

    _run(_scenario())


def test_watch_run_cancel_falls_back_to_polling_on_channel_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """长轮询通道故障（旧内核无方法）→ 降级 get_run_status 轮询，见 suspended 收口。"""
    mod = _load_server()
    monkeypatch.setattr(mod, "CANCEL_POLL_INTERVAL_SECONDS", 0.01)
    methods: list[str] = []

    class _LegacyKernelHandle:
        async def call(self, method: str, _params: dict[str, Any], *_args: Any) -> Any:
            methods.append(method)
            if method == "wait_run_stop":
                raise RuntimeError("unknown method: wait_run_stop")
            return {"status": "suspended"}

    cancel_event = asyncio.Event()

    async def _scenario() -> None:
        await asyncio.wait_for(
            mod._watch_run_cancel(_LegacyKernelHandle(), "run-w3", cancel_event), timeout=2
        )
        assert cancel_event.is_set()

    _run(_scenario())
    assert methods[0] == "wait_run_stop"
    assert "get_run_status" in methods, "通道故障必须降级回轮询通道"


def test_watch_run_cancel_survives_both_channel_errors_until_external_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """双通道持续故障 → 异常被吞继续循环（best-effort），仅外部取消能收口。"""
    mod = _load_server()
    monkeypatch.setattr(mod, "CANCEL_POLL_INTERVAL_SECONDS", 0.01)

    class _DownHandle:
        async def call(self, _method: str, _params: dict[str, Any], *_args: Any) -> Any:
            raise RuntimeError("executor channel down")

    cancel_event = asyncio.Event()

    async def _scenario() -> None:
        task = asyncio.ensure_future(
            mod._watch_run_cancel(_DownHandle(), "run-w4", cancel_event)
        )
        await asyncio.sleep(0.05)  # 期间多轮长轮询+降级轮询均失败
        assert not cancel_event.is_set()  # 通道故障不误置取消
        cancel_event.set()
        await asyncio.wait_for(task, timeout=2)  # 正常返回，不抛通道异常

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
    for name in ("get_provider_types", "get_llm_presets", "get_remote_models"):
        setattr(fake_routes, name, _boom)
    monkeypatch.setitem(sys.modules, "routes_llm_config", fake_routes)

    result = _run(
        mod.http_handle(path="/ext/llm_service/config/llm/presets", method="GET")
    )

    assert result["success"] is True  # 协议面成功信封，错误经 HTTP status 表达
    body = _decode(result["data"])
    assert result["data"]["status"] == 500
    assert body["error"] == "internal server error"
    assert "disk gone" in body["detail"]


# ─────────────────── _StreamOutbound：退化循环停止与用户可见信号 ───────────────────


class _PublisherStub:
    """event-bus 发布桩：记录流事件与 plugin_error（不连真通道）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.errors: list[tuple[dict[str, Any], str]] = []

    def put(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, payload))

    def emit_plugin_error(self, error: dict[str, Any], source: str) -> None:
        self.errors.append((error, source))


def test_outbound_returns_stop_on_repetition_with_content_and_notifies_once() -> None:
    """正文通道原地重复 → on_chunk 返回 "stop"（adapter 据此截断）+ 用户可见信号一条。

    断的是行为契约：有半截内容的截断轮（正文已累积），guard 判定后必须给
    adapter 停止信号，且 plugin_error 只发一次（截断对用户可见，禁静默降级）。
    """
    mod = _load_server()
    publisher = _PublisherStub()
    outbound = mod._StreamOutbound(publisher=publisher, cancel_event=asyncio.Event())

    results = [outbound.on_chunk({"type": "text", "content": "🍦" * 10}) for _ in range(60)]

    assert results[-1] == "stop"
    assert len(publisher.errors) == 1
    error, source = publisher.errors[0]
    assert error["code"] == "LLM_STREAM_REPETITION"
    assert source == "llm_service"
    # 判定轮发了 block_end（客户端不留半开块）
    assert publisher.events[-1][0] == "block_end"
    # 后续 chunk 不再重复通知
    outbound.on_chunk({"type": "thinking", "content": "x"})
    assert len(publisher.errors) == 1


def test_outbound_zero_output_repetition_skips_notice() -> None:
    """零产出退化轮不发截断提醒：llm_core 会抛错终止，引擎侧已发失败通知。

    同一事故只留一条通知（2026-11-10 用户裁定）——思考通道命中且正文/工具调用
    双空时，adapter 仍要拿到 "stop"，但不再叠加「已自动截断」。
    """
    mod = _load_server()
    publisher = _PublisherStub()
    outbound = mod._StreamOutbound(publisher=publisher, cancel_event=asyncio.Event())

    results = [
        outbound.on_chunk({"type": "thinking", "content": "🍦" * 10}) for _ in range(60)
    ]

    assert results[-1] == "stop"
    assert publisher.errors == []


def test_outbound_normal_stream_never_returns_stop() -> None:
    """正常文本流恒返回 None（不得因守卫误伤正常输出）。"""
    mod = _load_server()
    outbound = mod._StreamOutbound(publisher=None, cancel_event=asyncio.Event())

    text = "".join(
        f"第{i}步：检查配置项 config_{i} 的取值并记录到清单，随后继续下一项。\n"
        for i in range(60)
    )
    for i in range(0, len(text), 20):
        assert outbound.on_chunk({"type": "text", "content": text[i : i + 20]}) is None


def test_outbound_logs_repetition_evidence(caplog: pytest.LogCaptureFixture) -> None:
    """退化截断必须留证：命中通道 + 判定信息（reasons/onset）+ 原文尾部落日志。

    截断内容不进 assistant 消息（零产出轮 llm_core 直接抛错），不留证则事后
    无法判定真退化还是误判（2026-11-10 实测 8 次命中零现场）。无 event-bus
    的降级面同样要留证。simurg 引擎换代后判停点 = 首个检查点（hold=350），
    旧自研守卫的 500 字符窗口节奏退役（调研文档 §3 vetting 实测 corrupt@340）。
    """
    mod = _load_server()
    outbound = mod._StreamOutbound(publisher=None, cancel_event=asyncio.Event())

    with caplog.at_level(logging.WARNING):
        for _ in range(60):
            outbound.on_chunk({"type": "thinking", "content": "🍦" * 10})

    evidence = [r.getMessage() for r in caplog.records if "退化循环截断留证" in r.getMessage()]
    assert len(evidence) == 1
    assert "channel=thinking" in evidence[0]
    assert "chars=350" in evidence[0]
    # 引擎可读判定原因 + 环起点随留证落日志（自证缺口修复面）
    assert "reasons=" in evidence[0]
    assert "reasons=-" not in evidence[0]
    assert "onset_char=" in evidence[0]
    assert "exempted=False" in evidence[0]
    assert "🍦" * 10 in evidence[0]


def test_outbound_repetition_without_publisher_still_returns_stop() -> None:
    """无 event-bus（降级面）：判定后照样返回 stop，通知步骤跳过不炸。"""
    mod = _load_server()
    outbound = mod._StreamOutbound(publisher=None, cancel_event=asyncio.Event())

    results = [
        outbound.on_chunk({"type": "thinking", "content": "🍦" * 10}) for _ in range(60)
    ]

    assert results[-1] == "stop"
