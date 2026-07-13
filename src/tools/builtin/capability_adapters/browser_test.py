"""
浏览器测试适配器

暴露接口：
- BrowserTestTool：BuiltinTool，在浏览器中渲染页面、执行操作、收集验证数据

后端链: playwright_devtools → playwright → chrome_devtools
"""

import logging
from typing import Any

from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)

from ._base import CapabilityAdapterBase

logger = logging.getLogger(__name__)


class BrowserTestTool(CapabilityAdapterBase):
    """浏览器测试工具，支持多步操作和验证数据收集。"""

    _adapter_name = "browser_test"

    @staticmethod
    def get_tool_definition() -> Tool:
        return Tool(
            name="browser_test",
            description=(
                "在浏览器中渲染页面、执行交互操作、收集验证数据。"
                "支持截图、控制台日志、性能指标等验证类型。"
                "自动回退到可用的后端（playwright-devtools / Playwright / Chrome DevTools）。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url_or_html": {
                        "type": "string",
                        "description": "要渲染的 URL 或 HTML 内容",
                    },
                    "actions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": [
                                        "click",
                                        "type",
                                        "wait",
                                        "scroll",
                                        "select",
                                    ],
                                },
                                "selector": {"type": "string"},
                                "value": {"type": "string"},
                                "wait_ms": {"type": "integer"},
                            },
                            "required": ["type"],
                        },
                        "description": "要执行的浏览器操作序列",
                    },
                    "verify": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "screenshot",
                                "console_log",
                                "performance",
                            ],
                        },
                        "default": ["screenshot", "console_log"],
                        "description": "操作完成后要收集的验证数据类型",
                    },
                },
                "required": ["url_or_html"],
            },
            when_to_use=[
                "需要在浏览器中渲染并验证页面",
                "需要执行点击、输入等交互操作后截图",
                "需要收集控制台日志或性能数据",
                "需要对前端页面进行视觉回归测试",
            ],
            when_not_to_use=[
                "只需要获取网页文本内容（应使用 web_search content_only 模式）",
                "需要修改代码（应使用 file_write）",
            ],
            examples=[
                {
                    "input": {
                        "url_or_html": "http://localhost:3000",
                        "verify": ["screenshot", "console_log"],
                    },
                    "description": "渲染本地页面并截图 + 收集控制台日志",
                },
            ],
            source=ToolSource.BUILTIN,
            category=ToolCategory.EXECUTION,
            level=ToolLevel.USER,
            tags=["browser", "testing", "e2e", "screenshot", "mcp"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        url_or_html = inputs.get("url_or_html", "").strip()
        if not url_or_html:
            return create_failure_result(
                error="url_or_html 不能为空",
                error_code="EMPTY_INPUT",
            )

        actions = inputs.get("actions", [])
        verify = inputs.get("verify", ["screenshot", "console_log"])

        backends = self._get_backends()
        if not backends:
            return self._fail_no_backends()

        last_error: Exception | None = None
        attempted = False
        for backend in backends:
            if not backend.available:
                continue
            attempted = True
            try:
                steps = self._build_steps(backend, url_or_html, actions, verify)
                raw_results = await self._call_backend_multi_step(backend, steps)
                parsed_results = [self._extract_mcp_content(r) for r in raw_results]
                return self._transform_results(parsed_results, backend.name, verify)
            except Exception as e:
                logger.warning(
                    "[BrowserTest] 后端 '%s' 失败: %s",
                    backend.name,
                    e,
                )
                last_error = e

        if not attempted:
            return self._fail_no_backends()

        return create_failure_result(
            error=f"所有后端均失败: {last_error}",
            error_code="ALL_BACKENDS_FAILED",
        )

    def _build_steps(
        self,
        backend: Any,
        url_or_html: str,
        actions: list[dict[str, Any]],
        verify: list[str],
    ) -> list[tuple[str, dict[str, Any]]]:
        """构建多步 MCP 调用序列。"""
        tm = backend.tool_mapping
        steps: list[tuple[str, dict[str, Any]]] = []

        # 1. 导航
        nav_tool = tm.get("navigate", "browser_navigate")
        steps.append((nav_tool, {"url": url_or_html}))

        # 2. 交互操作
        interact_tool = tm.get("interact", "browser_click")
        for action in actions:
            action_type = action.get("type", "click")
            args: dict[str, Any] = {"action": action_type}
            if action.get("selector"):
                args["selector"] = action["selector"]
            if action.get("value"):
                args["value"] = action["value"]
            if action.get("wait_ms"):
                args["wait_ms"] = action["wait_ms"]
            steps.append((interact_tool, args))

        # 3. 验证数据收集
        for vtype in verify:
            if vtype == "screenshot":
                tool = tm.get("screenshot", "browser_screenshot")
                steps.append((tool, {}))
            elif vtype == "console_log":
                tool = tm.get("console", "browser_console")
                steps.append((tool, {}))
            elif vtype == "performance":
                tool = tm.get("performance", "browser_performance")
                steps.append((tool, {}))

        return steps

    def _transform_results(
        self,
        parsed_results: list[Any],
        backend_name: str,
        verify: list[str],
    ) -> ToolResult:
        """将多步 MCP 返回值规范化为统一格式。"""
        screenshot = None
        console_logs = []
        performance = {}

        for result in parsed_results:
            if not isinstance(result, dict):
                continue

            if result.get("type") == "screenshot" or "screenshot" in result or "image" in result:
                screenshot = result.get("screenshot") or result.get("image")
            if result.get("type") == "console_log" or "logs" in result or "console" in result:
                console_logs = result.get("logs") or result.get("console", [])
            if result.get("type") == "performance" or "metrics" in result or "performance" in result:
                performance = result.get("metrics") or result.get("performance", {})

        actions_count = max(0, len(parsed_results) - 1 - len(verify))

        return create_success_result(
            data={
                "screenshot": screenshot,
                "console_logs": console_logs,
                "performance": performance,
                "actions_completed": actions_count,
                "verify_types": verify,
                "backend_used": backend_name,
            },
            metadata={
                "adapter": "browser_test",
                "backend": backend_name,
            },
        )
