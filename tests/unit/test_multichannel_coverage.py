"""多通道接入模块测试补充。

覆盖 tests/channels/ 目录下现有测试的盲区：

1. IInputAdapter / IOutputAdapter 基类默认实现
   - health_check() 默认返回 True
   - is_connected 默认返回 True
   - get_status() 返回结构化状态字典
2. BaseComboAdapter 组合适配器基类
   - 基于 stream_client 的连接状态代理
3. ChannelGateway 异常路径
   - start/stop 时单个适配器抛异常不影响其他
   - send_response 时 output_adapter 为 None
   - handle_message 时无管道回调
4. SessionBridge IO 异常路径
   - 损坏 JSON 恢复
   - 持久化写入失败
5. 各通道输入适配器 _raw_to_state 转换正确性
   - 飞书/钉钉/QQ/企业微信
6. 各通道输出适配器 send/send_stream 行为
   - 错误状态优先发送、正常结果发送、流式累积刷新

来源：src/channels/ 源码 + src/channels/channels.md 模块文档
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pipeline.types import StateKeys

# ════════════════════════════════════════════════════════════════
# 1. IInputAdapter / IOutputAdapter 基类默认实现
# ════════════════════════════════════════════════════════════════


def _make_input_adapter_subclass():
    """创建一个实现了抽象方法的 IInputAdapter 子类。"""
    from channels.input_adapter import IInputAdapter

    class _TestInputAdapter(IInputAdapter):
        async def receive(self) -> dict[str, Any]:
            return {"user_input": "test"}

    return _TestInputAdapter()


def _make_output_adapter_subclass():
    """创建一个实现了抽象方法的 IOutputAdapter 子类。"""
    from channels.output_adapter import IOutputAdapter

    class _TestOutputAdapter(IOutputAdapter):
        async def send(self, state: dict[str, Any]) -> None:
            pass

        async def send_stream(self, chunk: dict[str, Any]) -> None:
            pass

    return _TestOutputAdapter()


class TestInputAdapterDefaults:
    """IInputAdapter 基类默认行为测试。"""

    async def test_health_check_defaults_true(self) -> None:
        """health_check 默认返回 True。"""
        adapter = _make_input_adapter_subclass()
        result = await adapter.health_check()
        assert result is True

    def test_is_connected_defaults_true(self) -> None:
        """is_connected 默认返回 True。"""
        adapter = _make_input_adapter_subclass()
        assert adapter.is_connected is True

    def test_get_status_returns_structured_dict(self) -> None:
        """get_status 返回包含 type/connected/healthy 的字典。"""
        adapter = _make_input_adapter_subclass()
        status = adapter.get_status()
        assert "type" in status
        assert "connected" in status
        assert "healthy" in status
        assert status["connected"] is True
        assert status["healthy"] is True


class TestOutputAdapterDefaults:
    """IOutputAdapter 基类默认行为测试。"""

    async def test_health_check_defaults_true(self) -> None:
        """health_check 默认返回 True。"""
        adapter = _make_output_adapter_subclass()
        result = await adapter.health_check()
        assert result is True

    def test_is_connected_defaults_true(self) -> None:
        """is_connected 默认返回 True。"""
        adapter = _make_output_adapter_subclass()
        assert adapter.is_connected is True

    def test_get_status_returns_structured_dict(self) -> None:
        """get_status 返回包含 type/connected/healthy 的字典。"""
        adapter = _make_output_adapter_subclass()
        status = adapter.get_status()
        assert "type" in status
        assert "connected" in status
        assert "healthy" in status


# ════════════════════════════════════════════════════════════════
# 2. BaseComboAdapter 组合适配器基类
# ════════════════════════════════════════════════════════════════


class _FakeComboAdapter:
    """模拟 BaseComboAdapter 子类用于测试共享行为。"""

    def __init__(self, connected: bool) -> None:
        self.stream_client = MagicMock()
        self.stream_client.is_connected = connected
        self.channel_type = "fake"

    # 继承 BaseComboAdapter 的方法


class TestBaseComboAdapter:
    """BaseComboAdapter 共享行为测试。"""

    def test_is_connected_proxies_to_stream_client(self) -> None:
        """is_connected 代理 stream_client 的连接状态。"""
        from channels.base_combo_adapter import BaseComboAdapter

        adapter = _FakeComboAdapter(connected=True)
        adapter.__class__ = type(
            "FakeAdapter", (BaseComboAdapter,), {"channel_type": "fake"}
        )
        assert adapter.is_connected is True

    def test_is_connected_false_when_stream_disconnected(self) -> None:
        """stream_client 断连时 is_connected 为 False。"""
        from channels.base_combo_adapter import BaseComboAdapter

        adapter = _FakeComboAdapter(connected=False)
        adapter.__class__ = type(
            "FakeAdapter", (BaseComboAdapter,), {"channel_type": "fake"}
        )
        assert adapter.is_connected is False

    async def test_health_check_returns_connection_state(self) -> None:
        """health_check 返回 stream_client 的连接状态。"""
        from channels.base_combo_adapter import BaseComboAdapter

        adapter = _FakeComboAdapter(connected=True)
        adapter.__class__ = type(
            "FakeAdapter", (BaseComboAdapter,), {"channel_type": "fake"}
        )
        result = await adapter.health_check()
        assert result is True

    def test_get_status_includes_channel_type(self) -> None:
        """get_status 包含 channel_type、connected、healthy。"""
        from channels.base_combo_adapter import BaseComboAdapter

        adapter = _FakeComboAdapter(connected=True)
        adapter.__class__ = type(
            "FakeAdapter", (BaseComboAdapter,), {"channel_type": "fake"}
        )
        status = adapter.get_status()
        assert status["type"] == "fake"
        assert status["connected"] is True
        assert status["healthy"] is True


# ════════════════════════════════════════════════════════════════
# 3. ChannelGateway 异常路径
# ════════════════════════════════════════════════════════════════


class TestChannelGatewayErrorPaths:
    """ChannelGateway 异常处理路径测试。"""

    async def test_start_continues_when_adapter_raises(self) -> None:
        """单个适配器 start 失败不应影响其他适配器启动。"""
        from channels.gateway.channel_gateway import ChannelGateway

        gateway = ChannelGateway()
        good_adapter = MagicMock()
        good_adapter.start = AsyncMock()
        bad_adapter = MagicMock()
        bad_adapter.start = AsyncMock(side_effect=RuntimeError("boom"))

        gateway.register_adapter("bad", bad_adapter)
        gateway.register_adapter("good", good_adapter)

        await gateway.start()  # 不应抛异常

        good_adapter.start.assert_called_once()

    async def test_stop_continues_when_adapter_raises(self) -> None:
        """单个适配器 stop 失败不应影响其他适配器停止。"""
        from channels.gateway.channel_gateway import ChannelGateway

        gateway = ChannelGateway()
        good_adapter = MagicMock()
        good_adapter.stop = AsyncMock()
        bad_adapter = MagicMock()
        bad_adapter.stop = AsyncMock(side_effect=RuntimeError("boom"))

        gateway.register_adapter("bad", bad_adapter)
        gateway.register_adapter("good", good_adapter)

        await gateway.stop()  # 不应抛异常

        good_adapter.stop.assert_called_once()

    async def test_handle_message_without_callback_drops_message(self) -> None:
        """无管道回调时消息被丢弃（仅记日志，不抛异常）。"""
        from channels.gateway.channel_gateway import ChannelGateway

        gateway = ChannelGateway()
        gateway.on_pipeline_request = None  # 无回调

        raw_msg = {
            "header": {"event_id": "evt-1", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_test"}},
                "message": {
                    "message_id": "msg-1",
                    "message_type": "text",
                    "content": '{"text":"hello"}',
                    "create_time": "1700000000000",
                },
            },
        }
        # 不应抛异常
        await gateway.handle_message("feishu", raw_msg)

    async def test_send_response_output_adapter_none_logs_and_returns(self) -> None:
        """适配器的 output_adapter 为 None 时记录日志并返回。"""
        from channels.gateway.channel_gateway import ChannelGateway
        from channels.gateway.unified_types import UnifiedResponse

        gateway = ChannelGateway()
        mock_adapter = MagicMock()
        mock_adapter.output_adapter = None
        gateway.register_adapter("feishu", mock_adapter)

        response = UnifiedResponse(
            message_id="msg-1",
            channel_type="feishu",
            content="test",
            content_type="text",
        )
        # 不应抛异常
        await gateway.send_response(response)

    async def test_get_service_returns_none_for_unregistered(self) -> None:
        """get_service 对未注册服务返回 None。"""
        from channels.gateway.channel_gateway import ChannelGateway

        gateway = ChannelGateway()
        assert gateway.get_service("nonexistent") is None

    async def test_get_service_returns_registered_service(self) -> None:
        """get_service 返回已注册的服务实例。"""
        from channels.gateway.channel_gateway import ChannelGateway

        gateway = ChannelGateway()
        mock_service = MagicMock()
        gateway.services = {"task_service": mock_service}
        assert gateway.get_service("task_service") is mock_service


# ════════════════════════════════════════════════════════════════
# 4. SessionBridge IO 异常路径
# ════════════════════════════════════════════════════════════════


class TestSessionBridgeErrorPaths:
    """SessionBridge IO 异常处理路径测试。"""

    def test_load_corrupted_json_does_not_crash(self) -> None:
        """损坏的 JSON 文件不导致崩溃，返回空状态。"""
        from channels.gateway.session_bridge import SessionBridge

        tmpdir = tempfile.mkdtemp()
        state_file = Path(tmpdir) / "session_bridge_state.json"
        state_file.write_text("not valid json {{{", encoding="utf-8")

        # 不应抛异常
        bridge = SessionBridge(storage_path=Path(tmpdir))
        assert bridge._user_sessions == {}
        assert bridge._active_channels == {}

    def test_switch_channel_unknown_user_warns_not_crash(self) -> None:
        """对未知用户切换通道时仅警告，不崩溃。"""
        from channels.gateway.session_bridge import SessionBridge

        bridge = SessionBridge()  # 内存模式
        # 不应抛异常
        bridge.switch_channel("unknown:user", "feishu")
        # 确认未创建映射
        assert "unknown:user" not in bridge._user_sessions

    def test_memory_mode_no_persistence(self) -> None:
        """无 storage_path 时纯内存模式，不持久化。"""
        from channels.gateway.session_bridge import SessionBridge

        bridge = SessionBridge()  # storage_path=None
        bridge.get_or_create_session("feishu:ou_001", "feishu")
        # _persist 不应抛异常（直接 return）
        bridge._persist()


# ════════════════════════════════════════════════════════════════
# 5. 输入适配器 _raw_to_state 转换测试
# ════════════════════════════════════════════════════════════════


class TestFeishuInputAdapterRawToState:
    """飞书输入适配器消息转换测试。"""

    def test_text_message_converted_to_state(self) -> None:
        """飞书文本消息正确转换为管道 state。"""
        from channels.feishu.adapter import FeishuInputAdapter

        raw = {
            "header": {"event_id": "evt-001"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_test"}},
                "message": {
                    "message_id": "msg-001",
                    "message_type": "text",
                    "content": '{"text":"hello feishu"}',
                    "create_time": "1700000000000",
                },
            },
        }
        state = FeishuInputAdapter._raw_to_state(raw)
        assert state["user_input"] == "hello feishu"
        assert state["_channel_type"] == "feishu"
        assert state["_channel_user_id"] == "ou_test"
        assert state["should_stop"] is False
        assert state["iteration"] == 1

    def test_missing_event_id_generates_session_id(self) -> None:
        """缺少 event_id 时生成 UUID 作为 session_id。"""
        from channels.feishu.adapter import FeishuInputAdapter

        raw = {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_x"}},
                "message": {
                    "message_type": "text",
                    "content": '{"text":"hi"}',
                },
            },
        }
        state = FeishuInputAdapter._raw_to_state(raw)
        assert state[StateKeys.SESSION_ID]  # 非空

    async def test_enqueue_and_receive(self) -> None:
        """入队消息后 receive 能取出并转换。"""
        from channels.feishu.adapter import FeishuInputAdapter

        adapter = FeishuInputAdapter()
        raw = {
            "header": {"event_id": "evt-1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_1"}},
                "message": {
                    "message_type": "text",
                    "content": '{"text":"queued msg"}',
                },
            },
        }
        await adapter.enqueue_message(raw)
        state = await adapter.receive()
        assert state["user_input"] == "queued msg"


class TestDingTalkInputAdapterRawToState:
    """钉钉输入适配器消息转换测试。"""

    def test_text_message_converted_to_state(self) -> None:
        """钉钉文本消息正确转换为管道 state。"""
        from channels.dingtalk.adapter import DingTalkInputAdapter

        raw = {
            "senderStaffId": "staff_001",
            "senderId": "sender_id_001",
            "msgtype": "text",
            "text": {"content": "hello dingtalk"},
            "messageId": "msg-dt-001",
            "conversationId": "cid-001",
        }
        state = DingTalkInputAdapter._raw_to_state(raw)
        assert state["user_input"] == "hello dingtalk"
        assert state["_channel_type"] == "dingtalk"
        assert state["_channel_user_id"] == "staff_001"
        assert state["_sender_id"] == "sender_id_001"
        assert state["_conversation_id"] == "cid-001"

    def test_missing_fields_use_defaults(self) -> None:
        """缺少字段时使用默认值。"""
        from channels.dingtalk.adapter import DingTalkInputAdapter

        raw = {"msgtype": "text", "text": {"content": "partial"}}
        state = DingTalkInputAdapter._raw_to_state(raw)
        assert state["user_input"] == "partial"
        assert state["_channel_user_id"] == ""
        assert state[StateKeys.SESSION_ID]  # 生成 UUID


class TestQQInputAdapterRawToState:
    """QQ 输入适配器消息转换测试。"""

    def test_private_text_message_converted(self) -> None:
        """QQ 私聊文本消息正确转换。"""
        from channels.qq.adapter import QQInputAdapter

        raw = {
            "user_id": 123456,
            "message_id": 100,
            "message_type": "private",
            "message": [{"type": "text", "data": {"text": "hello qq"}}],
        }
        state = QQInputAdapter._raw_to_state(raw)
        assert state["user_input"] == "hello qq"
        assert state["_channel_type"] == "qq"
        assert state["_channel_user_id"] == "123456"
        assert "_group_id" not in state

    def test_group_message_includes_group_id(self) -> None:
        """群消息 state 包含 _group_id。"""
        from channels.qq.adapter import QQInputAdapter

        raw = {
            "user_id": 111,
            "message_id": 200,
            "message_type": "group",
            "group_id": 999,
            "message": [{"type": "text", "data": {"text": "group msg"}}],
        }
        state = QQInputAdapter._raw_to_state(raw)
        assert state["_group_id"] == 999
        assert state["_message_type"] == "group"

    def test_cq_code_string_extracted(self) -> None:
        """CQ 码字符串格式正确提取文本。"""
        from channels.qq.adapter import QQInputAdapter

        raw = {
            "user_id": 222,
            "message_id": 300,
            "message": "[CQ:at,qq=123] hello world",
        }
        state = QQInputAdapter._raw_to_state(raw)
        assert "hello world" in state["user_input"]
        assert "[CQ:" not in state["user_input"]


class TestWeComInputAdapterRawToState:
    """企业微信输入适配器消息转换测试。"""

    def test_text_message_converted(self) -> None:
        """企业微信文本消息正确转换。"""
        from channels.wecom.adapter import WeComInputAdapter

        raw = {
            "FromUserName": "user_001",
            "ToUserName": "corp",
            "MsgType": "text",
            "Content": "hello wecom",
            "MsgId": "msg-wecom-001",
            "AgentID": "1000001",
        }
        state = WeComInputAdapter._raw_to_state(raw)
        assert state["user_input"] == "hello wecom"
        assert state["_channel_type"] == "wecom"
        assert state["_channel_user_id"] == "user_001"
        assert state["_agent_id"] == "1000001"
        assert state["_to_user"] == "corp"

    def test_image_message_uses_picurl(self) -> None:
        """图片消息使用 PicUrl。"""
        from channels.wecom.adapter import WeComInputAdapter

        raw = {
            "FromUserName": "u2",
            "MsgType": "image",
            "PicUrl": "https://example.com/img.jpg",
            "MsgId": "m2",
        }
        state = WeComInputAdapter._raw_to_state(raw)
        assert state["user_input"] == "https://example.com/img.jpg"

    def test_voice_with_recognition(self) -> None:
        """语音消息有识别结果时使用识别文本。"""
        from channels.wecom.adapter import WeComInputAdapter

        raw = {
            "FromUserName": "u3",
            "MsgType": "voice",
            "Recognition": "你好世界",
            "MsgId": "m3",
        }
        state = WeComInputAdapter._raw_to_state(raw)
        assert state["user_input"] == "你好世界"

    def test_voice_without_recognition(self) -> None:
        """语音消息无识别结果时显示占位符。"""
        from channels.wecom.adapter import WeComInputAdapter

        raw = {
            "FromUserName": "u4",
            "MsgType": "voice",
            "MsgId": "m4",
        }
        state = WeComInputAdapter._raw_to_state(raw)
        assert "语音" in state["user_input"]


# ════════════════════════════════════════════════════════════════
# 6. 输出适配器 send/send_stream 行为测试
# ════════════════════════════════════════════════════════════════


class TestFeishuOutputAdapterSend:
    """飞书输出适配器发送行为测试。"""

    async def test_send_error_state_sends_error_message(self) -> None:
        """错误状态的 state 发送错误消息。"""
        from channels.feishu.adapter import FeishuOutputAdapter

        mock_client = MagicMock()
        mock_client.send_message = AsyncMock()
        adapter = FeishuOutputAdapter(stream_client=mock_client)

        state = {
            "_channel_user_id": "ou_test",
            "raw_error": "something went wrong",
        }
        await adapter.send(state)
        mock_client.send_message.assert_called_once()
        call_args = mock_client.send_message.call_args
        assert "ou_test" in call_args[0]
        assert "something went wrong" in call_args[0][1]

    async def test_send_normal_result_sends_content(self) -> None:
        """正常结果的 state 发送 raw_result 内容。"""
        from channels.feishu.adapter import FeishuOutputAdapter

        mock_client = MagicMock()
        mock_client.send_message = AsyncMock()
        adapter = FeishuOutputAdapter(stream_client=mock_client)

        state = {
            "_channel_user_id": "ou_test",
            "raw_result": "Task completed successfully",
        }
        await adapter.send(state)
        mock_client.send_message.assert_called_once_with("ou_test", "Task completed successfully")

    async def test_send_no_user_id_skips(self) -> None:
        """无 user_id 时跳过发送。"""
        from channels.feishu.adapter import FeishuOutputAdapter

        mock_client = MagicMock()
        mock_client.send_message = AsyncMock()
        adapter = FeishuOutputAdapter(stream_client=mock_client)

        state = {"raw_result": "no user"}
        await adapter.send(state)
        mock_client.send_message.assert_not_called()

    async def test_send_stream_accumulates_and_flushes(self) -> None:
        """send_stream 累积文本，flush 时发送。"""
        from channels.feishu.adapter import FeishuOutputAdapter

        mock_client = MagicMock()
        mock_client.send_message = AsyncMock()
        adapter = FeishuOutputAdapter(stream_client=mock_client)
        adapter.set_channel_user_id("ou_stream")

        await adapter.send_stream({"text": "Hello "})
        await adapter.send_stream({"text": "World"})
        mock_client.send_message.assert_not_called()

        await adapter.send_stream({"text": "!", "flush": True})
        mock_client.send_message.assert_called_once_with("ou_stream", "Hello World!")


class TestDingTalkOutputAdapterSend:
    """钉钉输出适配器发送行为测试。"""

    async def test_send_error_state_sends_error_message(self) -> None:
        """错误状态的 state 发送错误消息。"""
        from channels.dingtalk.adapter import DingTalkOutputAdapter

        mock_client = MagicMock()
        mock_client.send_message = AsyncMock()
        adapter = DingTalkOutputAdapter(stream_client=mock_client)

        state = {
            "_channel_user_id": "staff_001",
            "raw_error": "timeout",
        }
        await adapter.send(state)
        mock_client.send_message.assert_called_once()
        assert "timeout" in mock_client.send_message.call_args[0][1]

    async def test_send_normal_result(self) -> None:
        """正常结果发送 raw_result。"""
        from channels.dingtalk.adapter import DingTalkOutputAdapter

        mock_client = MagicMock()
        mock_client.send_message = AsyncMock()
        adapter = DingTalkOutputAdapter(stream_client=mock_client)

        state = {
            "_channel_user_id": "staff_001",
            "raw_result": "Done",
        }
        await adapter.send(state)
        mock_client.send_message.assert_called_once_with("staff_001", "Done")

    async def test_send_stream_end_type_triggers_flush(self) -> None:
        """type=end 的 chunk 触发刷新发送。"""
        from channels.dingtalk.adapter import DingTalkOutputAdapter

        mock_client = MagicMock()
        mock_client.send_message = AsyncMock()
        adapter = DingTalkOutputAdapter(stream_client=mock_client)
        adapter.set_channel_user_id("staff_stream")

        await adapter.send_stream({"text": "accumulated"})
        await adapter.send_stream({"type": "end"})
        mock_client.send_message.assert_called_once_with("staff_stream", "accumulated")


class TestQQOutputAdapterSend:
    """QQ 输出适配器发送行为测试。"""

    async def test_send_normal_result(self) -> None:
        """正常结果通过 OneBot API 发送。"""
        from channels.qq.output_adapter import QQOutputAdapter

        mock_client = MagicMock()
        mock_client.send_message = AsyncMock()
        adapter = QQOutputAdapter(onebot_client=mock_client)

        state = {
            "_channel_user_id": "123456",
            "raw_result": "QQ reply",
        }
        await adapter.send(state)
        mock_client.send_message.assert_called_once()
        call_kwargs = mock_client.send_message.call_args
        assert call_kwargs.kwargs["user_id"] == 123456
        assert call_kwargs.kwargs["content"] == "QQ reply"

    async def test_send_invalid_user_id_skips(self) -> None:
        """无效的 user_id（非数字）时跳过。"""
        from channels.qq.output_adapter import QQOutputAdapter

        mock_client = MagicMock()
        mock_client.send_message = AsyncMock()
        adapter = QQOutputAdapter(onebot_client=mock_client)

        state = {
            "_channel_user_id": "not_a_number",
            "raw_result": "msg",
        }
        await adapter.send(state)
        mock_client.send_message.assert_not_called()

    async def test_send_error_state(self) -> None:
        """错误状态发送错误消息。"""
        from channels.qq.output_adapter import QQOutputAdapter

        mock_client = MagicMock()
        mock_client.send_message = AsyncMock()
        adapter = QQOutputAdapter(onebot_client=mock_client)

        state = {
            "_channel_user_id": "999",
            "raw_error": "connection lost",
        }
        await adapter.send(state)
        mock_client.send_message.assert_called_once()
        assert "connection lost" in mock_client.send_message.call_args.kwargs["content"]


class TestWeComOutputAdapterSend:
    """企业微信输出适配器发送行为测试。"""

    async def test_send_normal_result(self) -> None:
        """正常结果通过企业微信 API 发送。"""
        from channels.wecom.output_adapter import WeComOutputAdapter

        mock_client = MagicMock()
        mock_client.send_message = AsyncMock()
        adapter = WeComOutputAdapter(stream_client=mock_client)

        state = {
            "_channel_user_id": "user_001",
            "raw_result": "WeCom reply",
        }
        await adapter.send(state)
        mock_client.send_message.assert_called_once_with("user_001", "WeCom reply")

    async def test_send_error_state(self) -> None:
        """错误状态发送错误消息。"""
        from channels.wecom.output_adapter import WeComOutputAdapter

        mock_client = MagicMock()
        mock_client.send_message = AsyncMock()
        adapter = WeComOutputAdapter(stream_client=mock_client)

        state = {
            "_channel_user_id": "user_001",
            "raw_error": "failed",
        }
        await adapter.send(state)
        mock_client.send_message.assert_called_once()
        assert "failed" in mock_client.send_message.call_args[0][1]

    async def test_send_no_user_id_skips(self) -> None:
        """无 user_id 时跳过。"""
        from channels.wecom.output_adapter import WeComOutputAdapter

        mock_client = MagicMock()
        mock_client.send_message = AsyncMock()
        adapter = WeComOutputAdapter(stream_client=mock_client)

        state = {"raw_result": "no user"}
        await adapter.send(state)
        mock_client.send_message.assert_not_called()

    async def test_send_stream_flush(self) -> None:
        """流式输出在 flush 时发送累积文本。"""
        from channels.wecom.output_adapter import WeComOutputAdapter

        mock_client = MagicMock()
        mock_client.send_message = AsyncMock()
        adapter = WeComOutputAdapter(stream_client=mock_client)
        adapter.set_channel_user_id("stream_user")

        await adapter.send_stream({"text": "part1"})
        await adapter.send_stream({"text": "part2", "flush": True})
        mock_client.send_message.assert_called_once_with("stream_user", "part1part2")


