"""TTS 生成工具。

使用媒体 Provider 抽象层执行 TTS 文本合成，
将文本转换为语音输出（支持 mp3、wav、ogg 格式）。

暴露接口：
- TtsGenerateTool：TTS 生成工具类
- create_tts_generate_tool()：创建工具实例的工厂函数
"""

from __future__ import annotations

import logging
from typing import Any

from tools.builtin.base import BuiltinTool
from tools.media.base import MediaType
from tools.media.fallback import FallbackStrategy
from tools.media.provider_registry import MediaProviderRegistry
from tools.types import Tool
from tools.types import create_failure_result, create_success_result

logger = logging.getLogger(__name__)


class TtsGenerateTool(BuiltinTool):
    """TTS 文本转语音工具。

    通过媒体 Provider 抽象层将文本合成为语音，
    支持 mp3、wav、ogg 等多种音频格式。

    Attributes:
        _registry: 媒体 Provider 注册表
    """

    SUPPORTED_FORMATS = ("mp3", "wav", "ogg")
    DEFAULT_FORMAT = "mp3"
    DEFAULT_VOICE = "alloy"

    def __init__(
        self,
        registry: MediaProviderRegistry | None = None,
    ) -> None:
        """初始化 TTS 工具。

        Args:
            registry: 媒体 Provider 注册表（可选，用于 Provider 查找）
        """
        self._registry = registry

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义。

        Returns:
            Tool 实例
        """
        return Tool(
            name="tts_generate",
            description="将文本转换为语音（TTS）。支持多种语音和音频格式。",
            when_to_use="当用户需要将文本转换为语音时使用，例如生成语音回复、朗读文本等。",
            when_not_to_use="不需要语音输出时不要使用。",
            caveats="合成的语音质量取决于可用的 TTS Provider。",
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
            category="media",
            level="builtin",
            tags=["tts", "audio", "media", "synthesis"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行 TTS 合成。

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

        # 尝试使用媒体 Provider 抽象层
        if self._registry:
            try:
                chain = self._registry.get_chain_for_type(
                    MediaType.TTS,
                    strategy=FallbackStrategy.SEQUENTIAL,
                )
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

        # 无注册表时返回提示
        return create_failure_result(
            error="TTS Provider 未配置，请先配置媒体 Provider",
            error_code="NO_PROVIDER",
            metadata={"action": "tts_generate"},
        )


def create_tts_generate_tool(
    registry: MediaProviderRegistry | None = None,
) -> TtsGenerateTool:
    """创建 TTS 生成工具实例。

    Args:
        registry: 媒体 Provider 注册表（可选）

    Returns:
        TtsGenerateTool 实例
    """
    return TtsGenerateTool(registry=registry)


__all__ = ["TtsGenerateTool", "create_tts_generate_tool"]
