"""图像生成工具。

通过 MediaProviderRegistry 获取图像 Provider 链，支持 Prompt 模式和工作流模板模式生成图像。

暴露接口：
- get_tool_definition() -> Tool：工具定义
- ImageGenerateTool：图像生成工具类
- create_image_generate_tool()：创建工具实例的工厂函数
"""

from __future__ import annotations

import logging
from typing import Any

from tools.builtin.base import BuiltinTool
from tools.media.base import MediaType
from tools.media.fallback import FallbackStrategy
from tools.media.provider_registry import MediaProviderRegistry
from tools.types import (
    Tool,
    ToolCategory,
    ToolExample,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)


class ImageGenerateTool(BuiltinTool):
    """图像生成工具。

    通过 MediaProviderRegistry 获取 IMAGE 类型的 ProviderChain，
    使用 Fallback 链执行图像生成。

    支持两种模式：
    - Prompt 模式：传入文本 prompt，使用 Provider 默认工作流生成图像
    - 工作流模板模式：传入 workflow_template 名称和参数，加载预定义工作流
    """

    def __init__(
        self,
        registry: MediaProviderRegistry | None = None,
    ) -> None:
        """初始化图像生成工具。

        Args:
            registry: MediaProviderRegistry 实例（可选，用于 Provider 查找）
        """
        self._registry = registry

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义。

        Returns:
            Tool 实例，包含完整的工具定义信息
        """
        return Tool(
            name="image_generate",
            description=(
                "图像生成工具。支持两种模式："
                "1) Prompt 模式：传入文本 prompt 生成图像，使用内置默认工作流；"
                "2) 工作流模板模式：传入 workflow_template 名称和参数，加载预定义工作流生成。"
                "生成的图像保存为 PNG 文件。"
            ),
            when_to_use=[
                "需要根据文本描述生成图像",
                "需要使用 ComfyUI 工作流生成图像",
                "需要 AI 绘图/图片生成",
            ],
            when_not_to_use=[
                "需要编辑已有图片（使用图片编辑工具）",
                "需要从网页截图（使用截图工具）",
            ],
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "图像生成提示词（必填），描述想要生成的图像内容",
                    },
                    "negative_prompt": {
                        "type": "string",
                        "description": "负面提示词，描述不想在图像中出现的内容",
                        "default": "",
                    },
                    "width": {
                        "type": "integer",
                        "description": "图像宽度（像素），默认 512",
                        "default": 512,
                    },
                    "height": {
                        "type": "integer",
                        "description": "图像高度（像素），默认 512",
                        "default": 512,
                    },
                    "style": {
                        "type": "string",
                        "description": "图像风格（如 realistic, anime, oil-painting 等）",
                    },
                    "seed": {
                        "type": "integer",
                        "description": "随机种子，-1 为随机种子（可复现结果）",
                        "default": -1,
                    },
                    "workflow_template": {
                        "type": "string",
                        "description": "工作流模板名称（不含 .json 扩展名），不填则使用默认模板",
                    },
                },
                "required": ["prompt"],
            },
            source=ToolSource.BUILTIN,
            category=ToolCategory.SYSTEM,
            level=ToolLevel.USER,
            tags=["image", "generate", "ai", "comfyui", "drawing"],
            examples=[
                ToolExample(
                    input={"prompt": "a beautiful sunset over the ocean"},
                    output={"file_path": "/output/images/ComfyUI_00001_.png"},
                    description="使用默认模板生成日落图像",
                ),
                ToolExample(
                    input={
                        "prompt": "a cat sitting on a tree",
                        "width": 768,
                        "height": 512,
                        "seed": 42,
                    },
                    output={"file_path": "/output/images/ComfyUI_00002_.png"},
                    description="指定参数生成猫的图像",
                ),
            ],
            caveats=[
                "需要 ComfyUI 服务运行在本地或远程",
                "生成时间取决于工作流复杂度和服务器性能",
                "首次使用需要下载模型文件",
            ],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行图像生成。

        通过 MediaProviderRegistry 获取 IMAGE ProviderChain，
        使用 Fallback 策略执行生成。

        Args:
            inputs: 输入参数字典，必须包含 prompt

        Returns:
            ToolExecutionResult 包含生成结果或错误信息
        """
        prompt = inputs.get("prompt", "").strip()
        if not prompt:
            return create_failure_result(
                error="prompt 参数为必填项，不能为空",
                error_code="MISSING_PROMPT",
                metadata={"action": "image_generate"},
            )

        # 构建传递给 Provider 的参数
        kwargs: dict[str, Any] = {}

        optional_str_params = ["negative_prompt", "style", "workflow_template"]
        for param in optional_str_params:
            value = inputs.get(param)
            if value and isinstance(value, str):
                kwargs[param] = value

        optional_int_params = ["width", "height", "seed", "steps"]
        for param in optional_int_params:
            value = inputs.get(param)
            if value is not None and isinstance(value, (int, float)):
                kwargs[param] = int(value)

        # cfg_scale 可以是浮点数
        cfg_scale = inputs.get("cfg_scale")
        if cfg_scale is not None and isinstance(cfg_scale, (int, float)):
            kwargs["cfg_scale"] = float(cfg_scale)

        logger.info(
            "[ImageGenerate] 开始生成: prompt=%s, params=%s",
            prompt[:50],
            list(kwargs.keys()),
        )

        # 尝试使用媒体 Provider 抽象层
        if self._registry:
            try:
                chain = self._registry.get_chain_for_type(
                    MediaType.IMAGE,
                    strategy=FallbackStrategy.SEQUENTIAL,
                )

                result = await chain.execute_generate(prompt, **kwargs)

                # 构建成功结果
                output_data: dict[str, Any] = {
                    "file_path": str(result.file_path),
                    "media_type": result.media_type.value,
                    "provider": result.provider_name,
                }
                if result.metadata:
                    output_data["metadata"] = result.metadata

                return create_success_result(
                    data=output_data,
                    metadata={
                        "action": "image_generate",
                        "media_type": "image",
                    },
                )

            except RuntimeError as e:
                logger.error("[ImageGenerate] 生成失败: %s", e)
                return create_failure_result(
                    error=str(e),
                    error_code="GENERATION_FAILED",
                    metadata={"action": "image_generate"},
                )
            except TimeoutError as e:
                logger.error("[ImageGenerate] 生成超时: %s", e)
                return create_failure_result(
                    error=str(e),
                    error_code="GENERATION_TIMEOUT",
                    metadata={"action": "image_generate"},
                )
            except Exception as e:
                logger.error("[ImageGenerate] 未知错误: %s", e, exc_info=True)
                return create_failure_result(
                    error=f"图像生成失败: {e}",
                    error_code="UNKNOWN_ERROR",
                    metadata={"action": "image_generate"},
                )

        # 无注册表时返回提示
        return create_failure_result(
            error="图像生成 Provider 未配置，请先配置媒体 Provider",
            error_code="NO_PROVIDER",
            metadata={"action": "image_generate"},
        )


def create_image_generate_tool(
    registry: MediaProviderRegistry | None = None,
) -> ImageGenerateTool:
    """创建图像生成工具实例。

    Args:
        registry: 媒体 Provider 注册表（可选）

    Returns:
        ImageGenerateTool 实例
    """
    return ImageGenerateTool(registry=registry)


__all__ = ["ImageGenerateTool", "create_image_generate_tool"]
