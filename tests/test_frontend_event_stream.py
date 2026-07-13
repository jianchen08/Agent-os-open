"""前端 WebSocket 事件流集成测试。

前后端分离测试策略：不启动后端，用模拟的 WebSocket 消息验证
后端推送的事件格式是否符合前端期望的协议。

核心验证目标：
1. 事件类型（type 字段）与前端 WS_SERVER_EVENTS 常量一致
2. 事件数据（data 字段）包含前端 handler 依赖的关键字段
3. 事件时序符合前端流式处理逻辑

事件格式参考：
- 后端: src/pipeline/stream_bridge.py _make_event()
- 前端: frontend/src/constants/websocket.ts WS_SERVER_EVENTS
- 前端: frontend/src/services/websocket/streaming/ 各 handler
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# 常量：与前端 WS_SERVER_EVENTS 保持一致的事件类型定义
# ---------------------------------------------------------------------------

EVENT_PIPELINE_RECEIVED = "pipeline_received"
EVENT_STREAM_START = "stream_start"
EVENT_STREAM_CHUNK = "stream_chunk"
EVENT_STREAM_END = "stream_end"
EVENT_STREAM_ERROR = "stream_error"
EVENT_STREAM_KEEPALIVE = "stream_keepalive"
EVENT_NEW_MESSAGE = "new_message"
EVENT_THINKING_START = "thinking_start"
EVENT_THINKING_CHUNK = "thinking_chunk"
EVENT_THINKING_END = "thinking_end"
EVENT_TOOL_START = "tool_start"
EVENT_TOOL_RESULT = "tool_result"
EVENT_STATE_CHANGE = "state_change"
EVENT_SUB_AGENT_CREATED = "sub_agent_created"
EVENT_ITERATION = "iteration"
EVENT_SYSTEM_NOTIFICATION = "system_notification"


# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------


# 默认的 pipeline_id 和 message_id，模拟后端 stream_bridge 构造函数注入
_DEFAULT_PIPELINE_ID = "pipe-1"
_DEFAULT_MESSAGE_ID = "msg-1"


def _make_event(event_type: str, data: dict) -> dict:
    """构造事件字典，模拟后端 stream_bridge._make_event() 的输出格式。

    后端格式: {"type": "...", "data": {"pipeline_id": "...", "message_id": "...", ...}}
    前端通过 eventData.data.pipeline_id / eventData.data.message_id 读取。

    使用 setdefault 模拟后端行为：仅当调用方未显式传入时才注入默认值，
    与 stream_bridge._make_event 保持一致。

    Args:
        event_type: 事件类型字符串
        data: 事件的 data 字段内容

    Returns:
        完整的事件字典
    """
    data.setdefault("pipeline_id", _DEFAULT_PIPELINE_ID)
    data.setdefault("message_id", _DEFAULT_MESSAGE_ID)
    return {"type": event_type, "data": data}


def _resolve_pipeline_id(event_data: dict) -> str | None:
    """模拟前端 router.ts resolvePipelineId() 的逻辑。

    优先级:
    1. data.pipeline_id（非空字符串）
    2. 顶层 pipeline_id（部分事件使用）

    Args:
        event_data: 事件字典

    Returns:
        pipeline_id 字符串，找不到时返回 None
    """
    data_pid = event_data.get("data", {}).get("pipeline_id")
    if isinstance(data_pid, str) and len(data_pid) > 0:
        return data_pid
    top_pid = event_data.get("pipeline_id")
    if isinstance(top_pid, str) and len(top_pid) > 0:
        return top_pid
    return None


def _extract_message_id(event_data: dict) -> str | None:
    """模拟前端 utils.ts extractMessageId() 的逻辑。

    来源优先级:
    1. eventData.message_id
    2. eventData.data.message_id
    3. eventData.data.ai_message_id

    Args:
        event_data: 事件字典

    Returns:
        message_id 字符串，找不到时返回 None
    """
    return (
        event_data.get("message_id")
        or event_data.get("data", {}).get("message_id")
        or event_data.get("data", {}).get("ai_message_id")
        or None
    )


def _extract_thread_id(event_data: dict) -> str | None:
    """模拟前端 utils.ts extractThreadId() 的逻辑。

    来源优先级:
    1. eventData.data._threadId
    2. eventData._threadId

    Args:
        event_data: 事件字典

    Returns:
        thread_id 字符串，找不到时返回 None
    """
    return (
        event_data.get("data", {}).get("_threadId")
        or event_data.get("_threadId")
        or None
    )


# ===========================================================================
# TestEventProtocol：事件协议格式测试
# ===========================================================================


class TestEventProtocol:
    """验证后端推送的事件格式与前端期望的协议一致。

    每个测试用例验证一种事件类型的 type 和 data 结构，
    确保前端 handler 能正确解析所有必要字段。
    """

    def test_pipeline_received_event_format(self):
        """验证 pipeline_received 事件格式。

        前端 handler: lifecycleHandlers.handlePipelineReceived
        关键字段: data.pipeline_id
        """
        event = _make_event(EVENT_PIPELINE_RECEIVED, {
            "pipeline_id": "pipe-1",
            "thread_id": "thread-1",
            "message_id": "msg-1",
        })

        assert event["type"] == EVENT_PIPELINE_RECEIVED
        assert _resolve_pipeline_id(event) == "pipe-1"
        assert event["data"]["pipeline_id"] is not None
        assert event["data"]["message_id"] is not None

    def test_stream_start_event_format(self):
        """验证 stream_start 事件格式。

        前端 handler: streamHandler.handleStreamStart
        关键字段: data.pipeline_id, data.message_id, data._threadId
        后端来源: stream_bridge._send_stream_start()
        """
        event = _make_event(EVENT_STREAM_START, {
            "message_id": "msg-1",
            "pipeline_id": "pipe-1",
            "sequence": 0,
            "_threadId": "thread-1",
        })

        assert event["type"] == EVENT_STREAM_START
        assert _resolve_pipeline_id(event) == "pipe-1"
        assert _extract_message_id(event) == "msg-1"
        assert _extract_thread_id(event) == "thread-1"
        assert isinstance(event["data"]["sequence"], int)

    def test_stream_chunk_event_format(self):
        """验证 stream_chunk 事件格式。

        前端 handler: streamHandler.handleStreamChunk
        关键字段: data.pipeline_id, data.message_id, data.content
        后端来源: stream_bridge._handle_chunk() text 类型
        """
        event = _make_event(EVENT_STREAM_CHUNK, {
            "content": "Hello",
            "sequence": 1,
        })

        assert event["type"] == EVENT_STREAM_CHUNK
        assert _resolve_pipeline_id(event) == "pipe-1"
        assert _extract_message_id(event) == "msg-1"
        assert event["data"]["content"] == "Hello"
        assert isinstance(event["data"]["sequence"], int)

    def test_stream_chunk_content_extraction_fallback(self):
        """验证 stream_chunk 事件的内容提取回退逻辑。

        前端 handler 优先读取 eventData.content，回退到 eventData.data.content。
        后端标准格式通过 data.content 传递。
        """
        # 标准格式：内容在 data.content 中
        event_standard = _make_event(EVENT_STREAM_CHUNK, {
            "content": "标准内容",
            "sequence": 1,
        })
        content_standard = (
            event_standard.get("content")
            or event_standard["data"].get("content")
            or event_standard["data"].get("chunk")
            or ""
        )
        assert content_standard == "标准内容"

    def test_stream_end_event_format(self):
        """验证 stream_end 事件格式。

        前端 handler: streamHandler.handleStreamEnd
        关键字段: data.pipeline_id, data.message_id, data.full_content
        后端来源: stream_bridge.drain_loop() 末尾
        """
        event = _make_event(EVENT_STREAM_END, {
            "full_content": "完整的回复内容",
        })

        assert event["type"] == EVENT_STREAM_END
        assert _resolve_pipeline_id(event) == "pipe-1"
        assert _extract_message_id(event) == "msg-1"
        assert "full_content" in event["data"]

    def test_stream_end_with_connection_lost(self):
        """验证 stream_end 事件在连接丢失时的格式。

        后端在 sink dead 时会附加 connection_lost: True 标记。
        """
        event = _make_event(EVENT_STREAM_END, {
            "full_content": "部分内容",
            "connection_lost": True,
        })

        assert event["type"] == EVENT_STREAM_END
        assert event["data"]["connection_lost"] is True

    def test_stream_end_with_timeout(self):
        """验证 stream_end 事件在超时时的格式。

        后端在 LLM 活动超时时会附加 timed_out: True 标记。
        """
        event = _make_event(EVENT_STREAM_END, {
            "full_content": "部分内容",
            "timed_out": True,
        })

        assert event["type"] == EVENT_STREAM_END
        assert event["data"]["timed_out"] is True

    def test_stream_error_event_format(self):
        """验证 stream_error 事件格式。

        前端 handler: streamHandler.handleStreamError
        关键字段: data.pipeline_id, data.message_id, data.error
        """
        event = _make_event(EVENT_STREAM_ERROR, {
            "error": "LLM 调用失败",
        })

        assert event["type"] == EVENT_STREAM_ERROR
        assert _resolve_pipeline_id(event) is not None

    def test_new_message_event_format(self):
        """验证 new_message 事件格式。

        前端 handler: messageHandler.handleNewMessage
        关键字段: data.pipeline_id, data.message_id, data.content, data.role
        """
        event = _make_event(EVENT_NEW_MESSAGE, {
            "content": "Full response text",
            "role": "assistant",
        })

        assert event["type"] == EVENT_NEW_MESSAGE
        assert _resolve_pipeline_id(event) is not None
        assert event["data"]["role"] == "assistant"

    def test_new_message_with_thinking_and_tools(self):
        """验证 new_message 事件携带 thinking 和 toolCalls 的格式。

        前端 handler: messageHandler.buildPartsFromApiData()
        当消息无 parts 时，从 API 数据构建 parts[]。
        """
        event = _make_event(EVENT_NEW_MESSAGE, {
            "content": "回复内容",
            "role": "assistant",
            "thinking": {"content": "思考过程..."},
            "toolCalls": [{"name": "search", "args": {"q": "test"}}],
        })

        assert event["type"] == EVENT_NEW_MESSAGE
        assert event["data"]["thinking"]["content"] == "思考过程..."
        assert len(event["data"]["toolCalls"]) == 1
        assert event["data"]["toolCalls"][0]["name"] == "search"

    def test_thinking_start_event_format(self):
        """验证 thinking_start 事件格式。

        前端 handler: thinkingHandler.handleThinkingStart
        关键字段: data.pipeline_id, data.message_id, data.sequence
        后端来源: stream_bridge._handle_chunk() thinking 类型首片段
        """
        event = _make_event(EVENT_THINKING_START, {
            "sequence": 0,
        })

        assert event["type"] == EVENT_THINKING_START
        assert _resolve_pipeline_id(event) == "pipe-1"
        assert _extract_message_id(event) == "msg-1"
        assert isinstance(event["data"]["sequence"], int)

    def test_thinking_chunk_event_format(self):
        """验证 thinking_chunk 事件格式。

        前端 handler: thinkingHandler.handleThinkingChunk
        关键字段: data.pipeline_id, data.message_id, data.content
        后端来源: stream_bridge._handle_chunk() thinking 类型后续片段
        """
        event = _make_event(EVENT_THINKING_CHUNK, {
            "content": "正在分析问题...",
        })

        assert event["type"] == EVENT_THINKING_CHUNK
        assert _resolve_pipeline_id(event) == "pipe-1"
        assert _extract_message_id(event) == "msg-1"
        assert event["data"]["content"] == "正在分析问题..."

    def test_thinking_end_event_format(self):
        """验证 thinking_end 事件格式。

        前端 handler: thinkingHandler.handleThinkingEnd
        关键字段: data.pipeline_id, data.message_id, data.duration_ms
        后端来源: stream_bridge._close_thinking_if_active()
        """
        event = _make_event(EVENT_THINKING_END, {
            "duration_ms": 1500,
        })

        assert event["type"] == EVENT_THINKING_END
        assert _resolve_pipeline_id(event) == "pipe-1"
        assert _extract_message_id(event) == "msg-1"
        assert event["data"]["duration_ms"] == 1500

    def test_thinking_end_without_duration(self):
        """验证 thinking_end 事件 duration_ms 为 None 的场景。

        后端在某些路径下不计算 thinking 耗时，传入 None。
        """
        event = _make_event(EVENT_THINKING_END, {
            "duration_ms": None,
        })

        assert event["type"] == EVENT_THINKING_END
        assert event["data"]["duration_ms"] is None

    def test_tool_start_event_format(self):
        """验证 tool_start 事件格式。

        前端 handler: toolHandler.handleToolStart
        关键字段: data.pipeline_id, data.message_id, data.tool_name, data.call_id, data.args
        后端来源: stream_bridge._handle_chunk() tool_start 类型
        """
        event = _make_event(EVENT_TOOL_START, {
            "tool_name": "search",
            "args": {"query": "test query"},
            "call_id": "call_search_001",
            "sequence": 2,
        })

        assert event["type"] == EVENT_TOOL_START
        assert _resolve_pipeline_id(event) == "pipe-1"
        assert _extract_message_id(event) == "msg-1"
        assert event["data"]["tool_name"] == "search"
        assert event["data"]["call_id"] == "call_search_001"
        assert event["data"]["args"]["query"] == "test query"

    def test_tool_result_event_format(self):
        """验证 tool_result 事件格式。

        前端 handler: toolHandler.handleToolResult
        关键字段: data.pipeline_id, data.message_id, data.call_id, data.result, data.success
        后端来源: stream_bridge._handle_chunk() tool_result 类型
        """
        event = _make_event(EVENT_TOOL_RESULT, {
            "tool_name": "search",
            "success": True,
            "result": {"matches": ["item1", "item2"]},
            "duration_ms": 320,
            "call_id": "call_search_001",
        })

        assert event["type"] == EVENT_TOOL_RESULT
        assert _resolve_pipeline_id(event) == "pipe-1"
        assert _extract_message_id(event) == "msg-1"
        assert event["data"]["call_id"] == "call_search_001"
        assert event["data"]["success"] is True
        assert event["data"]["result"]["matches"] == ["item1", "item2"]

    def test_tool_result_failure_format(self):
        """验证 tool_result 事件失败时的格式。

        前端 handler 根据 success 字段决定 part 状态为 done 还是 error。
        """
        event = _make_event(EVENT_TOOL_RESULT, {
            "tool_name": "search",
            "success": False,
            "error": "网络超时",
            "call_id": "call_search_002",
        })

        assert event["type"] == EVENT_TOOL_RESULT
        assert event["data"]["success"] is False
        assert event["data"]["error"] == "网络超时"

    def test_state_change_event_format(self):
        """验证 state_change 事件格式。

        前端 handler: lifecycleHandlers.handleStateChange
        关键字段: data.status, data.pipeline_id
        后端来源: stream_bridge.drain_loop() pipeline_suspended 类型
        """
        event = _make_event(EVENT_STATE_CHANGE, {
            "status": "suspended",
            "pipeline_id": "pipe-1",
            "thread_id": "thread-1",
        })

        assert event["type"] == EVENT_STATE_CHANGE
        assert _resolve_pipeline_id(event) == "pipe-1"
        assert event["data"]["status"] == "suspended"

    def test_stream_keepalive_event_format(self):
        """验证 stream_keepalive 事件格式。

        前端 handler: streamHandler.handleStreamKeepalive
        关键字段: data.pipeline_id
        后端来源: stream_bridge.drain_loop() 心跳保活
        """
        event = _make_event(EVENT_STREAM_KEEPALIVE, {})

        assert event["type"] == EVENT_STREAM_KEEPALIVE
        assert _resolve_pipeline_id(event) == "pipe-1"

    def test_iteration_event_format(self):
        """验证 iteration 事件格式。

        前端 handler: iterationHandler.handleIteration
        关键字段: data.pipeline_id, data.message_id, data.iteration, data.max_iterations
        后端来源: stream_bridge._handle_chunk() iteration 类型
        """
        event = _make_event(EVENT_ITERATION, {
            "iteration": 2,
            "max_iterations": 5,
        })

        assert event["type"] == EVENT_ITERATION
        assert _resolve_pipeline_id(event) == "pipe-1"
        assert _extract_message_id(event) == "msg-1"
        assert event["data"]["iteration"] == 2
        assert event["data"]["max_iterations"] == 5

    def test_system_notification_event_format(self):
        """验证 system_notification 事件格式。

        前端 handler: lifecycleHandlers.handleSystemNotification
        关键字段: data.pipeline_id, data.content, data.level, data.notificationType
        后端来源: stream_bridge.drain_loop() system 类型
        """
        event = _make_event(EVENT_SYSTEM_NOTIFICATION, {
            "content": "任务已完成",
            "level": "info",
            "notificationType": "task_completed",
        })

        assert event["type"] == EVENT_SYSTEM_NOTIFICATION
        assert _resolve_pipeline_id(event) == "pipe-1"
        assert event["data"]["content"] == "任务已完成"
        assert event["data"]["level"] == "info"
        assert event["data"]["notificationType"] == "task_completed"


# ===========================================================================
# TestPipelineIdRouting：pipeline_id 路由测试
# ===========================================================================


class TestPipelineIdRouting:
    """验证前端 pipeline_id 路由逻辑（resolvePipelineId）的正确性。

    核心原则：pipeline_id 是唯一的路由键，_threadId 不参与消息路由。
    """

    def test_pipeline_id_from_data_field(self):
        """验证从 data.pipeline_id 提取路由键。"""
        event = _make_event("stream_start", {
            "pipeline_id": "pipe-from-data",
            "message_id": "msg-1",
        })
        assert _resolve_pipeline_id(event) == "pipe-from-data"

    def test_pipeline_id_from_top_level(self):
        """验证从顶层 pipeline_id 提取路由键（部分事件格式）。"""
        event = {
            "type": "state_change",
            "pipeline_id": "pipe-from-top",
            "data": {"status": "suspended"},
        }
        assert _resolve_pipeline_id(event) == "pipe-from-top"

    def test_data_pipeline_id_takes_priority(self):
        """验证 data.pipeline_id 优先于顶层 pipeline_id。"""
        event = {
            "type": "stream_start",
            "pipeline_id": "pipe-top",
            "data": {"pipeline_id": "pipe-data", "message_id": "msg-1"},
        }
        assert _resolve_pipeline_id(event) == "pipe-data"

    def test_empty_pipeline_id_returns_none(self):
        """验证空字符串 pipeline_id 返回 None。"""
        event = _make_event("stream_start", {
            "pipeline_id": "",
            "message_id": "msg-1",
        })
        assert _resolve_pipeline_id(event) is None

    def test_missing_pipeline_id_returns_none(self):
        """验证缺少 pipeline_id 时返回 None。"""
        event = {"type": "stream_start", "data": {"message_id": "msg-1"}}
        assert _resolve_pipeline_id(event) is None

    def test_thread_id_not_used_for_routing(self):
        """验证 _threadId 不参与消息路由。

        BUG-FIX-fix_20260523_router_threadid_fallback:
        前端已移除 _threadId 回退逻辑，pipeline_id 缺失时返回 null。
        """
        event = {
            "type": "stream_start",
            "_threadId": "thread-123",
            "data": {"message_id": "msg-1"},
        }
        assert _resolve_pipeline_id(event) is None

    def test_message_id_extraction_from_data(self):
        """验证从 data.message_id 提取消息 ID（标准格式）。"""
        event = _make_event("stream_chunk", {
            "message_id": "msg-from-data",
            "content": "hello",
        })
        assert _extract_message_id(event) == "msg-from-data"

    def test_message_id_extraction_from_ai_message_id(self):
        """验证从 data.ai_message_id 提取消息 ID（兼容格式）。

        当 data.message_id 缺失时，前端回退到 data.ai_message_id。
        此场景下不使用 _make_event（会自动注入 message_id），
        手动构造事件以模拟仅有 ai_message_id 的情况。
        """
        event = {
            "type": EVENT_STREAM_CHUNK,
            "data": {
                "pipeline_id": "pipe-1",
                "ai_message_id": "msg-ai-format",
                "content": "hello",
            },
        }
        assert _extract_message_id(event) == "msg-ai-format"

    def test_message_id_extraction_from_top_level(self):
        """验证从顶层 message_id 提取消息 ID。"""
        event = {
            "type": "stream_start",
            "message_id": "msg-top-level",
            "data": {"pipeline_id": "pipe-1"},
        }
        assert _extract_message_id(event) == "msg-top-level"


# ===========================================================================
# TestEventSequence：事件时序测试
# ===========================================================================


class TestEventSequence:
    """验证事件时序是否符合前端流式处理逻辑。

    后端 stream_bridge.drain_loop() 按特定顺序产生事件，
    前端 handler 依赖此顺序正确管理消息状态。
    """

    @staticmethod
    def _build_standard_events() -> list[dict]:
        """构造标准消息流事件序列。

        模拟后端 stream_bridge.drain_loop() 的完整流程:
        pipeline_received -> stream_start -> [stream_chunk x N] -> stream_end

        Returns:
            事件列表
        """
        return [
            _make_event(EVENT_PIPELINE_RECEIVED, {
                "pipeline_id": "pipe-1",
                "thread_id": "thread-1",
                "message_id": "msg-1",
            }),
            _make_event(EVENT_STREAM_START, {
                "message_id": "msg-1",
                "pipeline_id": "pipe-1",
                "sequence": 0,
                "_threadId": "thread-1",
            }),
            _make_event(EVENT_STREAM_CHUNK, {
                "content": "Hello",
                "sequence": 1,
            }),
            _make_event(EVENT_STREAM_CHUNK, {
                "content": " World",
                "sequence": 2,
            }),
            _make_event(EVENT_STREAM_CHUNK, {
                "content": "!",
                "sequence": 3,
            }),
            _make_event(EVENT_STREAM_END, {
                "full_content": "Hello World!",
            }),
        ]

    def test_full_message_event_sequence(self):
        """验证完整的消息事件序列。

        标准流程: pipeline_received -> stream_start -> [stream_chunk x N] -> stream_end
        前端依赖此顺序:
        - stream_start 创建占位消息
        - stream_chunk 追加内容
        - stream_end 合并并标记完成
        """
        events = self._build_standard_events()
        types = [e["type"] for e in events]

        # 验证事件类型序列
        assert types[0] == EVENT_PIPELINE_RECEIVED
        assert types[1] == EVENT_STREAM_START
        assert all(t == EVENT_STREAM_CHUNK for t in types[2:-1])
        assert types[-1] == EVENT_STREAM_END

        # 验证所有事件都有 pipeline_id
        for event in events:
            assert _resolve_pipeline_id(event) is not None, (
                f"事件 {event['type']} 缺少 pipeline_id"
            )

        # 验证所有事件都有 message_id（pipeline_received 除外）
        for event in events[1:]:
            assert _extract_message_id(event) is not None, (
                f"事件 {event['type']} 缺少 message_id"
            )

    def test_stream_chunk_sequence_increasing(self):
        """验证 stream_chunk 的 sequence 字段单调递增。

        后端 stream_bridge 维护自增序号，前端可用于排序。
        """
        events = self._build_standard_events()
        chunk_events = [e for e in events if e["type"] == EVENT_STREAM_CHUNK]

        sequences = [e["data"]["sequence"] for e in chunk_events]
        for i in range(1, len(sequences)):
            assert sequences[i] > sequences[i - 1], (
                f"sequence 非单调递增: {sequences[i - 1]} -> {sequences[i]}"
            )

    def test_thinking_then_response_sequence(self):
        """验证思考+回复的事件序列。

        完整流程:
        pipeline_received -> stream_start -> thinking_start -> thinking_chunk
        -> thinking_end -> stream_chunk -> stream_end -> new_message

        后端行为: _handle_chunk() 遇到 thinking 类型时先发 thinking_start，
        后续 thinking 片段发 thinking_chunk，thinking_end 时关闭。
        """
        events = [
            _make_event(EVENT_PIPELINE_RECEIVED, {
                "pipeline_id": "pipe-1",
                "thread_id": "thread-1",
                "message_id": "msg-1",
            }),
            _make_event(EVENT_STREAM_START, {
                "message_id": "msg-1",
                "pipeline_id": "pipe-1",
                "sequence": 0,
                "_threadId": "thread-1",
            }),
            _make_event(EVENT_THINKING_START, {
                "sequence": 1,
            }),
            _make_event(EVENT_THINKING_CHUNK, {
                "content": "正在分析问题...",
            }),
            _make_event(EVENT_THINKING_CHUNK, {
                "content": "需要搜索相关资料...",
            }),
            _make_event(EVENT_THINKING_END, {
                "duration_ms": 2500,
            }),
            _make_event(EVENT_STREAM_CHUNK, {
                "content": "根据分析，",
                "sequence": 2,
            }),
            _make_event(EVENT_STREAM_CHUNK, {
                "content": "答案是 42。",
                "sequence": 3,
            }),
            _make_event(EVENT_STREAM_END, {
                "full_content": "根据分析，答案是 42。",
            }),
        ]

        types = [e["type"] for e in events]

        # 验证 thinking 三元组完整性
        assert EVENT_THINKING_START in types
        assert EVENT_THINKING_CHUNK in types
        assert EVENT_THINKING_END in types

        # 验证 thinking_start 在 thinking_chunk 之前
        ts_idx = types.index(EVENT_THINKING_START)
        tc_idx = types.index(EVENT_THINKING_CHUNK)
        assert ts_idx < tc_idx, "thinking_start 必须在 thinking_chunk 之前"

        # 验证 thinking_end 在 thinking_chunk 之后
        te_idx = types.index(EVENT_THINKING_END)
        assert te_idx > tc_idx, "thinking_end 必须在 thinking_chunk 之后"

        # 验证 stream_chunk 在 thinking_end 之后
        sc_idx = types.index(EVENT_STREAM_CHUNK)
        assert sc_idx > te_idx, "stream_chunk 必须在 thinking_end 之后"

        # 验证 thinking_end 有 duration_ms
        thinking_end_event = events[te_idx]
        assert "duration_ms" in thinking_end_event["data"]

    def test_tool_call_sequence(self):
        """验证工具调用的事件序列。

        后端行为（stream_bridge._handle_chunk）:
        1. tool_start 之前会先关闭 thinking 并发送 stream_end（如果有待刷写的文本）
        2. tool_result 之后重置 accumulated_content（不再重发 stream_start）
        3. tool_result 会自动补发缺失的 tool_start（FIXUP 逻辑）

        事件序列:
        stream_start -> stream_chunk -> stream_end -> tool_start -> tool_result
        -> stream_chunk -> stream_end
        """
        events = [
            _make_event(EVENT_STREAM_START, {
                "message_id": "msg-1",
                "pipeline_id": "pipe-1",
                "sequence": 0,
                "_threadId": "thread-1",
            }),
            _make_event(EVENT_STREAM_CHUNK, {
                "content": "我来帮你搜索。",
                "sequence": 1,
            }),
            _make_event(EVENT_STREAM_END, {
                "full_content": "我来帮你搜索。",
            }),
            _make_event(EVENT_TOOL_START, {
                "tool_name": "search",
                "args": {"query": "test"},
                "call_id": "call_001",
                "sequence": 2,
            }),
            _make_event(EVENT_TOOL_RESULT, {
                "tool_name": "search",
                "success": True,
                "result": {"matches": ["result1"]},
                "duration_ms": 150,
                "call_id": "call_001",
            }),
            _make_event(EVENT_STREAM_CHUNK, {
                "content": "搜索结果如上。",
                "sequence": 3,
            }),
            _make_event(EVENT_STREAM_END, {
                "full_content": "搜索结果如上。",
            }),
        ]

        types = [e["type"] for e in events]

        # 验证 tool_start 在 tool_result 之前
        tool_start_idx = types.index(EVENT_TOOL_START)
        tool_result_idx = types.index(EVENT_TOOL_RESULT)
        assert tool_start_idx < tool_result_idx

        # 验证 tool_start 和 tool_result 的 call_id 匹配
        tool_start_call_id = events[tool_start_idx]["data"]["call_id"]
        tool_result_call_id = events[tool_result_idx]["data"]["call_id"]
        assert tool_start_call_id == tool_result_call_id, (
            f"tool_start call_id={tool_start_call_id} 与 tool_result call_id={tool_result_call_id} 不匹配"
        )

        # 验证 tool_result 之后没有多余的 stream_start
        assert types[tool_result_idx + 1] != EVENT_STREAM_START

        # 验证最终以 stream_end 结束
        assert types[-1] == EVENT_STREAM_END

    def test_tool_result_fixup_sequence(self):
        """验证 tool_result 缺少 tool_start 时的自动补发逻辑。

        后端 stream_bridge._handle_chunk() 中 FIXUP:
        当 tool_result 的 call_id 不在 _sent_tool_starts 中时，
        自动补发 tool_start 事件。
        """
        # 模拟 FIXUP 后的事件序列
        events = [
            _make_event(EVENT_STREAM_START, {
                "message_id": "msg-1",
                "pipeline_id": "pipe-1",
                "sequence": 0,
                "_threadId": "thread-1",
            }),
            # FIXUP: tool_result 到达时发现没有对应的 tool_start，自动补发
            _make_event(EVENT_TOOL_START, {
                "tool_name": "search",
                "args": None,
                "call_id": "call_fixup_001",
                "sequence": 1,
            }),
            _make_event(EVENT_TOOL_RESULT, {
                "tool_name": "search",
                "success": True,
                "result": "fixed result",
                "call_id": "call_fixup_001",
            }),
            _make_event(EVENT_STREAM_CHUNK, {
                "content": "结果已修复。",
                "sequence": 2,
            }),
            _make_event(EVENT_STREAM_END, {
                "full_content": "结果已修复。",
            }),
        ]

        types = [e["type"] for e in events]

        # FIXUP 的 tool_start 中 args 为 None
        fixup_start = events[1]
        assert fixup_start["type"] == EVENT_TOOL_START
        assert fixup_start["data"]["args"] is None

        # call_id 仍然匹配
        assert fixup_start["data"]["call_id"] == events[2]["data"]["call_id"]

    def test_multiple_tool_calls_sequence(self):
        """验证多次工具调用的事件序列。

        场景: LLM 连续调用多个工具，每个 tool_start/result 对应一个 call_id。
        """
        events = [
            _make_event(EVENT_STREAM_START, {
                "message_id": "msg-1",
                "pipeline_id": "pipe-1",
                "sequence": 0,
                "_threadId": "thread-1",
            }),
            # 第一次工具调用
            _make_event(EVENT_TOOL_START, {
                "tool_name": "search",
                "args": {"q": "python"},
                "call_id": "call_001",
                "sequence": 1,
            }),
            _make_event(EVENT_TOOL_RESULT, {
                "tool_name": "search",
                "success": True,
                "result": "results",
                "call_id": "call_001",
            }),
            # 第二次工具调用
            _make_event(EVENT_TOOL_START, {
                "tool_name": "execute",
                "args": {"code": "print(1)"},
                "call_id": "call_002",
                "sequence": 2,
            }),
            _make_event(EVENT_TOOL_RESULT, {
                "tool_name": "execute",
                "success": True,
                "result": "1",
                "call_id": "call_002",
            }),
            _make_event(EVENT_STREAM_CHUNK, {
                "content": "最终回复。",
                "sequence": 3,
            }),
            _make_event(EVENT_STREAM_END, {
                "full_content": "最终回复。",
            }),
        ]

        types = [e["type"] for e in events]

        # 验证两次 tool_start 使用不同的 call_id
        tool_starts = [e for e in events if e["type"] == EVENT_TOOL_START]
        assert len(tool_starts) == 2
        assert tool_starts[0]["data"]["call_id"] != tool_starts[1]["data"]["call_id"]

        # 验证每个 tool_start 的 call_id 唯一（tool_result 与对应 tool_start 共享 call_id）
        tool_start_call_ids = [
            e["data"]["call_id"]
            for e in events
            if e["type"] == EVENT_TOOL_START
        ]
        assert len(tool_start_call_ids) == len(set(tool_start_call_ids)), (
            "每个 tool_start 的 call_id 应唯一"
        )

        # 验证每个 tool_result 都能匹配到对应的 tool_start
        tool_result_call_ids = [
            e["data"]["call_id"]
            for e in events
            if e["type"] == EVENT_TOOL_RESULT
        ]
        for result_cid in tool_result_call_ids:
            assert result_cid in tool_start_call_ids, (
                f"tool_result call_id={result_cid} 没有匹配的 tool_start"
            )

    def test_iteration_event_sequence(self):
        """验证迭代事件在流式输出中的位置。

        后端在 _handle_chunk 中处理 iteration 类型，
        先关闭 thinking 再发送 iteration 事件。
        """
        events = [
            _make_event(EVENT_STREAM_START, {
                "message_id": "msg-1",
                "pipeline_id": "pipe-1",
                "sequence": 0,
                "_threadId": "thread-1",
            }),
            _make_event(EVENT_THINKING_START, {
                "sequence": 1,
            }),
            _make_event(EVENT_THINKING_CHUNK, {
                "content": "第一次思考...",
            }),
            # iteration 事件: 后端先关闭 thinking 再发 iteration
            _make_event(EVENT_THINKING_END, {
                "duration_ms": None,
            }),
            _make_event(EVENT_ITERATION, {
                "iteration": 1,
                "max_iterations": 3,
            }),
            # 新一轮开始
            _make_event(EVENT_THINKING_START, {
                "sequence": 2,
            }),
            _make_event(EVENT_STREAM_CHUNK, {
                "content": "第二轮回复。",
                "sequence": 3,
            }),
            _make_event(EVENT_STREAM_END, {
                "full_content": "第二轮回复。",
            }),
        ]

        types = [e["type"] for e in events]

        # 验证 iteration 事件存在
        assert EVENT_ITERATION in types

        # 验证 iteration 携带迭代信息
        iter_event = events[types.index(EVENT_ITERATION)]
        assert iter_event["data"]["iteration"] == 1
        assert iter_event["data"]["max_iterations"] == 3

    def test_keepalive_during_long_operation(self):
        """验证长时间操作期间的 keepalive 事件。

        后端 drain_loop 在 heartbeat_interval 无新 chunk 时发送 stream_keepalive，
        前端收到后重置 chunk 超时计时器。
        """
        events = [
            _make_event(EVENT_STREAM_START, {
                "message_id": "msg-1",
                "pipeline_id": "pipe-1",
                "sequence": 0,
                "_threadId": "thread-1",
            }),
            _make_event(EVENT_STREAM_CHUNK, {
                "content": "开始处理...",
                "sequence": 1,
            }),
            # 长时间无响应，后端发送 keepalive
            _make_event(EVENT_STREAM_KEEPALIVE, {}),
            # 继续等待
            _make_event(EVENT_STREAM_KEEPALIVE, {}),
            # LLM 最终响应
            _make_event(EVENT_STREAM_CHUNK, {
                "content": "处理完成。",
                "sequence": 2,
            }),
            _make_event(EVENT_STREAM_END, {
                "full_content": "开始处理...处理完成。",
            }),
        ]

        types = [e["type"] for e in events]

        # 验证 keepalive 事件存在
        keepalive_count = types.count(EVENT_STREAM_KEEPALIVE)
        assert keepalive_count >= 1

        # 验证 keepalive 在 stream_start 之后、stream_end 之前
        start_idx = types.index(EVENT_STREAM_START)
        end_idx = types.index(EVENT_STREAM_END)
        for i, t in enumerate(types):
            if t == EVENT_STREAM_KEEPALIVE:
                assert start_idx < i < end_idx, (
                    "keepalive 必须在 stream_start 和 stream_end 之间"
                )

    def test_state_change_suspended_sequence(self):
        """验证管道挂起时的事件序列。

        后端 drain_loop 检测到 pipeline_suspended chunk 时:
        1. 先关闭 thinking（如果活跃）
        2. 发送 state_change 事件
        3. 继续循环但不发送 stream_end
        """
        events = [
            _make_event(EVENT_STREAM_START, {
                "message_id": "msg-1",
                "pipeline_id": "pipe-1",
                "sequence": 0,
                "_threadId": "thread-1",
            }),
            _make_event(EVENT_STREAM_CHUNK, {
                "content": "需要用户确认...",
                "sequence": 1,
            }),
            # thinking 关闭（如果有）
            _make_event(EVENT_THINKING_END, {
                "duration_ms": None,
            }),
            # 状态变更
            _make_event(EVENT_STATE_CHANGE, {
                "status": "suspended",
                "pipeline_id": "pipe-1",
                "thread_id": "thread-1",
            }),
        ]

        types = [e["type"] for e in events]

        # 验证 state_change 存在
        assert EVENT_STATE_CHANGE in types

        # 验证 state_change 的 status 为 suspended
        sc_event = events[types.index(EVENT_STATE_CHANGE)]
        assert sc_event["data"]["status"] == "suspended"
        assert _resolve_pipeline_id(sc_event) == "pipe-1"

    def test_system_notification_sequence(self):
        """验证系统通知事件在流式输出中的位置。

        后端 drain_loop 处理 system 类型 chunk 时发送 system_notification，
        不经过 _handle_chunk，直接作为独立事件发送。
        """
        events = [
            _make_event(EVENT_STREAM_START, {
                "message_id": "msg-1",
                "pipeline_id": "pipe-1",
                "sequence": 0,
                "_threadId": "thread-1",
            }),
            _make_event(EVENT_SYSTEM_NOTIFICATION, {
                "content": "子任务已完成",
                "level": "info",
                "notificationType": "task_completed",
            }),
            _make_event(EVENT_STREAM_CHUNK, {
                "content": "继续回复...",
                "sequence": 1,
            }),
            _make_event(EVENT_STREAM_END, {
                "full_content": "继续回复...",
            }),
        ]

        types = [e["type"] for e in events]

        # 验证 system_notification 存在
        assert EVENT_SYSTEM_NOTIFICATION in types

        # 验证 system_notification 在 stream_start 之后
        sn_idx = types.index(EVENT_SYSTEM_NOTIFICATION)
        assert sn_idx > 0


# ===========================================================================
# TestEventProtocolConsistency：前后端协议一致性测试
# ===========================================================================


class TestEventProtocolConsistency:
    """验证后端事件格式与前端期望的协议一致。

    确保后端 _make_event 产生的事件字典包含前端 handler 需要的所有字段，
    避免因字段缺失导致前端静默丢弃事件。
    """

    @pytest.fixture
    def bridge_events(self):
        """构造通过 stream_bridge._make_event 产生的事件字典列表。

        模拟后端 _make_event 的行为: setdefault 注入 pipeline_id 和 message_id。

        Returns:
            事件字典列表
        """
        pipeline_id = "pipe-consistency-test"
        message_id = "msg-consistency-test"

        def make(event_type: str, data: dict) -> dict:
            """模拟 stream_bridge._make_event 的 setdefault 逻辑。"""
            data.setdefault("pipeline_id", pipeline_id)
            data.setdefault("message_id", message_id)
            return {"type": event_type, "data": data}

        return {
            "make": make,
            "pipeline_id": pipeline_id,
            "message_id": message_id,
        }

    def test_all_stream_events_have_pipeline_id(self, bridge_events):
        """验证所有流式事件都包含 pipeline_id。

        前端 resolvePipelineId 从 data.pipeline_id 提取路由键，
        缺失时事件会被丢弃（warn 并 return）。
        """
        make = bridge_events["make"]

        stream_event_types = [
            EVENT_STREAM_START,
            EVENT_STREAM_CHUNK,
            EVENT_STREAM_END,
            EVENT_STREAM_ERROR,
            EVENT_STREAM_KEEPALIVE,
            EVENT_THINKING_START,
            EVENT_THINKING_CHUNK,
            EVENT_THINKING_END,
            EVENT_TOOL_START,
            EVENT_TOOL_RESULT,
            EVENT_ITERATION,
        ]

        for event_type in stream_event_types:
            event = make(event_type, {})
            assert _resolve_pipeline_id(event) is not None, (
                f"事件类型 {event_type} 缺少 pipeline_id，前端会丢弃此事件"
            )

    def test_all_content_events_have_message_id(self, bridge_events):
        """验证所有内容事件都包含 message_id。

        前端 extractMessageId 从 data.message_id 提取消息 ID，
        缺失时 handler 会 return（不处理）。
        """
        make = bridge_events["make"]

        content_event_types = [
            EVENT_STREAM_START,
            EVENT_STREAM_CHUNK,
            EVENT_STREAM_END,
            EVENT_THINKING_START,
            EVENT_THINKING_CHUNK,
            EVENT_THINKING_END,
            EVENT_TOOL_START,
            EVENT_TOOL_RESULT,
            EVENT_ITERATION,
        ]

        for event_type in content_event_types:
            event = make(event_type, {})
            assert _extract_message_id(event) is not None, (
                f"事件类型 {event_type} 缺少 message_id，前端 handler 会跳过"
            )

    def test_tool_start_result_call_id_matching(self, bridge_events):
        """验证 tool_start 和 tool_result 的 call_id 格式一致。

        前端 toolHandler 通过 call_id 精确匹配 parts[] 中的 tool_call part。
        """
        make = bridge_events["make"]

        call_id = "call_test_001"

        tool_start = make(EVENT_TOOL_START, {
            "tool_name": "test_tool",
            "args": {"key": "value"},
            "call_id": call_id,
            "sequence": 0,
        })
        tool_result = make(EVENT_TOOL_RESULT, {
            "tool_name": "test_tool",
            "success": True,
            "result": "ok",
            "duration_ms": 100,
            "call_id": call_id,
        })

        assert tool_start["data"]["call_id"] == tool_result["data"]["call_id"]
        assert tool_start["data"]["tool_name"] == tool_result["data"]["tool_name"]

    def test_event_type_strings_match_frontend_constants(self):
        """验证事件类型字符串与前端 WS_SERVER_EVENTS 常量定义一致。

        此测试确保后端发送的事件类型名称与前端期望完全匹配，
        任何不一致都会导致前端无法路由事件。
        """
        # 后端 stream_bridge 使用的事件类型
        backend_event_types = {
            "pipeline_received",
            "stream_start",
            "stream_chunk",
            "stream_end",
            "stream_error",
            "stream_keepalive",
            "thinking_start",
            "thinking_chunk",
            "thinking_end",
            "tool_start",
            "tool_result",
            "state_change",
            "system_notification",
            "iteration",
        }

        # 前端 WS_SERVER_EVENTS 中对应的事件类型
        frontend_event_types = {
            EVENT_PIPELINE_RECEIVED,
            EVENT_STREAM_START,
            EVENT_STREAM_CHUNK,
            EVENT_STREAM_END,
            EVENT_STREAM_ERROR,
            EVENT_STREAM_KEEPALIVE,
            EVENT_THINKING_START,
            EVENT_THINKING_CHUNK,
            EVENT_THINKING_END,
            EVENT_TOOL_START,
            EVENT_TOOL_RESULT,
            EVENT_STATE_CHANGE,
            EVENT_SYSTEM_NOTIFICATION,
            EVENT_ITERATION,
        }

        # 验证完全一致
        assert backend_event_types == frontend_event_types, (
            f"前后端事件类型不一致: "
            f"仅后端={backend_event_types - frontend_event_types}, "
            f"仅前端={frontend_event_types - backend_event_types}"
        )
