"""视频生成工具

暴露接口：
- get_tool_definition() -> Tool：工具定义
- VideoGenerateTool：视频生成工具类
"""

from __future__ import annotations

import logging
from typing import Any

from tools.builtin.base import BuiltinTool
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)

# 没有 Provider 时的友好提示
_NO_PROVIDER_MESSAGE = "视频生成功能暂未配置 Provider，请配置 ComfyUI 等 Provider 后使用"


def _enrich_video_schema(tool: Tool, services: dict[str, Any]) -> Tool:
    """动态注入当前可用的视频 Provider 列表到工具 Schema。"""
    import copy  # noqa: PLC0415

    from tools.media.base import MediaType  # noqa: PLC0415

    media_registry = services.get("media_provider_registry")
    if media_registry is None:
        return tool

    available_providers = media_registry.list_by_type(MediaType.VIDEO)
    if not available_providers:
        return tool

    provider_names = [p.provider_name for p in available_providers]

    enriched = copy.deepcopy(tool)

    enriched.input_schema.setdefault("properties", {})
    enriched.input_schema["properties"]["provider"] = {
        "type": "string",
        "description": (f"指定使用的视频生成服务。当前可用: {', '.join(provider_names)}。不填则自动选择。"),
        "enum": provider_names + ["auto"],
    }

    provider_info = ", ".join(p.provider_name for p in available_providers)
    enriched.description += f"\n\n【当前可用 Provider】: {provider_info}"

    return enriched


