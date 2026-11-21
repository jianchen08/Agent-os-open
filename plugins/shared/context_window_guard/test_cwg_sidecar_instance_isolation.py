# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-coverage
"""context_window_guard sidecar 实例复用契约（per-pipeline 运行时状态隔离）。

同 sidecar 进程内一个 ContextWindowGuardPlugin 实例被多个管道并发复用，async
handler 在 SDK 层不串行（只有 sync handler 有锁）。2026-11-12 task_reminder
197d 事故同范式：per-pipeline 配置/计数写实例字段，await 处被其他管道覆盖。

本文件锁死三条契约：
1. 运行时窗口配置（window_cfg / trigger_ratio）解析成 execute 局部值——
   交叠执行的另一管道改不了本管道的有效触发比例；
2. ``_tracked_msg_count`` 按 pipeline_id 隔离（state 真值 ∪ 进程内该管道
   跨 run 缓存），A 管道的 60 条不会让 B 管道误走「无增量」估算档；
3. compression_failed 去重按管道——A 管道的失败已推过，不得静音 B 管道
   的首次失败。

交叠点用事件门控（``clean_if_window_changed`` 是 config 解析之后、阈值判定
之前的 await 点），不依赖 sleep 时序。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
_SHARED_DIR = str(_PLUGIN_DIR.parents[0])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)


def _load_plugin_module() -> Any:
    mod_name = "cwg_sidecar_isolation_test"
    sys.modules.pop(mod_name, None)
    module_path = _PLUGIN_DIR / "plugin.py"
    assert module_path.exists(), f"plugin.py missing at {module_path}"
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None, "Cannot load plugin.py"
    assert spec.loader is not None, "Cannot load plugin.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _RecordingCompressService:
    """记录每次 compress_messages 的 trigger_ratio；可选在调用中等待门闸。"""

    def __init__(self, release: asyncio.Event | None = None) -> None:
        self._release = release
        self.trigger_ratios: list[float] = []

    def setup(self, **kwargs: Any) -> None:  # noqa: ARG002 —— 服务注入契约
        pass

    async def compress_messages(self, **kwargs: Any) -> None:
        self.trigger_ratios.append(float(kwargs.get("trigger_ratio") or 0.0))
        if self._release is not None:
            await self._release.wait()


class _NullCompressService:
    """压缩无产出的服务桩（走降级 + compression_failed 通知路径）。"""

    def __init__(self) -> None:
        self.calls = 0

    def setup(self, **kwargs: Any) -> None:  # noqa: ARG002
        pass

    async def compress_messages(self, **kwargs: Any) -> None:  # noqa: ARG002
        self.calls += 1


def _state(pipeline_id: str, *, messages: int = 1) -> dict[str, Any]:
    """越过触发线的 state（80000 tokens 高于测试内任一经 ratio 折算的触发线）。"""
    return {
        "pipeline_id": pipeline_id,
        "context_window": 128_000,
        "messages": [
            {"role": "user", "content": "msg " + "x" * 100, "seq": i}
            for i in range(messages)
        ],
        "llm_usage": {"input_tokens": 80_000},
    }


def _ctx(mod: Any, state: dict[str, Any], config: dict[str, Any] | None = None) -> Any:
    return mod._make_minimal_ctx(
        state=state,
        config=config or {},
        pipeline_id=str(state.get("pipeline_id") or ""),
    )


class TestRuntimeConfigPerPipeline:
    def test_interleaved_executes_keep_own_trigger_ratio(self) -> None:
        """A 管道在 config 解析后让出，B 管道整轮跑完；A 的压缩调用必须仍带
        A 自己的 trigger_ratio（旧实现：实例字段已被 B 覆盖）。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None
        plugin = mod.ContextWindowGuardPlugin()

        release = asyncio.Event()
        entered_a = asyncio.Event()
        service_a = _RecordingCompressService(release=release)
        service_b = _RecordingCompressService()

        original_clean = plugin.clean_if_window_changed

        async def gated_clean(messages: Any, context_window: int, ctx: Any) -> Any:
            if ctx.state.get("pipeline_id") == "pipe-a":
                entered_a.set()
                await release.wait()
            return await original_clean(messages, context_window, ctx)

        plugin.clean_if_window_changed = gated_clean  # type: ignore[method-assign]

        ctx_a = _ctx(
            mod, _state("pipe-a"), {"context_window": {"compress_trigger_ratio": 0.05}}
        )
        ctx_b = _ctx(
            mod, _state("pipe-b"), {"context_window": {"compress_trigger_ratio": 0.2}}
        )
        ctx_a._services["context_service"] = service_a
        ctx_b._services["context_service"] = service_b

        async def scenario() -> None:
            # A 先跑到交叠点（config 已解析），B 整轮完成，再放行 A
            task_a = asyncio.create_task(plugin.execute(ctx_a))
            await entered_a.wait()
            await plugin.execute(ctx_b)
            release.set()
            await task_a

        _run(scenario())

        assert service_a.trigger_ratios == [0.05], (
            f"A 管道必须用自己注入的 0.05，实际 {service_a.trigger_ratios}（实例字段被 B 覆盖）"
        )
        assert service_b.trigger_ratios == [0.2]
        # 构造默认不被运行时解析改写（跨管道串台源）
        assert plugin._trigger_ratio == 0.55
        assert plugin._window_cfg == {}

    def test_state_override_stays_local_to_pipeline(self) -> None:
        """ctx.state 覆盖只作用于本管道：另一管道无该键时仍用注入命名空间值。"""
        mod = _load_plugin_module()
        plugin = mod.ContextWindowGuardPlugin({})
        ctx_a = _ctx(
            mod, _state("pipe-a"), {"context_window": {"compress_trigger_ratio": 0.4}}
        )
        ctx_a.state["context_guard.trigger_ratio"] = 0.01
        ctx_b = _ctx(
            mod, _state("pipe-b"), {"context_window": {"compress_trigger_ratio": 0.4}}
        )

        assert plugin._resolve_runtime_config(ctx_a)[1] == 0.01
        assert plugin._resolve_runtime_config(ctx_b)[1] == 0.4
        assert plugin._resolve_runtime_config(ctx_a)[1] == 0.01, "后一次解析不得污染前次"


