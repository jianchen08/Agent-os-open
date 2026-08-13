"""视频生成工具（0.2 迁移版）。

迁移（FP-MIGR 0.1→0.2）：
- 顶层 import 由 0.1 的 ``tools.builtin.base`` / ``tools.types`` / ``tools.media.*``
  改为 ``agentos_plugin_sdk``（Tool / 枚举 / 结果工厂）+ 就地 ``_media_core``
  （MediaType / FallbackStrategy / ProviderChain / MediaProviderClient /
  ProviderUnavailable）。
- ``_resolve_registry``（0.1 infrastructure.service_provider 直调）已删除；
  ``ProviderChain`` 从 ``_media_core`` 真实 import，``# noqa: F821`` 消音已删除。

F-MEDIA-2（provider 依赖迁移）：执行分两路径——
1. 注入注册表（构造参数 provider_registry）→ ProviderChain 链式执行（0.1 兼容语义）；
2. 否则经 tool-executor capability 调用后端服务（MediaProviderClient，
   服务契约 media.generate）——调用方未注入时返回**显式错误**
   （error_code=PROVIDER_UNAVAILABLE）。

产品决定：**不降级空转**——0.1 的「无 Provider 返回 not_configured 成功态」
（会让上层误以为生成已排队）已废除，统一改为显式 PROVIDER_UNAVAILABLE 错误。

暴露接口：
- get_tool_definition() -> Tool：工具定义
- VideoGenerateTool：视频生成工具类
"""

from __future__ import annotations

import logging
from typing import Any

from agentos_plugin_sdk import (
    BuiltinTool,
    Tool,
    ToolCategory,
    ToolExecutionResult,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)
from _media_core import (
    FallbackStrategy,
    MediaProviderClient,
    MediaProviderRegistry,
    MediaType,
    ProviderChain,
    ProviderUnavailable,
)

logger = logging.getLogger(__name__)


def _enrich_video_schema(tool: Tool, services: dict[str, Any]) -> Tool:
    """动态注入当前可用的视频 Provider 列表到工具 Schema。"""
    import copy  # noqa: PLC0415

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
    调用 Provider 执行视频生成（注入式兼容路径）；未注入注册表时经
    tool-executor capability 调用后端服务（F-MEDIA-2 主路径）。

    Args:
        provider_registry: 媒体 Provider 注册表实例，可选。
        capability_caller: 能力调用 async 函数（可选，F-MEDIA-2 主路径）。
    """

    def __init__(
        self,
        provider_registry: MediaProviderRegistry | None = None,
        capability_caller: Any | None = None,
    ) -> None:
        """初始化视频生成工具。

        Args:
            provider_registry: MediaProviderRegistry 实例，可选。
            capability_caller: 注入的能力调用 async 函数 `(method, params) -> Any`
                （经 tool-executor.invoke 调 media.generate；生产环境由插件注入）
        """
        self._provider_registry = provider_registry
        self._capability_caller = capability_caller

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义。"""
        return Tool(
            name="video_generate",
            description=(
                "视频生成工具。根据文本描述生成视频内容，当前为基线版本。"
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
                "需要先配置视频生成 Provider 后才能使用",
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

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行视频生成。

        F-MEDIA-2 双路径：注入注册表 → ProviderChain 链式执行（兼容）；
        否则经 capability 调用后端服务（未注入调用方时显式 PROVIDER_UNAVAILABLE）。
        0.1 的 not_configured 成功态已废除（不降级空转）。

        Args:
            inputs: 工具输入参数，包含 prompt（必填）
                和 duration、fps、resolution、style（可选）。

        Returns:
            ToolExecutionResult: 生成成功时包含文件路径和元数据；
                服务不可用时包含显式错误（error_code=PROVIDER_UNAVAILABLE）。
        """
        prompt = inputs.get("prompt", "").strip()
        if not prompt:
            return create_failure_result(
                error="prompt 参数不能为空，请提供视频内容描述",
                error_code="MISSING_PROMPT",
            )

        kwargs = self._build_kwargs(inputs)

        if self._provider_registry is not None:
            # 注入式注册表路径（0.1 ProviderChain 语义保留）
            chain = self._get_provider_chain(inputs)
            if chain is None:
                return self._provider_unavailable_result()
            return await self._execute_via_registry(prompt, chain, kwargs)
        # F-MEDIA-2 主路径：经 capability 调用后端服务
        return await self._execute_via_capability(prompt, inputs, kwargs)

    async def _execute_via_registry(
        self,
        prompt: str,
        chain: Any,
        kwargs: dict[str, Any],
    ) -> ToolExecutionResult:
        """注入式注册表路径：ProviderChain 链式执行（0.1 兼容语义）。"""
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
        except ProviderUnavailable as e:
            logger.warning("[VideoGenerate] Provider 不可用: %s", e)
            return create_failure_result(
                error=str(e),
                error_code="PROVIDER_UNAVAILABLE",
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
                MediaType.VIDEO,
                prompt,
                provider=inputs.get("provider"),
                **kwargs,
            )
        except ProviderUnavailable as e:
            logger.warning("[VideoGenerate] 媒体生成服务不可用: %s", e)
            return create_failure_result(
                error=str(e),
                error_code="PROVIDER_UNAVAILABLE",
            )
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

    @staticmethod
    def _provider_unavailable_result() -> ToolExecutionResult:
        """媒体生成服务不可用的显式错误结果（不降级空转）。"""
        return create_failure_result(
            error=(
                "媒体生成服务不可用（media.generate）：未注入 capability_caller，"
                "且未注入媒体 Provider 注册表"
            ),
            error_code="PROVIDER_UNAVAILABLE",
        )

    def _get_provider_chain(self, inputs: dict[str, Any] | None = None) -> Any | None:
        """获取 VIDEO 类型的 ProviderChain。

        Args:
            inputs: 工具输入参数，用于提取指定的 provider 名称

        Returns:
            ProviderChain 实例，如果注册表为空或链为空则返回 None。
        """
        if self._provider_registry is None:
            return None

        try:
            # 处理指定的 Provider
            provider_name = (inputs or {}).get("provider")
            if provider_name:
                provider = self._provider_registry.get(provider_name)
                if provider:
                    return ProviderChain(providers=[provider], strategy=FallbackStrategy.SEQUENTIAL)
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
