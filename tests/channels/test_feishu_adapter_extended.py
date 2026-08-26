# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""channel_feishu adapter 补充测试（A5.2 补）。

覆盖既有测试未达的 _extract_text 边界分支、_raw_to_state 的
无 header / 无 event 兜底、send 的 user_id 回退与空结果路径、
send_stream 的 end 标记与空累积路径。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

from tests.channels.conftest import use_channel

use_channel("feishu")
from adapter import FeishuInputAdapter, FeishuOutputAdapter, _extract_text
from stream_client import FeishuStreamClient


class TestExtractText:
    """_extract_text 文本提取边界。"""

    def test_text_type_returns_parsed_text(self) -> None:
        assert _extract_text("text", '{"text": "hello"}') == "hello"

    def test_post_type_falls_back_to_parsed_text(self) -> None:
        assert _extract_text("post", '{"text": "post body"}') == "post body"

    def test_invalid_json_returns_raw_content(self) -> None:
        assert _extract_text("text", "not-json") == "not-json"

    def test_none_content_returns_none(self) -> None:
        assert _extract_text("text", None) is None

    def test_text_type_missing_key_returns_empty(self) -> None:
        assert _extract_text("text", '{"title": "x"}') == ""

    def test_other_type_missing_key_returns_raw_content(self) -> None:
        assert _extract_text("image", '{"image_key": "k"}') == '{"image_key": "k"}'


class TestRawToStateFallbacks:
    """_raw_to_state 的兜底路径。"""

    def test_no_header_generates_session_id(self) -> None:
        state = FeishuInputAdapter._raw_to_state(
            {"event": {"message": {"message_type": "text", "content": '{"text":"hi"}'}}}
        )
        assert state["user_input"] == "hi"
        # 无 header 时 session_id 为 12 位 hex
        assert len(state["session_id"]) == 12
        assert all(c in "0123456789abcdef" for c in state["session_id"])

    def test_raw_without_event_uses_raw_as_event(self) -> None:
        state = FeishuInputAdapter._raw_to_state(
            {
                "header": {"event_id": "evt-9"},
                "sender": {"sender_id": {"open_id": "ou_x"}},
                "message": {"message_type": "text", "content": '{"text":"direct"}'},
            }
        )
        assert state["user_input"] == "direct"
        assert state["_channel_user_id"] == "ou_x"
        assert state["session_id"] == "evt-9"

    def test_missing_sender_and_message_defaults(self) -> None:
        state = FeishuInputAdapter._raw_to_state({"header": {"event_id": "evt-0"}})
        assert state["user_input"] == ""
        assert state["_channel_user_id"] == ""
        assert state["session_id"] == "evt-0"


class TestOutputSendFallbacks:
    """FeishuOutputAdapter.send 的 user_id 回退与空结果路径。"""

    @pytest.mark.asyncio
    async def test_send_uses_fallback_user_id(self) -> None:
        client = AsyncMock(spec=FeishuStreamClient)
        adapter = FeishuOutputAdapter(stream_client=client)
        adapter.set_channel_user_id("ou_fallback")
        await adapter.send({"raw_result": "via fallback"})
        client.send_message.assert_awaited_once_with("ou_fallback", "via fallback")

    @pytest.mark.asyncio
    async def test_send_empty_result_skips(self) -> None:
        client = AsyncMock(spec=FeishuStreamClient)
        adapter = FeishuOutputAdapter(stream_client=client)
        adapter.set_channel_user_id("ou_1")
        await adapter.send({"raw_result": ""})
        client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_error_precedes_result(self) -> None:
        client = AsyncMock(spec=FeishuStreamClient)
        adapter = FeishuOutputAdapter(stream_client=client)
        adapter.set_channel_user_id("ou_1")
        await adapter.send({"raw_result": "ok", "raw_error": "boom"})
        client.send_message.assert_awaited_once()
        assert "boom" in client.send_message.await_args[0][1]


class TestOutputSendStreamEnd:
    """send_stream 的 end 标记与空累积路径。"""

    @pytest.mark.asyncio
    async def test_end_marker_flushes_accumulated(self) -> None:
        client = AsyncMock(spec=FeishuStreamClient)
        adapter = FeishuOutputAdapter(stream_client=client)
        adapter.set_channel_user_id("ou_1")
        await adapter.send_stream({"text": "final", "type": "end"})
        client.send_message.assert_awaited_once_with("ou_1", "final")
        assert adapter._accumulated_text == ""

    @pytest.mark.asyncio
    async def test_flush_without_user_id_skips(self) -> None:
        client = AsyncMock(spec=FeishuStreamClient)
        adapter = FeishuOutputAdapter(stream_client=client)
        await adapter.send_stream({"text": "orphan", "flush": True})
        client.send_message.assert_not_called()
        # 无 user_id 时累积文本保留
        assert adapter._accumulated_text == "orphan"

    @pytest.mark.asyncio
    async def test_flush_with_empty_accumulation_skips(self) -> None:
        client = AsyncMock(spec=FeishuStreamClient)
        adapter = FeishuOutputAdapter(stream_client=client)
        adapter.set_channel_user_id("ou_1")
        await adapter.send_stream({"text": "", "flush": True})
        client.send_message.assert_not_called()
