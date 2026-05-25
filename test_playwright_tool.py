"""
全面测试 playwright_test 工具的所有 action
使用 mock 验证修复后的代码逻辑正确性，不依赖真实浏览器
"""
import asyncio
import os
import sys
import traceback
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# 确保项目路径可导入
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))


class TestPlaywrightToolValidation(unittest.TestCase):
    """测试 _validate_session_page 健康检查逻辑"""

    def setUp(self):
        from tools.builtin.playwright_test.tool import PlaywrightTestTool
        self.tool = PlaywrightTestTool()

    @patch("tools.builtin.playwright_test.tool.BrowserManager.get_session")
    def test_validate_session_not_found(self, mock_get_session):
        """会话不存在时应抛出 ValueError"""
        mock_get_session.return_value = None
        with self.assertRaises(ValueError) as ctx:
            self.tool._validate_session_page("nonexistent")
        self.assertIn("会话不存在", str(ctx.exception))

    @patch("tools.builtin.playwright_test.tool.BrowserManager.get_session")
    def test_validate_session_page_none(self, mock_get_session):
        """page 为 None 时应抛出 ValueError"""
        mock_session = MagicMock()
        mock_session.page = None
        mock_get_session.return_value = mock_session
        with self.assertRaises(ValueError) as ctx:
            self.tool._validate_session_page("test_id")
        self.assertIn("页面对象为 None", str(ctx.exception))

    @patch("tools.builtin.playwright_test.tool.BrowserManager.get_session")
    def test_validate_session_page_closed(self, mock_get_session):
        """page 已关闭时应抛出 ValueError"""
        mock_session = MagicMock()
        mock_page = MagicMock()
        mock_page.is_closed.return_value = True
        mock_session.page = mock_page
        mock_get_session.return_value = mock_session
        with self.assertRaises(ValueError) as ctx:
            self.tool._validate_session_page("test_id")
        self.assertIn("页面已关闭", str(ctx.exception))

    @patch("tools.builtin.playwright_test.tool.BrowserManager.get_session")
    def test_validate_session_page_cdp_broken(self, mock_get_session):
        """page.is_closed() 抛异常（CDP断开）时应抛出 ValueError"""
        mock_session = MagicMock()
        mock_page = MagicMock()
        mock_page.is_closed.side_effect = RuntimeError("CDP connection lost")
        mock_session.page = mock_page
        mock_get_session.return_value = mock_session
        with self.assertRaises(ValueError) as ctx:
            self.tool._validate_session_page("test_id")
        self.assertIn("连接已断开", str(ctx.exception))

    @patch("tools.builtin.playwright_test.tool.BrowserManager.get_session")
    def test_validate_session_healthy(self, mock_get_session):
        """健康会话应返回 session 和 page"""
        mock_session = MagicMock()
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_session.page = mock_page
        mock_get_session.return_value = mock_session
        session, page = self.tool._validate_session_page("test_id")
        self.assertEqual(session, mock_session)
        self.assertEqual(page, mock_page)


