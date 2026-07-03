"""
UI 代码生成适配器

暴露接口：
- DesignGenerateTool：BuiltinTool，从文字描述/截图生成前端 UI 代码

后端链: screenshot_to_code → stitch → magic
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


class DesignGenerateTool(CapabilityAdapterBase):
    """UI 代码生成工具，包装多个 MCP 后端。"""

    _adapter_name = "design_generate"

    @staticmethod
    def get_tool_definition() -> Tool:
        return Tool(
            name="design_generate",
            description=(
                "从文字描述或截图生成前端 UI 代码。"
                "支持多种输出格式（React/HTML/Tailwind），"
                "自动回退到可用的后端（screenshot-to-code / Stitch / Magic）。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "UI 描述文字，或截图文件路径（当 input_type=screenshot 时）",
                    },
                    "input_type": {
                        "type": "string",
                        "enum": ["text", "screenshot", "url"],
                        "default": "text",
                        "description": "输入类型：文字描述、截图文件路径、URL 引用",
                    },
                    "style_preferences": {
                        "type": "string",
                        "description": "风格指引，如 'modern', 'minimal', 'dark theme'",
                    },
                    "output_format": {
                        "type": "string",
                        "enum": ["react", "html", "tailwind"],
                        "default": "react",
                        "description": "输出代码格式",
                    },
                },
                "required": ["description"],
            },
            when_to_use=[
                "需要从文字描述或设计稿生成前端 UI 代码",
                "需要快速原型化 UI 组件或页面",
                "需要将截图转换为可用的前端代码",
            ],
            when_not_to_use=[
                "需要生成后端代码或 API 逻辑",
                "需要修改现有代码（应使用 file_write 或 code_writer agent）",
            ],
            source=ToolSource.BUILTIN,
            category=ToolCategory.WEB,
            level=ToolLevel.USER,
            tags=["design", "code-generation", "ui", "mcp"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        description = inputs.get("description", "").strip()
        if not description:
            return create_failure_result(
                error="description 不能为空",
                error_code="EMPTY_INPUT",
            )

        input_type = inputs.get("input_type", "text")
        output_format = inputs.get("output_format", "react")
        style_preferences = inputs.get("style_preferences", "")

        backends = self._get_backends()
        if not backends:
            return self._fail_no_backends()

        mcp_args = self._build_mcp_args(
            description=description,
            input_type=input_type,
            output_format=output_format,
            style_preferences=style_preferences,
        )

        last_error: Exception | None = None
        attempted = False
        for backend in backends:
            if not backend.available:
                continue
            attempted = True

            mcp_tool_name = backend.tool_mapping.get("generate", "generate")
            try:
                raw_result = await self._call_backend(backend, mcp_tool_name, mcp_args)
                parsed = self._extract_mcp_content(raw_result)
                return self._transform_result(parsed, backend.name, output_format)
            except Exception as e:
                logger.warning(
                    "[DesignGenerate] 后端 '%s' 失败: %s",
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

    def _build_mcp_args(
        self,
        description: str,
        input_type: str,
        output_format: str,
        style_preferences: str,
    ) -> dict[str, Any]:
        """构建传给 MCP 后端的参数。"""
        search_query = description[:60] if len(description) > 60 else description
        args: dict[str, Any] = {
            "message": description,
            "searchQuery": search_query,
        }
        return args

    def _transform_result(
        self,
        parsed: Any,
        backend_name: str,
        output_format: str,
    ) -> ToolResult:
        """将后端返回值规范化为统一格式。"""
        if isinstance(parsed, dict):
            code = parsed.get("code") or parsed.get("html") or ""
            files = parsed.get("files", [])
            preview_url = parsed.get("preview_url") or parsed.get("url")
        elif isinstance(parsed, str):
            code = parsed
            files = []
            preview_url = None
        else:
            code = str(parsed)
            files = []
            preview_url = None

        return create_success_result(
            data={
                "code": code,
                "language": output_format,
                "files": files,
                "preview_url": preview_url,
                "backend_used": backend_name,
            },
            metadata={
                "adapter": "design_generate",
                "backend": backend_name,
            },
        )
