"""
浏览器会话管理器

管理 Playwright 浏览器实例和页面，支持多会话并行。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)


class BrowserSession:
    """浏览器会话"""
    
    def __init__(
        self,
        session_id: str,
        browser_type: str,
        browser: Any,
        context: Any,
        page: Any,
    ):
        self.session_id = session_id
        self.browser_type = browser_type
        self.browser = browser
        self.context = context
        self.page = page
        self.console_messages: list[dict[str, Any]] = []
        self._console_handler: Any = None
    
    def cleanup(self):
        """清理会话资源"""
        try:
            if self._console_handler:
                try:
                    self.page.remove_listener("console", self._console_handler)
                except Exception:
                    pass
            if self.context:
                try:
                    self.context.close()
                except Exception:
                    pass
            if self.browser:
                try:
                    self.browser.close()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"清理会话 {self.session_id} 时出错: {e}")


class BrowserManager:
    """
    浏览器会话管理器
    
    管理多个浏览器会话，支持并发操作。
    """
    
    # 类级别会话存储
    _sessions: dict[str, BrowserSession] = {}
    
    @classmethod
    def create_session(
        cls,
        browser_type: str = "chromium",
        headless: bool = True,
        viewport_width: int = 1280,
        viewport_height: int = 720,
        slow_mo: int = 0,
        launch_options: dict | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """
        创建新的浏览器会话
        
        Args:
            browser_type: 浏览器类型 (chromium/firefox/webkit)
            headless: 是否无头模式
            viewport_width: 视口宽度
            viewport_height: 视口高度
            slow_mo: 操作延迟（毫秒）
            launch_options: 额外启动参数
        
        Returns:
            tuple[session_id, session_info]
        """
        try:
            from playwright.sync_api import sync_playwright
            
            # 创建会话ID
            session_id = str(uuid.uuid4())[:8]
            
            # 启动 Playwright
            playwright = sync_playwright().start()
            
            # 选择浏览器类型
            browser_map = {
                "chromium": playwright.chromium,
                "firefox": playwright.firefox,
                "webkit": playwright.webkit,
            }
            browser_launcher = browser_map.get(browser_type, browser_map["chromium"])
            
            # 构建启动参数
            options = {
                "headless": headless,
                "slow_mo": slow_mo,
            }
            if launch_options:
                options.update(launch_options)
            
            # 启动浏览器
            browser = browser_launcher.launch(**options)
            
            # 创建上下文和页面
            context = browser.new_context(
                viewport={"width": viewport_width, "height": viewport_height}
            )
            page = context.new_page()
            
            # 设置 console 监听
            console_messages: list[dict[str, Any]] = []
            
            def console_handler(msg):
                console_messages.append({
                    "type": msg.type,
                    "text": msg.text,
                    "location": msg.location,
                })
            
            page.on("console", console_handler)
            
            # 创建会话
            session = BrowserSession(
                session_id=session_id,
                browser_type=browser_type,
                browser=browser,
                context=context,
                page=page,
            )
            session.console_messages = console_messages
            session._console_handler = console_handler
            
            cls._sessions[session_id] = session
            
            session_info = {
                "session_id": session_id,
                "browser_type": browser_type,
                "browser": browser,
                "page": page,
            }
            
            logger.info(f"创建浏览器会话: {session_id}, 类型: {browser_type}")
            
            return session_id, session_info
            
        except ImportError as e:
            raise ImportError(f"Playwright 未安装: {e}")
        except Exception as e:
            raise RuntimeError(f"创建浏览器会话失败: {e}")
    
    @classmethod
    def get_session(cls, session_id: str) -> BrowserSession | None:
        """获取会话"""
        return cls._sessions.get(session_id)
    
    @classmethod
    def close_session(cls, session_id: str) -> dict[str, Any]:
        """
        关闭浏览器会话
        
        Args:
            session_id: 会话ID
        
        Returns:
            关闭结果
        """
        session = cls._sessions.get(session_id)
        if not session:
            return {
                "success": False,
                "error": f"会话不存在: {session_id}",
            }
        
        try:
            session.cleanup()
            del cls._sessions[session_id]
            logger.info(f"关闭浏览器会话: {session_id}")
            return {
                "success": True,
                "session_id": session_id,
                "message": "会话已关闭",
            }
        except Exception as e:
            logger.error(f"关闭会话 {session_id} 失败: {e}")
            return {
                "success": False,
                "session_id": session_id,
                "error": str(e),
            }
    
    @classmethod
    def get_all_sessions(cls) -> list[dict[str, Any]]:
        """获取所有会话"""
        return [
            {
                "session_id": s.session_id,
                "browser_type": s.browser_type,
            }
            for s in cls._sessions.values()
        ]