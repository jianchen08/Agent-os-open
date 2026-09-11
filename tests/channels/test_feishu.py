# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""飞书通道适配器测试。

测试 FeishuInputAdapter、FeishuOutputAdapter、FeishuAdapter 组合与
FeishuStreamClient（Mock）的核心功能。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit  # 0.2 TDD 分层：单元测试

from tests.channels.conftest import use_channel

use_channel("feishu")
from adapter import FeishuAdapter, FeishuInputAdapter, FeishuOutputAdapter
from stream_client import FeishuStreamClient

# ═══════════════════════════════════════════════════════════
# FeishuInputAdapter 测试
# ═══════════════════════════════════════════════════════════


class TestFeishuInputAdapter:
    """FeishuInputAdapter 输入适配器测试。"""

    @pytest.mark.asyncio
    async def test_receive_text_message(self) -> None:
        """飞书文本事件经 enqueue→receive 得到管道信封。"""
        adapter = FeishuInputAdapter()
        raw_msg = {
            "header": {"event_id": "evt-1", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_test"}},
                "message": {
                    "message_id": "msg-1",
                    "message_type": "text",
                    "content": '{"text":"hello feishu"}',
                    "create_time": "1700000000000",
                },
            },
        }
        await adapter.enqueue_message(raw_msg)
        state = await adapter.receive()
        assert state["user_input"] == "hello feishu"
        assert state["_channel_type"] == "feishu"
        assert state["_channel_user_id"] == "ou_test"
        assert state["session_id"] == "evt-1"

    @pytest.mark.asyncio
    async def test_receive_empty_queue(self) -> None:
        """空队列时 receive 阻塞（使用 asyncio.wait_for 超时验证）。"""
        adapter = FeishuInputAdapter()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.receive(), timeout=0.1)

    @pytest.mark.asyncio
    async def test_enqueue_and_receive(self) -> None:
        """enqueue_message 后 receive 能取出。"""
        adapter = FeishuInputAdapter()
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
        await adapter.enqueue_message(raw_msg)
        state = await adapter.receive()
        assert state["user_input"] == "hello"
        assert state["_channel_type"] == "feishu"


# ═══════════════════════════════════════════════════════════
# FeishuOutputAdapter 测试
# ═══════════════════════════════════════════════════════════


class TestFeishuOutputAdapter:
    """FeishuOutputAdapter 输出适配器测试。"""

    @pytest.mark.asyncio
    async def test_send_normal_result(self) -> None:
        """发送正常结果。"""
        client = AsyncMock(spec=FeishuStreamClient)
        adapter = FeishuOutputAdapter(stream_client=client)
        state = {
            "raw_result": "Hello result",
            "_channel_user_id": "ou_test",
            "ended": True,
        }
        await adapter.send(state)
        # FeishuStreamClient 是对外部飞书平台的边界：一条结果恰发一条消息
        # （含收件人与正文）即输出适配器的契约，交互本身就是行为。
        client.send_message.assert_called_once()
        call_args = client.send_message.call_args
        assert call_args[0][0] == "ou_test"
        assert call_args[0][1] == "Hello result"

    @pytest.mark.asyncio
    async def test_send_error(self) -> None:
        """发送错误信息。"""
        client = AsyncMock(spec=FeishuStreamClient)
        adapter = FeishuOutputAdapter(stream_client=client)
        state = {
            "raw_error": "Something went wrong",
            "_channel_user_id": "ou_test",
        }
        await adapter.send(state)
        # 错误信息必须送达用户（外部边界交互即契约）
        client.send_message.assert_called_once()
        assert "Something went wrong" in client.send_message.call_args[0][1]

    @pytest.mark.asyncio
    async def test_send_no_user_id(self) -> None:
        """无 user_id 时跳过发送。"""
        client = AsyncMock(spec=FeishuStreamClient)
        adapter = FeishuOutputAdapter(stream_client=client)
        state = {
            "raw_result": "Hello",
            "_channel_user_id": "",
        }
        await adapter.send(state)
        client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_stream_accumulate(self) -> None:
        """流式累积文本。"""
        client = AsyncMock(spec=FeishuStreamClient)
        adapter = FeishuOutputAdapter(stream_client=client)
        adapter.set_channel_user_id("ou_test")

        chunk1 = {"text": "Hello ", "type": "token"}
        chunk2 = {"text": "World", "type": "token"}
        await adapter.send_stream(chunk1)
        await adapter.send_stream(chunk2)
        # 流式消息应累积但不发送（公共只读观察面）
        assert adapter.accumulated_text() == "Hello World"
        client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_stream_flush(self) -> None:
        """flush 标记触发发送。"""
        client = AsyncMock(spec=FeishuStreamClient)
        adapter = FeishuOutputAdapter(stream_client=client)
        adapter.set_channel_user_id("ou_test")

        chunk = {"text": "Hello", "type": "token", "flush": True}
        await adapter.send_stream(chunk)
        # flush 恰好触发一次对外发送（外部边界交互即契约）
        client.send_message.assert_called_once()
        assert client.send_message.call_args[0][1] == "Hello"
        # 发送后累积文本清空（公共只读观察面）
        assert adapter.accumulated_text() == ""


