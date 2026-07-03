"""
浏览器会话管理器

管理 Playwright 浏览器实例和页面，支持多会话并行。
"""

from __future__ import annotations

import contextlib
import glob
import logging
import os
import time
import uuid
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
        playwright: Any = None,
    ):
        self.session_id = session_id
        self.browser_type = browser_type
        self.browser = browser
        self.context = context
        self.page = page
        self.playwright = playwright
        self.console_messages: list[dict[str, Any]] = []
        self._console_handler: Any = None

    async def save_state(self, path: str) -> dict[str, Any]:
        """
        保存浏览器状态到文件

        将当前上下文的 cookies 和 localStorage 保存到指定 JSON 文件。

        Args:
            path: 状态文件保存路径

        Returns:
            包含保存结果的字典
        """
        try:
            await self.context.storage_state(path=path)
            logger.info(f"会话 {self.session_id} 状态已保存到: {path}")
            return {"success": True, "path": path}
        except Exception as e:
            logger.error(f"保存会话 {self.session_id} 状态失败: {e}")
            return {"success": False, "error": str(e)}

    async def get_state(self) -> dict[str, Any]:
        """
        获取浏览器状态（内存中）

        返回当前上下文的 cookies 和 localStorage 状态数据，不写入文件。

        Returns:
            包含状态数据或错误信息的字典
        """
        try:
            state = await self.context.storage_state()
            return {"success": True, "state": state}
        except Exception as e:
            logger.error(f"获取会话 {self.session_id} 状态失败: {e}")
            return {"success": False, "error": str(e)}

    def is_browser_connected(self) -> bool:
        """
        检查浏览器进程是否仍然连接。

        通过 Playwright 的 browser.is_connected() 检查底层 CDP 连接状态。
        当浏览器进程崩溃或被强制关闭时，返回 False。

        Returns:
            True 如果浏览器连接正常，False 否之
        """
        try:
            if self.browser is None:
                return False
            return self.browser.is_connected()
        except Exception:
            return False

    async def check_cdp_health(self) -> tuple[bool, str]:  # noqa: PLR0911
        """
        通过实际 CDP 操作检查连接健康状态。

        执行一个轻量级的 page.evaluate 调用来验证 CDP transport 是否可用。
        这比仅检查 is_connected() 更可靠，因为它实际发送了一条 CDP 消息。

        Returns:
            (is_healthy, error_message): 健康状态和错误信息
        """
        try:
            if self.page is None:
                return False, "页面对象为 None"
            if self.page.is_closed():
                return False, "页面已关闭"
            # 实际执行一个 CDP 命令来验证 transport
            await self.page.evaluate("1 + 1")
            return True, ""
        except Exception as e:
            error_msg = str(e)
            if "NoneType" in error_msg and "send" in error_msg:
                return False, f"CDP transport 已断开（浏览器进程可能已崩溃）: {error_msg}"
            if "Target closed" in error_msg or "Target page" in error_msg:
                return False, f"浏览器目标已关闭: {error_msg}"
            if "Connection closed" in error_msg:
                return False, f"CDP 连接已断开: {error_msg}"
            return False, f"CDP 健康检查失败: {error_msg}"

    async def cleanup(self):
        """清理会话资源"""
        try:
            if self._console_handler:
                with contextlib.suppress(Exception):
                    self.page.remove_listener("console", self._console_handler)
            if self.context:
                with contextlib.suppress(Exception):
                    await self.context.close()
            if self.browser:
                with contextlib.suppress(Exception):
                    await self.browser.close()
            if self.playwright:
                with contextlib.suppress(Exception):
                    await self.playwright.stop()
        except Exception as e:
            logger.warning(f"清理会话 {self.session_id} 时出错: {e}")


