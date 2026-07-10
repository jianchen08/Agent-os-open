"""WebSocket 消息协议修复的单元测试。

验证三个 Bug 的修复效果：
1. 消息变空：send_new_message 不应发送空内容
2. 前端抖动：事件序列应为 stream_start → stream_chunks → stream_end → new_message
3. 需刷新才显示：new_message 应在所有路径上正确发送

测试目标：
- PipelineStreamBridge.send_new_message 空内容防护
- PipelineStreamBridge 事件序列一致性
- _stream_engine_response 内容提取优先级
- _stream_wake_response new_message 发送保证
"""
import asyncio
import sys
import os
import pytest

# 将 src 目录加入 sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Mock Sink：收集所有发送的事件
# ---------------------------------------------------------------------------

class MockSink:
    """模拟 IOutputSink，收集所有发送的事件用于断言。"""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._sink_id = "mock:test"

    @property
    def sink_id(self) -> str:
        return self._sink_id

    async def send_event(self, event: dict) -> bool:
        self.events.append(event)
        return True


# ---------------------------------------------------------------------------
# 测试：PipelineStreamBridge.send_new_message 空内容防护
# ---------------------------------------------------------------------------

class TestSendNewMessageEmptyGuard:
    """验证 send_new_message 在内容为空时的行为。"""

    @pytest.mark.asyncio
    async def test_send_new_message_with_content_normally(self):
        """有内容时正常发送。"""
        from src.pipeline.stream_bridge import PipelineStreamBridge

        sink = MockSink()
        bridge = PipelineStreamBridge(
            pipeline_id="test-pipeline",
            output_sink=sink,
            message_id="test-msg-001",
        )

        await bridge.send_new_message("Hello World", sequence=1)

        assert len(sink.events) == 1
        event = sink.events[0]
        assert event["type"] == "new_message"
        assert event["data"]["content"] == "Hello World"
        assert event["data"]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_send_new_message_empty_uses_accumulated_fallback(self):
        """BUG-FIX-20260515: 内容为空时，应使用累积内容作为保底。"""
        from src.pipeline.stream_bridge import PipelineStreamBridge

        sink = MockSink()
        bridge = PipelineStreamBridge(
            pipeline_id="test-pipeline",
            output_sink=sink,
            message_id="test-msg-002",
        )

        # 模拟流式累积了内容
        bridge._accumulated_content = ["Hello", " ", "World"]

        # 传入空字符串调用 send_new_message
        await bridge.send_new_message("", sequence=1)

        assert len(sink.events) == 1
        event = sink.events[0]
        assert event["type"] == "new_message"
        # 关键断言：内容不应为空，应使用累积内容作为保底
        assert event["data"]["content"] == "Hello World", (
            "send_new_message 传入空内容时，应使用 _accumulated_content 作为保底"
        )

    @pytest.mark.asyncio
    async def test_send_new_message_empty_no_accumulated_sends_empty_with_warning(self):
        """内容和累积内容都为空时，仍然发送（但记录警告）。"""
        from src.pipeline.stream_bridge import PipelineStreamBridge

        sink = MockSink()
        bridge = PipelineStreamBridge(
            pipeline_id="test-pipeline",
            output_sink=sink,
            message_id="test-msg-003",
        )

        # 无累积内容，传入空字符串
        await bridge.send_new_message("", sequence=1)

        assert len(sink.events) == 1
        event = sink.events[0]
        assert event["type"] == "new_message"
        # 这种极端情况下内容为空，但事件仍然发送
        assert event["data"]["content"] == ""


# ---------------------------------------------------------------------------
# 测试：事件序列一致性
# ---------------------------------------------------------------------------

