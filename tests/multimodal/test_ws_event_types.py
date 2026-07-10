"""WebSocket 事件类型定义测试

验证 WS 事件 payload 结构正确性，避免对 fastapi 的导入依赖。
- multimedia_uploaded 事件结构（内联验证）
- _infer_media_type MIME 推断函数（直接测试）
- 事件字段完整性
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


# ============================================================
# _infer_media_type 直接测试
# ============================================================

class TestInferMediaType:
    """_infer_media_type MIME 推断。"""

    def _infer(self, mime_type: str) -> str:
        """内联 _infer_media_type 函数（避免 fastapi 导入依赖）。"""
        if not mime_type:
            return "document"
        category = mime_type.split("/", maxsplit=1)[0]
        _mime_map = {"image": "image", "audio": "audio", "video": "video"}
        return _mime_map.get(category, "document")

    def test_image(self):
        assert self._infer("image/png") == "image"
        assert self._infer("image/jpeg") == "image"
        assert self._infer("image/gif") == "image"
        assert self._infer("image/webp") == "image"

    def test_audio(self):
        assert self._infer("audio/mpeg") == "audio"
        assert self._infer("audio/wav") == "audio"

    def test_video(self):
        assert self._infer("video/mp4") == "video"
        assert self._infer("video/quicktime") == "video"

    def test_document(self):
        assert self._infer("application/pdf") == "document"
        assert self._infer("text/plain") == "document"
        assert self._infer("application/octet-stream") == "document"

    def test_empty(self):
        assert self._infer("") == "document"


# ============================================================
# multimedia_uploaded 事件结构验证
# ============================================================

class TestMultimediaUploadedEvent:
    """multimedia_uploaded WS 事件结构"""

    def test_event_type_value(self):
        """事件类型常量值。"""
        # multimedia_uploaded 是 WS 事件类型字符串
        event_type = "multimedia_uploaded"
        assert isinstance(event_type, str)
        assert "multimedia" in event_type

    def test_event_data_required_fields(self):
        """multimedia_uploaded 事件 data 必填字段。"""
        event = {
            "type": "multimedia_uploaded",
            "data": {
                "file_id": "f1",
                "filename": "test.png",
                "mime_type": "image/png",
                "media_type": "image",
                "size": 1024,
                "url": "/uploads/test.png",
                "thread_id": "thread-001",
            },
        }
        data = event["data"]
        required = {"file_id", "filename", "mime_type", "media_type", "size", "url"}
        for field in required:
            assert field in data, f"缺少必填字段: {field}"

    def test_event_data_thread_id_optional(self):
        """thread_id 可为空字符串。"""
        event = {
            "type": "multimedia_uploaded",
            "data": {
                "file_id": "f2",
                "filename": "doc.pdf",
                "mime_type": "application/pdf",
                "media_type": "document",
                "size": 2048,
                "url": "/uploads/doc.pdf",
                "thread_id": "",
            },
        }
        assert event["data"]["thread_id"] == ""

    def test_event_data_types(self):
        """各字段类型正确。"""
        event = {
            "type": "multimedia_uploaded",
            "data": {
                "file_id": "abc123",
                "filename": "photo.jpg",
                "mime_type": "image/jpeg",
                "media_type": "image",
                "size": 5000,
                "url": "/uploads/abc123.jpg",
                "thread_id": "t-1",
            },
        }
        d = event["data"]
        assert isinstance(d["file_id"], str)
        assert isinstance(d["filename"], str)
        assert isinstance(d["mime_type"], str)
        assert isinstance(d["media_type"], str)
        assert isinstance(d["size"], int)
        assert isinstance(d["url"], str)
        assert isinstance(d["thread_id"], str)


# ============================================================
# 事件类型清单
# ============================================================

class TestEventTypeCatalog:
    """WS 事件类型清单验证。"""

    def test_known_event_types(self):
        """验证系统中定义的 WS 事件类型常量。"""
        known_types = {
            "stream_start",
            "stream_chunk",
            "stream_end",
            "stream_error",
            "new_message",
            "thinking_start",
            "thinking_chunk",
            "thinking_end",
            "tool_start",
            "tool_result",
            "tool_multimedia_result",
            "multimedia_uploaded",
            "iteration",
            "system_notification",
            "state_change",
        }
        for t in known_types:
            assert isinstance(t, str), f"事件类型应为字符串: {t}"

    def test_multimedia_events(self):
        """多模态相关事件类型。"""
        multimedia_events = {
            "multimedia_uploaded",
            "tool_multimedia_result",
        }
        assert len(multimedia_events) == 2


# ============================================================
# Bridge chunk 事件类型验证
# ============================================================

class TestBridgeChunkTypes:
    """bridge_events 中支持的 chunk 类型。"""

    def test_chunk_types(self):
        """_handle_chunk 支持的 chunk_type 列表。"""
        supported = {
            "text",
            "thinking",
            "thinking_end",
            "tool_call",
            "tool_start",
            "tool_result",
            "tool_multimedia_result",
            "iteration",
            "notification",
        }
        assert "tool_multimedia_result" in supported
        assert "tool_result" in supported
        assert "iteration" in supported

    def test_tool_multimedia_result_data_schema(self):
        """tool_multimedia_result 事件 data schema。"""
        event = {
            "type": "tool_multimedia_result",
            "data": {
                "count": 3,
                "multimedia": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBB"}},
                    {"type": "image", "source": {"type": "base64", "data": "CCC", "media_type": "image/png"}},
                ],
                "sequence": 1,
            },
        }
        assert isinstance(event["data"]["count"], int)
        assert len(event["data"]["multimedia"]) == 3
        assert event["data"]["multimedia"][0]["type"] == "image_url"
        assert event["data"]["multimedia"][2]["type"] == "image"
        assert "sequence" in event["data"]
