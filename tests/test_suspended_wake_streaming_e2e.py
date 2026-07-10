"""端到端测试：suspended 引擎被任务完成通知唤醒后的流式输出。

验证核心场景：
1. 引擎挂起后，任务完成通知通过 send_pipeline_message 唤醒引擎
2. 唤醒后 drain_loop 正确消费 LLM 生成的 chunk
3. 前端 sink 收到完整的流式事件序列

对比测试：
- idle 路径（用户消息首次发送）的流式输出
- suspended 唤醒路径（任务完成通知）的流式输出
两者应产生相同的流式事件序列。
"""
import asyncio
import sys
import os
import pytest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class FakeSink:
    """模拟 IOutputSink，收集所有发送的事件用于断言。"""

    def __init__(self):
        self.events: list[dict] = []

    async def send_event(self, event: dict) -> bool:
        self.events.append(event)
        return True

    @property
    def sink_id(self) -> str:
        return "fake-sink"


class FakeEngine:
    """模拟 PipelineEngine，支持真实的异步挂起/唤醒行为。

    与简单 mock 不同，这个 FakeEngine 使用 asyncio.Event 实现真实的
    挂起等待和唤醒逻辑，能模拟真实引擎的异步时序。
    """

    def __init__(self, pipeline_id: str = ""):
        self._pipeline_id = pipeline_id or uuid.uuid4().hex[:12]
        self._running = False
        self._suspended_state: dict | None = None
        self._wake_event: asyncio.Event | None = None
        self._pending_notifications: list[str] = []
        self._on_chunk = None
        self._streaming_flag = False
        self._run_started = True

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_suspended(self) -> bool:
        return self._suspended_state is not None

    @property
    def pipeline_id(self) -> str:
        return self._pipeline_id

    def inject_message(self, msg: str, *, source: str = "user") -> None:
        if not msg:
            return
        if self.is_suspended:
            if self._suspended_state is not None:
                existing = self._suspended_state.get("user_input", "")
                self._suspended_state["user_input"] = f"{msg}\n{existing}" if existing else msg
                self._suspended_state.setdefault("messages", []).append(
                    {"role": "user", "content": msg}
                )
            if self._wake_event is not None:
                self._wake_event.set()
        else:
            self._pending_notifications.append(msg)
            if self._wake_event is not None:
                self._wake_event.set()

    def consume_pending_notifications(self) -> list[str]:
        if not self._pending_notifications:
            return []
        notifs = self._pending_notifications[:]
        self._pending_notifications.clear()
        return notifs

    def save_streaming_context(self, state: dict) -> None:
        on_chunk = state.get("on_chunk")
        if on_chunk is not None:
            self._on_chunk = on_chunk
        self._streaming_flag = state.get("streaming", True)

    def restore_streaming_context(self, state: dict) -> None:
        if self._on_chunk is not None and "on_chunk" not in state:
            state["on_chunk"] = self._on_chunk
        if self._streaming_flag and "streaming" not in state:
            state["streaming"] = self._streaming_flag

    async def simulate_run_then_suspend(self, bridge, suspend_after=0.1):
        """模拟引擎运行一段时间后挂起。

        1. 设置 running 状态
        2. 发送 pipeline_suspended chunk
        3. 进入挂起等待
        """
        self._running = True
        self._suspended_state = None
        await asyncio.sleep(suspend_after)
        self._running = False
        self._suspended_state = {
            "messages": [{"role": "assistant", "content": "之前的内容"}],
            "user_input": "",
            "on_chunk": bridge.on_chunk,
            "streaming": True,
        }
        self._wake_event = asyncio.Event()
        if bridge.on_chunk:
            bridge.on_chunk({
                "type": "pipeline_suspended",
                "pipeline_id": self._pipeline_id,
            })
        await self._wake_event.wait()
        self._running = True
        self._suspended_state = None
        self._wake_event = None

    async def simulate_llm_response(self, chunks: list[str], delay=0.05):
        """模拟 LLM 生成流式内容。"""
        on_chunk = self._on_chunk
        if on_chunk is None:
            return
        for chunk_text in chunks:
            await asyncio.sleep(delay)
            on_chunk({"type": "text", "content": chunk_text})

    async def simulate_finish(self):
        """模拟引擎运行结束。"""
        self._running = False

    async def run(self, **kwargs):
        """模拟 engine.run()，供 idle 路径的 run_in_executor 调用。"""
        self._running = True
        await asyncio.sleep(0.5)
        self._running = False