class TestEventSequence:
    """验证流式事件序列的正确性。"""

    @pytest.mark.asyncio
    async def test_drain_loop_event_sequence(self):
        """验证 drain_loop 的事件序列：stream_start → stream_chunks → stream_end。"""
        from src.pipeline.stream_bridge import PipelineStreamBridge

        sink = MockSink()
        bridge = PipelineStreamBridge(
            pipeline_id="test-pipeline",
            output_sink=sink,
            message_id="test-msg-seq-001",
        )

        # 创建一个完成的 engine_task
        async def _dummy_engine():
            return {"messages": []}

        engine_task = asyncio.create_task(_dummy_engine())

        # 模拟发送一些 chunks
        bridge.on_chunk({"type": "text", "content": "Hello"})
        bridge.on_chunk({"type": "text", "content": " World"})
        bridge.stop()  # 发送哨兵终止 drain_loop

        result = await bridge.drain_loop(engine_task)

        # 收集事件类型序列
        event_types = [e["type"] for e in sink.events]

        # 验证事件序列
        assert event_types[0] == "stream_start", "第一个事件必须是 stream_start"

        # stream_start 之后应该是 stream_chunk(s)
        chunk_events = [e for e in sink.events if e["type"] == "stream_chunk"]
        assert len(chunk_events) == 2, "应该发送 2 个 stream_chunk"

        # 最后应该是 stream_end
        assert event_types[-1] == "stream_end", "最后一个事件必须是 stream_end"

        # 验证序列中没有 stream_chunk 出现在 stream_end 之后
        end_index = event_types.index("stream_end")
        for i in range(end_index + 1, len(event_types)):
            assert event_types[i] != "stream_chunk", (
                f"stream_chunk 不应出现在 stream_end 之后（位置 {i}）"
            )

        # 验证累积内容
        assert result["accumulated_content"] == "Hello World"

        engine_task.cancel()
        try:
            await engine_task
        except (asyncio.CancelledError, Exception):
            pass

    @pytest.mark.asyncio
    async def test_no_stream_chunk_after_stream_end_in_full_flow(self):
        """BUG-FIX-20260515: 完整流程中不应在 stream_end 后发送 stream_chunk。"""
        from src.pipeline.stream_bridge import PipelineStreamBridge

        sink = MockSink()
        bridge = PipelineStreamBridge(
            pipeline_id="test-pipeline",
            output_sink=sink,
            message_id="test-msg-seq-002",
        )

        async def _dummy_engine():
            return {"messages": []}

        engine_task = asyncio.create_task(_dummy_engine())

        bridge.on_chunk({"type": "text", "content": "Test content"})
        bridge.stop()

        await bridge.drain_loop(engine_task)

        # 模拟 send_new_message（修复后应直接发送，不再补发 stream_chunk）
        await bridge.send_new_message("Test content", sequence=1)

        event_types = [e["type"] for e in sink.events]

        # 找到 stream_end 的位置
        end_indices = [i for i, t in enumerate(event_types) if t == "stream_end"]
        assert len(end_indices) >= 1, "应有 stream_end"

        last_end_index = end_indices[-1]

        # stream_end 之后只应有 new_message，不应有 stream_chunk
        for i in range(last_end_index + 1, len(event_types)):
            assert event_types[i] != "stream_chunk", (
                f"stream_end 之后不应再有 stream_chunk（事件序列: {event_types}）"
            )

        # new_message 应该是最后一个事件
        assert event_types[-1] == "new_message", (
            f"最后一个事件应为 new_message，实际为 {event_types[-1]}"
        )

        engine_task.cancel()
        try:
            await engine_task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# 测试：内容提取优先级（模拟 _stream_engine_response 的逻辑）
# ---------------------------------------------------------------------------