# ═══════════════════════════════════════════════════════════
# FeishuAdapter 组合测试
# ═══════════════════════════════════════════════════════════


class TestFeishuAdapter:
    """FeishuAdapter 组合模式测试。"""

    def test_adapter_initialization(self) -> None:
        """验证组件初始化和回调绑定。"""
        adapter = FeishuAdapter(app_id="test_id", app_secret="test_secret")
        assert adapter.input_adapter is not None
        assert adapter.output_adapter is not None
        assert adapter.stream_client is not None
        # 验证回调绑定（绑定方法用 == 而非 is）
        assert adapter.stream_client.on_message == adapter.input_adapter.enqueue_message

    def test_channel_type(self) -> None:
        """channel_type 属性为 'feishu'。"""
        adapter = FeishuAdapter(app_id="test_id", app_secret="test_secret")
        assert adapter.channel_type == "feishu"

    @pytest.mark.asyncio
    async def test_start(self) -> None:
        """测试启动适配器。"""
        adapter = FeishuAdapter(app_id="test_id", app_secret="test_secret")
        adapter.stream_client.connect = AsyncMock()
        await adapter.start()
        # start 的对外行为就是与平台建立一次连接（外部边界交互即契约）
        adapter.stream_client.connect.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop(self) -> None:
        """测试停止适配器。"""
        adapter = FeishuAdapter(app_id="test_id", app_secret="test_secret")
        adapter.stream_client.disconnect = AsyncMock()
        await adapter.stop()
        # stop 的对外行为就是断开与平台的连接（外部边界交互即契约）
        adapter.stream_client.disconnect.assert_called_once()


# ═══════════════════════════════════════════════════════════
# FeishuStreamClient 测试（Mock）
# ═══════════════════════════════════════════════════════════


class TestFeishuStreamClient:
    """FeishuStreamClient 测试（Mock 外部调用）。"""

    def test_init(self) -> None:
        """测试初始化：未连接状态可观察；app_id/app_secret 的生效由
        取 token 报文体与发送鉴权头的行为断言承载（见 stream_client 测试）。"""
        client = FeishuStreamClient(app_id="test_id", app_secret="test_secret")
        assert client.is_connected is False

    @pytest.mark.asyncio
    async def test_send_message_calls_api(self) -> None:
        """验证 send_message 的 API 调用格式。"""
        client = FeishuStreamClient(app_id="test_id", app_secret="test_secret")

        # Mock _ensure_token 避免 token 刷新请求
        client._ensure_token = AsyncMock()
        client._tenant_token = "test_token"

        # Mock session
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"code": 0, "msg": "ok"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.closed = False
        client._session = mock_session

        await client.send_message("ou_test", "Hello")

        # aiohttp session 是对外部飞书 API 的网络边界：一次请求 + 报文格式即契约
        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        url = call_args[0][0]
        assert "open-apis/im/v1/messages" in url
        body = call_args[1]["json"]
        assert body["receive_id"] == "ou_test"
        assert body["msg_type"] == "text"
        headers = call_args[1]["headers"]
        assert "Bearer test_token" in headers["Authorization"]

    @pytest.mark.asyncio
    async def test_connect_gets_endpoint(self) -> None:
        """验证 connect 流程中获取 endpoint。"""
        client = FeishuStreamClient(
            app_id="test_id", app_secret="test_secret", max_retries=1
        )

        # Mock _ensure_token
        client._ensure_token = AsyncMock()
        # Mock _get_endpoint 返回空字符串（触发重试，然后退出）
        client._get_endpoint = AsyncMock(return_value="")

        # connect 在 max_retries=1 时会因获取不到 endpoint 而退出
        await client.connect()
        # 可观察行为：拿不到 endpoint → 干净退出（不抛出）且连接未建立。
        # _ensure_token/_get_endpoint 的 mock 仅为隔离外部 token/endpoint 网络请求。
        assert client.is_connected is False