@pytest.fixture(autouse=True)
def _clean_registry():
    from pipeline.registry import get_engine_registry
    registry = get_engine_registry()
    registry._engines.clear()
    yield
    registry._engines.clear()


class TestIdlePathStreaming:
    """基线测试：idle 路径的流式输出（用户首次发消息）。"""

    @pytest.mark.asyncio
    async def test_idle_path_produces_full_stream_events(self):
        """idle 路径：引擎在 idle 状态接收消息 → run → 流式输出 → 完整事件序列。"""
        from pipeline.message_bus import send_pipeline_message
        from pipeline.registry import get_engine_registry
        from pipeline.stream_bridge import PipelineStreamBridge

        registry = get_engine_registry()
        pid = "idle-test-pipe"

        engine = FakeEngine(pid)
        engine._run_started = False  # idle 状态：未启动过 run()
        registry.register(pid, engine, thread_id="ws-thread-001")

        sink = FakeSink()
        bridge = PipelineStreamBridge(pipeline_id=pid, output_sink=sink)
        registry.set_bridge(pid, bridge)

        engine._on_chunk = bridge.on_chunk

        async def _engine_lifecycle():
            engine._running = True
            await asyncio.sleep(0.1)
            bridge.on_chunk({"type": "text", "content": "你好"})
            bridge.on_chunk({"type": "text", "content": "世界"})
            await asyncio.sleep(0.2)
            engine._running = False

        lifecycle_task = asyncio.create_task(_engine_lifecycle())

        result = await send_pipeline_message(
            pid, "hello",
            output_sink=sink,
            streaming=True,
        )

        assert result.success

        await asyncio.sleep(1.0)
        lifecycle_task.cancel()
        try:
            await lifecycle_task
        except (asyncio.CancelledError, Exception):
            pass

        event_types = [e["type"] for e in sink.events]
        has_stream_start = "stream_start" in event_types
        has_stream_chunk = "stream_chunk" in event_types

        print(f"\n[IDLE] 事件类型序列: {event_types}")
        print(f"[IDLE] stream_start: {has_stream_start}, stream_chunk: {has_stream_chunk}")

        assert has_stream_start, "idle 路径应有 stream_start"
        assert has_stream_chunk, "idle 路径应有 stream_chunk"