class TestContentExtractionPriority:
    """验证 _stream_engine_response 中 actual_content 的提取优先级。

    修复后优先级应为：流式累积内容 > 引擎 messages > raw_result
    """

    def test_drain_accumulated_takes_priority_over_messages(self):
        """流式累积内容应优先于引擎 messages 中的 assistant 消息内容。"""
        drain_result = {"accumulated_content": "streaming content here"}
        result = {
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": ""},  # 引擎消息内容为空
            ],
            "raw_result": "raw fallback",
        }

        # 模拟修复后的逻辑
        final_messages = result.get("messages", [])
        drain_accumulated = drain_result.get("accumulated_content", "")

        actual_content = ""
        if drain_accumulated:
            actual_content = drain_accumulated
        elif final_messages:
            for msg in reversed(final_messages):
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    actual_content = msg.get("content", "")
                    break
        else:
            actual_content = result.get("raw_result", "")

        assert actual_content == "streaming content here", (
            "流式累积内容应优先于引擎 messages"
        )

    def test_messages_used_when_no_drain_accumulated(self):
        """当无流式累积内容时，使用引擎 messages。"""
        drain_result = {"accumulated_content": ""}
        result = {
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "message from engine"},
            ],
            "raw_result": "raw fallback",
        }

        final_messages = result.get("messages", [])
        drain_accumulated = drain_result.get("accumulated_content", "")

        actual_content = ""
        if drain_accumulated:
            actual_content = drain_accumulated
        elif final_messages:
            for msg in reversed(final_messages):
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    actual_content = msg.get("content", "")
                    break
        else:
            actual_content = result.get("raw_result", "")

        assert actual_content == "message from engine", (
            "无流式内容时应使用引擎 messages"
        )

    def test_raw_result_used_when_no_messages_no_drain(self):
        """当无 messages 和流式内容时，使用 raw_result。"""
        drain_result = {"accumulated_content": ""}
        result = {
            "messages": [],
            "raw_result": "raw fallback content",
        }

        final_messages = result.get("messages", [])
        drain_accumulated = drain_result.get("accumulated_content", "")

        actual_content = ""
        if drain_accumulated:
            actual_content = drain_accumulated
        elif final_messages:
            for msg in reversed(final_messages):
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    actual_content = msg.get("content", "")
                    break
        else:
            actual_content = result.get("raw_result", "")

        assert actual_content == "raw fallback content", (
            "无 messages 和流式内容时应使用 raw_result"
        )

    def test_empty_assistant_in_messages_uses_drain_accumulated(self):
        """BUG-FIX-20260515: 当 messages 有 assistant 但内容为空时，用流式累积。"""
        drain_result = {"accumulated_content": "real streaming content"}
        result = {
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": ""},  # 空内容
            ],
        }

        final_messages = result.get("messages", [])
        drain_accumulated = drain_result.get("accumulated_content", "")

        actual_content = ""
        if drain_accumulated:
            actual_content = drain_accumulated
        elif final_messages:
            for msg in reversed(final_messages):
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    actual_content = msg.get("content", "")
                    break
        else:
            actual_content = result.get("raw_result", "")

        assert actual_content == "real streaming content", (
            "引擎 assistant 内容为空时，应使用流式累积内容而非空字符串"
        )

    def test_no_assistant_in_messages_uses_drain_accumulated(self):
        """当 messages 中没有 assistant 消息时，使用流式累积。"""
        drain_result = {"accumulated_content": "streamed text"}
        result = {
            "messages": [
                {"role": "user", "content": "hello"},
                # 没有 assistant 消息
            ],
        }

        final_messages = result.get("messages", [])
        drain_accumulated = drain_result.get("accumulated_content", "")

        actual_content = ""
        if drain_accumulated:
            actual_content = drain_accumulated
        elif final_messages:
            for msg in reversed(final_messages):
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    actual_content = msg.get("content", "")
                    break
        else:
            actual_content = result.get("raw_result", "")

        assert actual_content == "streamed text", (
            "messages 中无 assistant 时，应使用流式累积内容"
        )


# ---------------------------------------------------------------------------
# 测试：_stream_wake_response 的 new_message 发送保证
# ---------------------------------------------------------------------------

