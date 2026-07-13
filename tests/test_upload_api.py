"""文件上传 API + multimedia_uploaded WS 事件测试。

覆盖场景：
- POST /api/v1/artifacts/upload 上传文件，返回 artifact_id + url
- 返回结构包含 file_id, filename, mime_type, media_type, size, url
- WS 事件 multimedia_uploaded 在上传成功后推送
- 不同类型文件正确推断 media_type
"""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app(tmp_path, monkeypatch):
    """创建测试用 FastAPI 应用实例。"""
    monkeypatch.setenv("MULTIMODAL_STORAGE_DIR", str(tmp_path / "meta"))
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))

    # Mock 不可用的外部依赖，避免 create_app 导入链中断
    import sys
    import types

    for mod_name in ("av", "cv2", "litellm", "PIL", "PIL.Image"):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)

    from channels.api.app import create_app

    return create_app()


@pytest.fixture
def client(app):
    """创建 TestClient，跳过认证。

    使用 yield 保持 patch 在整个测试方法执行期间活跃。
    """
    with patch("channels.api.deps.get_current_user") as mock_auth:
        mock_auth.return_value = {"sub": "test-user-001", "username": "tester"}
        tc = TestClient(app)
        yield tc


@pytest.fixture
def mock_ws_notifier():
    """Mock ws_interaction_notifier 的 send_to_user。"""
    with patch(
        "channels.websocket.ws_handler.ws_interaction_notifier"
    ) as mock_notifier:
        mock_notifier.send_to_user = AsyncMock(return_value=True)
        yield mock_notifier


class TestUploadEndpoint:
    """POST /api/v1/artifacts/upload 端点测试。"""

    def test_upload_image_returns_artifact_info(self, client, mock_ws_notifier):
        """上传图片文件，返回完整 artifact 信息。"""
        img_content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        files = {"file": ("test.png", io.BytesIO(img_content), "image/png")}

        resp = client.post(
            "/api/v1/artifacts/upload",
            files=files,
            headers={"Authorization": "Bearer fake-token"},
        )

        assert resp.status_code == 200
        body = resp.json()

        assert "file_id" in body
        assert len(body["file_id"]) > 0
        assert body["filename"] == "test.png"
        assert body["mime_type"] == "image/png"
        assert body["media_type"] == "image"
        assert body["size"] == len(img_content)
        assert "url" in body
        assert body["url"].startswith("/uploads/")

    def test_upload_triggers_ws_event(self, client, mock_ws_notifier):
        """上传成功后推送 multimedia_uploaded WS 事件。"""
        img_content = b"\x89PNG" + b"\x00" * 10
        files = {"file": ("photo.png", io.BytesIO(img_content), "image/png")}

        client.post(
            "/api/v1/artifacts/upload",
            files=files,
            headers={"Authorization": "Bearer fake-token"},
        )

        mock_ws_notifier.send_to_user.assert_called_once()
        call_args = mock_ws_notifier.send_to_user.call_args
        event = call_args[0][1]

        assert event["type"] == "multimedia_uploaded"
        assert "file_id" in event["data"]
        assert event["data"]["filename"] == "photo.png"
        assert event["data"]["media_type"] == "image"

    def test_upload_jpeg_infers_image(self, client, mock_ws_notifier):
        """JPEG 文件正确推断为 image 类型。"""
        files = {"file": ("photo.jpg", io.BytesIO(b"\xff\xd8\xff\xe0"), "image/jpeg")}

        resp = client.post(
            "/api/v1/artifacts/upload",
            files=files,
            headers={"Authorization": "Bearer fake-token"},
        )

        assert resp.status_code == 200
        assert resp.json()["media_type"] == "image"

    def test_upload_pdf_infers_document(self, client, mock_ws_notifier):
        """PDF 文件正确推断为 document 类型。"""
        files = {"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")}

        resp = client.post(
            "/api/v1/artifacts/upload",
            files=files,
            headers={"Authorization": "Bearer fake-token"},
        )

        assert resp.status_code == 200
        assert resp.json()["media_type"] == "document"

    def test_upload_with_thread_id_in_event(self, client, mock_ws_notifier):
        """上传时带 thread_id，WS 事件包含该字段。"""
        files = {"file": ("t.png", io.BytesIO(b"\x89PNG"), "image/png")}
        data = {"thread_id": "thread-abc-123"}

        client.post(
            "/api/v1/artifacts/upload",
            files=files,
            data=data,
            headers={"Authorization": "Bearer fake-token"},
        )

        call_args = mock_ws_notifier.send_to_user.call_args
        event = call_args[0][1]
        assert event["data"]["thread_id"] == "thread-abc-123"

    def test_upload_mp4_infers_video(self, client, mock_ws_notifier):
        """MP4 文件正确推断为 video 类型。"""
        files = {"file": ("clip.mp4", io.BytesIO(b"\x00\x00\x00 ftyp"), "video/mp4")}

        resp = client.post(
            "/api/v1/artifacts/upload",
            files=files,
            headers={"Authorization": "Bearer fake-token"},
        )

        assert resp.status_code == 200
        assert resp.json()["media_type"] == "video"

    def test_upload_mp3_infers_audio(self, client, mock_ws_notifier):
        """MP3 文件正确推断为 audio 类型。"""
        files = {"file": ("song.mp3", io.BytesIO(b"ID3\x03\x00"), "audio/mpeg")}

        resp = client.post(
            "/api/v1/artifacts/upload",
            files=files,
            headers={"Authorization": "Bearer fake-token"},
        )

        assert resp.status_code == 200
        assert resp.json()["media_type"] == "audio"