class VideoGenerateTool(BuiltinTool):
    """视频生成工具。

    通过 MediaProviderRegistry 获取 VIDEO 类型的 ProviderChain，
    调用 Provider 执行视频生成。当没有可用的 Provider 时，
    优雅降级并返回友好提示信息。

    Args:
        provider_registry: 媒体 Provider 注册表实例，可选。
            如果不提供，execute() 将返回未配置提示。
    """

    def __init__(
        self,
        provider_registry: Any | None = None,
    ) -> None:
        """初始化视频生成工具。

        Args:
            provider_registry: MediaProviderRegistry 实例，可选。
        """
        self._provider_registry = provider_registry

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义。"""
        return Tool(
            name="video_generate",
            description=(
                "视频生成工具。根据文本描述生成视频内容，当前为基线版本，支持通过 ComfyUI 等 Provider 执行生成。"
            ),
            when_to_use=[
                "需要根据文本描述生成视频内容",
                "需要为演示或展示制作短视频片段",
                "需要将创意想法快速转化为视频",
            ],
            when_not_to_use=[
                "需要编辑已有视频（使用视频编辑工具）",
                "需要播放或转码视频（使用对应工具）",
                "需要实时视频流处理",
            ],
            caveats=[
                "视频生成通常为异步长任务，执行时间可能较长",
                "生成质量取决于 Provider 和模型能力",
                "需要配置视频生成 Provider（如 ComfyUI）后才能使用",
            ],
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "视频内容描述，用于指导视频生成（必填）",
                    },
                    "duration": {
                        "type": "number",
                        "description": "视频时长（秒），默认由 Provider 决定",
                    },
                    "fps": {
                        "type": "integer",
                        "description": "帧率（fps），默认由 Provider 决定",
                    },
                    "resolution": {
                        "type": "string",
                        "description": "视频分辨率（如 '1920x1080'），默认由 Provider 决定",
                    },
                    "style": {
                        "type": "string",
                        "description": "视频风格（如 'realistic', 'anime', 'cartoon'），默认由 Provider 决定",
                    },
                    "provider": {
                        "type": "string",
                        "description": "指定使用的视频生成 Provider（不填则自动选择）",
                    },
                },
                "required": ["prompt"],
            },
            source=ToolSource.BUILTIN,
            category=ToolCategory.EXECUTION,
            level=ToolLevel.USER,
            tags=["video", "generate", "media", "creative"],
        )

    def get_schema_enricher(self):
        """获取视频生成工具的 Schema 丰富器。"""
        return _enrich_video_schema

    async def execute(self, inputs: dict[str, Any]) -> Any:
        """执行视频生成。

        尝试通过 MediaProviderRegistry 获取 VIDEO ProviderChain 并调用。
        如果没有可用的 Provider，返回友好的提示信息。

        Args:
            inputs: 工具输入参数，包含 prompt（必填）
                和 duration、fps、resolution、style（可选）。

        Returns:
            ToolExecutionResult: 生成成功时包含文件路径和元数据；
                无 Provider 时包含友好提示；失败时包含错误信息。
        """
        prompt = inputs.get("prompt", "").strip()
        if not prompt:
            return create_failure_result(
                error="prompt 参数不能为空，请提供视频内容描述",
                error_code="MISSING_PROMPT",
            )

        # 尝试获取 ProviderChain
        chain = self._get_provider_chain(inputs)
        if chain is None:
            return self._no_provider_result()

        # 构建可选参数，过滤 None 值
        kwargs = self._build_kwargs(inputs)

        try:
            result = await chain.execute_generate(prompt, **kwargs)
            return create_success_result(
                data={
                    "file_path": str(result.file_path),
                    "media_type": result.media_type.value,
                    "duration_seconds": result.duration_seconds,
                    "provider_name": result.provider_name,
                    "metadata": result.metadata,
                },
                metadata={"action": "video_generate", "provider": result.provider_name},
            )
        except RuntimeError as e:
            logger.warning("[VideoGenerate] Provider 执行失败: %s", e)
            return create_failure_result(
                error=f"视频生成失败: {e}",
                error_code="GENERATE_FAILED",
            )
        except Exception as e:
            logger.error("[VideoGenerate] 未预期的错误: %s", e)
            return create_failure_result(
                error=f"视频生成异常: {e}",
                error_code="GENERATE_FAILED",
            )

    def _resolve_registry(self) -> Any:
        """从 ServiceProvider 懒加载获取 MediaProviderRegistry。

        Returns:
            MediaProviderRegistry 实例，获取失败返回 None
        """
        try:
            from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

            provider = get_service_provider()
            return provider.get("media_provider_registry")
        except Exception:
            return None

    def _get_provider_chain(self, inputs: dict[str, Any] | None = None) -> Any | None:
        """获取 VIDEO 类型的 ProviderChain。

        Args:
            inputs: 工具输入参数，用于提取指定的 provider 名称

        Returns:
            ProviderChain 实例，如果注册表为空或链为空则返回 None。
        """
        if self._provider_registry is None:
            self._provider_registry = self._resolve_registry()

        if self._provider_registry is None:
            return None

        try:
            from tools.media.base import MediaType  # noqa: PLC0415
            from tools.media.fallback import FallbackStrategy  # noqa: PLC0415

            # 处理指定的 Provider
            provider_name = (inputs or {}).get("provider")
            if provider_name:
                provider = self._provider_registry.get(provider_name)
                if provider:
                    return ProviderChain(providers=[provider], strategy=FallbackStrategy.SEQUENTIAL)  # noqa: F821
                logger.warning(
                    "[VideoGenerate] 指定的 Provider '%s' 不存在，使用自动选择",
                    provider_name,
                )

            chain = self._provider_registry.get_chain_for_type(MediaType.VIDEO)
            if chain.providers:
                return chain
            return None
        except Exception as e:
            logger.debug("[VideoGenerate] 获取 ProviderChain 失败: %s", e)
            return None

    def _build_kwargs(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """构建传递给 Provider 的可选参数，过滤掉 None 值。

        Args:
            inputs: 原始输入参数

        Returns:
            非空的可选参数字典
        """
        optional_keys = ("duration", "fps", "resolution", "style")
        kwargs: dict[str, Any] = {}
        for key in optional_keys:
            value = inputs.get(key)
            if value is not None:
                kwargs[key] = value
        return kwargs

    @staticmethod
    def _no_provider_result() -> Any:
        """生成无 Provider 时的友好提示结果。"""
        return create_success_result(
            data={
                "status": "not_configured",
                "message": _NO_PROVIDER_MESSAGE,
            },
            metadata={"action": "video_generate", "fallback": True},
        )