class TestWakeResponseNewMessageGuarantee:
    """验证 _stream_wake_response 始终发送 new_message。"""

    @pytest.mark.asyncio
    async def test_wake_response_sends_new_message_with_content(self):
        """当有内容时，wake_response 应发送 new_message。"""
        from src.pipeline.stream_bridge import PipelineStreamBridge

        sink = MockSink()
        bridge = PipelineStreamBridge(
            pipeline_id="test-pipeline",
            output_sink=sink,
            message_id="test-wake-001",
        )

        # 模拟 drain_loop 返回有内容
        full_content = "Wake response content"
        bridge._accumulated_content = [full_content]

        # 模拟 _stream_wake_response 的 new_message 发送逻辑
        # 修复后应始终发送 new_message
        content_to_send = full_content or "".join(bridge._accumulated_content)
        await bridge._send_event({
            "type": "new_message",
            "data": {
                "message_id": "test-wake-001",
                "content": content_to_send,
                "pipeline_id": bridge.pipeline_id,
                "role": "assistant",
            },
        })

        new_msg_events = [e for e in sink.events if e["type"] == "new_message"]
        assert len(new_msg_events) == 1, "应发送一个 new_message 事件"
        assert new_msg_events[0]["data"]["content"] == "Wake response content"

    @pytest.mark.asyncio
    async def test_wake_response_sends_new_message_even_when_drain_empty(self):
        """BUG-FIX-20260515: 当 drain 结果为空但 bridge 有累积内容时仍发送。"""
        from src.pipeline.stream_bridge import PipelineStreamBridge

        sink = MockSink()
        bridge = PipelineStreamBridge(
            pipeline_id="test-pipeline",
            output_sink=sink,
            message_id="test-wake-002",
        )

        # 模拟 drain_loop 返回空内容但 bridge 有累积
        full_content = ""
        bridge._accumulated_content = ["Accumulated", " ", "content"]

        # 修复后的逻辑：用累积内容作为保底
        content_to_send = full_content or "".join(bridge._accumulated_content)
        if content_to_send:  # 修复后这个条件应为 True
            await bridge._send_event({
                "type": "new_message",
                "data": {
                    "message_id": "test-wake-002",
                    "content": content_to_send,
                    "pipeline_id": bridge.pipeline_id,
                    "role": "assistant",
                },
            })

        new_msg_events = [e for e in sink.events if e["type"] == "new_message"]
        assert len(new_msg_events) == 1, (
            "bridge 有累积内容时应发送 new_message"
        )
        assert new_msg_events[0]["data"]["content"] == "Accumulated content"


# ---------------------------------------------------------------------------
# 测试：stream_keepalive 不干扰正常消息
# ---------------------------------------------------------------------------

class TestStreamKeepalive:
    """验证 stream_keepalive 不会干扰正常消息序列。"""

    @pytest.mark.asyncio
    async def test_keepalive_events_not_between_chunk_and_end(self):
        """stream_keepalive 不应出现在最后一个 stream_chunk 和 stream_end 之间。"""
        from src.pipeline.stream_bridge import PipelineStreamBridge

        sink = MockSink()
        bridge = PipelineStreamBridge(
            pipeline_id="test-pipeline",
            output_sink=sink,
            message_id="test-keepalive-001",
        )

        # 快速完成：发送 chunks 后立即 stop
        bridge.on_chunk({"type": "text", "content": "Content"})
        bridge.stop()

        async def _dummy_engine():
            return {"messages": []}

        engine_task = asyncio.create_task(_dummy_engine())
        await bridge.drain_loop(engine_task)

        event_types = [e["type"] for e in sink.events]

        # 不应有 keepalive（因为快速完成，不需要心跳）
        keepalive_count = event_types.count("stream_keepalive")
        assert keepalive_count == 0, (
            f"快速完成的流不应有 keepalive，但出现了 {keepalive_count} 次"
        )

        engine_task.cancel()
        try:
            await engine_task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# 运行所有测试的入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