class TestTrackedCountPerPipeline:
    def _capture_tracked(self, plugin: Any) -> dict[str, int]:
        """包一层估算入口，按 pipeline_id 记录 execute 传入的追踪计数。"""
        captured: dict[str, int] = {}
        original = plugin._estimate_effective_tokens

        async def spy(messages: Any, ctx: Any, tracked_msg_count: int) -> Any:
            captured[str(ctx.state.get("pipeline_id") or "")] = tracked_msg_count
            return await original(messages, ctx, tracked_msg_count)

        plugin._estimate_effective_tokens = spy  # type: ignore[method-assign]
        return captured

    def test_pipeline_count_does_not_leak_to_other_pipeline(self) -> None:
        """A 管道跑完 60 条消息后，B 管道的估算基线必须仍是 0。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None
        plugin = mod.ContextWindowGuardPlugin()
        captured = self._capture_tracked(plugin)

        ctx_a = _ctx(mod, _state("pipe-a", messages=60))
        ctx_a._services["context_service"] = _NullCompressService()
        _run(plugin.execute(ctx_a))
        assert captured["pipe-a"] == 0, "首轮无历史计数"
        ctx_b = _ctx(mod, _state("pipe-b", messages=3))
        ctx_b._services["context_service"] = _NullCompressService()
        _run(plugin.execute(ctx_b))
        assert captured["pipe-b"] == 0, (
            f"B 管道不得继承 A 的 60 条追踪计数（实际 {captured['pipe-b']}）"
        )

    def test_same_pipeline_keeps_count_across_runs(self) -> None:
        """同一管道的进程内缓存仍跨 execute 继承（state 丢失时的既有语义保留）。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None
        plugin = mod.ContextWindowGuardPlugin()
        captured = self._capture_tracked(plugin)

        for _ in range(2):
            ctx = _ctx(mod, _state("pipe-a", messages=60))
            ctx._services["context_service"] = _NullCompressService()
            _run(plugin.execute(ctx))
        assert captured["pipe-a"] == 60, "同管道第二轮应读到第一轮写入的追踪计数"

    def test_state_value_wins_over_cache(self) -> None:
        """state 真值高于进程内缓存（跨进程恢复后 state 是唯一权威）。"""
        mod = _load_plugin_module()
        plugin = mod.ContextWindowGuardPlugin()
        plugin._tracked_by_pipeline["pipe-a"] = 3
        state = _state("pipe-a")
        state["_tracked_msg_count"] = 9
        assert plugin._tracked_message_count(_ctx(mod, state)) == 9

    def test_non_int_state_count_treated_as_zero(self) -> None:
        """state 里的坏形态（字符串/布尔）不参与比较，按 0 计（防 TypeError 炸估算）。"""
        mod = _load_plugin_module()
        plugin = mod.ContextWindowGuardPlugin()
        plugin._tracked_by_pipeline["pipe-a"] = 2
        for bad in ("7", True, None):
            state = _state("pipe-a")
            state["_tracked_msg_count"] = bad
            assert plugin._tracked_message_count(_ctx(mod, state)) == 2, bad

    def test_cache_is_bounded_by_pipeline_capacity(self) -> None:
        """进程内缓存有界：超过上限按插入序淘汰最旧管道（长期 sidecar 不无界增长）。"""
        mod = _load_plugin_module()
        plugin = mod.ContextWindowGuardPlugin()
        plugin._tracked_cache_max = 3
        for i in range(5):
            plugin._set_tracked_message_count(_ctx(mod, _state(f"pipe-{i}")), i)
        assert set(plugin._tracked_by_pipeline) == {"pipe-2", "pipe-3", "pipe-4"}
        # 再次触达旧管道 → 重插为最近使用，下一条新管道挤掉的是更旧者
        plugin._set_tracked_message_count(_ctx(mod, _state("pipe-2")), 9)
        plugin._set_tracked_message_count(_ctx(mod, _state("pipe-5")), 5)
        assert set(plugin._tracked_by_pipeline) == {"pipe-2", "pipe-4", "pipe-5"}
        assert plugin._tracked_by_pipeline["pipe-2"] == 9


