"""WebSocket 通道单元测试。

测试覆盖：
- protocol.py: 事件类型、信封序列化/反序列化、数据类
- session_manager.py: 会话注册/注销/查找/广播/清理
- server.py: 服务器启动/停止、WebSocket 连接处理
- adapter.py: 输入/输出适配器、流式推送、执行控制
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from channels.websocket.adapter import (
    WebSocketAdapter,
    WebSocketInputAdapter,
    WebSocketOutputAdapter,
)
from channels.websocket.protocol import (
    ConnectionConfirmationData,
    ControlCommand,
    ErrorData,
    EventEnvelope,
    EventType,
    ExecutionDoneData,
    ExecutionProgressData,
    ExecutionStartData,
    PipelineEndData,
    PipelineStartData,
    StreamChunkData,
    StreamEndData,
    StreamStartData,
    create_event,
)
from channels.websocket.server import WebSocketServer
from channels.websocket.session_manager import (
    SessionInfo,
    SessionManager,
)


# ============================================================
# Protocol 测试
# ============================================================


class TestEventType:
    """EventType 枚举测试。"""

    def test_all_event_types_defined(self) -> None:
        """验证所有协议定义的事件类型都已定义。"""
        expected = [
            "connection_confirmation",
            "stream_start", "stream_chunk", "stream_end",
            "thinking_start", "thinking_chunk", "thinking_end",
            "execution_start", "execution_progress", "execution_done",
            "pipeline_start", "pipeline_end",
            "iteration_start", "iteration_end",
            "plugin_error", "pipeline_error",
            "user_input", "stop_generation", "resume_action",
        ]
        for name in expected:
            assert any(e.value == name for e in EventType), f"Missing EventType: {name}"

    def test_event_type_is_string(self) -> None:
        """EventType 值应为字符串。"""
        assert EventType.STREAM_CHUNK.value == "stream_chunk"
        assert isinstance(EventType.STREAM_CHUNK.value, str)


class TestControlCommand:
    """ControlCommand 枚举测试。"""

    def test_control_commands(self) -> None:
        """验证控制命令类型。"""
        assert ControlCommand.STOP_GENERATION.value == "stop_generation"
        assert ControlCommand.RESUME_ACTION.value == "resume_action"


class TestEventEnvelope:
    """EventEnvelope 信封测试。"""

    def test_create_envelope(self) -> None:
        """创建基本信封。"""
        envelope = EventEnvelope(type="stream_chunk", data={"content": "hello"})
        assert envelope.type == "stream_chunk"
        assert envelope.data == {"content": "hello"}
        assert envelope.timestamp  # 自动生成
        assert envelope.request_id  # 自动生成

    def test_envelope_to_dict(self) -> None:
        """信封序列化为字典。"""
        envelope = EventEnvelope(
            type="stream_start",
            data={"message_id": "123"},
            timestamp="2025-01-01T00:00:00.000Z",
            request_id="req-1",
        )
        result = envelope.to_dict()
        assert result == {
            "type": "stream_start",
            "data": {"message_id": "123"},
            "timestamp": "2025-01-01T00:00:00.000Z",
            "request_id": "req-1",
        }

    def test_envelope_from_dict(self) -> None:
        """从字典反序列化信封。"""
        data = {
            "type": "stream_chunk",
            "data": {"content": "hi"},
            "timestamp": "2025-01-01T00:00:00.000Z",
            "request_id": "req-2",
        }
        envelope = EventEnvelope.from_dict(data)
        assert envelope.type == "stream_chunk"
        assert envelope.data == {"content": "hi"}
        assert envelope.timestamp == "2025-01-01T00:00:00.000Z"
        assert envelope.request_id == "req-2"

    def test_envelope_from_dict_missing_type(self) -> None:
        """缺少 type 字段应抛出 ValueError。"""
        with pytest.raises(ValueError, match="type"):
            EventEnvelope.from_dict({"data": {}})

    def test_envelope_from_dict_defaults(self) -> None:
        """缺少可选字段时应使用默认值。"""
        envelope = EventEnvelope.from_dict({"type": "test"})
        assert envelope.data == {}
        assert envelope.timestamp  # 自动生成
        assert envelope.request_id  # 自动生成

    def test_roundtrip(self) -> None:
        """信封序列化/反序列化往返一致。"""
        original = EventEnvelope(
            type="pipeline_end",
            data={"status": "completed"},
            timestamp="2025-01-01T00:00:00.000Z",
            request_id="req-3",
        )
        serialized = original.to_dict()
        restored = EventEnvelope.from_dict(serialized)
        assert restored.type == original.type
        assert restored.data == original.data
        assert restored.timestamp == original.timestamp
        assert restored.request_id == original.request_id


class TestDataClasses:
    """数据类序列化测试。"""

    def test_stream_start_data(self) -> None:
        """StreamStartData 序列化。"""
        data = StreamStartData(message_id="m1", model="MiniMax-M2.7", thinking_enabled=True)
        result = data.to_dict()
        assert result == {
            "message_id": "m1",
            "model": "MiniMax-M2.7",
            "thinking_enabled": True,
        }

    def test_stream_chunk_data(self) -> None:
        """StreamChunkData 序列化。"""
        data = StreamChunkData(message_id="m1", content="hello", sequence=5)
        result = data.to_dict()
        assert result == {"message_id": "m1", "content": "hello", "sequence": 5}

    def test_stream_end_data(self) -> None:
        """StreamEndData 序列化。"""
        data = StreamEndData(message_id="m1", full_content="hello world", usage={"prompt_tokens": 10})
        result = data.to_dict()
        assert result["message_id"] == "m1"
        assert result["full_content"] == "hello world"
        assert result["usage"] == {"prompt_tokens": 10}

    def test_execution_start_data(self) -> None:
        """ExecutionStartData 序列化（无 parent_id）。"""
        data = ExecutionStartData(execution_id="e1", tool_name="search", params={"q": "test"})
        result = data.to_dict()
        assert "parent_id" not in result
        assert result["tool_name"] == "search"

    def test_execution_start_data_with_parent(self) -> None:
        """ExecutionStartData 序列化（有 parent_id）。"""
        data = ExecutionStartData(execution_id="e1", tool_name="search", params={}, parent_id="e0")
        result = data.to_dict()
        assert result["parent_id"] == "e0"

    def test_execution_progress_data(self) -> None:
        """ExecutionProgressData 序列化。"""
        data = ExecutionProgressData(execution_id="e1", progress=50.0, message="Half done")
        result = data.to_dict()
        assert result["progress"] == 50.0
        assert result["message"] == "Half done"

    def test_execution_progress_data_minimal(self) -> None:
        """ExecutionProgressData 最小序列化（无可选字段）。"""
        data = ExecutionProgressData(execution_id="e1", progress=25.0)
        result = data.to_dict()
        assert "message" not in result
        assert "partial_output" not in result

    def test_execution_done_data(self) -> None:
        """ExecutionDoneData 序列化。"""
        data = ExecutionDoneData(execution_id="e1", status="success", result="found", duration=1.5)
        result = data.to_dict()
        assert result == {"execution_id": "e1", "status": "success", "result": "found", "duration": 1.5}

    def test_pipeline_start_data(self) -> None:
        """PipelineStartData 序列化。"""
        data = PipelineStartData(session_id="s1", agent_level="l1_main", config={"model": "test"})
        result = data.to_dict()
        assert result["session_id"] == "s1"
        assert result["agent_level"] == "l1_main"

    def test_pipeline_end_data(self) -> None:
        """PipelineEndData 序列化。"""
        data = PipelineEndData(session_id="s1", status="completed", total_iterations=5, total_duration=3.2)
        result = data.to_dict()
        assert result["status"] == "completed"
        assert result["total_iterations"] == 5

    def test_error_data(self) -> None:
        """ErrorData 序列化（含可选字段）。"""
        data = ErrorData(error="timeout", phase="core", plugin="LLMCore", policy="retry")
        result = data.to_dict()
        assert result["error"] == "timeout"
        assert result["plugin"] == "LLMCore"
        assert result["policy"] == "retry"

    def test_error_data_minimal(self) -> None:
        """ErrorData 最小序列化。"""
        data = ErrorData(error="fail", phase="output")
        result = data.to_dict()
        assert "plugin" not in result
        assert "policy" not in result
        assert "fallback" not in result

    def test_connection_confirmation_data(self) -> None:
        """ConnectionConfirmationData 序列化。"""
        data = ConnectionConfirmationData(session_id="s1", thread_id="t1")
        result = data.to_dict()
        assert result["session_id"] == "s1"
        assert result["thread_id"] == "t1"
        assert result["status"] == "connected"


class TestCreateEvent:
    """create_event 工厂函数测试。"""

    def test_create_event_basic(self) -> None:
        """创建基本事件。"""
        event = create_event(EventType.STREAM_CHUNK, {"content": "hi"})
        assert event.type == "stream_chunk"
        assert event.data == {"content": "hi"}

    def test_create_event_with_request_id(self) -> None:
        """创建带指定 request_id 的事件。"""
        event = create_event(EventType.PIPELINE_START, {}, request_id="custom-id")
        assert event.request_id == "custom-id"

    def test_create_event_no_data(self) -> None:
        """创建无数据事件。"""
        event = create_event(EventType.PIPELINE_END)
        assert event.data == {}


# ============================================================
# SessionManager 测试
# ============================================================


class MockWebSocket:
    """模拟 WebSocket 连接。"""

    def __init__(self) -> None:
        self.closed = False
        self.sent_messages: list[str] = []
        self._send_error: Exception | None = None

    async def send_str(self, data: str) -> None:
        """记录发送的消息。"""
        if self._send_error:
            raise self._send_error
        self.sent_messages.append(data)


class TestSessionManager:
    """SessionManager 会话管理器测试。"""

    @pytest.fixture
    def manager(self) -> SessionManager:
        """创建会话管理器实例。"""
        return SessionManager(session_timeout=60.0)

    @pytest.mark.asyncio
    async def test_register_session(self, manager: SessionManager) -> None:
        """注册新会话。"""
        ws = MockWebSocket()
        session_id = await manager.register(ws, thread_id="thread-1")

        assert session_id
        assert manager.active_count == 1
        session = manager.get_session(session_id)
        assert session is not None
        assert session.ws is ws
        assert session.thread_id == "thread-1"

    @pytest.mark.asyncio
    async def test_register_without_thread_id(self, manager: SessionManager) -> None:
        """注册不带 thread_id 的会话。"""
        ws = MockWebSocket()
        session_id = await manager.register(ws)

        session = manager.get_session(session_id)
        assert session is not None
        assert session.thread_id == ""

    @pytest.mark.asyncio
    async def test_unregister_session(self, manager: SessionManager) -> None:
        """注销会话。"""
        ws = MockWebSocket()
        session_id = await manager.register(ws, thread_id="t1")

        await manager.unregister(session_id)
        assert manager.active_count == 0
        assert manager.get_session(session_id) is None

    @pytest.mark.asyncio
    async def test_unregister_nonexistent(self, manager: SessionManager) -> None:
        """注销不存在的会话不报错。"""
        await manager.unregister("nonexistent")  # 不应抛异常

    @pytest.mark.asyncio
    async def test_reconnect_replaces_old_session(self, manager: SessionManager) -> None:
        """重连时注销旧会话。"""
        ws1 = MockWebSocket()
        ws2 = MockWebSocket()
        old_id = await manager.register(ws1, thread_id="thread-1")
        new_id = await manager.register(ws2, thread_id="thread-1")

        # 旧会话被注销
        assert manager.get_session(old_id) is None
        # 新会话存在
        assert manager.get_session(new_id) is not None
        # thread_id 映射到新会话
        assert manager.get_session_by_thread("thread-1") is not None
        assert manager.get_session_by_thread("thread-1").session_id == new_id

    @pytest.mark.asyncio
    async def test_send_to_session(self, manager: SessionManager) -> None:
        """向指定会话发送消息。"""
        ws = MockWebSocket()
        session_id = await manager.register(ws)

        result = await manager.send_to(session_id, "hello")
        assert result is True
        assert ws.sent_messages == ["hello"]

    @pytest.mark.asyncio
    async def test_send_to_unknown_session(self, manager: SessionManager) -> None:
        """向不存在的会话发送消息。"""
        result = await manager.send_to("nonexistent", "hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_to_closed_ws(self, manager: SessionManager) -> None:
        """向已关闭的 WebSocket 发送消息。"""
        ws = MockWebSocket()
        ws.closed = True
        session_id = await manager.register(ws)

        result = await manager.send_to(session_id, "hello")
        assert result is False
        # 应自动注销
        assert manager.get_session(session_id) is None

    @pytest.mark.asyncio
    async def test_send_to_failing_ws(self, manager: SessionManager) -> None:
        """向发送失败的 WebSocket 发送消息。"""
        ws = MockWebSocket()
        ws._send_error = RuntimeError("connection lost")
        session_id = await manager.register(ws)

        result = await manager.send_to(session_id, "hello")
        assert result is False
        # 应自动注销
        assert manager.get_session(session_id) is None

    @pytest.mark.asyncio
    async def test_broadcast(self, manager: SessionManager) -> None:
        """广播消息到所有会话。"""
        ws1 = MockWebSocket()
        ws2 = MockWebSocket()
        await manager.register(ws1)
        await manager.register(ws2)

        count = await manager.broadcast("announcement")
        assert count == 2
        assert ws1.sent_messages == ["announcement"]
        assert ws2.sent_messages == ["announcement"]

    @pytest.mark.asyncio
    async def test_broadcast_partial_failure(self, manager: SessionManager) -> None:
        """广播时部分连接失败。"""
        ws1 = MockWebSocket()
        ws2 = MockWebSocket()
        ws2.closed = True
        await manager.register(ws1)
        sid2 = await manager.register(ws2)

        count = await manager.broadcast("msg")
        assert count == 1
        assert ws1.sent_messages == ["msg"]

    @pytest.mark.asyncio
    async def test_get_session_by_thread(self, manager: SessionManager) -> None:
        """通过 thread_id 查找会话。"""
        ws = MockWebSocket()
        session_id = await manager.register(ws, thread_id="thread-42")

        found = manager.get_session_by_thread("thread-42")
        assert found is not None
        assert found.session_id == session_id

    @pytest.mark.asyncio
    async def test_get_session_by_thread_not_found(self, manager: SessionManager) -> None:
        """查找不存在的 thread_id。"""
        assert manager.get_session_by_thread("nonexistent") is None

    @pytest.mark.asyncio
    async def test_session_touch(self) -> None:
        """SessionInfo.touch() 更新活跃时间。"""
        info = SessionInfo(session_id="s1", ws=MockWebSocket(), connected_at=0, last_active_at=0)
        old_time = info.last_active_at
        info.touch()
        assert info.last_active_at > old_time

    @pytest.mark.asyncio
    async def test_cleanup_stale(self, manager: SessionManager) -> None:
        """清理超时会话。"""
        ws = MockWebSocket()
        session_id = await manager.register(ws)

        # 手动设置最后活跃时间为很久以前
        session = manager.get_session(session_id)
        assert session is not None
        session.last_active_at = time.time() - 120  # 2分钟前

        cleaned = await manager.cleanup_stale()
        assert cleaned == 1
        assert manager.active_count == 0

    @pytest.mark.asyncio
    async def test_cleanup_no_stale(self, manager: SessionManager) -> None:
        """没有超时会话时不清理。"""
        ws = MockWebSocket()
        await manager.register(ws)

        cleaned = await manager.cleanup_stale()
        assert cleaned == 0
        assert manager.active_count == 1


# ============================================================
# WebSocketServer 测试
# ============================================================


class TestWebSocketServer:
    """WebSocketServer 服务器测试。"""

    def test_server_creation(self) -> None:
        """创建服务器实例。"""
        server = WebSocketServer(host="127.0.0.1", port=9999)
        assert server.host == "127.0.0.1"
        assert server.port == 9999
        assert server.session_manager is not None

    def test_server_with_custom_session_manager(self) -> None:
        """使用自定义 SessionManager 创建服务器。"""
        manager = SessionManager()
        server = WebSocketServer(session_manager=manager)
        assert server.session_manager is manager

    def test_on_message_setter(self) -> None:
        """设置消息处理器。"""
        server = WebSocketServer()
        handler = AsyncMock()
        server.on_message = handler
        assert server.on_message is handler

    def test_on_disconnect_setter(self) -> None:
        """设置断连处理器。"""
        server = WebSocketServer()
        handler = AsyncMock()
        server.on_disconnect = handler
        assert server.on_disconnect is handler

    @pytest.mark.asyncio
    async def test_start_stop(self) -> None:
        """服务器启动和停止。"""
        server = WebSocketServer(host="127.0.0.1", port=0)  # port=0 让 OS 分配
        await server.start()
        assert server._runner is not None
        await server.stop()
        assert server._runner is None

    @pytest.mark.asyncio
    async def test_send_event(self) -> None:
        """通过服务器发送事件。"""
        server = WebSocketServer()
        ws = MockWebSocket()
        session_id = await server.session_manager.register(ws)

        event = create_event(EventType.CONNECTION_CONFIRMATION, {"session_id": session_id})
        result = await server.send_event(session_id, event)
        assert result is True
        assert len(ws.sent_messages) == 1

        # 验证发送的是合法 JSON
        parsed = json.loads(ws.sent_messages[0])
        assert parsed["type"] == "connection_confirmation"

    @pytest.mark.asyncio
    async def test_send_event_to_unknown_session(self) -> None:
        """向未知会话发送事件。"""
        server = WebSocketServer()
        event = create_event(EventType.PIPELINE_END, {})
        result = await server.send_event("nonexistent", event)
        assert result is False


# ============================================================
# WebSocketInputAdapter 测试
# ============================================================


class TestWebSocketInputAdapter:
    """WebSocketInputAdapter 输入适配器测试。"""

    @pytest.fixture
    def adapter(self) -> WebSocketInputAdapter:
        """创建输入适配器实例。"""
        return WebSocketInputAdapter()

    @pytest.mark.asyncio
    async def test_receive_user_input(self, adapter: WebSocketInputAdapter) -> None:
        """接收用户输入消息。"""
        # 模拟前端发送消息
        await adapter.enqueue_message("session-1", {
            "type": "user_input",
            "data": {"content": "你好"},
        })

        state = await adapter.receive()
        assert state["user_input"] == "你好"
        assert state["core_type"] == "llm_call"
        assert state["should_stop"] is False
        assert state["_ws_session_id"] == "session-1"

    @pytest.mark.asyncio
    async def test_receive_stop_generation(self, adapter: WebSocketInputAdapter) -> None:
        """接收停止生成控制命令。"""
        await adapter.enqueue_message("session-1", {
            "type": "stop_generation",
            "data": {},
        })

        state = await adapter.receive()
        assert state["should_stop"] is True
        assert state["_ws_session_id"] == "session-1"

    @pytest.mark.asyncio
    async def test_receive_resume_action_approved(self, adapter: WebSocketInputAdapter) -> None:
        """接收审批通过命令。"""
        await adapter.enqueue_message("session-1", {
            "type": "resume_action",
            "data": {"approved": True},
        })

        state = await adapter.receive()
        assert state["should_stop"] is False
        assert state["approval_required"] is False
        assert state["_approval_result"] is True

    @pytest.mark.asyncio
    async def test_receive_resume_action_rejected(self, adapter: WebSocketInputAdapter) -> None:
        """接收审批拒绝命令。"""
        await adapter.enqueue_message("session-1", {
            "type": "resume_action",
            "data": {"approved": False},
        })

        state = await adapter.receive()
        assert state["should_stop"] is True
        assert state["_approval_result"] is False

    @pytest.mark.asyncio
    async def test_receive_empty_input(self, adapter: WebSocketInputAdapter) -> None:
        """接收空内容的用户输入。"""
        await adapter.enqueue_message("session-1", {
            "type": "user_input",
            "data": {},
        })

        state = await adapter.receive()
        assert state["user_input"] == ""
        assert state["should_stop"] is False

    @pytest.mark.asyncio
    async def test_multiple_messages_in_order(self, adapter: WebSocketInputAdapter) -> None:
        """多条消息按顺序处理。"""
        await adapter.enqueue_message("s1", {"type": "user_input", "data": {"content": "first"}})
        await adapter.enqueue_message("s1", {"type": "user_input", "data": {"content": "second"}})

        state1 = await adapter.receive()
        state2 = await adapter.receive()
        assert state1["user_input"] == "first"
        assert state2["user_input"] == "second"


# ============================================================
# WebSocketOutputAdapter 测试
# ============================================================


class TestWebSocketOutputAdapter:
    """WebSocketOutputAdapter 输出适配器测试。"""

    @pytest.fixture
    def setup(self) -> tuple[WebSocketOutputAdapter, WebSocketServer, MockWebSocket, str]:
        """创建输出适配器及依赖。"""
        server = WebSocketServer()
        ws = MockWebSocket()
        session_id = asyncio.get_event_loop().run_until_complete(
            server.session_manager.register(ws)
        )
        adapter = WebSocketOutputAdapter(server=server)
        adapter.set_session_id(session_id)
        return adapter, server, ws, session_id

    @pytest.mark.asyncio
    async def test_send_normal_result(self) -> None:
        """发送正常管道结果。"""
        server = WebSocketServer()
        ws = MockWebSocket()
        session_id = await server.session_manager.register(ws)
        adapter = WebSocketOutputAdapter(server=server)
        adapter.set_session_id(session_id)

        await adapter.send({
            "raw_result": "Hello!",
            "ended": True,
            "iteration": 1,
            "_ws_session_id": session_id,
        })

        # 应发送 pipeline_end 事件
        assert len(ws.sent_messages) == 1
        parsed = json.loads(ws.sent_messages[0])
        assert parsed["type"] == "pipeline_end"
        assert parsed["data"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_send_error_result(self) -> None:
        """发送错误结果。"""
        server = WebSocketServer()
        ws = MockWebSocket()
        session_id = await server.session_manager.register(ws)
        adapter = WebSocketOutputAdapter(server=server)
        adapter.set_session_id(session_id)

        await adapter.send({
            "raw_error": "LLM timeout",
            "iteration": 2,
            "_ws_session_id": session_id,
        })

        # 应发送 pipeline_error + pipeline_end
        assert len(ws.sent_messages) == 2
        error_msg = json.loads(ws.sent_messages[0])
        assert error_msg["type"] == "pipeline_error"
        end_msg = json.loads(ws.sent_messages[1])
        assert end_msg["type"] == "pipeline_end"
        assert end_msg["data"]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_send_stopped_result(self) -> None:
        """发送停止信号结果。"""
        server = WebSocketServer()
        ws = MockWebSocket()
        session_id = await server.session_manager.register(ws)
        adapter = WebSocketOutputAdapter(server=server)
        adapter.set_session_id(session_id)

        await adapter.send({
            "should_stop": True,
            "iteration": 3,
            "_ws_session_id": session_id,
        })

        assert len(ws.sent_messages) == 1
        parsed = json.loads(ws.sent_messages[0])
        assert parsed["type"] == "pipeline_end"
        assert parsed["data"]["status"] == "stopped"

    @pytest.mark.asyncio
    async def test_send_pipeline_start(self) -> None:
        """发送 pipeline_start 事件。"""
        server = WebSocketServer()
        ws = MockWebSocket()
        session_id = await server.session_manager.register(ws)
        adapter = WebSocketOutputAdapter(server=server)
        adapter.set_session_id(session_id)

        await adapter.send_pipeline_start(session_id, agent_level="l1_main")

        assert len(ws.sent_messages) == 1
        parsed = json.loads(ws.sent_messages[0])
        assert parsed["type"] == "pipeline_start"
        assert parsed["data"]["session_id"] == session_id

    @pytest.mark.asyncio
    async def test_stream_output(self) -> None:
        """流式输出完整流程。"""
        server = WebSocketServer()
        ws = MockWebSocket()
        session_id = await server.session_manager.register(ws)
        adapter = WebSocketOutputAdapter(server=server)
        adapter.set_session_id(session_id)

        # 发送多个 chunk
        await adapter.send_stream({"text": "H", "type": "token"})
        await adapter.send_stream({"text": "i", "type": "token"})
        await adapter.end_stream(usage={"prompt_tokens": 5, "completion_tokens": 2})

        # 应有: stream_start + 2*stream_chunk + stream_end
        assert len(ws.sent_messages) == 4

        start = json.loads(ws.sent_messages[0])
        assert start["type"] == "stream_start"

        chunk1 = json.loads(ws.sent_messages[1])
        assert chunk1["type"] == "stream_chunk"
        assert chunk1["data"]["content"] == "H"
        assert chunk1["data"]["sequence"] == 1

        chunk2 = json.loads(ws.sent_messages[2])
        assert chunk2["data"]["content"] == "i"
        assert chunk2["data"]["sequence"] == 2

        end = json.loads(ws.sent_messages[3])
        assert end["type"] == "stream_end"
        assert end["data"]["full_content"] == "Hi"
        assert end["data"]["usage"]["prompt_tokens"] == 5

    @pytest.mark.asyncio
    async def test_stream_error_chunk(self) -> None:
        """流式输出中的错误 chunk。"""
        server = WebSocketServer()
        ws = MockWebSocket()
        session_id = await server.session_manager.register(ws)
        adapter = WebSocketOutputAdapter(server=server)
        adapter.set_session_id(session_id)

        await adapter.send_stream({"text": "timeout", "type": "error"})

        assert len(ws.sent_messages) == 1
        parsed = json.loads(ws.sent_messages[0])
        assert parsed["type"] == "pipeline_error"

    @pytest.mark.asyncio
    async def test_stream_without_session(self) -> None:
        """未设置 session_id 时流式输出被跳过。"""
        server = WebSocketServer()
        adapter = WebSocketOutputAdapter(server=server)
        # 不调用 set_session_id

        await adapter.send_stream({"text": "hello", "type": "token"})
        # 不应崩溃，消息被跳过


# ============================================================
# WebSocketAdapter 组合适配器测试
# ============================================================


class TestWebSocketAdapter:
    """WebSocketAdapter 组合适配器测试。"""

    @pytest.mark.asyncio
    async def test_adapter_creation(self) -> None:
        """创建组合适配器。"""
        adapter = WebSocketAdapter(host="127.0.0.1", port=9876)
        assert adapter.input_adapter is not None
        assert adapter.output_adapter is not None
        assert adapter.server is not None
        assert adapter.session_manager is not None

    @pytest.mark.asyncio
    async def test_adapter_start_stop(self) -> None:
        """适配器启动和停止。"""
        adapter = WebSocketAdapter(host="127.0.0.1", port=0)
        await adapter.start()
        assert adapter.server._runner is not None
        await adapter.stop()
        assert adapter.server._runner is None

    @pytest.mark.asyncio
    async def test_input_output_integration(self) -> None:
        """输入→输出的集成测试。"""
        adapter = WebSocketAdapter()
        ws = MockWebSocket()
        session_id = await adapter.session_manager.register(ws)

        # 模拟前端发送消息 → input_adapter 队列
        await adapter.input_adapter.enqueue_message(session_id, {
            "type": "user_input",
            "data": {"content": "测试消息"},
        })

        # 从 input_adapter 取出
        state = await adapter.input_adapter.receive()
        assert state["user_input"] == "测试消息"
        assert state["_ws_session_id"] == session_id

        # 通过 output_adapter 推送结果
        adapter.output_adapter.set_session_id(session_id)
        await adapter.output_adapter.send({
            "ended": True,
            "iteration": 1,
            "_ws_session_id": session_id,
        })

        # 验证前端收到消息
        assert len(ws.sent_messages) == 1
        parsed = json.loads(ws.sent_messages[0])
        assert parsed["type"] == "pipeline_end"

    @pytest.mark.asyncio
    async def test_stop_generation_flow(self) -> None:
        """停止生成完整流程。"""
        adapter = WebSocketAdapter()
        ws = MockWebSocket()
        session_id = await adapter.session_manager.register(ws)

        # 前端发送停止命令
        await adapter.input_adapter.enqueue_message(session_id, {
            "type": "stop_generation",
            "data": {},
        })

        state = await adapter.input_adapter.receive()
        assert state["should_stop"] is True

        # 推送停止结果
        adapter.output_adapter.set_session_id(session_id)
        await adapter.output_adapter.send({
            "should_stop": True,
            "iteration": 2,
            "_ws_session_id": session_id,
        })

        parsed = json.loads(ws.sent_messages[0])
        assert parsed["data"]["status"] == "stopped"
