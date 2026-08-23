"""图像生成工具（0.2 迁移版）。

通过 MediaProviderRegistry 获取图像 Provider 链，支持 Prompt 模式和工作流模板模式生成图像。

迁移（FP-MIGR 0.1→0.2）：
- 顶层 import 由 0.1 的 ``tools.builtin.base`` / ``tools.types`` / ``tools.media.*``
  改为 ``agentos_plugin_sdk``（Tool / ToolExecutionResult / 枚举 / 结果工厂）+ 就地
  ``_media_core``（MediaType / FallbackStrategy / ProviderChain / MediaProviderRegistry /
  MediaProviderClient / ProviderUnavailable）。
- ``_resolve_registry``（0.1 infrastructure.service_provider 直调）已删除；
  ``ProviderChain`` 从 ``_media_core`` 真实 import，``# noqa: F821`` 消音已删除。

F-MEDIA-2（provider 依赖迁移）：执行分两路径——
1. 注入注册表（构造参数 registry）→ ProviderChain 链式执行（0.1 兼容语义）；
2. 否则经 tool-executor capability 调用后端服务（MediaProviderClient，
   服务契约 media.generate）——调用方未注入时返回**显式错误**
   （error_code=PROVIDER_UNAVAILABLE），**不降级空转**（旧的「Provider 未配置」
   提示已废除）。

暴露接口：
- get_tool_definition() -> Tool：工具定义
- ImageGenerateTool：图像生成工具类
- create_image_generate_tool()：创建工具实例的工厂函数
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

from _media_core import (
    FallbackStrategy,
    MediaProviderClient,
    MediaProviderRegistry,
    MediaType,
    ProviderChain,
    ProviderUnavailable,
)

from agentos_plugin_sdk import (
    BuiltinTool,
    Tool,
    ToolCategory,
    ToolExample,
    ToolExecutionResult,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)


def _enrich_image_schema(tool: Tool, services: dict[str, Any]) -> Tool:
    """动态注入当前可用的图像 Provider 列表到工具 Schema。"""
    import copy  # noqa: PLC0415

    media_registry = services.get("media_provider_registry")
    if media_registry is None:
        return tool

    available_providers = media_registry.list_by_type(MediaType.IMAGE)
    if not available_providers:
        return tool

    provider_names = [p.provider_name for p in available_providers]

    enriched = copy.deepcopy(tool)

    enriched.input_schema.setdefault("properties", {})
    enriched.input_schema["properties"]["provider"] = {
        "type": "string",
        "description": (f"指定使用的图像生成服务。当前可用: {', '.join(provider_names)}。不填则自动选择。"),
        "enum": provider_names + ["auto"],
    }

    provider_info = ", ".join(p.provider_name for p in available_providers)
    enriched.description += f"\n\n【当前可用 Provider】: {provider_info}"

    return enriched


class ImageGenerateTool(BuiltinTool):
    """图像生成工具。

    通过 MediaProviderRegistry 获取 IMAGE 类型的 ProviderChain，
    使用 Fallback 链执行图像生成（注入式兼容路径）；未注入注册表时经
    tool-executor capability 调用后端服务（F-MEDIA-2 主路径）。

    支持两种模式：
    - Prompt 模式：传入文本 prompt，使用 Provider 默认工作流生成图像
    - 工作流模板模式：传入 workflow_template 名称和参数，加载预定义工作流
    """

    def __init__(
        self,
        registry: MediaProviderRegistry | None = None,
        capability_caller: Any | None = None,
    ) -> None:
        """初始化图像生成工具。

        Args:
            registry: MediaProviderRegistry 实例（可选，注入式兼容路径；
                非 None 时走 ProviderChain 链式执行）
            capability_caller: 注入的能力调用 async 函数 `(method, params) -> Any`
                （F-MEDIA-2 主路径——经 tool-executor.invoke 调 media.generate；
                生产环境由插件注入，测试传 AsyncMock）
        """
        self._registry = registry
        self._capability_caller = capability_caller

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
                    "provider": {
                        "type": "string",
                        "description": "指定使用的图像生成 Provider（不填则自动选择）",
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

    def get_schema_enricher(self):
        """获取图像生成工具的 Schema 丰富器。"""
        return _enrich_image_schema

    @staticmethod
    def _build_multimodal_content(file_path: str) -> list[dict[str, Any]] | None:
        """读取生成的图片文件，构建 OpenAI vision 格式的多模态内容块。

        Args:
            file_path: 图片文件路径

        Returns:
            多模态内容块列表，文件不存在或读取失败时返回 None
        """
        if not file_path or not os.path.isfile(file_path):  # noqa: PTH113
            return None
        try:
            with open(file_path, "rb") as f:
                b64_data = base64.b64encode(f.read()).decode("utf-8")
        except OSError:
            logger.warning("[ImageGenerate] 读取图片文件失败: %s", file_path)
            return None

        ext = os.path.splitext(file_path)[1].lower()  # noqa: PTH122
        mime_map = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }
        mime_type = mime_map.get(ext, "image/png")

        return [{"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_data}"}}]

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行图像生成。

        F-MEDIA-2 双路径：注入注册表 → ProviderChain 链式执行（兼容）；
        否则经 capability 调用后端服务（未注入调用方时显式 PROVIDER_UNAVAILABLE）。

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

        kwargs = self._build_kwargs(inputs)

        logger.info(
            "[ImageGenerate] 开始生成: prompt=%s, params=%s",
            prompt[:50],
            list(kwargs.keys()),
        )

        if self._registry is not None:
            # 注入式注册表路径（0.1 ProviderChain 语义保留）
            return await self._execute_via_registry(prompt, inputs, kwargs)
        # F-MEDIA-2 主路径：经 capability 调用后端服务
        return await self._execute_via_capability(prompt, inputs, kwargs)

    def _build_kwargs(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """构建传递给 Provider 的可选参数（过滤无效值）。"""
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

        return kwargs

    async def _execute_via_registry(
        self,
        prompt: str,
        inputs: dict[str, Any],
        kwargs: dict[str, Any],
    ) -> ToolExecutionResult:
        """注入式注册表路径：ProviderChain 链式执行（0.1 兼容语义）。"""
        assert self._registry is not None, "调用方已按 _registry 非空分流"
        try:
            chain = self._registry.get_chain_for_type(
                MediaType.IMAGE,
                strategy=FallbackStrategy.SEQUENTIAL,
            )

            # 处理指定的 Provider
            provider_name = inputs.get("provider")
            if provider_name:
                provider = self._registry.get(provider_name)
                if provider:
                    chain = ProviderChain(providers=[provider], strategy=FallbackStrategy.SEQUENTIAL)
                else:
                    logger.warning(
                        "[ImageGenerate] 指定的 Provider '%s' 不存在，使用自动选择",
                        provider_name,
                    )

            if chain is None:
                return self._provider_unavailable_result()

            result = await chain.execute_generate(prompt, **kwargs)
            return self._build_success_result(result)
        except ProviderUnavailable as e:
            logger.warning("[ImageGenerate] Provider 不可用: %s", e)
            return create_failure_result(
                error=str(e),
                error_code="PROVIDER_UNAVAILABLE",
                metadata={"action": "image_generate"},
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

    async def _execute_via_capability(
        self,
        prompt: str,
        inputs: dict[str, Any],
        kwargs: dict[str, Any],
    ) -> ToolExecutionResult:
        """F-MEDIA-2 主路径：经 tool-executor capability 调用后端服务。

        调用方未注入（无 capability_caller）时返回**显式错误**
        PROVIDER_UNAVAILABLE——不静默空转（产品决定：迁移依赖而非降级）。
        """
        if self._capability_caller is None:
            return self._provider_unavailable_result()

        client = MediaProviderClient(self._capability_caller)
        try:
            result = await client.execute_generate(
                MediaType.IMAGE,
                prompt,
                provider=inputs.get("provider"),
                **kwargs,
            )
        except ProviderUnavailable as e:
            logger.warning("[ImageGenerate] 媒体生成服务不可用: %s", e)
            return create_failure_result(
                error=str(e),
                error_code="PROVIDER_UNAVAILABLE",
                metadata={"action": "image_generate"},
            )
        return self._build_success_result(result)

    @staticmethod
    def _provider_unavailable_result() -> ToolExecutionResult:
        """媒体生成服务不可用的显式错误结果（不降级空转）。"""
        return create_failure_result(
            error=(
                "媒体生成服务不可用（media.generate）：未注入 capability_caller，"
                "且未注入媒体 Provider 注册表"
            ),
            error_code="PROVIDER_UNAVAILABLE",
            metadata={"action": "image_generate"},
        )

    def _build_success_result(self, result: Any) -> ToolExecutionResult:
        """把生成结果映射为统一成功结果（含多模态内容块）。"""
        file_path = str(result.file_path)
        # 构建成功结果
        output_data: dict[str, Any] = {
            "file_path": file_path,
            "media_type": result.media_type.value,
            "provider": result.provider_name,
        }
        if result.metadata:
            output_data["metadata"] = result.metadata

        # MM-3: 构建多模态内容块，供管道引擎注入下一轮 LLM 调用
        multimodal_content = self._build_multimodal_content(file_path)

        return create_success_result(
            data=output_data,
            metadata={
                "action": "image_generate",
                "media_type": "image",
                **({"multimodal_content": multimodal_content} if multimodal_content else {}),
            },
        )


def create_image_generate_tool(
    registry: MediaProviderRegistry | None = None,
    capability_caller: Any | None = None,
) -> ImageGenerateTool:
    """创建图像生成工具实例。

    Args:
        registry: 媒体 Provider 注册表（可选）
        capability_caller: 能力调用 async 函数（可选，F-MEDIA-2 主路径）

    Returns:
        ImageGenerateTool 实例
    """
    return ImageGenerateTool(registry=registry, capability_caller=capability_caller)


__all__ = ["ImageGenerateTool", "create_image_generate_tool"]
