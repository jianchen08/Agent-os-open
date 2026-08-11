"""语音识别（ASR）服务。

作为多模态体系的一部分，提供"音频→文本"的转写能力，统一服务两类场景：
- 前端语音输入按钮在浏览器 Web Speech API 不可用时（如 network 错误）的服务端降级
- 多模态预处理器中，不支持音频输入的模型收到音频附件时，转成文字 text block

配置驱动：通过 ``config/models/asr.yaml`` 切换服务商，默认指向智谱 GLM-ASR，
将来换讯飞/阿里云只需新增 provider 段并修改 ``default_provider``，无需改代码。

采用业界通用的 OpenAI 兼容契约 ``POST /audio/transcriptions``
（multipart 上传音频，返回 ``{"text": "..."}``），降低切换服务商成本。

暴露接口：
- ASRConfig：ASR 配置 Pydantic 模型
- ASRService：ASR 转写服务
- get_asr_service：模块级单例工厂
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import aiohttp
import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# 默认配置值（仅当 asr.yaml 或环境变量均缺失时使用）
DEFAULT_API_BASE = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "glm-asr-v1"
DEFAULT_LANGUAGE = "zh-CN"
DEFAULT_TIMEOUT_SECONDS = 60

# 音频 MIME → 扩展名映射，用于 multipart 上传时的 filename
_MIME_TO_EXT: dict[str, str] = {
    "audio/webm": "webm",
    "audio/wav": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
}


class ASRConfig(BaseModel):
    """ASR 服务配置。

    Attributes:
        api_base: ASR 服务商 API 基础地址
        api_key: API 密钥（Bearer 鉴权）
        model: 转写模型名称
        language: 识别语言代码（如 zh-CN），可被请求参数覆盖
        enabled: 是否启用 ASR（未配置 key 时自动置为 False）
        timeout: 单次转写请求超时（秒）
    """

    api_base: str = Field(default=DEFAULT_API_BASE, description="ASR 服务商 API 基础地址")
    api_key: str = Field(default="", description="API 密钥")
    model: str = Field(default=DEFAULT_MODEL, description="转写模型名称")
    language: str = Field(default=DEFAULT_LANGUAGE, description="识别语言代码")
    enabled: bool = Field(default=True, description="是否启用 ASR")
    timeout: int = Field(default=DEFAULT_TIMEOUT_SECONDS, description="请求超时（秒）")


def _resolve_env_value(raw: str) -> str:
    """解析配置值中的 ``${ENV_VAR}`` 占位符。

    Args:
        raw: 原始配置值，可能形如 ``${ZHIPU_API_KEY}``

    Returns:
        替换为环境变量值后的字符串；未设置时返回空串
    """
    if not raw:
        return ""
    stripped = raw.strip()
    if stripped.startswith("${") and stripped.endswith("}"):
        env_name = stripped[2:-1].strip()
        return os.environ.get(env_name, "")
    return raw


def load_asr_config(config_path: Path | None = None) -> ASRConfig:
    """从 YAML 配置文件加载 ASR 配置。

    配置文件结构（``config/models/asr.yaml``）::

        asr:
          enabled: true
          default_provider: glm
          providers:
            glm:
              api_base: "https://open.bigmodel.cn/api/paas/v4"
              api_key: "${ZHIPU_API_KEY}"
              model: "glm-asr-v1"
              language: "zh-CN"

    Args:
        config_path: 配置文件路径；为 None 时使用默认路径

    Returns:
        ASRConfig 实例。未找到配置文件或文件缺失关键字段时，回退到环境变量/默认值。
    """
    if config_path is None:
        # 项目根 = src/multimodal/asr.py 向上回溯两级
        config_path = Path(__file__).resolve().parents[2] / "config" / "models" / "asr.yaml"

    if not config_path.exists():
        logger.debug("[ASR] 配置文件不存在: %s，使用默认值", config_path)
        api_key = _resolve_env_value("${ZHIPU_API_KEY}")
        return ASRConfig(api_key=api_key, enabled=bool(api_key))

    try:
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("[ASR] 配置文件解析失败: %s", exc)
        api_key = _resolve_env_value("${ZHIPU_API_KEY}")
        return ASRConfig(api_key=api_key, enabled=bool(api_key))

    asr_section = (raw or {}).get("asr", {}) if isinstance(raw, dict) else {}
    if not isinstance(asr_section, dict):
        return ASRConfig()

    enabled = bool(asr_section.get("enabled", True))
    providers = asr_section.get("providers", {}) or {}
    default_provider = asr_section.get("default_provider", "")

    # 选取 default_provider 对应配置；缺失则取第一个 provider
    provider_conf: dict[str, Any] = {}
    if default_provider and default_provider in providers:
        provider_conf = providers[default_provider]
    elif providers:
        provider_conf = next(iter(providers.values()))

    api_key = _resolve_env_value(str(provider_conf.get("api_key", "")))
    config = ASRConfig(
        api_base=str(provider_conf.get("api_base", DEFAULT_API_BASE)),
        api_key=api_key,
        model=str(provider_conf.get("model", DEFAULT_MODEL)),
        language=str(provider_conf.get("language", DEFAULT_LANGUAGE)),
        enabled=enabled and bool(api_key),
    )
    logger.info(
        "[ASR] 配置已加载: provider=%s, model=%s, enabled=%s",
        default_provider or "(fallback)",
        config.model,
        config.enabled,
    )
    return config


class ASRService:
    """语音识别（ASR）转写服务。

    通过 OpenAI 兼容的 ``POST /audio/transcriptions`` 接口调用配置的 ASR 服务商，
    将音频字节流转写为文本。HTTP 调用模式参考 ``MiniMaxTTSProvider``。

    Attributes:
        _config: ASR 配置
    """

    def __init__(self, config: ASRConfig | None = None) -> None:
        """初始化 ASRService。

        Args:
            config: ASR 配置；为 None 时自动加载默认配置
        """
        self._config = config if config is not None else load_asr_config()

    @property
    def config(self) -> ASRConfig:
        """当前 ASR 配置。"""
        return self._config

    def is_available(self) -> bool:
        """检查 ASR 服务是否可用（已启用且 API Key 已配置）。"""
        return self._config.enabled and bool(self._config.api_key)

    async def transcribe(
        self,
        audio_bytes: bytes,
        mime_type: str,
        language: str | None = None,
    ) -> str:
        """将音频字节流转写为文本。

        通过 multipart 上传音频到 ``{api_base}/audio/transcriptions``，
        返回响应中的 ``text`` 字段。

        Args:
            audio_bytes: 音频文件二进制内容
            mime_type: 音频 MIME 类型（如 ``audio/webm``）
            language: 识别语言代码，覆盖默认配置；为 None 时用配置默认值

        Returns:
            转写得到的文本

        Raises:
            RuntimeError: ASR 未配置、HTTP 错误、业务错误或响应缺少文本
        """
        if not self.is_available():
            raise RuntimeError("ASR 服务未配置或未启用（缺少 API Key 或 asr.yaml）")

        if not audio_bytes:
            raise ValueError("audio_bytes 不能为空")

        ext = _MIME_TO_EXT.get(mime_type, "webm")
        filename = f"audio.{ext}"
        lang = language or self._config.language
        url = f"{self._config.api_base.rstrip('/')}/audio/transcriptions"

        logger.info(
            "[ASR] 提交转写: size=%d bytes, mime=%s, model=%s, lang=%s",
            len(audio_bytes),
            mime_type,
            self._config.model,
            lang,
        )

        form = aiohttp.FormData()
        form.add_field("file", audio_bytes, filename=filename, content_type=mime_type)
        form.add_field("model", self._config.model)
        form.add_field("language", lang)

        headers = {"Authorization": f"Bearer {self._config.api_key}"}

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    url,
                    data=form,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self._config.timeout),
                ) as resp,
            ):
                if resp.status != 200:
                    error_text = await resp.text()
                    raise RuntimeError(f"ASR API 调用失败 (status={resp.status}): {error_text}")
                result = await resp.json()
        except aiohttp.ClientError as exc:
            raise RuntimeError(f"ASR 网络请求失败: {exc}") from exc

        text = ""
        if isinstance(result, dict):
            text = str(result.get("text", "")).strip()
        # 部分服务商可能在不同的字段返回，做容错
        if not text and isinstance(result, dict):
            for key in ("transcription", "result", "data"):
                val = result.get(key)
                if isinstance(val, str) and val.strip():
                    text = val.strip()
                    break

        if not text:
            raise RuntimeError(f"ASR 响应中缺少转写文本: {result}")

        logger.info("[ASR] 转写完成: text_length=%d", len(text))
        return text


# ---------------------------------------------------------------------------
# 模块级单例工厂
# ---------------------------------------------------------------------------

_asr_service: ASRService | None = None


def get_asr_service() -> ASRService:
    """获取全局 ASR 服务单例。

    首次调用时加载配置并实例化；后续调用直接返回缓存实例。
    """
    global _asr_service  # noqa: PLW0603
    if _asr_service is None:
        _asr_service = ASRService()
    return _asr_service


def reset_asr_service() -> None:
    """重置 ASR 服务单例（主要用于测试）。"""
    global _asr_service  # noqa: PLW0603
    _asr_service = None


__all__ = [
    "ASRConfig",
    "ASRService",
    "get_asr_service",
    "load_asr_config",
    "reset_asr_service",
]
