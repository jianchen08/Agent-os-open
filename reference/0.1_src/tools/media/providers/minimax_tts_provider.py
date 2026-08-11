"""MiniMax TTS 语音合成 Provider。

通过 MiniMax API (api.minimaxi.com) 实现 TTS 语音合成，
支持 speech-02-hd 等模型的文本转语音功能，采用同步调用模式。

暴露接口：
- MiniMaxTTSProvider：MiniMax TTS 语音合成 Provider
"""

from __future__ import annotations

import base64
import logging
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from tools.media.base import MediaProvider, MediaProviderConfig, MediaResult, MediaType

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.minimaxi.com/v1"
DEFAULT_MODEL = "speech-02-turbo"
DEFAULT_VOICE_ID = "male-qn-qingse"


class MiniMaxTTSProvider(MediaProvider):
    """MiniMax TTS 语音合成 Provider。

    通过 MiniMax REST API 同步合成语音，支持：
    - 多种模型选择（speech-02-hd、speech-02-turbo 等）
    - 自定义 voice_id、语速、音调、音量
    - 自定义采样率和比特率
    - 多种音频格式输出

    Attributes:
        _api_base: MiniMax API 基础地址
        _api_key: API 密钥
        _model: 模型名称
        _output_dir: 输出目录
        _voice_id: 默认语音 ID
    """

    def __init__(self, config: MediaProviderConfig) -> None:
        """初始化 MiniMaxTTSProvider。

        Args:
            config: Provider 配置，config 字段支持：
                - api_key: API 密钥（必填）
                - model: 模型名称（默认 speech-02-hd）
                - output_dir: 输出目录（默认 ./output/tts）
                - voice_id: 默认语音 ID（默认 male-qn-qingse）
        """
        super().__init__(
            provider_name="minimax_tts",
            media_type=MediaType.TTS,
            config=config,
        )
        cfg = config.config
        self._api_base: str = cfg.get("api_base", DEFAULT_API_BASE)
        self._api_key: str = cfg.get("api_key", "")
        self._model: str = cfg.get("model", DEFAULT_MODEL)
        self._output_dir: Path = Path(cfg.get("output_dir", "./output/tts"))
        self._voice_id: str = cfg.get("voice_id", DEFAULT_VOICE_ID)

    async def is_available(self) -> bool:
        """检查 MiniMax API 是否可用（API Key 已配置）。"""
        return bool(self._api_key)

    async def synthesize(self, text: str, **kwargs: Any) -> MediaResult:
        """将文本合成为语音文件。

        同步调用 MiniMax TTS API，将返回的 base64 音频数据保存为文件。

        Args:
            text: 要合成的文本内容
            **kwargs: 可选参数：
                - voice_id: 语音 ID（覆盖默认值）
                - speed: 语速（0.5-2.0，默认 1.0）
                - pitch: 音调（-12 到 12，默认 0）
                - vol: 音量（0.1-10.0，默认 1.0）
                - format: 音频格式（mp3/wav/pcm/flac，默认 mp3）
                - sample_rate: 采样率（默认 32000）
                - bitrate: 比特率（默认 128000）

        Returns:
            MediaResult 包含生成的音频文件路径和元数据

        Raises:
            RuntimeError: API 调用失败
            ValueError: API Key 未配置或文本为空
        """
        if not self._api_key:
            raise ValueError("MiniMax API Key 未配置")

        if not text or not text.strip():
            raise ValueError("text 参数不能为空")

        payload = self._build_payload(text, **kwargs)

        logger.info(
            "[MiniMax TTS] 提交语音合成: text_length=%d, model=%s, voice=%s",
            len(text),
            self._model,
            payload.get("voice_setting", {}).get("voice_id", self._voice_id),
        )

        response_data = await self._call_api(payload)

        self._output_dir.mkdir(parents=True, exist_ok=True)
        file_path = await self._save_audio(response_data, **kwargs)

        audio_duration = response_data.get("data", {}).get("duration")
        extra_info = response_data.get("extra_info", {})

        metadata: dict[str, Any] = {
            "text_length": len(text),
            "model": self._model,
            "provider": "minimax_tts",
            "voice_id": payload.get("voice_setting", {}).get("voice_id", self._voice_id),
        }
        if audio_duration is not None:
            metadata["duration"] = audio_duration
        if extra_info.get("audio_sample_rate"):
            metadata["sample_rate"] = extra_info["audio_sample_rate"]
        if extra_info.get("bitrate"):
            metadata["bitrate"] = extra_info["bitrate"]

        logger.info("[MiniMax TTS] 文件已保存: %s", file_path)
        return MediaResult(
            file_path=file_path,
            media_type=MediaType.TTS,
            duration_seconds=audio_duration,
            metadata=metadata,
            provider_name=self.provider_name,
        )

    def _build_payload(self, text: str, **kwargs: Any) -> dict[str, Any]:
        """构建 TTS API 请求参数。

        Args:
            text: 要合成的文本
            **kwargs: 可选合成参数

        Returns:
            MiniMax API 请求体字典
        """
        voice_id = kwargs.get("voice_id", self._voice_id)
        speed = kwargs.get("speed", 1.0)
        pitch = kwargs.get("pitch", 0)
        vol = kwargs.get("vol", 1.0)
        audio_format = kwargs.get("format", "mp3")
        sample_rate = kwargs.get("sample_rate", 32000)
        bitrate = kwargs.get("bitrate", 128000)

        payload: dict[str, Any] = {
            "model": self._model,
            "text": text,
            "stream": False,
            "voice_setting": {
                "voice_id": voice_id,
                "speed": float(speed),
                "vol": float(vol),
                "pitch": int(pitch),
            },
            "audio_setting": {
                "sample_rate": int(sample_rate),
                "bitrate": int(bitrate),
                "format": audio_format,
            },
        }

        return payload

    async def _call_api(self, payload: dict[str, Any]) -> dict[str, Any]:
        """调用 MiniMax TTS API。

        Args:
            payload: 请求体字典

        Returns:
            API 响应 JSON 字典

        Raises:
            RuntimeError: HTTP 错误或业务错误
        """
        url = f"{self._api_base}/t2a_v2"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        async with (
            aiohttp.ClientSession() as session,
            session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp,
        ):
            if resp.status != 200:
                error_text = await resp.text()
                raise RuntimeError(f"MiniMax TTS API 调用失败 (status={resp.status}): {error_text}")
            result = await resp.json()

        base_resp = result.get("base_resp", {})
        if base_resp.get("status_code", 0) != 0:
            raise RuntimeError(f"MiniMax TTS 业务错误: {base_resp.get('status_msg', 'unknown')}")

        audio_data = result.get("data", {}).get("audio")
        if not audio_data:
            raise RuntimeError(f"MiniMax TTS 响应中缺少音频数据: {result}")

        return result

    async def _save_audio(self, response_data: dict[str, Any], **kwargs: Any) -> Path:
        """将 base64 音频数据解码并保存为文件。

        Args:
            response_data: MiniMax TTS API 响应体
            **kwargs: 可选参数（用于确定文件扩展名）

        Returns:
            保存后的本地文件路径

        Raises:
            RuntimeError: 音频数据解码失败
        """
        audio_base64 = response_data.get("data", {}).get("audio", "")
        try:
            content = base64.b64decode(audio_base64)
        except Exception as e:
            raise RuntimeError(f"解码音频数据失败: {e}") from e

        audio_format = kwargs.get("format", "mp3")
        ext = audio_format if audio_format in ("mp3", "wav", "flac") else "mp3"
        filename = f"minimax_tts_{uuid.uuid4().hex[:8]}.{ext}"
        output_path = self._output_dir / filename
        output_path.write_bytes(content)
        return output_path


__all__ = ["MiniMaxTTSProvider"]