class TestCompressFailureDedupePerPipeline:
    def test_each_pipeline_gets_own_first_failure_event(self) -> None:
        """A 管道已推过 compression_failed，B 管道的首次失败仍必须推送。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None
        events: list[tuple[str, dict[str, Any]]] = []

        async def _fake_emit(event: str, payload: dict[str, Any], thread_id: str) -> None:  # noqa: ARG001
            events.append((event, payload))

        mod.set_frontend_emit(_fake_emit)
        plugin = mod.ContextWindowGuardPlugin({"trigger_ratio": 0.5})
        try:
            for pipeline_id in ("pipe-a", "pipe-b"):
                ctx = _ctx(mod, _state(pipeline_id))
                ctx._services["context_service"] = _NullCompressService()
                _run(plugin.execute(ctx))
            # 同管道第二轮：不得重复推（同一故障周期不刷屏）
            ctx = _ctx(mod, _state("pipe-a"))
            ctx._services["context_service"] = _NullCompressService()
            _run(plugin.execute(ctx))
        finally:
            mod.set_frontend_emit(None)

        failed = [payload for name, payload in events if name == "compression_failed"]
        assert [p["pipeline_id"] for p in failed] == ["pipe-a", "pipe-b"], (
            f"每个管道各一次首次失败通知，实际 {[p['pipeline_id'] for p in failed]}"
        )

    def test_failure_notified_cache_is_bounded(self) -> None:
        """去重缓存有界：超限按插入序淘汰最旧管道（长期 sidecar 不无界增长）。"""
        mod = _load_plugin_module()
        mod._memory_backend = None
        mod._capability_caller = None
        emitted: list[str] = []

        async def _fake_emit(event: str, payload: dict[str, Any], thread_id: str) -> None:  # noqa: ARG001
            if event == "compression_failed":
                emitted.append(str(payload.get("pipeline_id")))

        mod.set_frontend_emit(_fake_emit)
        plugin = mod.ContextWindowGuardPlugin({"trigger_ratio": 0.5})
        plugin._tracked_cache_max = 2
        try:
            for pipeline_id in ("pipe-1", "pipe-2", "pipe-3"):
                ctx = _ctx(mod, _state(pipeline_id))
                ctx._services["context_service"] = _NullCompressService()
                _run(plugin.execute(ctx))
        finally:
            mod.set_frontend_emit(None)

        assert emitted == ["pipe-1", "pipe-2", "pipe-3"]
        assert list(plugin._compress_fail_notified) == ["pipe-2", "pipe-3"]