class BrowserManager:
    """
    浏览器会话管理器

    管理多个浏览器会话，支持并发操作。
    """

    # 默认状态文件保存目录
    DEFAULT_STATE_DIR = os.path.join("data", "browser_state")
    # 保留的状态文件最大数量
    MAX_STATE_FILES = 5

    # 类级别会话存储
    _sessions: dict[str, BrowserSession] = {}

    @classmethod
    async def create_session(  # noqa: PLR0915
        cls,
        browser_type: str = "chromium",
        headless: bool = True,
        viewport_width: int = 1280,
        viewport_height: int = 720,
        slow_mo: int = 0,
        launch_options: dict | None = None,
        storage_state: str | dict | None = None,
        auto_persist: bool = True,
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
            storage_state: 浏览器状态文件路径或状态字典，用于恢复 cookies 和 localStorage
            auto_persist: 是否自动持久化状态（启动时自动恢复，关闭时自动保存），默认 True

        Returns:
            tuple[session_id, session_info]
        """
        try:
            from playwright.async_api import async_playwright  # noqa: PLC0415

            # 自动恢复逻辑：当 auto_persist=True 且用户未显式提供 storage_state 时，
            # 尝试从默认状态目录加载最新的状态文件
            restored_state_path = None
            if auto_persist and storage_state is None:
                restored_state_path = cls.auto_load_state()
                if restored_state_path:
                    storage_state = restored_state_path
                    logger.info(f"自动恢复浏览器状态: {restored_state_path}")

            # 创建会话ID
            session_id = str(uuid.uuid4())[:8]

            # 启动 Playwright
            playwright = await async_playwright().start()

            # 选择浏览器类型
            browser_map = {
                "chromium": playwright.chromium,
                "firefox": playwright.firefox,
                "webkit": playwright.webkit,
            }
            browser_launcher = browser_map.get(browser_type, browser_map["chromium"])

            # 构建启动参数
            options: dict[str, Any] = {
                "headless": headless,
                "slow_mo": slow_mo,
            }
            if launch_options:
                options.update(launch_options)

            # 启动浏览器
            browser = await browser_launcher.launch(**options)

            # 创建上下文和页面
            context_options: dict[str, Any] = {
                "viewport": {"width": viewport_width, "height": viewport_height},
            }
            if storage_state is not None:
                context_options["storage_state"] = storage_state
            context = await browser.new_context(**context_options)
            page = await context.new_page()

            # === 启动后立即进行 CDP 健康检查 ===
            # 导航到 about:blank 验证 CDP transport 是否真正可用。
            # 如果浏览器进程因缺少系统库等原因立即崩溃，
            # page.goto() 会抛出 NoneType send 错误，在此处捕获可给出明确诊断。
            try:
                await page.goto("about:blank", timeout=10000)
            except Exception as health_check_error:
                error_msg = str(health_check_error)
                logger.error(f"浏览器启动后 CDP 健康检查失败: {health_check_error}")

                # 清理已创建的资源
                with contextlib.suppress(Exception):
                    await page.close()
                with contextlib.suppress(Exception):
                    await context.close()
                with contextlib.suppress(Exception):
                    await browser.close()
                with contextlib.suppress(Exception):
                    await playwright.stop()

                # 根据错误类型给出针对性提示
                if "NoneType" in error_msg and "send" in error_msg:
                    raise RuntimeError(  # noqa: B904
                        "浏览器启动后 CDP 连接立即断开，通常是因为浏览器进程缺少系统依赖库。"
                        "请运行以下命令安装依赖：\n"
                        "  python3 -m playwright install-deps chromium\n"
                        "或手动安装：sudo apt-get install -y libnspr4 libnss3 libasound2\n"
                        f"原始错误: {error_msg}"
                    )
                if "Target" in error_msg and ("closed" in error_msg.lower()):
                    raise RuntimeError(  # noqa: B904
                        "浏览器进程在启动后立即退出，可能原因：\n"
                        "1. 缺少系统依赖库（运行 python3 -m playwright install-deps chromium）\n"
                        "2. 系统资源不足\n"
                        "3. 沙箱环境限制（可尝试 headless=True 或 launch_options 中添加 '--no-sandbox'）\n"
                        f"原始错误: {error_msg}"
                    )
                raise RuntimeError(  # noqa: B904
                    f"浏览器启动后健康检查失败: {health_check_error}"
                )

            # 设置 console 监听
            console_messages: list[dict[str, Any]] = []

            def console_handler(msg: Any) -> None:
                console_messages.append(
                    {
                        "type": msg.type,
                        "text": msg.text,
                        "location": msg.location,
                    }
                )

            page.on("console", console_handler)

            # 创建会话
            session = BrowserSession(
                session_id=session_id,
                browser_type=browser_type,
                browser=browser,
                context=context,
                page=page,
                playwright=playwright,
            )
            session.console_messages = console_messages
            session._console_handler = console_handler

            cls._sessions[session_id] = session

            session_info = {
                "session_id": session_id,
                "browser_type": browser_type,
                "browser": browser,
                "page": page,
                "auto_persist": auto_persist,
                "restored_state": restored_state_path,
            }

            logger.info(f"创建浏览器会话: {session_id}, 类型: {browser_type}")

            return session_id, session_info

        except ImportError as e:
            raise ImportError(f"Playwright 未安装: {e}")  # noqa: B904
        except RuntimeError:
            raise  # 重新抛出上面已包装的 RuntimeError
        except Exception as e:
            error_msg = str(e)
            # 检测 Playwright 的 TargetClosedError（浏览器启动即崩溃）
            if "Target" in error_msg and ("closed" in error_msg.lower() or "Close" in error_msg):
                raise RuntimeError(  # noqa: B904
                    "浏览器启动失败（进程立即退出），通常是因为缺少系统依赖库。\n"
                    "请运行以下命令安装依赖：\n"
                    "  python3 -m playwright install-deps chromium\n"
                    "或手动安装：sudo apt-get install -y libnspr4 libnss3 libasound2\n"
                    f"原始错误: {error_msg}"
                )
            raise RuntimeError(f"创建浏览器会话失败: {e}")  # noqa: B904

    @classmethod
    def get_session(cls, session_id: str) -> BrowserSession | None:
        """获取会话"""
        return cls._sessions.get(session_id)

    @classmethod
    async def close_session(cls, session_id: str) -> dict[str, Any]:
        """
        关闭浏览器会话

        自动保存会话状态到默认目录后关闭，保存失败不影响关闭流程。

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

        # 在关闭前自动保存会话状态
        save_result = await cls.auto_save_state(session_id)

        try:
            await session.cleanup()
            del cls._sessions[session_id]
            logger.info(f"关闭浏览器会话: {session_id}")
            result = {
                "success": True,
                "session_id": session_id,
                "message": "会话已关闭",
            }
            # 如果自动保存成功，附带保存信息
            if save_result.get("success"):
                result["auto_saved_state"] = save_result.get("path")
            return result
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

    @classmethod
    async def save_session_state(cls, session_id: str, path: str) -> dict[str, Any]:
        """
        保存指定会话的浏览器状态到文件

        Args:
            session_id: 会话ID
            path: 状态文件保存路径

        Returns:
            包含保存结果的字典
        """
        session = cls.get_session(session_id)
        if not session:
            return {
                "success": False,
                "error": f"会话不存在: {session_id}",
            }
        return await session.save_state(path)

    @classmethod
    def list_saved_states(cls, directory: str) -> list[str]:
        """
        扫描目录下的浏览器状态文件

        查找指定目录中所有 .json 格式的状态文件。

        Args:
            directory: 要扫描的目录路径

        Returns:
            匹配的状态文件路径列表
        """
        try:
            pattern = os.path.join(directory, "*.json")
            files = glob.glob(pattern)  # noqa: PTH207
            return sorted(files)
        except Exception as e:
            logger.error(f"扫描状态文件失败: {e}")
            return []

    @classmethod
    async def auto_save_state(cls, session_id: str) -> dict[str, Any]:
        """
        自动保存指定会话状态到默认目录

        保存会话的 cookies 和 localStorage 到 DEFAULT_STATE_DIR 目录，
        文件命名格式为 state_{session_id}_{timestamp}.json。
        自动清理旧状态文件，只保留最近 MAX_STATE_FILES 个。

        Args:
            session_id: 会话ID

        Returns:
            包含保存结果的字典，成功时包含 path 字段
        """
        session = cls.get_session(session_id)
        if not session:
            return {
                "success": False,
                "error": f"会话不存在: {session_id}",
            }

        try:
            # 确保状态目录存在
            state_dir = cls.DEFAULT_STATE_DIR
            os.makedirs(state_dir, exist_ok=True)  # noqa: PTH103

            # 生成状态文件路径
            timestamp = int(time.time())
            filename = f"state_{session_id}_{timestamp}.json"
            state_path = os.path.join(state_dir, filename)

            # 保存状态
            save_result = await session.save_state(state_path)
            if not save_result.get("success"):
                logger.warning(f"自动保存会话 {session_id} 状态失败: {save_result.get('error')}")
                return save_result

            # 清理旧状态文件，只保留最近的 MAX_STATE_FILES 个
            cls._cleanup_old_state_files(state_dir)

            logger.info(f"自动保存会话 {session_id} 状态到: {state_path}")
            return {"success": True, "path": state_path}

        except Exception as e:
            # 保存失败不影响关闭流程，仅记录警告
            logger.warning(f"自动保存会话 {session_id} 状态异常: {e}")
            return {"success": False, "error": str(e)}

    @classmethod
    def auto_load_state(cls) -> str | None:
        """
        从默认状态目录加载最新的状态文件

        扫描 DEFAULT_STATE_DIR 目录，返回最新的状态文件路径。
        按文件修改时间排序，返回最新的一个。

        Returns:
            最新的状态文件路径，如不存在返回 None
        """
        state_dir = cls.DEFAULT_STATE_DIR
        if not os.path.isdir(state_dir):  # noqa: PTH112
            return None

        try:
            # 查找所有状态文件
            pattern = os.path.join(state_dir, "state_*.json")
            state_files = glob.glob(pattern)  # noqa: PTH207

            if not state_files:
                return None

            # 按修改时间排序，取最新的
            state_files.sort(key=os.path.getmtime, reverse=True)
            latest_file = state_files[0]

            logger.info(f"找到最新的浏览器状态文件: {latest_file}")
            return latest_file

        except Exception as e:
            logger.warning(f"加载浏览器状态文件失败: {e}")
            return None

    @classmethod
    def _cleanup_old_state_files(cls, state_dir: str) -> None:
        """
        清理旧的状态文件，只保留最新的 MAX_STATE_FILES 个

        按文件修改时间排序，删除超出数量限制的旧文件。

        Args:
            state_dir: 状态文件目录路径
        """
        try:
            pattern = os.path.join(state_dir, "state_*.json")
            state_files = glob.glob(pattern)  # noqa: PTH207

            if len(state_files) <= cls.MAX_STATE_FILES:
                return

            # 按修改时间排序（最新在前）
            state_files.sort(key=os.path.getmtime, reverse=True)

            # 删除超出数量的旧文件
            for old_file in state_files[cls.MAX_STATE_FILES :]:
                try:
                    os.remove(old_file)  # noqa: PTH107
                    logger.info(f"清理旧状态文件: {old_file}")
                except Exception as e:
                    logger.warning(f"清理旧状态文件失败: {old_file}, 错误: {e}")

        except Exception as e:
            logger.warning(f"清理旧状态文件异常: {e}")
