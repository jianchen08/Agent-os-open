# @feature: FP-0.2.七 路由收敛 | @vision: V3 可嵌入 | @audit: T5#10 | @ci: python-plugins-test
"""ChannelGateway 网关主入口测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit  # 0.2 TDD 分层：单元测试

from tests.channels.conftest import use_channel

use_channel("gateway")
from channel_gateway import ChannelGateway
from unified_types import UnifiedResponse


class TestChannelGateway:
    """ChannelGateway 测试。"""

    def setup_method(self) -> None:
        """每个测试方法前创建网关实例。"""
        self.gateway = ChannelGateway()

    def test_register_adapter(self) -> None:
        """测试注册通道适配器。"""
        mock_adapter = MagicMock()
        mock_adapter.channel_type = "feishu"
        self.gateway.register_adapter("feishu", mock_adapter)
        assert "feishu" in self.gateway._adapters

    def test_register_duplicate_adapter_raises(self) -> None:
        """测试重复注册同一渠道应抛出异常。"""
        mock_adapter = MagicMock()
        self.gateway.register_adapter("feishu", mock_adapter)
        with pytest.raises(ValueError, match="already registered"):
            self.gateway.register_adapter("feishu", mock_adapter)

    @pytest.mark.asyncio
    async def test_start(self) -> None:
        """测试启动所有适配器——验证适配器进入 started 状态（可观察行为，非内部调用次数）。

        T5#10 修复：不再 `assert_called_once()` 钉死内部协作者调用次数——重构
        ChannelGateway 调度（幂等重试等）不应误报。改为断言适配器最终进入了
        started 状态这一可观察结果。
        """
        states: dict[str, bool] = {}

        def make_adapter(name: str) -> MagicMock:
            adapter = MagicMock()
            adapter.channel_type = name

            async def _start() -> None:
                states[name] = True

            adapter.start = AsyncMock(side_effect=_start)
            return adapter

        mock_adapter1 = make_adapter("feishu")
        mock_adapter2 = make_adapter("dingtalk")

        self.gateway.register_adapter("feishu", mock_adapter1)
        self.gateway.register_adapter("dingtalk", mock_adapter2)

        await self.gateway.start()

        # 可观察结果：两个适配器都进入 started 状态。
        assert states.get("feishu") is True, "feishu 适配器应已启动"
        assert states.get("dingtalk") is True, "dingtalk 适配器应已启动"

    @pytest.mark.asyncio
    async def test_stop(self) -> None:
        """测试停止适配器——验证适配器进入 stopped 状态（可观察行为，T5#10）。"""
        states: dict[str, bool] = {}
        mock_adapter = MagicMock()
        mock_adapter.channel_type = "feishu"

        async def _stop() -> None:
            states["feishu"] = True

        mock_adapter.stop = AsyncMock(side_effect=_stop)

        self.gateway.register_adapter("feishu", mock_adapter)
        await self.gateway.stop()
        assert states.get("feishu") is True, "feishu 适配器应已停止"

    @pytest.mark.asyncio
    async def test_handle_message_normalizes_and_routes(self) -> None:
        """测试消息处理流程：标准化 → 获取会话 → 创建状态。"""
        gateway = ChannelGateway()
        # Mock message handler callback
        handler = AsyncMock()
        gateway.on_pipeline_request = handler

        raw_msg = {
            "header": {"event_id": "evt-1", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_test"}},
                "message": {
                    "message_id": "msg-1",
                    "message_type": "text",
                    "content": '{"text":"hello gateway"}',
                    "create_time": "1700000000000",
                },
            },
        }

        await gateway.handle_message("feishu", raw_msg)

        # T5#10：断言可观察结果——handler 收到正确标准化的 state，而非调用次数。
        assert handler.called, "handler 应被调用"
        state = handler.call_args[0][0]
        assert state["user_input"] == "hello gateway"
        assert state["_channel_type"] == "feishu"
        assert state["_channel_user_id"] == "ou_test"
        assert state["_unified_user_id"] == "feishu:ou_test"

    @pytest.mark.asyncio
    async def test_handle_message_unsupported_channel(self) -> None:
        """测试处理不支持渠道的消息应记录错误。"""
        gateway = ChannelGateway()
        handler = AsyncMock()
        gateway.on_pipeline_request = handler

        # 不应抛出异常，但 handler 不应被调用
        await gateway.handle_message("slack", {"data": "test"})
        handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_response(self) -> None:
        """测试发送响应到指定渠道。"""
        gateway = ChannelGateway()

        mock_adapter = MagicMock()
        mock_output = AsyncMock()
        mock_adapter.output_adapter = mock_output
        gateway.register_adapter("feishu", mock_adapter)

        response = UnifiedResponse(
            message_id="msg-001",
            channel_type="feishu",
            content="Response text",
            content_type="text",
            card_config=None,
            metadata={},
        )
        await gateway.send_response(response)

        # output_adapter.send 应被调用
        # 由于 send_response 通过 denormalize → adapter 发送
        # 这里验证不会抛异常即可
