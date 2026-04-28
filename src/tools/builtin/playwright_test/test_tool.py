"""
Playwright 测试工具单元测试

测试 PlaywrightTestTool 的基本功能。
"""

import pytest
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class TestPlaywrightTestTool:
    """Playwright 测试工具测试类"""
    
    def test_get_tool_definition(self):
        """测试工具定义获取"""
        from src.tools.builtin.playwright_test import PlaywrightTestTool
        
        tool_def = PlaywrightTestTool.get_tool_definition()
        
        assert tool_def is not None
        assert tool_def.name == "playwright_test"
        assert tool_def.category.value == "execution"
        assert "chromium" in str(tool_def.input_schema)
    
    def test_tool_definition_schema(self):
        """测试工具定义 Schema 结构"""
        from src.tools.builtin.playwright_test import PlaywrightTestTool
        
        tool_def = PlaywrightTestTool.get_tool_definition()
        schema = tool_def.input_schema
        
        # 验证必需字段
        assert "action" in schema["properties"]
        assert "session_id" in schema["properties"]
        
        # 验证 action 选项
        action_enum = schema["properties"]["action"]["enum"]
        assert "browser_launch" in action_enum
        assert "navigate" in action_enum
        assert "interact" in action_enum
        assert "capture_console" in action_enum
        assert "screenshot_compare" in action_enum
        assert "close" in action_enum
    
    def test_execute_invalid_action(self):
        """测试执行无效操作"""
        import asyncio
        from src.tools.builtin.playwright_test import PlaywrightTestTool
        
        tool = PlaywrightTestTool()
        
        async def run_test():
            result = await tool.execute({"action": "invalid_action"})
            return result
        
        result = asyncio.run(run_test())
        
        assert result.success is False
        assert "不支持的操作" in result.error


class TestBrowserManager:
    """浏览器管理器测试类"""
    
    def test_session_creation_requires_import(self):
        """测试会话创建需要 Playwright"""
        from src.tools.builtin.playwright_test.browser_manager import BrowserManager
        
        assert hasattr(BrowserManager, "create_session")
        assert hasattr(BrowserManager, "get_session")
        assert hasattr(BrowserManager, "close_session")
        assert hasattr(BrowserManager, "_sessions")


class TestScreenshotManager:
    """截图管理器测试类"""
    
    def test_methods_exist(self):
        """测试截图管理器方法存在"""
        from src.tools.builtin.playwright_test.screenshot import ScreenshotManager
        
        assert hasattr(ScreenshotManager, "capture_full_page")
        assert hasattr(ScreenshotManager, "capture_element")
        assert hasattr(ScreenshotManager, "compare_images")
        assert hasattr(ScreenshotManager, "save_baseline")
    
    def test_compare_images_missing_file(self):
        """测试图片对比文件不存在的情况"""
        from src.tools.builtin.playwright_test.screenshot import ScreenshotManager
        
        result = ScreenshotManager.compare_images(
            baseline_path="/nonexistent/baseline.png",
            current_path="/nonexistent/current.png",
        )
        
        assert result["success"] is False
        assert "不存在" in result["error"]


class TestToolCategory:
    """工具分类测试"""
    
    def test_category_is_execution(self):
        """测试工具分类是 EXECUTION"""
        from src.tools.builtin.playwright_test import PlaywrightTestTool
        from src.tools.types import ToolCategory
        
        tool_def = PlaywrightTestTool.get_tool_definition()
        
        assert tool_def.category == ToolCategory.EXECUTION


class TestToolSource:
    """工具来源测试"""
    
    def test_source_is_builtin(self):
        """测试工具来源是 BUILTIN"""
        from src.tools.builtin.playwright_test import PlaywrightTestTool
        from src.tools.types import ToolSource
        
        tool_def = PlaywrightTestTool.get_tool_definition()
        
        assert tool_def.source == ToolSource.BUILTIN


if __name__ == "__main__":
    pytest.main([__file__, "-v"])