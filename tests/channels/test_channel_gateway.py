# @feature: FP-0.2.七 路由收敛 | @vision: V3 可嵌入 | @audit: T5#10 | @ci: python-coverage
"""ChannelGateway 网关主入口测试。

行为契约（fail-closed）：handle_message/send_response 返回真实结果——
成功带 handled/sent 标记，失败返回结构化 error 值或上抛异常，
调用方必须能区分"已处理"与"已丢弃"。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit  # 0.2 TDD 分层：单元测试

from tests.channels.conftest import use_channel

use_channel("gateway")
from channel_gateway import ChannelGateway
from unified_types import UnifiedResponse


def _feishu_text_event(content: str = "hello gateway", event_id: str = "evt-1") -> dict[str, Any]:
    """构造标准飞书文本消息事件。"""
    return {
        "header": {"event_id": event_id, "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_test"}},
            "message": {
                "message_id": f"msg-{event_id}",
                "message_type": "text",
                "content": '{"text":"' + content + '"}',
                "create_time": "1700000000000",
            },
        },
    }


def _unified_response(channel: str = "feishu") -> UnifiedResponse:
    return UnifiedResponse(
        message_id="msg-001",
        channel_type=channel,
        content="Response text",
        content_type="text",
        card_config=None,
        metadata={},
    )


class TestRegistration:
    @pytest.mark.asyncio
    async def test_register_adapter_routes_to_registered_output(self) -> None:
        """注册后该渠道响应经其输出适配器投递（注册行为经路由可观察）。"""
        gateway = ChannelGateway()
        adapter = MagicMock()
        adapter.channel_type = "feishu"
        adapter.output_adapter = AsyncMock()
        gateway.register_adapter("feishu", adapter)

        result = await gateway.send_response(_unified_response())

        assert result["sent"] is True
        adapter.output_adapter.send.assert_awaited_once()

    def test_register_duplicate_adapter_raises(self) -> None:
        gateway = ChannelGateway()
        mock_adapter = MagicMock()
        gateway.register_adapter("feishu", mock_adapter)
        with pytest.raises(ValueError, match="already registered"):
            gateway.register_adapter("feishu", mock_adapter)


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start(self) -> None:
        """启动所有适配器——断言适配器进入 started 状态（可观察结果，非内部调用次数）。"""
        gateway = ChannelGateway()
        states: dict[str, bool] = {}

        def make_adapter(name: str) -> MagicMock:
            adapter = MagicMock()
            adapter.channel_type = name

            async def _start() -> None:
                states[name] = True

            adapter.start = AsyncMock(side_effect=_start)
            return adapter

        gateway.register_adapter("feishu", make_adapter("feishu"))
        gateway.register_adapter("dingtalk", make_adapter("dingtalk"))

        await gateway.start()

        assert states.get("feishu") is True, "feishu 适配器应已启动"
        assert states.get("dingtalk") is True, "dingtalk 适配器应已启动"

    @pytest.mark.asyncio
    async def test_stop(self) -> None:
        """停止适配器——断言适配器进入 stopped 状态（可观察结果）。"""
        gateway = ChannelGateway()
        states: dict[str, bool] = {}
        mock_adapter = MagicMock()
        mock_adapter.channel_type = "feishu"

        async def _stop() -> None:
            states["feishu"] = True

        mock_adapter.stop = AsyncMock(side_effect=_stop)

        gateway.register_adapter("feishu", mock_adapter)
        await gateway.stop()
        assert states.get("feishu") is True, "feishu 适配器应已停止"


class TestHandleMessageOutcome:
    """handle_message 返回真实处理结果（fail-closed 契约）。"""

    @pytest.mark.asyncio
    async def test_success_reports_handled_true(self) -> None:
        """消息经标准化并送达管道回调后，结果必须如实标记 handled=True。"""
        gateway = ChannelGateway()
        handler = AsyncMock()
        gateway.on_pipeline_request = handler

        result = await gateway.handle_message("feishu", _feishu_text_event())

        assert result["handled"] is True
        # 同一行为第二组输入：钉钉渠道同契约
        result_dt = await gateway.handle_message(
            "dingtalk",
            {
                "msgtype": "text",
                "text": {"content": "hi"},
                "senderStaffId": "u1",
                "messageId": "m1",
            },
        )
        assert result_dt["handled"] is True
        assert handler.called

    @pytest.mark.asyncio
    async def test_handle_message_builds_channel_state_envelope(self) -> None:
        """标准化产物的管道初始 state 信封：输入、渠道与统一用户标识。"""
        gateway = ChannelGateway()
        states: list[dict[str, Any]] = []

        async def _handler(state: dict[str, Any]) -> None:
            states.append(dict(state))

        gateway.on_pipeline_request = _handler

        await gateway.handle_message("feishu", _feishu_text_event("hello envelope"))

        state = states[0]
        assert state["user_input"] == "hello envelope"
        assert state["_channel_type"] == "feishu"
        assert state["_channel_user_id"] == "ou_test"
        assert state["_unified_user_id"] == "feishu:ou_test"

    @pytest.mark.asyncio
    async def test_unsupported_channel_returns_failure_value(self) -> None:
        """不支持渠道的消息被丢弃时，调用方拿到失败值而非假成功。"""
        gateway = ChannelGateway()
        handler = AsyncMock()
        gateway.on_pipeline_request = handler

        result = await gateway.handle_message("slack", {"data": "test"})

        assert result["handled"] is False
        assert result["error"]
        handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_pipeline_handler_reports_error(self) -> None:
        """管道未接线：消息无法进管道，结果必须是明确 error 而非 handled=True。"""
        gateway = ChannelGateway()  # 不设置 on_pipeline_request

        result = await gateway.handle_message("feishu", _feishu_text_event())

        assert result["handled"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_pipeline_callback_exception_propagates(self) -> None:
        """管道回调失败属于处理失败：异常传播给调用方，不得吞成静默丢弃。"""
        gateway = ChannelGateway()

        async def _boom(state: dict[str, Any]) -> None:
            raise RuntimeError("pipeline crashed")

        gateway.on_pipeline_request = _boom

        with pytest.raises(RuntimeError, match="pipeline crashed"):
            await gateway.handle_message("feishu", _feishu_text_event())


class TestSendResponseOutcome:
    """send_response 返回真实发送结果（fail-closed 契约）。"""

    @pytest.mark.asyncio
    async def test_success_reports_sent_true(self) -> None:
        gateway = ChannelGateway()
        sent_states: list[dict[str, Any]] = []

        class _Output:
            async def send(self, state: dict[str, Any]) -> None:
                sent_states.append(dict(state))

        adapter = MagicMock()
        adapter.output_adapter = _Output()
        gateway.register_adapter("feishu", adapter)

        result = await gateway.send_response(_unified_response())

        assert result["sent"] is True
        assert sent_states
        assert sent_states[0]["raw_result"] == "Response text"

    @pytest.mark.asyncio
    async def test_no_adapter_returns_failure_value(self) -> None:
        """目标渠道无适配器 = 未发送：返回结构化失败值而非 sent=True。"""
        gateway = ChannelGateway()

        result = await gateway.send_response(_unified_response(channel="nonexistent"))

        assert result["sent"] is False
        assert "nonexistent" in result["error"]

    @pytest.mark.asyncio
    async def test_output_send_failure_propagates(self) -> None:
        """底层渠道发送失败：异常传播，管道链路上游可感知，不得吞成假成功。"""
        gateway = ChannelGateway()

        class _BrokenOutput:
            async def send(self, state: dict[str, Any]) -> None:
                raise RuntimeError("channel api failed")

        adapter = MagicMock()
        adapter.output_adapter = _BrokenOutput()
        gateway.register_adapter("feishu", adapter)

        with pytest.raises(RuntimeError, match="channel api failed"):
            await gateway.send_response(_unified_response())
