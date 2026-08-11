"""
Playwright 前端测试封装工具

暴露接口：
- get_tool_definition() -> Tool：工具定义
- execute(self, inputs: dict) -> ToolExecutionResult：工具执行
"""

import json
import logging
from typing import Any, ClassVar

from core.results import ToolExecutionResult
from tools.builtin.base import BuiltinTool
from tools.types import (
    Tool,
    ToolCategory,
    ToolSource,
    create_failure_result,
    create_success_result,
)

from .browser_manager import BrowserManager
from .screenshot import ScreenshotManager

logger = logging.getLogger(__name__)


class PlaywrightTestTool(BuiltinTool):
    """
    Playwright 前端测试封装工具

    提供浏览器启动、页面导航、元素交互、console 捕获、截图对比等功能。
    支持 Chromium/Firefox/WebKit 三种浏览器引擎。
    """

    # Playwright async 对象与创建它的事件循环强绑定，必须固定在主管道 loop 执行，
    # 否则跨调用对象生命周期不一致（CDP transport 失效）。
    run_on_main_loop: ClassVar[bool] = True

    # 类级别会话存储
    _sessions: dict[str, Any] = {}

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="playwright_test",
            description="基于 Playwright 的前端 E2E 测试封装工具，支持浏览器启动、页面导航、元素交互、console 捕获和截图对比。",
            when_to_use=[
                "前端页面 E2E 测试",
                "浏览器交互自动化",
                "UI 自动化测试",
                "截图对比验证",
            ],
            when_not_to_use=[
                "API 接口测试（使用 http 工具）",
                "单元测试（使用 pytest）",
                "简单文件操作",
            ],
            caveats=[
                "需要 Playwright 和 Pillow 依赖",
                "多会话并行时注意资源管理",
                "截图对比需要先保存基准图片",
            ],
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "browser_launch",
                            "navigate",
                            "interact",
                            "capture_console",
                            "screenshot_compare",
                            "save_state",
                            "restore_state",
                            "evaluate",
                            "close",
                        ],
                        "description": "操作类型",
                    },
                    # browser_launch 参数
                    "browser": {
                        "type": "string",
                        "enum": ["chromium", "firefox", "webkit"],
                        "description": "浏览器类型，默认 chromium",
                    },
                    "headless": {
                        "type": "boolean",
                        "description": "是否无头模式，默认 true",
                    },
                    "viewport_width": {
                        "type": "integer",
                        "description": "视口宽度，默认 1280",
                    },
                    "viewport_height": {
                        "type": "integer",
                        "description": "视口高度，默认 720",
                    },
                    "slow_mo": {
                        "type": "integer",
                        "description": "操作延迟（毫秒），默认 0",
                    },
                    "launch_options": {
                        "type": "string",
                        "description": "额外启动参数（JSON 字符串）",
                    },
                    # navigate 参数
                    "session_id": {
                        "type": "string",
                        "description": "浏览器会话ID（navigate/interact/capture_console/screenshot_compare/close 时必填）",
                    },
                    "url": {
                        "type": "string",
                        "description": "目标URL（navigate 时必填）",
                    },
                    "wait_until": {
                        "type": "string",
                        "enum": ["load", "domcontentloaded", "networkidle", "commit"],
                        "description": "等待策略，默认 load",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时时间（毫秒），默认 30000",
                    },
                    # interact 参数
                    "action_type": {
                        "type": "string",
                        "enum": ["click", "type", "select", "drag", "hover", "upload"],
                        "description": "交互类型（interact 时使用）",
                    },
                    "selector": {
                        "type": "string",
                        "description": "元素选择器（CSS 或 XPath）",
                    },
                    "value": {
                        "type": "string",
                        "description": "输入值（type 时为文本，select 时为选项值，evaluate 时为 JS 表达式）",
                    },
                    "target_selector": {
                        "type": "string",
                        "description": "拖拽目标选择器（drag 时必填）",
                    },
                    "file_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "上传文件路径列表（upload 时必填）",
                    },
                    # capture_console 参数
                    "filter_type": {
                        "type": "string",
                        "enum": ["log", "warn", "error", "exception", "all"],
                        "description": "过滤类型，默认 all",
                    },
                    "assert_absent": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "断言不存在的错误级别列表",
                    },
                    "assert_present": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "断言必须存在的消息关键词列表",
                    },
                    "clear": {
                        "type": "boolean",
                        "description": "捕获后是否清空已记录的 console，默认 false",
                    },
                    # screenshot_compare 参数
                    "screenshot_action": {
                        "type": "string",
                        "enum": ["full_page", "element", "compare"],
                        "description": "截图类型（screenshot_compare 时使用）",
                    },
                    "baseline_path": {
                        "type": "string",
                        "description": "基准截图路径（compare 时必填）",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "截图保存路径",
                    },
                    "threshold": {
                        "type": "number",
                        "description": "像素对比阈值（0.0-1.0），默认 0.1",
                    },
                    # save_state / restore_state 参数
                    "state_path": {
                        "type": "string",
                        "description": "浏览器状态文件路径（save_state/restore_state 时使用）",
                    },
                    # browser_launch 可选参数
                    "storage_state": {
                        "type": "string",
                        "description": "恢复浏览器状态文件路径（browser_launch 时可选，用于恢复已保存的状态）",
                    },
                    "auto_persist": {
                        "type": "boolean",
                        "description": "是否自动持久化浏览器状态（关闭时自动保存，启动时自动恢复），默认 true",
                    },
                },
                "required": ["action"],
            },
            source=ToolSource.BUILTIN,
            category=ToolCategory.EXECUTION,
            dangerous_operations=[],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行工具"""
        action = inputs.get("action", "")

        # 路由到对应的处理方法
        handlers = {
            "browser_launch": self._handle_browser_launch,
            "navigate": self._handle_navigate,
            "interact": self._handle_interact,
            "capture_console": self._handle_capture_console,
            "screenshot_compare": self._handle_screenshot_compare,
            "save_state": self._handle_save_state,
            "restore_state": self._handle_restore_state,
            "evaluate": self._handle_evaluate,
            "close": self._handle_close,
        }

        handler = handlers.get(action)
        if not handler:
            return create_failure_result(f"不支持的操作: {action}")

        try:
            return await handler(inputs)
        except Exception as e:
            logger.error(f"Playwright 测试工具执行失败: {e}")
            return create_failure_result(str(e))

    def _validate_session_page(self, session_id: str) -> tuple[Any, Any]:
        """
        验证会话和页面健康状态。

        检查会话是否存在、page 是否为 None、page 是否已关闭。
        当浏览器进程崩溃或 CDP 连接断开时，page 对象可能变为无效，
        后续操作（如 goto）会在 Playwright 内部 CDP transport 层抛出
        'NoneType' object has no attribute 'send' 错误。
        此方法在操作前提前检测并给出明确错误信息。

        Returns:
            tuple[BrowserSession, Page]: 验证通过的会话和页面对象

        Raises:
            ValueError: 会话不存在、page 为 None 或 page 已关闭
        """
        session = BrowserManager.get_session(session_id)
        if not session:
            raise ValueError(f"会话不存在: {session_id}")

        page = session.page
        if page is None:
            raise ValueError(f"会话 {session_id} 的页面对象为 None，浏览器可能未正确启动。请重新创建会话。")

        try:
            if page.is_closed():
                raise ValueError(f"会话 {session_id} 的页面已关闭，请重新创建会话。")
        except Exception as e:
            # page.is_closed() 本身可能因 CDP 连接断开而失败
            if isinstance(e, ValueError):
                raise
            raise ValueError(  # noqa: B904
                f"会话 {session_id} 的页面连接已断开（CDP 错误），请重新创建会话。原始错误: {e}"
            )

        return session, page

    async def _handle_browser_launch(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """处理浏览器启动"""
        try:
            browser = inputs.get("browser", "chromium")
            headless = inputs.get("headless", True)
            viewport_width = inputs.get("viewport_width", 1280)
            viewport_height = inputs.get("viewport_height", 720)
            slow_mo = inputs.get("slow_mo", 0)
            launch_options_str = inputs.get("launch_options")
            storage_state = inputs.get("storage_state")
            auto_persist = inputs.get("auto_persist", True)

            # 解析启动参数
            launch_options = None
            if launch_options_str:
                try:
                    launch_options = json.loads(launch_options_str)
                except json.JSONDecodeError:
                    return create_failure_result("launch_options 必须是有效的 JSON 字符串")

            # 创建会话
            session_id, session_info = await BrowserManager.create_session(
                browser_type=browser,
                headless=headless,
                viewport_width=viewport_width,
                viewport_height=viewport_height,
                slow_mo=slow_mo,
                launch_options=launch_options,
                storage_state=storage_state,
                auto_persist=auto_persist,
            )

            # 存储会话
            self._sessions[session_id] = session_info

            # 验证会话页面可用（防止浏览器启动后立即崩溃的情况）
            try:
                session, page = self._validate_session_page(session_id)
            except ValueError as e:
                # 启动后页面不可用，清理会话并返回错误
                logger.warning(f"浏览器启动后页面不可用: {e}")
                await BrowserManager.close_session(session_id)
                if session_id in self._sessions:
                    del self._sessions[session_id]
                return create_failure_result(
                    f"浏览器启动后页面不可用: {e}。"
                    f"可能是浏览器依赖缺失，请运行 'playwright install-deps' 安装系统依赖。"
                )

            # 构建返回信息
            message = f"{browser} 浏览器会话已创建"
            restored_state = session_info.get("restored_state")
            if restored_state:
                message += f"，已自动恢复状态: {restored_state}"

            return create_success_result(
                data={
                    "session_id": session_id,
                    "browser_type": browser,
                    "headless": headless,
                    "viewport": {
                        "width": viewport_width,
                        "height": viewport_height,
                    },
                    "auto_persist": auto_persist,
                    "restored_state": restored_state,
                    "message": message,
                }
            )
        except Exception as e:
            logger.error(f"浏览器启动失败: {e}")
            return create_failure_result(f"浏览器启动失败: {str(e)}")

    async def _handle_navigate(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """处理页面导航"""
        try:
            session_id = inputs.get("session_id")
            if not session_id:
                return create_failure_result("session_id 是必填参数")

            # 验证会话和页面健康状态
            try:
                session, page = self._validate_session_page(session_id)
            except ValueError as e:
                return create_failure_result(str(e))

            url = inputs.get("url")
            if not url:
                return create_failure_result("url 是必填参数")

            wait_until = inputs.get("wait_until", "load")
            timeout = inputs.get("timeout", 30000)

            # 导航到目标 URL
            response = await page.goto(url, wait_until=wait_until, timeout=timeout)

            # 获取页面信息
            title = await page.title()
            current_url = page.url

            return create_success_result(
                data={
                    "session_id": session_id,
                    "title": title,
                    "url": current_url,
                    "status": response.status if response else None,
                    "load_state": wait_until,
                    "message": "页面导航成功",
                }
            )
        except ValueError:
            raise  # 让上层的 except 接管 ValueError
        except Exception as e:
            logger.error(f"页面导航失败: {e}")
            # 检测 CDP 连接断开的情况，给出明确提示
            error_msg = str(e)
            if "NoneType" in error_msg and "send" in error_msg:
                return create_failure_result(
                    f"页面导航失败: CDP 连接已断开，浏览器进程可能已崩溃。"
                    f"请关闭当前会话并重新创建。原始错误: {error_msg}"
                )
            return create_failure_result(f"页面导航失败: {error_msg}")

    async def _handle_interact(self, inputs: dict[str, Any]) -> ToolExecutionResult:  # noqa: PLR0911,PLR0912
        """处理元素交互"""
        try:
            session_id = inputs.get("session_id")
            if not session_id:
                return create_failure_result("session_id 是必填参数")

            # 验证会话和页面健康状态
            try:
                session, page = self._validate_session_page(session_id)
            except ValueError as e:
                return create_failure_result(str(e))

            action_type = inputs.get("action_type")
            if not action_type:
                return create_failure_result("action_type 是必填参数")

            selector = inputs.get("selector")
            if not selector:
                return create_failure_result("selector 是必填参数")

            timeout = inputs.get("timeout", 30000)

            # 等待元素出现
            locator = page.locator(selector)
            await locator.wait_for(timeout=timeout)

            result = {"action": action_type, "selector": selector}

            if action_type == "click":
                await locator.click(timeout=timeout)
                result["message"] = "点击成功"

            elif action_type == "type":
                value = inputs.get("value", "")
                await locator.fill(value)
                result["message"] = f"输入文本成功: {value}"

            elif action_type == "select":
                value = inputs.get("value")
                if not value:
                    return create_failure_result("select 操作需要 value 参数")
                await locator.select_option(value)
                result["message"] = f"选择选项成功: {value}"

            elif action_type == "drag":
                target_selector = inputs.get("target_selector")
                if not target_selector:
                    return create_failure_result("drag 操作需要 target_selector 参数")
                await locator.drag_to(page.locator(target_selector))
                result["message"] = "拖拽成功"

            elif action_type == "hover":
                await locator.hover()
                result["message"] = "悬停成功"

            elif action_type == "upload":
                file_paths = inputs.get("file_paths")
                if not file_paths:
                    return create_failure_result("upload 操作需要 file_paths 参数")
                await locator.set_input_files(file_paths)
                result["message"] = f"文件上传成功: {file_paths}"

            else:
                return create_failure_result(f"不支持的交互类型: {action_type}")

            # 获取元素状态
            result["element_visible"] = await locator.is_visible()
            result["element_enabled"] = await locator.is_enabled()

            return create_success_result(data=result)
        except ValueError:
            raise
        except Exception as e:
            logger.error(f"元素交互失败: {e}")
            return create_failure_result(f"元素交互失败: {str(e)}")

    async def _handle_capture_console(self, inputs: dict[str, Any]) -> ToolExecutionResult:  # noqa: PLR0912
        """处理 console 捕获"""
        try:
            session_id = inputs.get("session_id")
            if not session_id:
                return create_failure_result("session_id 是必填参数")

            # 验证会话和页面健康状态
            try:
                session, page = self._validate_session_page(session_id)
            except ValueError as e:
                return create_failure_result(str(e))

            filter_type = inputs.get("filter_type", "all")
            assert_absent = inputs.get("assert_absent", [])
            assert_present = inputs.get("assert_present", [])
            clear = inputs.get("clear", False)

            # 获取 console 消息
            console_messages = session.console_messages

            # 过滤消息
            if filter_type != "all":
                console_messages = [msg for msg in console_messages if msg.get("type") == filter_type]

            # 断言检查
            assertion_results = {
                "passed": True,
                "errors": [],
            }

            # 检查不应存在的消息类型
            # assert_absent 是一个消息类型列表（如 ["error", "exception"]），
            # 表示这些类型的消息不应出现在捕获的 console 中
            if assert_absent:
                absent_types_lower = {t.lower() for t in assert_absent}
                for msg in console_messages:
                    msg_type_lower = msg.get("type", "").lower()
                    # 检查消息类型是否在 absent 列表中
                    if msg_type_lower in absent_types_lower:
                        assertion_results["passed"] = False
                        assertion_results["errors"].append(
                            f"断言失败: 不应存在的 {msg_type_lower} 消息: {msg.get('text', '')}"
                        )

            # 检查必须存在的消息关键词
            if assert_present:
                present_found = dict.fromkeys(assert_present, False)
                for msg in console_messages:
                    for keyword in present_found:
                        if keyword.lower() in msg.get("text", "").lower():
                            present_found[keyword] = True

                for keyword, found in present_found.items():
                    if not found:
                        assertion_results["passed"] = False
                        assertion_results["errors"].append(f"断言失败: 缺少必需消息关键词: {keyword}")

            # 清空 console（如果需要）
            if clear:
                session.console_messages = []

            return create_success_result(
                data={
                    "session_id": session_id,
                    "console_messages": console_messages,
                    "assertion_results": assertion_results,
                    "filter_type": filter_type,
                    "total_messages": len(console_messages),
                }
            )
        except ValueError:
            raise
        except Exception as e:
            logger.error(f"console 捕获失败: {e}")
            return create_failure_result(f"console 捕获失败: {str(e)}")

    async def _handle_screenshot_compare(self, inputs: dict[str, Any]) -> ToolExecutionResult:  # noqa: PLR0911,PLR0912
        """处理截图对比"""
        try:
            session_id = inputs.get("session_id")
            if not session_id:
                return create_failure_result("session_id 是必填参数")

            # 验证会话和页面健康状态
            try:
                session, page = self._validate_session_page(session_id)
            except ValueError as e:
                return create_failure_result(str(e))

            screenshot_action = inputs.get("screenshot_action", "full_page")
            selector = inputs.get("selector")
            baseline_path = inputs.get("baseline_path")
            output_path = inputs.get("output_path")
            threshold = inputs.get("threshold", 0.1)

            if screenshot_action == "full_page":
                result = await ScreenshotManager.capture_full_page(page, output_path)

            elif screenshot_action == "element":
                if not selector:
                    return create_failure_result("element 截图需要 selector 参数")
                result = await ScreenshotManager.capture_element(page, selector, output_path)

            elif screenshot_action == "compare":
                if not baseline_path:
                    return create_failure_result("compare 截图需要 baseline_path 参数")
                if not output_path:
                    return create_failure_result("compare 截图需要 output_path 参数")

                # 先截取当前页面
                compare_result = await ScreenshotManager.capture_full_page(page, output_path)
                if not compare_result.get("success"):
                    return create_failure_result(compare_result.get("error", "截图失败"))

                # 然后对比
                result = ScreenshotManager.compare_images(baseline_path, output_path, threshold)

            else:
                return create_failure_result(f"不支持的截图类型: {screenshot_action}")

            if result.get("success"):
                # MM-3: 截图结果附带多模态内容块，供管道引擎注入下一轮 LLM 调用
                b64 = result.get("base64_data")
                mime = result.get("mime_type", "image/png")
                mm_content: list[dict[str, Any]] | None = None
                if b64:
                    mm_content = [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        }
                    ]

                metadata: dict[str, Any] = {}
                if mm_content:
                    metadata["multimodal_content"] = mm_content

                return create_success_result(
                    data={
                        "session_id": session_id,
                        "action": screenshot_action,
                        **result,
                    },
                    metadata=metadata if metadata else None,
                )
            return create_failure_result(result.get("error", "截图操作失败"))
        except ValueError:
            raise
        except Exception as e:
            logger.error(f"截图对比失败: {e}")
            return create_failure_result(f"截图对比失败: {str(e)}")

    async def _handle_save_state(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """处理保存浏览器状态"""
        try:
            session_id = inputs.get("session_id")
            if not session_id:
                return create_failure_result("session_id 是必填参数")

            state_path = inputs.get("state_path")
            if not state_path:
                return create_failure_result("state_path 是必填参数")

            # 保存会话状态
            result = await BrowserManager.save_session_state(session_id, state_path)

            if result.get("success"):
                return create_success_result(
                    data={
                        "session_id": session_id,
                        "state_path": state_path,
                        "message": "浏览器状态保存成功",
                    }
                )
            return create_failure_result(result.get("error", "保存浏览器状态失败"))
        except Exception as e:
            logger.error(f"保存浏览器状态失败: {e}")
            return create_failure_result(f"保存浏览器状态失败: {str(e)}")

    async def _handle_restore_state(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """处理恢复浏览器状态"""
        try:
            state_path = inputs.get("state_path")
            if not state_path:
                return create_failure_result("state_path 是必填参数")

            browser = inputs.get("browser", "chromium")
            headless = inputs.get("headless", True)

            # 创建新会话并恢复状态
            # 注意：auto_persist=False 防止自动加载旧状态覆盖用户指定的 state_path
            session_id, session_info = await BrowserManager.create_session(
                browser_type=browser,
                headless=headless,
                storage_state=state_path,
                auto_persist=False,
            )

            # 存储会话
            self._sessions[session_id] = session_info

            return create_success_result(
                data={
                    "session_id": session_id,
                    "browser_type": browser,
                    "state_path": state_path,
                    "message": "浏览器状态恢复成功",
                }
            )
        except Exception as e:
            logger.error(f"恢复浏览器状态失败: {e}")
            return create_failure_result(f"恢复浏览器状态失败: {str(e)}")

    async def _handle_evaluate(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """处理 JS 表达式执行，提取页面元素文本和属性值"""
        try:
            session_id = inputs.get("session_id")
            if not session_id:
                return create_failure_result("session_id 是必填参数")

            # 验证会话和页面健康状态
            try:
                session, page = self._validate_session_page(session_id)
            except ValueError as e:
                return create_failure_result(str(e))

            value = inputs.get("value")
            if not value:
                return create_failure_result("value 是必填参数，需提供 JS 表达式")

            # 执行 JS 表达式
            js_result = await page.evaluate(value)

            # 标准化返回值类型
            result_type = type(js_result).__name__
            if js_result is None:
                result_value = None
            elif isinstance(js_result, (list, dict)):
                result_value = js_result
            else:
                result_value = str(js_result)

            return create_success_result(
                data={
                    "session_id": session_id,
                    "expression": value,
                    "result": result_value,
                    "result_type": result_type,
                    "message": "JS 表达式执行成功",
                }
            )

        except ValueError:
            raise
        except Exception as e:
            logger.error(f"JS 表达式执行失败: {e}")
            return create_failure_result(f"JS 表达式执行失败: {str(e)}")

    async def _handle_close(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """处理关闭浏览器"""
        try:
            session_id = inputs.get("session_id")
            if not session_id:
                return create_failure_result("session_id 是必填参数")

            # 关闭会话（BrowserManager.close_session 已内置自动保存）
            result = await BrowserManager.close_session(session_id)

            # 清理本地存储
            if session_id in self._sessions:
                del self._sessions[session_id]

            if result.get("success"):
                # 构建返回信息，提示自动保存结果
                message = result.get("message", "会话已关闭")
                auto_saved = result.get("auto_saved_state")
                if auto_saved:
                    message += f"，状态已自动保存到: {auto_saved}"
                result["message"] = message
                return create_success_result(data=result)
            return create_failure_result(result.get("error", "关闭会话失败"))
        except Exception as e:
            logger.error(f"关闭浏览器失败: {e}")
            return create_failure_result(f"关闭浏览器失败: {str(e)}")