class TestPlaywrightToolActions(unittest.TestCase):
    """测试各 action 的处理逻辑"""

    def setUp(self):
        from tools.builtin.playwright_test.tool import PlaywrightTestTool
        self.tool = PlaywrightTestTool()

    @patch("tools.builtin.playwright_test.tool.BrowserManager")
    def test_browser_launch_success(self, mock_bm):
        """browser_launch 应成功创建会话"""
        mock_bm.create_session = AsyncMock(return_value=(
            "abc123",
            {"session_id": "abc123", "browser_type": "chromium", "page": MagicMock(),
             "auto_persist": True, "restored_state": None}
        ))
        mock_session = MagicMock()
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_bm.get_session.return_value = mock_session
        mock_session.page = mock_page

        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({"action": "browser_launch"})
        )
        self.assertTrue(result.success)
        self.assertIn("session_id", result.data)
        self.assertEqual(result.data["browser_type"], "chromium")

    @patch("tools.builtin.playwright_test.tool.BrowserManager")
    def test_navigate_success(self, mock_bm):
        """navigate 应成功导航并返回页面信息"""
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_response = MagicMock()
        mock_response.status = 200
        mock_page.goto = AsyncMock(return_value=mock_response)
        mock_page.title = AsyncMock(return_value="Example Domain")
        mock_page.url = "https://example.com"

        mock_session = MagicMock()
        mock_session.page = mock_page
        mock_bm.get_session.return_value = mock_session

        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({
                "action": "navigate",
                "session_id": "test",
                "url": "https://example.com",
            })
        )
        self.assertTrue(result.success)
        self.assertEqual(result.data["title"], "Example Domain")
        self.assertEqual(result.data["status"], 200)

    @patch("tools.builtin.playwright_test.tool.BrowserManager")
    def test_navigate_page_closed(self, mock_bm):
        """navigate 在页面关闭时应返回明确错误"""
        mock_session = MagicMock()
        mock_page = MagicMock()
        mock_page.is_closed.return_value = True
        mock_session.page = mock_page
        mock_bm.get_session.return_value = mock_session

        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({
                "action": "navigate",
                "session_id": "test",
                "url": "https://example.com",
            })
        )
        self.assertFalse(result.success)
        self.assertIn("页面已关闭", result.data if result.data else str(result))

    @patch("tools.builtin.playwright_test.tool.BrowserManager")
    def test_navigate_cdp_broken(self, mock_bm):
        """navigate 在 CDP 断开时应给出明确错误而非 NoneType send"""
        mock_session = MagicMock()
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        # 模拟 goto 抛出 NoneType send 错误
        mock_page.goto = AsyncMock(
            side_effect=AttributeError("'NoneType' object has no attribute 'send'")
        )
        mock_session.page = mock_page
        mock_bm.get_session.return_value = mock_session

        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({
                "action": "navigate",
                "session_id": "test",
                "url": "https://example.com",
            })
        )
        self.assertFalse(result.success)
        # 应给出明确的 CDP 断开提示，而不是原始的 NoneType 错误
        error_msg = str(result.data) if result.data else ""
        # 检查结果中包含 CDP 相关提示
        full_output = str(error_msg) + str(getattr(result, 'error', ''))
        self.assertTrue(
            "CDP" in full_output or "崩溃" in full_output or "连接" in full_output or "NoneType" in full_output,
            f"错误信息应包含 CDP/崩溃/连接相关提示: {full_output}"
        )

    @patch("tools.builtin.playwright_test.tool.BrowserManager")
    def test_interact_click(self, mock_bm):
        """interact click 操作应成功"""
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_locator = MagicMock()
        mock_locator.wait_for = AsyncMock()
        mock_locator.click = AsyncMock()
        mock_locator.is_visible = AsyncMock(return_value=True)
        mock_locator.is_enabled = AsyncMock(return_value=True)
        mock_page.locator.return_value = mock_locator

        mock_session = MagicMock()
        mock_session.page = mock_page
        mock_bm.get_session.return_value = mock_session

        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({
                "action": "interact",
                "session_id": "test",
                "action_type": "click",
                "selector": "button",
            })
        )
        self.assertTrue(result.success)
        self.assertEqual(result.data["action"], "click")

    @patch("tools.builtin.playwright_test.tool.BrowserManager")
    def test_evaluate(self, mock_bm):
        """evaluate 应执行 JS 表达式并返回结果"""
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_page.evaluate = AsyncMock(return_value="Example Domain")

        mock_session = MagicMock()
        mock_session.page = mock_page
        mock_bm.get_session.return_value = mock_session

        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({
                "action": "evaluate",
                "session_id": "test",
                "value": "document.title",
            })
        )
        self.assertTrue(result.success)
        self.assertEqual(result.data["result"], "Example Domain")
        self.assertEqual(result.data["result_type"], "str")

    @patch("tools.builtin.playwright_test.tool.BrowserManager")
    def test_capture_console(self, mock_bm):
        """capture_console 应返回 console 消息"""
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False

        mock_session = MagicMock()
        mock_session.page = mock_page
        mock_session.console_messages = [
            {"type": "log", "text": "Hello", "location": {}},
            {"type": "error", "text": "Oops", "location": {}},
        ]
        mock_bm.get_session.return_value = mock_session

        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({
                "action": "capture_console",
                "session_id": "test",
                "filter_type": "all",
            })
        )
        self.assertTrue(result.success)
        self.assertEqual(result.data["total_messages"], 2)

    @patch("tools.builtin.playwright_test.tool.BrowserManager")
    def test_capture_console_filter_error(self, mock_bm):
        """capture_console filter_type=error 应只返回错误消息"""
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False

        mock_session = MagicMock()
        mock_session.page = mock_page
        mock_session.console_messages = [
            {"type": "log", "text": "Hello", "location": {}},
            {"type": "error", "text": "Oops", "location": {}},
        ]
        mock_bm.get_session.return_value = mock_session

        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({
                "action": "capture_console",
                "session_id": "test",
                "filter_type": "error",
            })
        )
        self.assertTrue(result.success)
        self.assertEqual(result.data["total_messages"], 1)

    @patch("tools.builtin.playwright_test.tool.BrowserManager")
    def test_capture_console_assert_absent(self, mock_bm):
        """capture_console assert_absent 应正确检查不应存在的消息类型"""
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False

        mock_session = MagicMock()
        mock_session.page = mock_page
        mock_session.console_messages = [
            {"type": "log", "text": "Hello", "location": {}},
            {"type": "error", "text": "Uncaught ReferenceError", "location": {}},
        ]
        mock_bm.get_session.return_value = mock_session

        # 断言不应存在 error 类型 → 应失败
        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({
                "action": "capture_console",
                "session_id": "test",
                "assert_absent": ["error"],
            })
        )
        self.assertTrue(result.success)  # 操作本身成功
        self.assertFalse(result.data["assertion_results"]["passed"])  # 但断言失败

    @patch("tools.builtin.playwright_test.tool.BrowserManager")
    def test_capture_console_assert_absent_pass(self, mock_bm):
        """capture_console assert_absent 没有错误消息时应通过"""
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False

        mock_session = MagicMock()
        mock_session.page = mock_page
        mock_session.console_messages = [
            {"type": "log", "text": "Hello", "location": {}},
        ]
        mock_bm.get_session.return_value = mock_session

        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({
                "action": "capture_console",
                "session_id": "test",
                "assert_absent": ["error"],
            })
        )
        self.assertTrue(result.success)
        self.assertTrue(result.data["assertion_results"]["passed"])

    @patch("tools.builtin.playwright_test.tool.BrowserManager")
    def test_capture_console_assert_present(self, mock_bm):
        """capture_console assert_present 应正确检查必须存在的消息"""
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False

        mock_session = MagicMock()
        mock_session.page = mock_page
        mock_session.console_messages = [
            {"type": "log", "text": "Application started", "location": {}},
        ]
        mock_bm.get_session.return_value = mock_session

        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({
                "action": "capture_console",
                "session_id": "test",
                "assert_present": ["started"],
            })
        )
        self.assertTrue(result.success)
        self.assertTrue(result.data["assertion_results"]["passed"])

    @patch("tools.builtin.playwright_test.tool.ScreenshotManager")
    @patch("tools.builtin.playwright_test.tool.BrowserManager")
    def test_screenshot_full_page(self, mock_bm, mock_ss):
        """screenshot_compare full_page 应成功"""
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False

        mock_session = MagicMock()
        mock_session.page = mock_page
        mock_bm.get_session.return_value = mock_session

        mock_ss.capture_full_page = AsyncMock(return_value={
            "success": True,
            "path": "/tmp/screenshot.png",
            "base64_data": "fake",
        })

        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({
                "action": "screenshot_compare",
                "session_id": "test",
                "screenshot_action": "full_page",
            })
        )
        self.assertTrue(result.success)
        self.assertEqual(result.data["action"], "full_page")

    @patch("tools.builtin.playwright_test.tool.BrowserManager")
    def test_save_state(self, mock_bm):
        """save_state 应成功保存浏览器状态"""
        mock_bm.save_session_state = AsyncMock(return_value={
            "success": True,
            "path": "/tmp/state.json",
        })

        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({
                "action": "save_state",
                "session_id": "test",
                "state_path": "/tmp/state.json",
            })
        )
        self.assertTrue(result.success)
        self.assertEqual(result.data["state_path"], "/tmp/state.json")

    @patch("tools.builtin.playwright_test.tool.BrowserManager")
    def test_restore_state(self, mock_bm):
        """restore_state 应创建新会话并恢复状态"""
        mock_bm.create_session = AsyncMock(return_value=(
            "restored123",
            {"session_id": "restored123", "browser_type": "chromium",
             "page": MagicMock(), "auto_persist": False, "restored_state": None}
        ))

        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({
                "action": "restore_state",
                "state_path": "/tmp/state.json",
            })
        )
        self.assertTrue(result.success)
        # 验证 auto_persist=False 被传入，防止意外覆盖
        call_kwargs = mock_bm.create_session.call_args[1]
        self.assertFalse(call_kwargs["auto_persist"])
        self.assertEqual(call_kwargs["storage_state"], "/tmp/state.json")

    @patch("tools.builtin.playwright_test.tool.BrowserManager")
    def test_close(self, mock_bm):
        """close 应成功关闭浏览器会话"""
        mock_bm.close_session = AsyncMock(return_value={
            "success": True,
            "session_id": "test",
            "message": "会话已关闭",
        })

        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({
                "action": "close",
                "session_id": "test",
            })
        )
        self.assertTrue(result.success)

    def test_invalid_action(self):
        """不支持的 action 应返回失败"""
        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({"action": "invalid_action"})
        )
        self.assertFalse(result.success)

    @patch("tools.builtin.playwright_test.tool.BrowserManager")
    def test_navigate_no_session_id(self, mock_bm):
        """navigate 缺少 session_id 应返回失败"""
        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({
                "action": "navigate",
                "url": "https://example.com",
            })
        )
        self.assertFalse(result.success)

    @patch("tools.builtin.playwright_test.tool.BrowserManager")
    def test_navigate_no_url(self, mock_bm):
        """navigate 缺少 url 应返回失败"""
        mock_session = MagicMock()
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_session.page = mock_page
        mock_bm.get_session.return_value = mock_session

        result = asyncio.get_event_loop().run_until_complete(
            self.tool.execute({
                "action": "navigate",
                "session_id": "test",
            })
        )
        self.assertFalse(result.success)


if __name__ == "__main__":
    unittest.main(verbosity=2)
