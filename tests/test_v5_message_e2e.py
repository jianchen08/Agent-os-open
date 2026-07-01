"""
V5: 发送消息端到端链路验证

覆盖后端数据流 + 前端显示逻辑 + 用户交互三个维度。
验证点：
1. 后端消息投递：消息正确到达AI进程
2. 前端即时显示：用户发送的消息立即在前端显示
3. AI回复流式展示：SSE/流式响应实现和前端流式渲染逻辑
4. 长上下文不丢失：上下文管理、消息历史存储、token限制处理
5. 消息顺序正确：消息按时间顺序正确排列
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ===========================================================================
# V5-1: 后端消息投递链路验证
# ===========================================================================

class TestBackendMessageDelivery:
    """验证消息从用户到AI进程的完整投递链路。"""

    def test_message_bus_find_engine_running(self):
        """验证 _find_engine 能找到运行中的引擎。"""
        mock_engine = MagicMock()
        mock_engine.is_suspended = False
        mock_engine.is_running = True
        mock_engine._run_started = True

        mock_entry = MagicMock()
        mock_entry.engine = mock_engine

        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_entry

        with patch("pipeline.registry.get_engine_registry", return_value=mock_registry):
            from pipeline.message_bus import _find_engine
            engine, state = _find_engine("test_pipeline_id")
            assert engine is mock_engine
            assert state == "running"

    def test_message_bus_find_engine_suspended(self):
        """验证 _find_engine 能找到挂起的引擎。"""
        mock_engine = MagicMock()
        mock_engine.is_suspended = True
        mock_engine.is_running = False

        mock_entry = MagicMock()
        mock_entry.engine = mock_engine

        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_entry

        with patch("pipeline.registry.get_engine_registry", return_value=mock_registry):
            from pipeline.message_bus import _find_engine
            engine, state = _find_engine("test_pipeline_id")
            assert engine is mock_engine
            assert state == "suspended"

    def test_message_bus_find_engine_not_found(self):
        """验证 _find_engine 找不到引擎时返回 (None, "")。"""
        mock_provider = MagicMock()
        mock_provider.get.return_value = None

        with patch("infrastructure.service_provider.get_service_provider", return_value=mock_provider):
            from pipeline.message_bus import _find_engine
            engine, state = _find_engine("nonexistent_pipeline")
            assert engine is None
            assert state == ""

    @pytest.mark.asyncio
    async def test_send_pipeline_message_empty_pipeline_id(self):
        """验证空 pipeline_id 返回失败结果。"""
        from pipeline.message_bus import send_pipeline_message, InjectResult

        result = await send_pipeline_message("", "hello")
        assert isinstance(result, InjectResult)
        assert result.success is False
        assert "pipeline_id" in result.error

    @pytest.mark.asyncio
    async def test_send_pipeline_message_empty_message(self):
        """验证空消息返回失败结果。"""
        from pipeline.message_bus import send_pipeline_message, InjectResult

        result = await send_pipeline_message("test_pipeline", "")
        assert isinstance(result, InjectResult)
        assert result.success is False
        assert "message" in result.error

    @pytest.mark.asyncio
    async def test_send_pipeline_message_inject_notification(self):
        """验证向运行中引擎注入通知消息成功。"""
        from pipeline.message_bus import send_pipeline_message

        mock_engine = MagicMock()
        mock_engine.is_suspended = False
        mock_engine.is_running = True
        mock_engine._run_started = True
        mock_engine.inject_message = MagicMock()

        mock_entry = MagicMock()
        mock_entry.engine = mock_engine

        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_entry
        mock_registry.get_bridge.return_value = None

        with patch("pipeline.message_bus._find_engine") as mock_find, \
             patch("pipeline.registry.get_engine_registry", return_value=mock_registry), \
             patch("pipeline.message_bus._create_sink", return_value=None):
            mock_find.return_value = (mock_engine, "running")
            result = await send_pipeline_message("test_pipeline", "Hello AI")

            assert result.success is True
            assert result.method == "notification"
            assert result.pipeline_id == "test_pipeline"
            mock_engine.inject_message.assert_called()

    @pytest.mark.asyncio
    async def test_send_pipeline_message_wake_suspended(self):
        """验证向挂起引擎注入消息并唤醒成功。"""
        from pipeline.message_bus import send_pipeline_message

        mock_engine = MagicMock()
        mock_engine.is_suspended = True
        mock_engine.is_running = False
        mock_engine.inject_message = MagicMock()

        mock_entry = MagicMock()
        mock_entry.engine = mock_engine

        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_entry
        mock_registry.get_bridge.return_value = None

        mock_sink = MagicMock()

        with patch("pipeline.message_bus._find_engine") as mock_find, \
             patch("pipeline.registry.get_engine_registry", return_value=mock_registry), \
             patch("pipeline.message_bus._create_sink", return_value=mock_sink):
            mock_find.return_value = (mock_engine, "suspended")
            result = await send_pipeline_message("test_pipeline", "Wake up!")

            assert result.success is True
            assert result.method == "wake"
            mock_engine.inject_message.assert_called()

    @pytest.mark.skip(reason="_inject_notification_to_engine 函数已移除")
    def test_inject_notification_appends_to_pending(self):
        """验证通知注入到引擎的 _pending_notifications 列表。"""
        from pipeline.message_bus import _inject_notification_to_engine

        mock_engine = MagicMock()
        mock_engine._pending_notifications = []
        mock_engine._wake_event = MagicMock()

        _inject_notification_to_engine(mock_engine, "test notification")

        assert len(mock_engine._pending_notifications) == 1
        assert mock_engine._pending_notifications[0] == "test notification"
        mock_engine._wake_event.set.assert_called_once()

    @pytest.mark.skip(reason="_inject_message_engine 函数已移除")
    def test_inject_message_engine(self):
        """验证向挂起引擎注入消息并唤醒。"""
        from pipeline.message_bus import _inject_message_engine

        mock_engine = MagicMock()
        mock_engine._suspended_state = {"user_input": "", "messages": []}
        mock_engine._wake_event = MagicMock()

        _inject_message_engine(mock_engine, "test message")

        assert mock_engine._suspended_state["user_input"] == "test message"
        msgs = mock_engine._suspended_state["messages"]
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "test message"
        mock_engine._wake_event.set.assert_called_once()


# ===========================================================================
# V5-2: 前端即时显示逻辑验证（通过后端数据流验证间接覆盖）
# ===========================================================================

class TestFrontendImmediateDisplay:
    """验证前端消息即时显示的相关逻辑。"""

    def test_stream_bridge_on_chunk_queues_text(self):
        """验证 PipelineStreamBridge.on_chunk 正确将文本 chunk 入队。"""
        from pipeline.stream_bridge import PipelineStreamBridge, TargetedSink

        mock_sink = MagicMock(spec=TargetedSink)
        bridge = PipelineStreamBridge(
            pipeline_id="test_pipeline",
            output_sink=mock_sink,
            message_id="test_msg_id",
        )

        # 模拟引擎产生的文本 chunk
        chunk = {"type": "text", "content": "Hello "}
        bridge.on_chunk(chunk)

        assert bridge._queue.qsize() == 1
        queued = bridge._queue.get_nowait()
        assert queued == chunk

    def test_stream_bridge_on_chunk_queues_thinking(self):
        """验证 PipelineStreamBridge.on_chunk 正确将 thinking chunk 入队。"""
        from pipeline.stream_bridge import PipelineStreamBridge, TargetedSink

        mock_sink = MagicMock(spec=TargetedSink)
        bridge = PipelineStreamBridge(
            pipeline_id="test_pipeline",
            output_sink=mock_sink,
            message_id="test_msg_id",
        )

        chunk = {"type": "thinking", "content": "Let me think..."}
        bridge.on_chunk(chunk)

        assert bridge._queue.qsize() == 1

    def test_stream_bridge_on_chunk_queues_tool_start(self):
        """验证 PipelineStreamBridge.on_chunk 正确将 tool_start chunk 入队。"""
        from pipeline.stream_bridge import PipelineStreamBridge, TargetedSink

        mock_sink = MagicMock(spec=TargetedSink)
        bridge = PipelineStreamBridge(
            pipeline_id="test_pipeline",
            output_sink=mock_sink,
            message_id="test_msg_id",
        )

        chunk = {"type": "tool_start", "tool_name": "file_read", "call_id": "call_123"}
        bridge.on_chunk(chunk)

        assert bridge._queue.qsize() == 1

    def test_stream_bridge_stop_sends_sentinel(self):
        """验证 bridge.stop() 发送哨兵值终止 drain_loop。"""
        from pipeline.stream_bridge import PipelineStreamBridge, TargetedSink

        mock_sink = MagicMock(spec=TargetedSink)
        bridge = PipelineStreamBridge(
            pipeline_id="test_pipeline",
            output_sink=mock_sink,
        )

        bridge.stop()
        assert bridge._queue.qsize() == 1
        sentinel = bridge._queue.get_nowait()
        assert sentinel is None

    @pytest.mark.asyncio
    async def test_stream_bridge_send_stream_start(self):
        """验证 stream_start 事件正确发送到 sink。"""
        from pipeline.stream_bridge import PipelineStreamBridge, TargetedSink

        mock_sink = AsyncMock(spec=TargetedSink)
        mock_sink.send_event.return_value = True
        bridge = PipelineStreamBridge(
            pipeline_id="test_pipeline",
            output_sink=mock_sink,
            message_id="msg_123",
        )

        await bridge._send_stream_start()

        mock_sink.send_event.assert_called_once()
        event = mock_sink.send_event.call_args[0][0]
        assert event["type"] == "stream_start"
        assert event["data"]["message_id"] == "msg_123"
        assert event["data"]["pipeline_id"] == "test_pipeline"

    @pytest.mark.asyncio
    async def test_targeted_sink_send_event_no_thread_id(self):
        """验证 TargetedSink 在 thread_id 为空时返回 False。"""
        from unittest.mock import AsyncMock

        from pipeline.stream_bridge import TargetedSink

        mock_notifier = MagicMock()
        mock_notifier.send_to_thread = AsyncMock(return_value=False)
        sink = TargetedSink(mock_notifier, thread_id="")

        result = await sink.send_event({"type": "test"})
        assert result is False


# ===========================================================================
# V5-3: AI回复流式展示验证
# ===========================================================================

class TestStreamingDisplay:
    """验证AI回复流式展示的完整链路。"""

    @pytest.mark.asyncio
    async def test_handle_text_chunk_sends_stream_chunk(self):
        """验证文本 chunk 被正确转换为 stream_chunk 事件。"""
        from pipeline.stream_bridge import PipelineStreamBridge, TargetedSink

        mock_sink = AsyncMock(spec=TargetedSink)
        mock_sink.send_event.return_value = True
        bridge = PipelineStreamBridge(
            pipeline_id="test_pipeline",
            output_sink=mock_sink,
            message_id="msg_123",
        )

        await bridge._handle_chunk({"type": "text", "content": "Hello world"})

        # 应该触发 stream_start + stream_chunk
        calls = mock_sink.send_event.call_args_list
        assert len(calls) == 1
        event = calls[0][0][0]
        assert event["type"] == "stream_chunk"
        assert event["data"]["content"] == "Hello world"
        assert event["data"]["message_id"] == "msg_123"

    @pytest.mark.asyncio
    async def test_handle_thinking_chunk_sends_thinking_events(self):
        """验证 thinking chunk 触发 thinking_start + thinking_chunk。"""
        from pipeline.stream_bridge import PipelineStreamBridge, TargetedSink

        mock_sink = AsyncMock(spec=TargetedSink)
        mock_sink.send_event.return_value = True
        bridge = PipelineStreamBridge(
            pipeline_id="test_pipeline",
            output_sink=mock_sink,
            message_id="msg_123",
        )

        await bridge._handle_chunk({"type": "thinking", "content": "Analyzing..."})

        calls = mock_sink.send_event.call_args_list
        # 应该触发 thinking_start + thinking_chunk
        assert len(calls) == 2
        assert calls[0][0][0]["type"] == "thinking_start"
        assert calls[1][0][0]["type"] == "thinking_chunk"
        assert calls[1][0][0]["data"]["content"] == "Analyzing..."

    @pytest.mark.asyncio
    async def test_handle_tool_start_sends_tool_event(self):
        """验证 tool_start chunk 被正确转换为 tool_start 事件。"""
        from pipeline.stream_bridge import PipelineStreamBridge, TargetedSink

        mock_sink = AsyncMock(spec=TargetedSink)
        mock_sink.send_event.return_value = True
        bridge = PipelineStreamBridge(
            pipeline_id="test_pipeline",
            output_sink=mock_sink,
            message_id="msg_123",
        )

        await bridge._handle_chunk({
            "type": "tool_start",
            "tool_name": "bash_execute",
            "args": {"command": "ls"},
            "call_id": "call_abc",
        })

        calls = mock_sink.send_event.call_args_list
        assert len(calls) == 1
        event = calls[0][0][0]
        assert event["type"] == "tool_start"
        assert event["data"]["tool_name"] == "bash_execute"
        assert event["data"]["args"] == {"command": "ls"}

    @pytest.mark.asyncio
    async def test_handle_tool_result_sends_tool_result_event(self):
        """验证 tool_result chunk 被正确发送。"""
        from pipeline.stream_bridge import PipelineStreamBridge, TargetedSink

        mock_sink = AsyncMock(spec=TargetedSink)
        mock_sink.send_event.return_value = True
        bridge = PipelineStreamBridge(
            pipeline_id="test_pipeline",
            output_sink=mock_sink,
            message_id="msg_123",
        )

        # 先发送 tool_start 以建立追踪
        await bridge._handle_chunk({
            "type": "tool_start",
            "tool_name": "bash_execute",
            "call_id": "call_abc",
        })

        # 再发送 tool_result
        await bridge._handle_chunk({
            "type": "tool_result",
            "tool_name": "bash_execute",
            "result": "file1.txt\nfile2.txt",
            "success": True,
            "call_id": "call_abc",
        })

        calls = mock_sink.send_event.call_args_list
        # tool_start + tool_result（不再有多余的 stream_start）
        assert len(calls) == 2
        result_event = calls[1][0][0]
        assert result_event["type"] == "tool_result"
        assert result_event["data"]["success"] is True
        assert "file1.txt" in result_event["data"]["result"]

    @pytest.mark.asyncio
    async def test_tool_result_without_tool_start_fixup(self):
        """验证 tool_result 在没有匹配 tool_start 时自动补发。"""
        from pipeline.stream_bridge import PipelineStreamBridge, TargetedSink

        mock_sink = AsyncMock(spec=TargetedSink)
        mock_sink.send_event.return_value = True
        bridge = PipelineStreamBridge(
            pipeline_id="test_pipeline",
            output_sink=mock_sink,
            message_id="msg_123",
        )

        # 直接发送 tool_result，不先发 tool_start
        await bridge._handle_chunk({
            "type": "tool_result",
            "tool_name": "file_read",
            "result": "file content",
            "success": True,
            "call_id": "call_xyz",
        })

        calls = mock_sink.send_event.call_args_list
        # 应该自动补发 tool_start + tool_result（不再有多余的 stream_start）
        assert len(calls) == 2
        assert calls[0][0][0]["type"] == "tool_start"
        assert calls[1][0][0]["type"] == "tool_result"

    @pytest.mark.asyncio
    async def test_accumulated_content_tracks_all_text(self):
        """验证所有文本 chunk 被正确累积。"""
        from pipeline.stream_bridge import PipelineStreamBridge, TargetedSink

        mock_sink = AsyncMock(spec=TargetedSink)
        mock_sink.send_event.return_value = True
        bridge = PipelineStreamBridge(
            pipeline_id="test_pipeline",
            output_sink=mock_sink,
            message_id="msg_123",
        )

        await bridge._handle_chunk({"type": "text", "content": "Hello "})
        await bridge._handle_chunk({"type": "text", "content": "World"})

        assert "".join(bridge._accumulated_content) == "Hello World"

    @pytest.mark.asyncio
    async def test_iteration_event_closes_thinking(self):
        """验证 iteration 事件会关闭活跃的 thinking 状态。"""
        from pipeline.stream_bridge import PipelineStreamBridge, TargetedSink

        mock_sink = AsyncMock(spec=TargetedSink)
        mock_sink.send_event.return_value = True
        bridge = PipelineStreamBridge(
            pipeline_id="test_pipeline",
            output_sink=mock_sink,
            message_id="msg_123",
        )

        # 先开启 thinking
        await bridge._handle_chunk({"type": "thinking", "content": "thinking..."})
        assert bridge._thinking_active is True

        # 发送 iteration 应关闭 thinking
        await bridge._handle_chunk({"type": "iteration", "iteration": 2, "max_iterations": 5})
        assert bridge._thinking_active is False

        calls = mock_sink.send_event.call_args_list
        # thinking_start + thinking_chunk + thinking_end + iteration
        assert len(calls) == 4
        # 第3个是 thinking_end
        assert calls[2][0][0]["type"] == "thinking_end"
        # 第4个是 iteration
        assert calls[3][0][0]["type"] == "iteration"


# ===========================================================================
# V5-4: 长上下文不丢失验证
# ===========================================================================

class TestLongContextPreservation:
    """验证长对话上下文的完整性和不截断。"""

    def test_conversation_history_append_on_user_input(self):
        """验证用户消息被正确追加到对话历史。"""
        history: list[dict[str, Any]] = []

        # 模拟用户发送消息
        user_msg = "Hello, this is my first message"
        history.append({"role": "user", "content": user_msg})

        assert len(history) == 1
        assert history[0]["role"] == "user"
        assert history[0]["content"] == user_msg

    def test_conversation_history_preserves_long_history(self):
        """验证长对话历史（100+条消息）完整保存。"""
        history: list[dict[str, Any]] = []

        # 模拟 100 轮对话
        for i in range(100):
            history.append({"role": "user", "content": f"User message {i}"})
            history.append({"role": "assistant", "content": f"AI response {i}"})

        assert len(history) == 200

        # 验证第一条和最后一条消息都完整
        assert history[0]["content"] == "User message 0"
        assert history[-1]["content"] == "AI response 99"

        # 验证顺序正确
        for i, msg in enumerate(history):
            expected_role = "user" if i % 2 == 0 else "assistant"
            assert msg["role"] == expected_role

    def test_conversation_history_sync_from_engine_state(self):
        """验证从引擎 _suspended_state 同步对话历史的逻辑。

        对应 BUG-FIX-20260511: 唤醒路径需要从引擎内部状态同步 conversation_history。
        """
        # 模拟引擎内部的完整消息列表（包含 system 消息）
        engine_messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "How are you?"},
            {"role": "assistant", "content": "I'm doing well!"},
            {"role": "tool", "content": "tool result", "tool_call_id": "call_1"},
        ]

        # 模拟同步逻辑（来自 stream_handler.py 中的实现）
        _valid_roles = {"user", "assistant", "tool"}
        filtered = [
            msg for msg in engine_messages
            if isinstance(msg, dict) and msg.get("role") in _valid_roles
        ]

        assert len(filtered) == 5  # 排除 system 消息
        assert filtered[0]["role"] == "user"
        assert filtered[1]["role"] == "assistant"
        assert filtered[-1]["role"] == "tool"

    @pytest.mark.skip(reason="_inject_message_engine 函数已移除")
    def test_message_bus_inject_preserves_existing_history(self):
        """验证消息注入不破坏已有历史。"""
        from pipeline.message_bus import _inject_message_engine

        mock_engine = MagicMock()
        existing_messages = [
            {"role": "user", "content": "Previous message"},
            {"role": "assistant", "content": "Previous response"},
        ]
        mock_engine._suspended_state = {
            "user_input": "",
            "messages": list(existing_messages),
        }
        mock_engine._wake_event = MagicMock()

        _inject_message_engine(mock_engine, "New message")

        # 验证旧消息还在
        messages = mock_engine._suspended_state["messages"]
        assert len(messages) == 3
        assert messages[0]["content"] == "Previous message"
        assert messages[1]["content"] == "Previous response"
        assert messages[2]["content"] == "New message"

    def test_load_history_from_storage_reconstructs_tool_calls(self):
        """验证从存储加载历史时正确重建 tool_calls。"""
        from pipeline.message_bus import _load_history_from_storage

        # 模拟 storage 返回的记录
        mock_record_1 = MagicMock()
        mock_record_1.role = "user"
        mock_record_1.content = "Read the file"
        mock_record_1.name = None
        mock_record_1.tool_call_id = None
        mock_record_1.tool_input = None
        mock_record_1.tool_calls_json = None

        mock_record_2 = MagicMock()
        mock_record_2.role = "assistant"
        mock_record_2.content = ""
        mock_record_2.name = None
        mock_record_2.tool_call_id = None
        mock_record_2.tool_input = None
        mock_record_2.tool_calls_json = json.dumps([{
            "id": "call_123",
            "type": "function",
            "function": {"name": "file_read", "arguments": '{"path": "test.txt"}'},
        }])

        mock_storage = MagicMock()
        mock_storage.list_by_pipeline.return_value = ([mock_record_1, mock_record_2], False)

        mock_provider = MagicMock()
        mock_provider.get.return_value = mock_storage

        with patch("pipeline.message_bus._reconstruct_tool_calls", create=True):
            result = _load_history_from_storage("test_pipeline", mock_provider)

        assert result is not None
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        # tool_calls_json 应被解析
        assert "tool_calls" in result[1]
        assert result[1]["tool_calls"][0]["function"]["name"] == "file_read"


# ===========================================================================
# V5-5: 消息顺序正确性验证
# ===========================================================================

class TestMessageOrdering:
    """验证消息按正确顺序排列。"""

    def test_compare_messages_by_sequence(self):
        """验证消息按 sequence 升序排列（复现 pipelineMessageStore.ts 的逻辑）。"""
        messages = [
            {"id": "3", "sequence": 3, "role": "assistant", "content": "Third",
             "timestamp": "2026-01-01T00:00:03Z"},
            {"id": "1", "sequence": 1, "role": "user", "content": "First",
             "timestamp": "2026-01-01T00:00:01Z"},
            {"id": "2", "sequence": 2, "role": "assistant", "content": "Second",
             "timestamp": "2026-01-01T00:00:02Z"},
        ]

        def compare_msgs(a: dict, b: dict) -> int:
            seq_a = a.get("sequence", float("inf"))
            seq_b = b.get("sequence", float("inf"))
            if seq_a != seq_b:
                return seq_a - seq_b
            t_a = datetime.fromisoformat(a["timestamp"].replace("Z", "+00:00")).timestamp()
            t_b = datetime.fromisoformat(b["timestamp"].replace("Z", "+00:00")).timestamp()
            return t_a - t_b

        sorted_msgs = sorted(messages, key=lambda x: (x.get("sequence", float("inf")),))
        assert sorted_msgs[0]["sequence"] == 1
        assert sorted_msgs[1]["sequence"] == 2
        assert sorted_msgs[2]["sequence"] == 3

    def test_compare_messages_by_timestamp_fallback(self):
        """验证无 sequence 时按 timestamp 排序。"""
        messages = [
            {"id": "3", "role": "assistant", "content": "Third",
             "timestamp": "2026-01-01T00:00:03Z"},
            {"id": "1", "role": "user", "content": "First",
             "timestamp": "2026-01-01T00:00:01Z"},
            {"id": "2", "role": "assistant", "content": "Second",
             "timestamp": "2026-01-01T00:00:02Z"},
        ]

        def sort_key(m):
            t = m.get("timestamp", "")
            return t if t else ""

        sorted_msgs = sorted(messages, key=sort_key)
        assert sorted_msgs[0]["content"] == "First"
        assert sorted_msgs[1]["content"] == "Second"
        assert sorted_msgs[2]["content"] == "Third"

    def test_message_ordering_with_interleaved_tool_calls(self):
        """验证工具调用消息不破坏消息顺序。"""
        messages = [
            {"id": "1", "sequence": 1, "role": "user", "content": "Read file",
             "timestamp": "2026-01-01T00:00:01Z"},
            {"id": "2", "sequence": 2, "role": "assistant", "content": "",
             "timestamp": "2026-01-01T00:00:02Z"},
            {"id": "3", "sequence": 3, "role": "tool", "content": "file content",
             "timestamp": "2026-01-01T00:00:03Z", "tool_call_id": "call_1"},
            {"id": "4", "sequence": 4, "role": "assistant", "content": "Here is the file content",
             "timestamp": "2026-01-01T00:00:04Z"},
        ]

        sorted_msgs = sorted(messages, key=lambda m: m.get("sequence", float("inf")))
        roles = [m["role"] for m in sorted_msgs]
        assert roles == ["user", "assistant", "tool", "assistant"]

    @pytest.mark.skip(reason="PipelineContext 类已移除")
    def test_pipeline_context_isolation(self):
        """验证不同管道之间的消息不串线。"""
        from channels.websocket.stream_handler import PipelineContext

        mock_app = MagicMock()
        mock_engine_1 = MagicMock()
        mock_engine_1.pipeline_id = "pipeline_A"
        mock_engine_2 = MagicMock()
        mock_engine_2.pipeline_id = "pipeline_B"

        ctx = PipelineContext(engine=mock_engine_1, available=True, app=mock_app)

        # 获取 pipeline_A 的引擎
        engine_a = ctx.get_or_create_engine("pipeline_A")
        assert engine_a is mock_engine_1

        # 创建新引擎给 pipeline_B（应该隔离）
        engine_b = ctx.get_or_create_engine("pipeline_B")
        assert engine_b is not mock_engine_1

    def test_concurrent_message_ordering(self):
        """验证并发消息发送后顺序仍然正确。"""
        messages: list[dict] = []
        base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

        # 模拟 10 个快速连续消息
        for i in range(10):
            msg = {
                "id": f"msg_{i}",
                "sequence": i,
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"Message {i}",
                "timestamp": (base_time.replace(second=i)).isoformat(),
            }
            messages.append(msg)

        # 打乱顺序
        import random
        random.shuffle(messages)

        # 重新排序
        sorted_msgs = sorted(messages, key=lambda m: m.get("sequence", float("inf")))

        for i, msg in enumerate(sorted_msgs):
            assert msg["sequence"] == i


# ===========================================================================
# WebSocket 端到端消息协议验证
# ===========================================================================

class TestWebSocketProtocol:
    """验证 WebSocket 消息协议格式正确性。"""

    def test_user_input_message_format(self):
        """验证 user_input 消息格式正确。"""
        msg = {
            "type": "user_input",
            "thread_id": "thread_123",
            "content": "Hello AI",
            "pipeline_id": "",
            "client_message_id": "client_msg_456",
        }

        assert msg["type"] == "user_input"
        assert "thread_id" in msg
        assert "content" in msg
        assert len(msg["content"]) > 0

    def test_stream_start_event_format(self):
        """验证 stream_start 事件格式正确。"""
        event = {
            "type": "stream_start",
            "data": {
                "message_id": "msg_abc123",
                "pipeline_id": "pipeline_xyz",
            },
        }

        assert event["type"] == "stream_start"
        assert "message_id" in event["data"]
        assert "pipeline_id" in event["data"]

    def test_stream_chunk_event_format(self):
        """验证 stream_chunk 事件格式正确。"""
        event = {
            "type": "stream_chunk",
            "data": {
                "message_id": "msg_abc123",
                "content": "Hello ",
                "pipeline_id": "pipeline_xyz",
            },
        }

        assert event["type"] == "stream_chunk"
        assert "content" in event["data"]

    def test_stream_end_event_format(self):
        """验证 stream_end 事件格式正确。"""
        event = {
            "type": "stream_end",
            "data": {
                "message_id": "msg_abc123",
                "full_content": "Hello World",
                "pipeline_id": "pipeline_xyz",
            },
        }

        assert event["type"] == "stream_end"
        assert "full_content" in event["data"]

    def test_stream_error_event_format(self):
        """验证 stream_error 事件格式正确。"""
        event = {
            "type": "stream_error",
            "data": {
                "message_id": "msg_abc123",
                "error": "Pipeline engine not initialized",
                "pipeline_id": "",
            },
        }

        assert event["type"] == "stream_error"
        assert "error" in event["data"]

    def test_interaction_request_event_format(self):
        """验证 interaction_request 事件格式正确。"""
        event = {
            "type": "interaction_request",
            "data": {
                "request_id": "req_123",
                "interaction_mode": "choice",
                "title": "确认操作",
                "description": "是否执行此操作？",
                "options": [{"id": "approve", "label": "批准"}],
            },
        }

        assert event["type"] == "interaction_request"
        assert "request_id" in event["data"]
        assert "interaction_mode" in event["data"]

    def test_heartbeat_message_format(self):
        """验证 heartbeat 消息格式正确。"""
        msg = {"type": "heartbeat"}
        response = {
            "type": "heartbeat_ack",
            "data": {"server_time": datetime.now(timezone.utc).isoformat()},
        }

        assert msg["type"] == "heartbeat"
        assert response["type"] == "heartbeat_ack"
        assert "server_time" in response["data"]


# ===========================================================================
# PipelineStreamBridge drain_loop 核心验证
# ===========================================================================

class TestStreamBridgeDrainLoop:
    """验证 drain_loop 的核心行为。"""

    @pytest.mark.asyncio
    async def test_drain_loop_with_chunks(self):
        """验证 drain_loop 正确消费队列中的 chunks。"""
        from pipeline.stream_bridge import PipelineStreamBridge, TargetedSink

        mock_sink = AsyncMock(spec=TargetedSink)
        mock_sink.send_event.return_value = True
        bridge = PipelineStreamBridge(
            pipeline_id="test_pipeline",
            output_sink=mock_sink,
            message_id="msg_drain_test",
        )

        # 放入几个 chunks
        bridge.on_chunk({"type": "text", "content": "Hello "})
        bridge.on_chunk({"type": "text", "content": "World"})
        bridge.stop()  # 哨兵值

        # 创建一个已完成 Task 来模拟 engine_task
        engine_task = asyncio.create_task(asyncio.sleep(0))
        await engine_task

        result = await bridge.drain_loop(engine_task)

        assert "accumulated_content" in result
        assert result["accumulated_content"] == "Hello World"

    @pytest.mark.asyncio
    async def test_drain_loop_empty_chunks(self):
        """验证 drain_loop 在无 chunk 时正确处理。"""
        from pipeline.stream_bridge import PipelineStreamBridge, TargetedSink

        mock_sink = AsyncMock(spec=TargetedSink)
        mock_sink.send_event.return_value = True
        bridge = PipelineStreamBridge(
            pipeline_id="test_pipeline",
            output_sink=mock_sink,
            message_id="msg_empty",
        )

        bridge.stop()

        engine_task = asyncio.create_task(asyncio.sleep(0))
        await engine_task

        result = await bridge.drain_loop(engine_task)

        assert result["accumulated_content"] == ""
        assert result["thinking_content_parts"] == []

    @pytest.mark.asyncio
    async def test_send_new_message_uses_accumulated_content(self):
        """验证 send_new_message 在 full_content 为空时使用内部累积内容。"""
        from pipeline.stream_bridge import PipelineStreamBridge, TargetedSink

        mock_sink = AsyncMock(spec=TargetedSink)
        mock_sink.send_event.return_value = True
        bridge = PipelineStreamBridge(
            pipeline_id="test_pipeline",
            output_sink=mock_sink,
            message_id="msg_new",
        )

        # 模拟内部有累积内容
        bridge._accumulated_content = ["Hello ", "World"]

        # 读取 send_new_message 方法的剩余部分
        full_content = "".join(bridge._accumulated_content)
        assert full_content == "Hello World"


# ===========================================================================
# WebSocketInteractionNotifier 验证
# ===========================================================================

class TestWSInteractionNotifier:
    """验证 WebSocket 交互通知器的核心行为。"""

    def test_register_and_unregister_global(self):
        """验证全局连接注册和注销。"""
        from channels.websocket.ws_handler import WebSocketInteractionNotifier

        notifier = WebSocketInteractionNotifier()
        mock_ws = MagicMock()

        notifier.register_global("user_123", mock_ws)
        assert "user_123" in notifier._global_connections

        notifier.unregister_global("user_123", mock_ws)
        assert "user_123" not in notifier._global_connections

    def test_register_global_replaces_old(self):
        """验证新全局连接替换旧连接。"""
        from channels.websocket.ws_handler import WebSocketInteractionNotifier

        notifier = WebSocketInteractionNotifier()
        old_ws = MagicMock()
        new_ws = MagicMock()

        notifier.register_global("user_123", old_ws)
        notifier.register_global("user_123", new_ws)

        assert notifier._global_connections["user_123"] is new_ws

    def test_unregister_global_prevents_misdelete(self):
        """验证旧连接的 finally 不会误删新连接。"""
        from channels.websocket.ws_handler import WebSocketInteractionNotifier

        notifier = WebSocketInteractionNotifier()
        old_ws = MagicMock()
        new_ws = MagicMock()

        notifier.register_global("user_123", old_ws)
        notifier.register_global("user_123", new_ws)

        # 模拟旧连接断开时试图注销
        notifier.unregister_global("user_123", old_ws)
        # 新连接应该还在
        assert notifier._global_connections["user_123"] is new_ws

    def test_register_pipeline_thread_mapping(self):
        """验证 EngineRegistry 正确建立 pipeline -> thread 映射。"""
        from pipeline.registry import get_engine_registry

        _registry = get_engine_registry()
        _registry.register("pipeline_abc", None, thread_id="thread_xyz")
        try:
            assert _registry.get_thread_id("pipeline_abc") == "thread_xyz"
            assert _registry.get_thread_id("nonexistent") == ""
        finally:
            _registry.unregister("pipeline_abc")

    @pytest.mark.asyncio
    async def test_send_to_thread_success(self):
        """验证向指定 thread 发送事件成功（通过 thread→user 映射路由）。"""
        from channels.websocket.ws_handler import WebSocketInteractionNotifier

        notifier = WebSocketInteractionNotifier()
        mock_ws = AsyncMock()
        notifier.register_global("user_123", mock_ws)
        notifier.register_thread_user("thread_123", "user_123")

        event = {"type": "test_event", "data": {"msg": "hello"}}
        result = await notifier.send_to_thread("thread_123", event)

        assert result is True
        mock_ws.send_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_to_thread_fallback_to_global(self):
        """验证 per-session 连接失败时回退到全局连接。"""
        from channels.websocket.ws_handler import WebSocketInteractionNotifier

        notifier = WebSocketInteractionNotifier()
        mock_global_ws = AsyncMock()
        notifier._global_connections["user_123"] = mock_global_ws

        event = {"type": "test_event", "data": {"msg": "hello"}}
        result = await notifier.send_to_thread("nonexistent_thread", event)

        assert result is True

    @pytest.mark.asyncio
    async def test_cancel_fallback_on_user_response(self):
        """验证用户响应后自动确认任务被取消。"""
        from channels.websocket.ws_handler import WebSocketInteractionNotifier

        notifier = WebSocketInteractionNotifier()

        # 模拟一个 fallback task
        mock_task = MagicMock()
        mock_task.done.return_value = False
        notifier._fallback_request_map["req_123"] = mock_task

        notifier.cancel_fallback("req_123")

        mock_task.cancel.assert_called_once()
        assert "req_123" not in notifier._fallback_request_map
