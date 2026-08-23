"""TTS 生成工具（0.2 迁移版）。

使用媒体 Provider 抽象层执行 TTS 文本合成，
将文本转换为语音输出（支持 mp3、wav、ogg 格式）。

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
- TtsGenerateTool：TTS 生成工具类
- create_tts_generate_tool()：创建工具实例的工厂函数
"""

from __future__ import annotations

import logging
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
    ToolExecutionResult,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)


def _enrich_tts_schema(tool: Tool, services: dict[str, Any]) -> Tool:
    """动态注入当前可用的 TTS Provider 列表到工具 Schema。"""
    import copy  # noqa: PLC0415

    media_registry = services.get("media_provider_registry")
    if media_registry is None:
        return tool

    available_providers = media_registry.list_by_type(MediaType.TTS)
    if not available_providers:
        return tool

    provider_names = [p.provider_name for p in available_providers]

    enriched = copy.deepcopy(tool)

    enriched.input_schema.setdefault("properties", {})
    enriched.input_schema["properties"]["provider"] = {
        "type": "string",
        "description": (f"指定使用的 TTS 服务。当前可用: {', '.join(provider_names)}。不填则自动选择。"),
        "enum": provider_names + ["auto"],
    }

    provider_info = ", ".join(p.provider_name for p in available_providers)
    enriched.description += f"\n\n【当前可用 Provider】: {provider_info}"

    return enriched


