"""截图 base64 返回测试 - 验证截图结果包含 base64_data 和 mime_type"""

import base64
import os
import tempfile
from unittest.mock import MagicMock


from tools.builtin.playwright_test.screenshot import ScreenshotManager


class TestScreenshotBase64:
    """截图返回 base64 编码数据"""

    def _make_mock_page(self, screenshot_bytes: bytes | None = b"fake-png-data"):
        """创建 mock page 对象"""
        page = MagicMock()
        page.screenshot.return_value = None  # screenshot(path=...) 写文件不返回数据

        def fake_screenshot(path=None, full_page=False):
            if path:
                with open(path, "wb") as f:
                    f.write(screenshot_bytes or b"")

        page.screenshot.side_effect = fake_screenshot
        return page

    def test_full_page_returns_base64_data(self):
        page = self._make_mock_page(b"\x89PNG\r\n\x1a\nfake_image_data")
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "test.png")
            result = ScreenshotManager.capture_full_page(page, output_path)

        assert result["success"] is True
        assert "base64_data" in result
        assert result["mime_type"] == "image/png"
        # 验证 base64 可解码
        decoded = base64.b64decode(result["base64_data"])
        assert decoded == b"\x89PNG\r\n\x1a\nfake_image_data"

    def test_full_page_base64_matches_file(self):
        test_data = b"\x89PNG test data for consistency check"
        page = self._make_mock_page(test_data)
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "consistency.png")
            result = ScreenshotManager.capture_full_page(page, output_path)

            # 文件内容和 base64 解码后一致
            with open(output_path, "rb") as f:
                file_data = f.read()
            decoded = base64.b64decode(result["base64_data"])
            assert decoded == file_data

    def test_element_returns_base64_data(self):
        element_mock = MagicMock()
        element_mock.screenshot.return_value = None

        def fake_element_screenshot(path=None):
            if path:
                with open(path, "wb") as f:
                    f.write(b"element_screenshot_data")

        element_mock.screenshot.side_effect = fake_element_screenshot

        page = MagicMock()
        locator = MagicMock()
        locator.first = element_mock
        locator.wait_for.return_value = None
        page.locator.return_value = locator

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "element.png")
            result = ScreenshotManager.capture_element(page, "#btn", output_path)

        assert result["success"] is True
        assert "base64_data" in result
        assert result["mime_type"] == "image/png"

    def test_full_page_auto_path(self):
        """不指定路径时自动生成临时文件"""
        page = self._make_mock_page(b"auto_path_data")
        result = ScreenshotManager.capture_full_page(page, None)

        assert result["success"] is True
        assert "base64_data" in result
        assert result["path"]  # 应有路径
        # 清理临时文件
        if os.path.exists(result["path"]):
            os.unlink(result["path"])

    def test_screenshot_failure_no_base64(self):
        """截图失败时不返回 base64"""
        page = MagicMock()
        page.screenshot.side_effect = Exception("截图失败")

        result = ScreenshotManager.capture_full_page(page, "/tmp/should_not_exist.png")
        assert result["success"] is False
        assert "base64_data" not in result
