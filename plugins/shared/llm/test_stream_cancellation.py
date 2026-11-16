# @feature: FP-0.2.二 内部模块统一 manifest 化 | @ci: python-coverage
"""流式取消可中断：上游静默（不吐 chunk）时停止信号秒级生效。

锁定 2026-11-06 实测缺陷：``_consume_stream`` 主循环 ``await aiter.__anext__()``
期间无人监听 cancel_event——上游不吐 chunk（思考型模型/长 TTFT）时点停止要
干等到下一个 chunk 或 inter_chunk_timeout（缺省 90s）；首 chunk 预取阶段
（KeyPool 排队+建连+TTFT，``_establish_first_chunk``）同样不查取消。前端停止
护栏 TTL 仅 15s，后端超时不收尾会让生成态"复活"。

行为契约（断输入→输出/时延上界，不钉实现）：
- 流中取消（chunk 正常流动）：下一个 chunk 处 interrupted（既有路径回归，
  与静默取消构成区分度输入对）
- 流中取消（上游静默挂起）：cancel_event 置位后 <2s 返回 interrupted，
  底层流已关闭（防连接泄漏）
- 首 token 预取期间取消：interrupted <2s，底层流已关闭
- inter_chunk_timeout 语义保留：静默满超时仍走既有超时错误路径
  （半截快照 error 返回，不拖到缺省 90s）
- 无取消：流正常 streamed 完整返回（abort 通道不误伤既有完成路径）
- KeyPool 预算展宽覆写不吞 abort_event（生产 adapter 类全链路生效）

上游全桩（假 async generator，事件/短睡眠控制，绝不触网）。

共跑范式：adapter/exceptions/router_factory 一律**函数内惰性导入**
（同 test_streaming_server.py）——收集期不做裸名换血，避免挤占字母序在前
测试文件（如 test_plugin_provider_route 钉 router_factory 查表）的
sys.modules 槽位。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
# 本插件目录置 sys.path[0]：车道共跑时其他插件同名 adapter.py 目录可能占住前位
_s = str(_PLUGIN_DIR)
while _s in sys.path:
    sys.path.remove(_s)
sys.path.insert(0, _s)


def _load_server() -> Any:
    """按显式路径加载 llm 插件 server 模块（唯一模块名隔离同名 server.py）。"""
    mod_name = "llm_server_stream_cancellation_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None, "cannot load llm plugin server.py"
    assert spec.loader is not None, "cannot load llm plugin server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeBus:
    """记录 emit 调用的伪 event-bus capability handle。"""

    def __init__(self) -> None:
        self.emits: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.emits.append((method, params))


class _FakeRunStatus:
    """伪 pipeline-executor（旧内核两参形态）：wait_run_stop 长轮询通道故障
    → ``_watch_run_cancel`` 降级回 get_run_status 轮询——取消信号经降级轮询
    送达（与 test_streaming_server.TestCancelPolling 同款驱动）。

    ``gate_before_terminal``（可选）：返回非 running 状态（取消态）前先等待
    该事件（编排确定性同步——如 flowing 取消测试等首 chunk 交付）；超时 2s
    兜底放行（到点继续原逻辑，让测试如实红而非挂死）。
    """

    def __init__(
        self,
        statuses: list[dict[str, Any]],
        *,
        gate_before_terminal: asyncio.Event | None = None,
    ) -> None:
        self._statuses = list(statuses)
        self._gate = gate_before_terminal
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        if self._statuses:
            status = self._statuses.pop(0)
            if status.get("status") != "running" and self._gate is not None:
                try:
                    await asyncio.wait_for(self._gate.wait(), timeout=2.0)
                except TimeoutError:
                    pass  # 放行兜底：gate 未到点也让测试按真实编排走红/绿
            return status
        return {"status": "running"}


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


def _upstream_chunk(content: str | None = None) -> Any:
    """归一 chunk 形态的上游载荷（_process_chunk 消费的属性面）。"""
    delta = SimpleNamespace(content=content, reasoning_content=None, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)


class _FakeUpstreamStream:
    """假上游流：按序吐出预置 chunk，随后按 ``hang_after`` 分岔——

    hang_after=True（静默挂起）：停在 asyncio.Event.wait 永不续吐（思考型
    模型/长 TTFT 的上游静默形态）；aclose 置位即结束等待（模拟真实 aclose
    掐断挂起的接收协程），保证取消/超时/收尾各路径下事件循环无残留任务。
    hang_after=False（正常耗尽）：预置 chunk 吐完即 StopAsyncIteration。
    aclose 计数暴露（取消路径必须关闭底层流，防连接泄漏）。

    ``first_chunk_served``：消费循环第二次进入 ``__anext__``（首 chunk 已
    被取走并同步累积、开始等待下一个 chunk）时置位——「流动中取消」类
    测试经 ``_FakeRunStatus(gate_before_terminal=…)`` 等待该事件放行取消，
    保证取消确定落在首 chunk 累积之后（partial 非空可断言）；不等待方
    （静默/首 token 取消测试）不受影响。
    """

    def __init__(
        self, chunks: list[Any], chunk_delay: float = 0.0, hang_after: bool = True
    ) -> None:
        self._chunks = list(chunks)
        self._chunk_delay = chunk_delay
        self._hang_after = hang_after
        self.aclose_count = 0
        self._hang = asyncio.Event()
        self._served = 0
        self.first_chunk_served = asyncio.Event()

    def __aiter__(self) -> _FakeUpstreamStream:
        return self

    async def __anext__(self) -> Any:
        if self._served >= 1 and not self.first_chunk_served.is_set():
            # 首 chunk 已交付（消费循环对它的累积无 await 间隔，同一同步步
            # 内完成）——此刻置位即「流动中」起点。
            self.first_chunk_served.set()
        self._served += 1
        if self._chunks:
            if self._chunk_delay:
                await asyncio.sleep(self._chunk_delay)
            return self._chunks.pop(0)
        if self._hang_after:
            await self._hang.wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.aclose_count += 1
        self._hang.set()


def _make_adapter(
    chunks: list[Any], chunk_delay: float = 0.0, hang_after: bool = True
) -> tuple[Any, _FakeUpstreamStream]:
    """真基类 adapter（直连形态）+ 假上游流。

    仅桩 ``_do_completion`` 返回预置流，establish/消费/超时/取消全链路走
    生产实现；adapter 模块函数内导入（与被测 server.py 同批解析，类对象
    同一实例——见文件头共跑范式注）。
    """
    import adapter as adapter_mod

    class _StubDoCompletionAdapter(adapter_mod._BaseLLMAdapter):
        def __init__(self, stream: _FakeUpstreamStream) -> None:
            self._stream = stream

        async def _do_completion(self, **kwargs: Any) -> Any:
            return self._stream

    stream = _FakeUpstreamStream(chunks, chunk_delay=chunk_delay, hang_after=hang_after)
    return _StubDoCompletionAdapter(stream), stream


class TestStreamCancellation:
    """llm.complete_stream × 真 adapter × 假上游流：取消可中断契约。"""

    def _setup_cancel_env(
        self,
        mod: Any,
        bus: _FakeBus,
        run_statuses: list[dict[str, Any]],
        gate_before_terminal: asyncio.Event | None = None,
    ) -> float:
        run_status = _FakeRunStatus(run_statuses, gate_before_terminal=gate_before_terminal)
        _inject(mod, "event-bus", bus)
        _inject(mod, "pipeline-executor", run_status)
        old_interval = mod.CANCEL_POLL_INTERVAL_SECONDS
        mod.CANCEL_POLL_INTERVAL_SECONDS = 0.02
        return old_interval

    # ── 盲区A（主修复）：上游静默不吐 chunk 时取消 ────────────────

    def test_cancel_during_silent_stream_interrupts_within_bound(self) -> None:
        """上游静默挂起时取消 <2s 生效：interrupted + 半截快照 + 流已关闭。

        修复前：主循环 await __anext__() 无人监听 cancel_event，桩挂起点
        永不吐 chunk → 干等到 inter_chunk_timeout（缺省 90s）→ 2s 上界必超时红。
        """
        mod = _load_server()
        bus = _FakeBus()
        adapter, stream = _make_adapter([_upstream_chunk("partial")])
        mod._adapter = adapter
        old_interval = self._setup_cancel_env(
            mod, bus, [{"status": "running"}, {"status": "suspended"}]
        )
        try:
            t0 = time.monotonic()
            result = _run(
                asyncio.wait_for(
                    mod.llm_complete_stream(
                        model="glm-5.2",
                        messages=[{"role": "user", "content": "hi"}],
                        run_id="run-silent",
                    ),
                    timeout=2.0,
                )
            )
            elapsed = time.monotonic() - t0
        finally:
            mod.CANCEL_POLL_INTERVAL_SECONDS = old_interval

        assert result["status"] == "interrupted"
        assert result["finish_reason"] == "interrupted"
        # 首个 chunk 已累积进半截快照（取消前的内容不丢）
        assert result["partial"]["text"] == "partial"
        assert elapsed < 2.0, f"取消后 {elapsed:.2f}s 才中断（须秒级）"
        assert stream.aclose_count >= 1, "取消路径必须关闭底层流（防连接泄漏）"

    # ── 盲区B：首 token 预取期间取消 ─────────────────────────────

    def test_cancel_before_first_token_interrupts_within_bound(self) -> None:
        """首 chunk 预取（建连+TTFT）挂起期间取消 <2s 生效，流已关闭。

        修复前：_establish_first_chunk 伞预算（缺省 90s）内无人查取消——
        KeyPool 排队+建连+首 token 全算进停止延迟 → 2s 上界必超时红。
        """
        mod = _load_server()
        bus = _FakeBus()
        adapter, stream = _make_adapter([])  # 零预置 chunk：首个 __anext__ 挂起
        mod._adapter = adapter
        old_interval = self._setup_cancel_env(
            mod, bus, [{"status": "running"}, {"status": "suspended"}]
        )
        try:
            t0 = time.monotonic()
            result = _run(
                asyncio.wait_for(
                    mod.llm_complete_stream(
                        model="glm-5.2",
                        messages=[{"role": "user", "content": "hi"}],
                        run_id="run-first-token",
                    ),
                    timeout=2.0,
                )
            )
            elapsed = time.monotonic() - t0
        finally:
            mod.CANCEL_POLL_INTERVAL_SECONDS = old_interval

        assert result["status"] == "interrupted"
        assert result["finish_reason"] == "interrupted"
        assert result["partial"]["text"] is None  # 零累积：首 token 未到
        assert elapsed < 2.0, f"取消后 {elapsed:.2f}s 才中断（须秒级）"
        assert stream.aclose_count >= 1, "预取取消路径必须关闭底层流（防连接泄漏）"

    # ── 区分度输入：chunk 正常流动时取消（既有路径回归） ──────────

    def test_cancel_during_flowing_chunks_interrupts_at_next_chunk(self) -> None:
        """chunk 正常流动时取消：下一个 chunk 处 interrupted（与静默取消构
        成输入对——流活跃走 on_chunk 检查点，流静默走可中断等待，均 <2s）。"""
        mod = _load_server()
        bus = _FakeBus()
        chunks = [_upstream_chunk(f"c{i}") for i in range(20)]
        adapter, stream = _make_adapter(chunks, chunk_delay=0.03)
        mod._adapter = adapter
        # 确定性同步：取消态返回前等首 chunk 交付（否则负载敏感下取消可落
        # 在首 token 前 → partial 空，测试意图「流动中取消不丢已累积内容」
        # 失去编排保证）
        old_interval = self._setup_cancel_env(
            mod,
            bus,
            [{"status": "running"}, {"status": "suspended"}],
            gate_before_terminal=stream.first_chunk_served,
        )
        try:
            t0 = time.monotonic()
            result = _run(
                asyncio.wait_for(
                    mod.llm_complete_stream(
                        model="glm-5.2",
                        messages=[{"role": "user", "content": "hi"}],
                        run_id="run-flowing",
                    ),
                    timeout=2.0,
                )
            )
            elapsed = time.monotonic() - t0
        finally:
            mod.CANCEL_POLL_INTERVAL_SECONDS = old_interval

        assert result["status"] == "interrupted"
        partial_text = result["partial"]["text"] or ""
        assert 0 < len(partial_text) < 60, "部分 chunk 已累积（取消前的内容不丢）"
        assert elapsed < 2.0, f"取消后 {elapsed:.2f}s 才中断（须秒级）"
        assert stream.aclose_count >= 1

    # ── 语义保留：inter_chunk_timeout / 无取消完成 ────────────────

    def test_inter_chunk_timeout_semantics_preserved(self) -> None:
        """静默满 inter_chunk_timeout 走既有超时错误路径：半截快照 error 返回，
        到点即断（不拖到缺省 90s）。"""
        mod = _load_server()
        bus = _FakeBus()
        adapter, stream = _make_adapter([_upstream_chunk("partial")])
        _inject(mod, "event-bus", bus)
        mod._adapter = adapter

        t0 = time.monotonic()
        result = _run(
            asyncio.wait_for(
                mod.llm_complete_stream(
                    model="glm-5.2",
                    messages=[{"role": "user", "content": "hi"}],
                    inter_chunk_timeout=0.05,
                ),
                timeout=5.0,
            )
        )
        elapsed = time.monotonic() - t0

        assert result["status"] == "error"
        assert result["finish_reason"] == "error"
        assert result["partial"]["text"] == "partial"
        assert result["llm_error_info"]["error_type"] == "WireTimeoutError"
        assert elapsed < 2.0
        assert stream.aclose_count >= 1

    def test_no_cancel_completes_normally(self) -> None:
        """无取消（run_id 缺席，cancel_event 从未置位）：流完整消费，
        streamed + 全文返回——可中断等待不误伤既有完成路径。"""
        mod = _load_server()
        bus = _FakeBus()
        adapter, stream = _make_adapter(
            [_upstream_chunk("He"), _upstream_chunk("llo")], hang_after=False
        )
        _inject(mod, "event-bus", bus)
        mod._adapter = adapter

        result = _run(
            asyncio.wait_for(
                mod.llm_complete_stream(
                    model="glm-5.2",
                    messages=[{"role": "user", "content": "hi"}],
                ),
                timeout=5.0,
            )
        )

        assert result["status"] == "streamed"
        assert result["text"] == "Hello"
        assert result["partial"] is None
        assert result["finish_reason"] == "stop"
        assert stream.aclose_count >= 1  # 正常收尾同样关流


class TestKeyPoolAbortForwarding:
    """KeyPool 预算展宽覆写不吞 abort_event（生产 adapter 类首 token 取消生效）。"""

    def test_keypool_establish_forwards_abort_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """覆写签名必须把 abort_event 原样透传基类（漏传 = 生产路径盲区B 仍在）。"""
        import adapter as adapter_mod
        import router_factory as _rf_mod

        captured: dict[str, Any] = {}

        async def _spy_establish(
            self: Any,
            model: str,
            call_kwargs: dict[str, Any],
            first_chunk_timeout: float,
            *,
            total_budget: float | None = None,
            abort_event: asyncio.Event | None = None,
        ) -> tuple[Any, Any]:
            captured["abort_event"] = abort_event
            return SimpleNamespace(), SimpleNamespace()

        monkeypatch.setattr(adapter_mod._BaseLLMAdapter, "_establish_first_chunk", _spy_establish)

        def _no_route(_model_id: str) -> None:
            return None

        def _no_pool(_provider: str) -> None:
            return None

        def _no_fallbacks(_model_id: str) -> list[str]:
            return []

        monkeypatch.setattr(_rf_mod, "get_provider_for_model", _no_route)
        monkeypatch.setattr(_rf_mod, "get_key_pool", _no_pool)
        monkeypatch.setattr(_rf_mod, "get_fallback_models", _no_fallbacks)

        pool_adapter = adapter_mod.KeyPoolAdapter(router=SimpleNamespace())
        ev = asyncio.Event()
        asyncio.run(
            pool_adapter._establish_first_chunk("prov/model-x", {}, 30.0, abort_event=ev)
        )
        assert captured["abort_event"] is ev


class TestAbortOuterCancelGuard:
    """abort 收尾不得吞外层任务级取消（server 契约：任务级取消原样传播）。"""

    def test_abort_path_propagates_outer_task_cancel(self) -> None:
        """停止信号胜出的收尾窗口内，外层任务级取消在飞 → CancelledError 原样
        透传（不被收尾 suppress 吞掉换发业务返回）。"""
        import adapter as adapter_mod

        async def _scenario() -> None:
            ev = asyncio.Event()
            ev.set()  # 停止信号先置位：abort 通道必然胜出
            release = asyncio.Event()
            main = asyncio.current_task()
            assert main is not None

            async def _body() -> None:
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    # 收尾拖一拍（真实关流形态）——留外层取消注入窗
                    await asyncio.sleep(0.01)
                    raise

            async def _cancel_main_soon() -> None:
                await asyncio.sleep(0.003)
                main.cancel()

            canceler = asyncio.ensure_future(_cancel_main_soon())
            try:
                await adapter_mod._await_with_escape(
                    _body(), 10.0, what="outer-cancel-guard", abort_event=ev
                )
            finally:
                canceler.cancel()

        async def _runner() -> None:
            with pytest.raises(asyncio.CancelledError):
                await _scenario()

        asyncio.run(_runner())