class TtsGenerateTool(BuiltinTool):
    """TTS 文本转语音工具。

    通过媒体 Provider 抽象层将文本合成为语音，
    支持 mp3、wav、ogg 等多种音频格式。

    Attributes:
        _registry: 媒体 Provider 注册表（注入式兼容路径）
        _capability_caller: 能力调用 async 函数（F-MEDIA-2 主路径）
    """

    SUPPORTED_FORMATS = ("mp3", "wav", "ogg")
    DEFAULT_FORMAT = "mp3"
    DEFAULT_VOICE = "alloy"

    def __init__(
        self,
        registry: MediaProviderRegistry | None = None,
        capability_caller: Any | None = None,
    ) -> None:
        """初始化 TTS 工具。

        Args:
            registry: 媒体 Provider 注册表（可选，注入式兼容路径）
            capability_caller: 注入的能力调用 async 函数 `(method, params) -> Any`
                （F-MEDIA-2 主路径——经 tool-executor.invoke 调 media.generate）
        """
        self._registry = registry
        self._capability_caller = capability_caller

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义。

        Returns:
            Tool 实例
        """
        return Tool(
            name="tts_generate",
            description="将文本转换为语音（TTS）。支持多种语音和音频格式。",
            when_to_use=["当用户需要将文本转换为语音时使用，例如生成语音回复、朗读文本等。"],
            when_not_to_use=["不需要语音输出时不要使用。"],
            caveats=["合成的语音质量取决于可用的 TTS Provider。"],
            input_schema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "要合成的文本内容",
                    },
                    "voice": {
                        "type": "string",
                        "description": "语音名称（如 alloy, echo, fable 等）",
                        "default": "alloy",
                    },
                    "format": {
                        "type": "string",
                        "description": "输出音频格式（mp3, wav, ogg）",
                        "default": "mp3",
                        "enum": ["mp3", "wav", "ogg"],
                    },
                    "speed": {
                        "type": "number",
                        "description": "语速倍率（0.5 ~ 2.0）",
                        "default": 1.0,
                    },
                },
                "required": ["text"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "生成的音频文件路径",
                    },
                    "duration_seconds": {
                        "type": "number",
                        "description": "音频时长（秒）",
                    },
                    "format": {
                        "type": "string",
                        "description": "音频格式",
                    },
                    "voice": {
                        "type": "string",
                        "description": "使用的语音名称",
                    },
                },
            },
            category=ToolCategory.EXECUTION,
            level=ToolLevel.USER,
            source=ToolSource.BUILTIN,
            tags=["tts", "audio", "media", "synthesis"],
        )

    def get_schema_enricher(self):
        """获取 TTS 工具的 Schema 丰富器。"""
        return _enrich_tts_schema

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行 TTS 合成。

        F-MEDIA-2 双路径：注入注册表 → ProviderChain 链式执行（兼容）；
        否则经 capability 调用后端服务（未注入调用方时显式 PROVIDER_UNAVAILABLE）。

        Args:
            inputs: 输入参数，包含 text、voice、format、speed 等

        Returns:
            ToolExecutionResult 包含生成的音频文件信息
        """
        text = inputs.get("text", "").strip()
        if not text:
            return create_failure_result(
                error="文本内容不能为空",
                error_code="EMPTY_TEXT",
                metadata={"action": "tts_generate"},
            )

        voice = inputs.get("voice", self.DEFAULT_VOICE)
        audio_format = inputs.get("format", self.DEFAULT_FORMAT)
        speed = inputs.get("speed", 1.0)

        # 验证音频格式
        if audio_format not in self.SUPPORTED_FORMATS:
            return create_failure_result(
                error=f"不支持的音频格式: {audio_format}，支持: {', '.join(self.SUPPORTED_FORMATS)}",
                error_code="UNSUPPORTED_FORMAT",
                metadata={"action": "tts_generate"},
            )

        # 验证语速范围
        if not (0.5 <= speed <= 2.0):
            return create_failure_result(
                error=f"语速 {speed} 超出范围（0.5 ~ 2.0）",
                error_code="INVALID_SPEED",
                metadata={"action": "tts_generate"},
            )

        logger.info("[TTS] 开始合成: text=%s, voice=%s, format=%s", text[:50], voice, audio_format)

        if self._registry is not None:
            # 注入式注册表路径（0.1 ProviderChain 语义保留）
            return await self._execute_via_registry(text, inputs, voice, audio_format, speed)
        # F-MEDIA-2 主路径：经 capability 调用后端服务
        return await self._execute_via_capability(text, inputs, voice, audio_format, speed)

    async def _execute_via_registry(
        self,
        text: str,
        inputs: dict[str, Any],
        voice: str,
        audio_format: str,
        speed: float,
    ) -> ToolExecutionResult:
        """注入式注册表路径：ProviderChain 链式执行（0.1 兼容语义）。"""
        assert self._registry is not None, "调用方已按 _registry 非空分流"
        try:
            chain = self._registry.get_chain_for_type(
                MediaType.TTS,
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
                        "[TTS] 指定的 Provider '%s' 不存在，使用自动选择",
                        provider_name,
                    )

            if chain is None:
                return self._provider_unavailable_result()

            result = await chain.execute_synthesize(
                text,
                voice=voice,
                format=audio_format,
                speed=speed,
            )
            return create_success_result(
                data={
                    "file_path": str(result.file_path),
                    "duration_seconds": result.duration_seconds,
                    "format": audio_format,
                    "voice": voice,
                    "provider": result.provider_name,
                },
                metadata={
                    "action": "tts_generate",
                    "media_type": "tts",
                },
            )
        except ProviderUnavailable as e:
            logger.warning("[TTS] Provider 不可用: %s", e)
            return create_failure_result(
                error=str(e),
                error_code="PROVIDER_UNAVAILABLE",
                metadata={"action": "tts_generate"},
            )
        except RuntimeError as e:
            logger.warning("[TTS] Provider 合成失败: %s", e)
            return create_failure_result(
                error=str(e),
                error_code="TTS_PROVIDER_FAILED",
                metadata={"action": "tts_generate"},
            )
        except Exception as e:
            logger.error("[TTS] 合成异常: %s", e)
            return create_failure_result(
                error=f"TTS 合成失败: {e}",
                error_code="TTS_FAILED",
                metadata={"action": "tts_generate"},
            )

    async def _execute_via_capability(
        self,
        text: str,
        inputs: dict[str, Any],
        voice: str,
        audio_format: str,
        speed: float,
    ) -> ToolExecutionResult:
        """F-MEDIA-2 主路径：经 tool-executor capability 调用后端服务。

        调用方未注入（无 capability_caller）时返回**显式错误**
        PROVIDER_UNAVAILABLE——不静默空转（产品决定：迁移依赖而非降级）。
        """
        if self._capability_caller is None:
            return self._provider_unavailable_result()

        client = MediaProviderClient(self._capability_caller)
        try:
            result = await client.execute_synthesize(
                MediaType.TTS,
                text,
                provider=inputs.get("provider"),
                voice=voice,
                format=audio_format,
                speed=speed,
            )
        except ProviderUnavailable as e:
            logger.warning("[TTS] 媒体生成服务不可用: %s", e)
            return create_failure_result(
                error=str(e),
                error_code="PROVIDER_UNAVAILABLE",
                metadata={"action": "tts_generate"},
            )
        return create_success_result(
            data={
                "file_path": str(result.file_path),
                "duration_seconds": result.duration_seconds,
                "format": audio_format,
                "voice": voice,
                "provider": result.provider_name,
            },
            metadata={
                "action": "tts_generate",
                "media_type": "tts",
            },
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
            metadata={"action": "tts_generate"},
        )


def create_tts_generate_tool(
    registry: MediaProviderRegistry | None = None,
    capability_caller: Any | None = None,
) -> TtsGenerateTool:
    """创建 TTS 生成工具实例。

    Args:
        registry: 媒体 Provider 注册表（可选）
        capability_caller: 能力调用 async 函数（可选，F-MEDIA-2 主路径）

    Returns:
        TtsGenerateTool 实例
    """
    return TtsGenerateTool(registry=registry, capability_caller=capability_caller)


__all__ = ["TtsGenerateTool", "create_tts_generate_tool"]
