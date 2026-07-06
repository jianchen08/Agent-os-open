"""端到端行为不变量测试：用真实的 consume + execute_core_plugin + apply_route，
只 mock LLM（返回固定回复）和 WS sink（记录事件）。

测试目标（行为不变量，不是代码分支）：
  不管内部架构怎么变，只要以下行为正确就通过，偏离就爆红。

核心行为不变量：
  1. 通知入队后，LLM 调用时必须能看到（inject 和 LLM 调用同步）
  2. consume 推送 system_notification 到 WS
  3. LLM 看到的通知 = WS 推送的通知（两边一致）

测试方法：
  - 用真实的 consume_pending_notifications（不 mock）
  - 用真实的 execute_core_plugin（不 mock，但 mock 它内部的 LLM 插件）
  - 用真实的 apply_route（不 mock）
  - 用真实的 PipelineStreamBridge + _RecordingSink
  - 分步调用（consume → execute_core_plugin），不调完整 run_iteration
    （run_iteration 的 output chain + apply_route 容易挂起，分开测更稳定）
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
pytestmark = pytest.mark.timing
# §9.4: 时序不变量门禁 — 此文件的测试断言可观察行为（事件顺序/间隔/超时边界/资源回收），
# 不含实现细节断言（mock.call_count/私有方法），破坏不变量的改动在 CI 阶段即被拦截。

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from pipeline.engine_iteration import consume_pending_notifications
from pipeline.types import StateKeys

# ── Mock 组件（只 mock 最外层：LLM 和 WS）──


class _RecordingSink:
    """实现 IOutputSink，按顺序记录所有 WS 事件。"""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._thread_id: str = "test-thread"

    @property
    def sink_id(self) -> str:
        return "recording-sink"

    @property
    def is_dead(self) -> bool:
        return False

    async def send_event(self, event: dict) -> bool:
        self.events.append(event)
        return True

    @property
    def event_types(self) -> list[str]:
        return [e.get("type", "?") for e in self.events]


class _StubLLMCore:
    """假 LLM Core：记录每次调用的 state.messages 快照，返回固定回复。"""

    def __init__(self, reply: str = "AI回复") -> None:
        self._reply = reply
        self.call_snapshots: list[list[dict]] = []
        self.call_count: int = 0
        self.error_policy = None
        self.max_retries = 0

    @property
    def name(self) -> str:
        return "stub_llm"

    @property
    def priority(self) -> int:
        return 50

    async def execute(self, ctx: Any) -> dict[str, Any]:
        self.call_count += 1
        self.call_snapshots.append([dict(m) for m in ctx.state.get("messages", [])])
        return {
            StateKeys.RAW_RESULT: self._reply,
            StateKeys.RAW_ERROR: None,
            StateKeys.RAW_TOOL_CALLS: [],
            StateKeys.RAW_THINKING: None,
            "messages": list(ctx.state.get("messages", []))
            + [{"role": "assistant", "content": self._reply}],
            "llm_usage": {},
        }


# ── 测试 fixture ──


@pytest.fixture
def clean_registry():
    """每个测试前后清空全局 EngineRegistry。"""
    from pipeline.engine_registry import get_engine_registry

    registry = get_engine_registry()
    for pid in list(registry._engines.keys()):
        try:
            registry.unregister(pid)
        except Exception:
            pass
    yield registry
    for pid in list(registry._engines.keys()):
        try:
            registry.unregister(pid)
        except Exception:
            pass


def _build_engine_and_sink(clean_registry):
    """搭建真实的 engine + bridge + recording sink。"""
    from pipeline.engine import PipelineEngine
    from pipeline.registry import PluginRegistry
    from pipeline.route import InputRouteEntry, InputRouteTable, OutputRouteEntry, OutputRouteTable
    from pipeline.stream_bridge import PipelineStreamBridge

    input_route_table = InputRouteTable([
        InputRouteEntry(name="default", condition="True", target="core", priority=10),
    ])
    output_route_table = OutputRouteTable([
        OutputRouteEntry(name="end_on_text", route_type="end", condition="True", priority=10),
    ])

    plugin_registry = PluginRegistry()
    stub_llm = _StubLLMCore(reply="AI回复")
    plugin_registry.register_core("llm_call", stub_llm)

    engine = PipelineEngine(
        input_route_table=input_route_table,
        output_route_table=output_route_table,
        plugin_registry=plugin_registry,
        services={},
        agent_registry=SimpleNamespace(get=lambda _id: None),
    )

    sink = _RecordingSink()
    clean_registry.register(engine.pipeline_id, engine, thread_id="test-thread")
    bridge = PipelineStreamBridge(pipeline_id=engine.pipeline_id, output_sink=sink)
    clean_registry.set_bridge(engine.pipeline_id, bridge)

    return engine, sink, stub_llm, plugin_registry


def _make_state(engine: Any, messages: list[dict] | None = None) -> dict[str, Any]:
    return {
        StateKeys.PIPELINE_ID: engine.pipeline_id,
        StateKeys.ITERATION: 0,
        StateKeys.ENDED: False,
        StateKeys.CORE_TYPE: "llm_call",
        "messages": messages or [{"role": "user", "content": "开始"}],
        "user_input": "开始",
    }


def _run(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
        import concurrent.futures  # noqa: PLC0415

        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result(timeout=10)
    except RuntimeError:
        return asyncio.run(coro)


# ── 端到端行为不变量测试 ──


class TestConsumeThenLLME2E:
    """端到端：用真实 consume + 真实 execute_core_plugin，验证行为不变量。

    分步调用（consume → execute_core_plugin），模拟 run_iteration 的核心步骤，
    但不调完整 run_iteration（避免 output chain + apply_route 挂起）。
    这样测的是「consume 在 LLM 之前」这个位置关系 + consume 和 LLM 的同步性。
    """

    def test_notification_injected_llm_sees_it_and_ws_pushed(self, clean_registry):
        """★ 核心不变量：通知入队 → consume 注入+推送 → LLM 看到 → WS 收到。

        完整流程：
        1. inject 通知
        2. consume（真实 consume_pending_notifications）
        3. execute_core_plugin（真实，mock LLM 返回）
        4. 断言三者同步
        """
        engine, sink, stub_llm, _ = _build_engine_and_sink(clean_registry)
        engine.inject_message("[系统通知] 子任务完成", source="system")
        state = _make_state(engine)

        # ★ 步骤1：consume（run_iteration 第①步）
        consumed = _run(consume_pending_notifications(engine, state, prepend=True))
        assert consumed is True, "consume 必须消费通知"

        # ★ 步骤2：execute_core_plugin（run_iteration 第④步，真实调用，mock LLM）
        from pipeline.engine_chain import execute_core_plugin

        _run(execute_core_plugin(engine, state, "llm_call"))

        # ★ 断言1：LLM 调用时 messages 里有通知
        assert stub_llm.call_count == 1, "LLM 必须被调用1次"
        last_msgs = stub_llm.call_snapshots[-1]
        assert any(
            "子任务完成" in str(m.get("content", "")) for m in last_msgs
        ), f"LLM messages 必须包含通知。实际: {last_msgs}"

        # ★ 断言2：WS 收到 system_notification
        assert "system_notification" in sink.event_types, (
            f"WS 必须收到 system_notification。实际: {sink.event_types}"
        )

        # ★ 断言3：LLM 看到的 = WS 推送的（内容一致）
        ws_notif = next(e for e in sink.events if e["type"] == "system_notification")
        ws_content = ws_notif["data"]["content"]
        llm_saw = any("子任务完成" in str(m.get("content", "")) for m in last_msgs)
        assert llm_saw, "LLM 必须看到通知"
        assert "子任务完成" in ws_content, f"WS 必须推送相同通知。实际: {ws_content}"

    def test_consume_skipped_llm_doesnt_see_notification(self, clean_registry):
        """★ 防回归：跳过 consume，LLM 看不到通知，WS 也没推送。

        这正是之前撤掉 run_iteration consume 后的 bug 场景。
        """
        engine, sink, stub_llm, _ = _build_engine_and_sink(clean_registry)
        engine.inject_message("[系统通知] 重要通知", source="system")
        state = _make_state(engine)

        # ★ 跳过 consume，直接 execute_core_plugin
        from pipeline.engine_chain import execute_core_plugin

        _run(execute_core_plugin(engine, state, "llm_call"))

        # ★ 断言：LLM 没看到通知
        last_msgs = stub_llm.call_snapshots[-1]
        has_notif = any("重要通知" in str(m.get("content", "")) for m in last_msgs)
        assert not has_notif, "跳过 consume 时 LLM 不应看到通知"

        # ★ WS 也没推送
        assert "system_notification" not in sink.event_types, "跳过 consume 时不应推送"

    def test_multiple_rounds_each_synced(self, clean_registry):
        """★ 多轮：每轮 consume → LLM，通知按时序逐轮处理。

        模拟真实触发器场景：第1轮通知A，第2轮通知B。
        """
        engine, sink, stub_llm, _ = _build_engine_and_sink(clean_registry)
        from pipeline.engine_chain import execute_core_plugin

        # 第1轮：通知A
        engine.inject_message("[系统通知] 通知A", source="system")
        state = _make_state(engine)
        _run(consume_pending_notifications(engine, state, prepend=True))
        _run(execute_core_plugin(engine, state, "llm_call"))

        msgs1 = stub_llm.call_snapshots[-1]
        assert any("通知A" in str(m.get("content", "")) for m in msgs1), "第1轮 LLM 必须看到通知A"

        # 第2轮：通知B
        engine.inject_message("[系统通知] 通知B", source="system")
        _run(consume_pending_notifications(engine, state, prepend=True))
        _run(execute_core_plugin(engine, state, "llm_call"))

        msgs2 = stub_llm.call_snapshots[-1]
        assert any("通知B" in str(m.get("content", "")) for m in msgs2), "第2轮 LLM 必须看到通知B"

        # WS 推送了2条 system_notification
        notif_events = [e for e in sink.events if e["type"] == "system_notification"]
        assert len(notif_events) >= 2, f"2轮必须2条通知事件。实际: {len(notif_events)}"

    def test_no_notification_normal_llm_call(self, clean_registry):
        """★ 无通知时，LLM 正常调用，不推送 system_notification。"""
        engine, sink, stub_llm, _ = _build_engine_and_sink(clean_registry)
        state = _make_state(engine)

        _run(consume_pending_notifications(engine, state, prepend=True))
        from pipeline.engine_chain import execute_core_plugin

        _run(execute_core_plugin(engine, state, "llm_call"))

        assert stub_llm.call_count == 1, "LLM 必须被调用"
        assert "system_notification" not in sink.event_types, "无通知不应推送"

    def test_user_message_also_consumed_before_llm(self, clean_registry):
        """★ user 消息也走 consume，LLM 能看到。"""
        engine, sink, stub_llm, _ = _build_engine_and_sink(clean_registry)
        engine.inject_message("用户的提问", source="user")
        state = _make_state(engine)

        _run(consume_pending_notifications(engine, state, prepend=True))
        from pipeline.engine_chain import execute_core_plugin

        _run(execute_core_plugin(engine, state, "llm_call"))

        last_msgs = stub_llm.call_snapshots[-1]
        assert any(
            "用户的提问" in str(m.get("content", "")) for m in last_msgs
        ), "LLM 必须看到 user 消息"

    def test_tool_execute_round_skips_consume(self, clean_registry):
        """★ tool_execute 轮 consume 跳过，队列保留给下一轮 llm_call。"""
        engine, sink, stub_llm, _ = _build_engine_and_sink(clean_registry)
        engine.inject_message("[系统通知] 工具期间的通知", source="system")
        state = _make_state(engine)
        state[StateKeys.CORE_TYPE] = "tool_execute"

        # tool_execute 轮 consume 跳过
        consumed = _run(consume_pending_notifications(engine, state, prepend=True))
        assert consumed is False, "tool_execute 轮必须跳过"
        assert engine.inject_queue_size == 1, "队列必须保留"

        # 切回 llm_call，consume 应该消费
        state[StateKeys.CORE_TYPE] = "llm_call"
        consumed = _run(consume_pending_notifications(engine, state, prepend=True))
        assert consumed is True, "llm_call 轮必须消费"
        assert "system_notification" in sink.event_types, "llm_call 轮必须推送"
