"""Edge TTS Provider，基于 edge-tts 库实现语音合成。

暴露接口：
- EdgeTTSProvider：Edge TTS 语音合成 Provider
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any

from tools.media.base import MediaProvider, MediaProviderConfig, MediaResult, MediaType

logger = logging.getLogger(__name__)

# 默认语音
_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

# 尝试导入 edge-tts
try:
    import edge_tts
except ImportError:
    edge_tts = None  # type: ignore[assignment]


class EdgeTTSProvider(MediaProvider):
    """基于 Microsoft Edge TTS 的语音合成 Provider。

    使用 edge-tts 库将文本转换为语音，输出 MP3 文件。

    Args:
        config: Provider 配置。
    """

    def __init__(self, config: MediaProviderConfig | None = None) -> None:
        """初始化 EdgeTTSProvider。

        Args:
            config: Provider 配置，默认为空配置。
        """
        if config is None:
            config = MediaProviderConfig(class_name="EdgeTTSProvider")
        super().__init__(
            provider_name="edge_tts",
            media_type=MediaType.TTS,
            config=config,
        )

    async def is_available(self) -> bool:
        """检查 edge-tts 库是否已安装。

        Returns:
            True 表示可用，False 表示不可用。
        """
        return edge_tts is not None

    async def synthesize(
        self,
        text: str,
        voice: str | None = None,
        rate: str | None = None,
        **kwargs: Any,
    ) -> MediaResult:
        """将文本合成为语音 MP3 文件。

        Args:
            text: 要合成的文本内容。
            voice: 语音名称，默认 zh-CN-XiaoxiaoNeural。
            rate: 语速调整（如 "+20%"、"-10%"），默认不变。
            **kwargs: 额外参数（暂未使用）。

        Returns:
            MediaResult: 包含生成的音频文件路径和元数据。

        Raises:
            RuntimeError: edge-tts 库未安装或合成失败。
            ValueError: text 参数为空。
        """
        if edge_tts is None:
            raise RuntimeError("edge-tts 库未安装，请执行 `pip install edge-tts` 后重试")

        if not text or not text.strip():
            raise ValueError("text 参数不能为空")

        voice = voice or _DEFAULT_VOICE

        # 构建语速参数
        rate_param = rate if rate else "+0%"

        # 生成输出文件路径
        output_dir = tempfile.mkdtemp(prefix="tts_")
        file_name = f"{uuid.uuid4().hex}.mp3"
        output_path = Path(output_dir) / file_name

        try:
            communicate = edge_tts.Communicate(text, voice, rate=rate_param)
            await communicate.save(str(output_path))
        except Exception as e:
            raise RuntimeError(f"Edge TTS 语音合成失败: {e}") from e

        return MediaResult(
            file_path=output_path,
            media_type=MediaType.TTS,
            metadata={
                "voice": voice,
                "rate": rate_param,
                "text_length": len(text),
            },
            provider_name=self.provider_name,
        )