class TestSuspendedWakeStreaming:
    """核心测试：suspended 引擎被任务完成通知唤醒后的流式输出。"""

    @pytest.mark.asyncio
    async def test_system_notification_wake_produces_stream_events(self):
        """任务完成通知唤醒 suspended 引擎 → drain_loop 应消费 LLM chunk → 前端收到流式事件。"""
        from pipeline.message_bus import send_pipeline_message
        from pipeline.registry import get_engine_registry
        from pipeline.stream_bridge import PipelineStreamBridge

        registry = get_engine_registry()
        pid = "wake-test-pipe"

        engine = FakeEngine(pid)
        registry.register(pid, engine, thread_id="ws-thread-002")

        sink = FakeSink()
        bridge = PipelineStreamBridge(pipeline_id=pid, output_sink=sink)
        registry.set_bridge(pid, bridge)

        # 手动启动 drain_loop（模拟真实环境中 ensure_bridge 的行为）
        # 使用永不完成的 Future 作为 engine_task，防止 drain_loop 因 engine_task=None 立即退出
        _never_done = asyncio.get_running_loop().create_future()
        async def _drain():
            await bridge.drain_loop(_never_done, heartbeat_interval=5.0)
        drain_task = asyncio.create_task(_drain())

        engine._on_chunk = bridge.on_chunk
        engine._streaming_flag = True

        async def _engine_full_lifecycle():
            await engine.simulate_run_then_suspend(bridge, suspend_after=0.1)
            await asyncio.sleep(0.05)
            await engine.simulate_llm_response(["任务", "完成", "通知"], delay=0.05)
            await asyncio.sleep(0.1)
            await engine.simulate_finish()

        lifecycle_task = asyncio.create_task(_engine_full_lifecycle())

        await asyncio.sleep(0.3)

        result = await send_pipeline_message(
            pid,
            "[系统通知] 子任务已完成",
            metadata={"source": "system"},
            output_sink=sink,
        )

        assert result.success, f"send_pipeline_message 应成功，实际: {result.error}"
        assert result.method == "wake", f"应为 wake 方法，实际: {result.method}"

        await asyncio.sleep(1.0)

        event_types = [e["type"] for e in sink.events]
        has_stream_start = "stream_start" in event_types
        has_stream_chunk = "stream_chunk" in event_types
        has_system_notification = "system_notification" in event_types
        has_stream_end = "stream_end" in event_types

        print(f"\n[WAKE] 事件类型序列: {event_types}")
        print(f"[WAKE] stream_start: {has_stream_start}, stream_chunk: {has_stream_chunk}, "
              f"system_notification: {has_system_notification}, stream_end: {has_stream_end}")

        for evt in sink.events:
            print(f"  {evt['type']}: {str(evt.get('data', {}))[:80]}")

        assert has_stream_start, "应有 stream_start"
        assert has_stream_chunk, "应有 stream_chunk（唤醒后 LLM 产出被消费）"

        if not has_stream_start:
            print("\n*** BUG 确认: suspended 唤醒后没有 stream_start! ***")
            print("*** 这说明 drain_loop 没有正确启动或提前退出 ***")
        if not has_stream_chunk:
            print("\n*** BUG 确认: suspended 唤醒后没有 stream_chunk! ***")
            print("*** LLM 的 chunk 没有被 drain_loop 消费 ***")

        lifecycle_task.cancel()
        drain_task.cancel()
        try:
            await lifecycle_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await drain_task
        except (asyncio.CancelledError, Exception):
            pass

    @pytest.mark.asyncio
    async def test_user_message_wake_suspended_produces_stream_events(self):
        """对比测试：用户消息唤醒 suspended 引擎，验证是否也有同样问题。"""
        from pipeline.message_bus import send_pipeline_message
        from pipeline.registry import get_engine_registry
        from pipeline.stream_bridge import PipelineStreamBridge

        registry = get_engine_registry()
        pid = "user-wake-test-pipe"

        engine = FakeEngine(pid)
        registry.register(pid, engine, thread_id="ws-thread-003")

        sink = FakeSink()
        bridge = PipelineStreamBridge(pipeline_id=pid, output_sink=sink)
        registry.set_bridge(pid, bridge)

        engine._on_chunk = bridge.on_chunk
        engine._streaming_flag = True

        async def _engine_full_lifecycle():
            await engine.simulate_run_then_suspend(bridge, suspend_after=0.1)
            await asyncio.sleep(0.05)
            await engine.simulate_llm_response(["用户", "消息", "回复"], delay=0.05)
            await asyncio.sleep(0.1)
            await engine.simulate_finish()

        lifecycle_task = asyncio.create_task(_engine_full_lifecycle())

        await asyncio.sleep(0.3)

        result = await send_pipeline_message(
            pid,
            "你好，请继续",
            output_sink=sink,
            streaming=True,
        )

        assert result.success, f"send_pipeline_message 应成功，实际: {result.error}"
        assert result.method == "wake", f"应为 wake 方法，实际: {result.method}"

        await asyncio.sleep(1.0)

        event_types = [e["type"] for e in sink.events]
        has_stream_start = "stream_start" in event_types
        has_stream_chunk = "stream_chunk" in event_types

        print(f"\n[USER-WAKE] 事件类型序列: {event_types}")
        print(f"[USER-WAKE] stream_start: {has_stream_start}, stream_chunk: {has_stream_chunk}")

        for evt in sink.events:
            print(f"  {evt['type']}: {str(evt.get('data', {}))[:80]}")

        lifecycle_task.cancel()
        try:
            await lifecycle_task
        except (asyncio.CancelledError, Exception):
            pass


