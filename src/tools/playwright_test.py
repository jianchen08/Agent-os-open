"""
Playwright 前端测试封装工具 - 兼容入口

暴露接口：
- PlaywrightTestTool：主工具类
"""

from tools.builtin.playwright_test import PlaywrightTestTool

__all__ = ["PlaywrightTestTool"]
