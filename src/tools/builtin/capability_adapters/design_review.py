"""
设计审查适配器

暴露接口：
- DesignReviewTool：BuiltinTool，对比设计参考与实现，识别视觉问题

后端链: playwright_screenshot → figma_compare
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


class DesignReviewTool(CapabilityAdapterBase):
    """设计审查工具，对比设计参考与实现页面。"""

    _adapter_name = "design_review"

    @staticmethod
    def get_tool_definition() -> Tool:
        return Tool(
            name="design_review",
            description=(
                "对比设计参考与实际实现，识别视觉差异和问题。"
                "支持 Figma URL、截图、文字描述作为设计参考，"
                "从布局、颜色、字体、间距、响应式等维度进行检查。"
                "自动回退到可用的后端。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "design_reference": {
                        "type": "string",
                        "description": "设计参考：Figma URL、截图文件路径、或文字描述",
                    },
                    "reference_type": {
                        "type": "string",
                        "enum": ["figma_url", "screenshot", "description"],
                        "default": "description",
                        "description": "设计参考的类型",
                    },
                    "implementation_url": {
                        "type": "string",
                        "description": "实现页面的 URL",
                    },
                    "check_types": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "layout",
                                "color",
                                "typography",
                                "responsive",
                                "spacing",
                            ],
                        },
                        "default": ["layout", "color", "typography"],
                        "description": "要检查的视觉维度",
                    },
                },
                "required": ["design_reference", "implementation_url"],
            },
            when_to_use=[
                "需要对比设计稿与实际实现的视觉差异",
                "需要自动化设计审查（布局、颜色、字体等）",
                "需要在前端开发完成后进行设计还原度检查",
            ],
            when_not_to_use=[
                "需要功能逻辑测试（应使用 browser_test）",
                "需要代码质量检查（应使用 code_reviewer agent）",
            ],
            source=ToolSource.BUILTIN,
            category=ToolCategory.ANALYSIS,
            level=ToolLevel.USER,
            tags=["design", "review", "visual", "comparison", "mcp"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        design_reference = inputs.get("design_reference", "").strip()
        implementation_url = inputs.get("implementation_url", "").strip()

        if not design_reference:
            return create_failure_result(
                error="design_reference 不能为空",
                error_code="EMPTY_INPUT",
            )
        if not implementation_url:
            return create_failure_result(
                error="implementation_url 不能为空",
                error_code="EMPTY_INPUT",
            )

        reference_type = inputs.get("reference_type", "description")
        check_types = inputs.get(
            "check_types", ["layout", "color", "typography"]
        )

        backends = self._get_backends()
        if not backends:
            return self._fail_no_backends()

        last_error: Exception | None = None
        for backend in backends:
            if not backend.available:
                continue

            try:
                steps = self._build_steps(
                    backend,
                    design_reference=design_reference,
                    reference_type=reference_type,
                    implementation_url=implementation_url,
                    check_types=check_types,
                )
                raw_results = await self._call_backend_multi_step(
                    backend, steps
                )
                parsed_results = [
                    self._extract_mcp_content(r) for r in raw_results
                ]
                return self._transform_results(
                    parsed_results, backend.name, check_types
                )
            except Exception as e:
                logger.warning(
                    "[DesignReview] 后端 '%s' 失败: %s",
                    backend.name,
                    e,
                )
                last_error = e

        return create_failure_result(
            error=f"所有后端均失败: {last_error}",
            error_code="ALL_BACKENDS_FAILED",
        )

    def _build_steps(
        self,
        backend: Any,
        design_reference: str,
        reference_type: str,
        implementation_url: str,
        check_types: list[str],
    ) -> list[tuple[str, dict[str, Any]]]:
        """构建审查步骤：截图设计参考 → 截图实现 → 对比。"""
        tm = backend.tool_mapping
        steps: list[tuple[str, dict[str, Any]]] = []

        # 1. 截图设计参考
        screenshot_tool = tm.get("screenshot", "browser_screenshot")
        if reference_type == "figma_url":
            steps.append((screenshot_tool, {"url": design_reference}))
        elif reference_type == "screenshot":
            steps.append(
                (screenshot_tool, {"image_path": design_reference})
            )
        else:
            steps.append(
                (
                    screenshot_tool,
                    {"description": design_reference},
                )
            )

        # 2. 截图实现页面
        steps.append((screenshot_tool, {"url": implementation_url}))

        # 3. 对比
        compare_tool = tm.get("compare", "visual_compare")
        steps.append(
            (
                compare_tool,
                {
                    "check_types": check_types,
                },
            )
        )

        return steps

    def _transform_results(
        self,
        parsed_results: list[Any],
        backend_name: str,
        check_types: list[str],
    ) -> ToolResult:
        """将审查结果规范化为统一格式。"""
        issues = []
        visual_diff = None
        score = None

        for result in parsed_results:
            if not isinstance(result, dict):
                continue

            if "issues" in result:
                issues = result["issues"]
            if "diff" in result or "visual_diff" in result:
                visual_diff = result.get("diff") or result.get("visual_diff")
            if "score" in result or "similarity" in result:
                score = result.get("score") or result.get("similarity")

        high_count = sum(
            1 for i in issues if isinstance(i, dict) and i.get("severity") == "high"
        )
        medium_count = sum(
            1
            for i in issues
            if isinstance(i, dict) and i.get("severity") == "medium"
        )
        low_count = len(issues) - high_count - medium_count

        summary = (
            f"发现 {len(issues)} 个问题: "
            f"{high_count} 高 / {medium_count} 中 / {low_count} 低"
        )

        return create_success_result(
            data={
                "issues": issues,
                "visual_diff": visual_diff,
                "summary": summary,
                "score": score,
                "check_types": check_types,
                "backend_used": backend_name,
            },
            metadata={
                "adapter": "design_review",
                "backend": backend_name,
            },
        )