class TestDrainLoopSuspendRace:
    """验证 drain_loop 在引擎挂起时不退出，持续空转等待。"""

    @pytest.mark.asyncio
    async def test_drain_loop_survives_suspend(self):
        """验证 drain_loop 在引擎 suspended 时不退出，等待引擎唤醒后消费 chunk。

        模拟场景：
        1. drain_loop 启动时引擎为 suspended
        2. 300ms 后引擎醒来，开始产出 chunk
        3. drain_loop 应等到 chunk 并正确消费
        """
        from pipeline.stream_bridge import PipelineStreamBridge

        sink = FakeSink()
        bridge = PipelineStreamBridge(pipeline_id="race-test", output_sink=sink)

        engine_ref = {"suspended": True, "running": False}

        async def _delayed_wake():
            await asyncio.sleep(0.3)
            engine_ref["suspended"] = False
            engine_ref["running"] = True
            bridge.on_chunk({"type": "text", "content": "唤醒"})
            await asyncio.sleep(0.2)
            engine_ref["running"] = False

        async def _engine_tracker():
            await asyncio.sleep(0.5)
            while engine_ref["running"] or engine_ref["suspended"]:
                await asyncio.sleep(0.1)

        tracker = asyncio.create_task(_engine_tracker())
        wake_task = asyncio.create_task(_delayed_wake())

        result = await bridge.drain_loop(
            tracker,
            heartbeat_interval=5.0,
        )

        event_types = [e["type"] for e in sink.events]

        print(f"\n[RACE] 事件类型序列: {event_types}")
        print(f"[RACE] drain_loop 结果: {result}")

        assert "stream_start" in event_types
        assert "stream_chunk" in event_types, "引擎唤醒后应消费 chunk"
        assert "stream_end" in event_types
        assert "唤醒" in result["accumulated_content"]

        tracker.cancel()
        wake_task.cancel()
        try:
            await tracker
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await wake_task
        except (asyncio.CancelledError, Exception):
            pass

    @pytest.mark.asyncio
    async def test_drain_loop_survives_delayed_wake(self):
        """验证 drain_loop 在引擎延迟唤醒时仍能消费 chunk。

        模拟真实场景：
        1. drain_loop 启动时引擎仍为 suspended
        2. 100ms 后引擎醒来
        3. 200ms 后 LLM 开始产生 chunk
        """
        from pipeline.stream_bridge import PipelineStreamBridge

        sink = FakeSink()
        bridge = PipelineStreamBridge(pipeline_id="delayed-wake", output_sink=sink)

        engine_ref = {"suspended": True, "running": False}

        async def _delayed_wake():
            await asyncio.sleep(0.15)
            engine_ref["suspended"] = False
            engine_ref["running"] = True
            await asyncio.sleep(0.05)
            bridge.on_chunk({"type": "text", "content": "延迟"})
            bridge.on_chunk({"type": "text", "content": "唤醒"})
            await asyncio.sleep(0.3)
            engine_ref["running"] = False

        async def _engine_tracker():
            await asyncio.sleep(0.5)
            while engine_ref["running"] or engine_ref["suspended"]:
                await asyncio.sleep(0.1)

        tracker = asyncio.create_task(_engine_tracker())
        wake_task = asyncio.create_task(_delayed_wake())

        result = await bridge.drain_loop(
            tracker,
            heartbeat_interval=5.0,
        )

        event_types = [e["type"] for e in sink.events]

        print(f"\n[DELAYED-WAKE] 事件类型序列: {event_types}")
        print(f"[DELAYED-WAKE] 累积内容: {result['accumulated_content']}")

        has_chunk = "stream_chunk" in event_types
        if not has_chunk:
            print("\n*** BUG 复现: drain_loop 在引擎唤醒前就退出了! ***")
            print(f"*** 引擎在 ~150ms 后醒来，但 drain_loop 在 100ms timeout 时检测到 suspended → 退出 ***")
        else:
            print("\n*** drain_loop 正确等待了引擎唤醒 ***")

        tracker.cancel()
        wake_task.cancel()
        try:
            await tracker
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await wake_task
        except (asyncio.CancelledError, Exception):
            pass


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
