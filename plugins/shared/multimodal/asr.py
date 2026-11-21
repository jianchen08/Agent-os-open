"""语音识别（ASR）服务。

作为多模态体系的一部分，提供"音频→文本"的转写能力，统一服务两类场景：
- 前端语音输入按钮在浏览器 Web Speech API 不可用时（如 network 错误）的服务端降级
- 多模态预处理器中，不支持音频输入的模型收到音频附件时，转成文字 text block

配置驱动：通过 ``config/models/asr.yaml`` 声明 provider 与尝试顺序，默认本地
SenseVoiceSmall（sherpa-onnx，纯 CPU、离线、零费用），本地不可用时按链回退
免费远端（SiliconFlow SenseVoiceSmall），再次回退既有付费智谱 GLM-ASR。
配置真值走用户空间优先解析（ADR 2026-09-13-unified-user-root：用户层存在该
文件即整体替换工厂配置），与内核 config_files 读写同一落点；服务实例按 stat
签名探测配置变更并热重载（设置页保存免重启生效——watcher 只扫 plugins 目录，
config/ 变更不触发插件 respawn）。

远端采用业界通用的 OpenAI 兼容契约 ``POST /audio/transcriptions``
（multipart 上传音频，返回 ``{"text": "..."}``），降低切换服务商成本；
本地引擎由 sherpa-onnx 在进程内解码 16-bit PCM WAV（多声道先下混单声道）。

配置结构（两种形态等价，旧形态 = 单 provider 远端）::

    asr:
      enabled: true
      default_provider: local
      fallback_chain: [local, siliconflow, glm]   # 按序尝试，跳过不可用者
      providers:
        local: {type: local, engine: sensevoice, model_dir: "", ...}
        siliconflow: {type: openai, api_base: ..., api_key: "${SILICONFLOW_API_KEY}", ...}

    asr:
      enabled: true
      default_provider: glm
      providers:
        glm: {api_base: ..., api_key: "${ZHIPU_API_KEY}", model: glm-asr, ...}

暴露接口：
- ASRConfig：ASR 配置 Pydantic 模型（含旧扁平字段与 provider 链）
- ASRProviderConfig：单个 provider 配置
- ASRService：ASR 转写服务
- get_asr_service：模块级单例工厂
"""

from __future__ import annotations

import array
import asyncio
import io
import logging
import os
import re
import ssl
import threading
import time
import urllib.request
import wave
from pathlib import Path
from typing import Any

import aiohttp
import yaml
from _root_api import user_space
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# 默认配置值（仅当 asr.yaml 或环境变量均缺失时使用）
DEFAULT_API_BASE = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "glm-asr"
DEFAULT_LANGUAGE = "zh-CN"
DEFAULT_TIMEOUT_SECONDS = 60

# 本地 SenseVoiceSmall（sherpa-onnx）默认坐标：模型目录名与下载源
SENSEVOICE_MODEL_DIR_NAME = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
DEFAULT_LOCAL_DOWNLOAD_BASE = (
    "https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main"
)
DEFAULT_LOCAL_MODEL_FILE = "model.int8.onnx"
DEFAULT_LOCAL_TOKENS_FILE = "tokens.txt"

# SenseVoice 输出中的语言/情感/事件等特殊标记（如 <|zh|><|NEUTRAL|><|Speech|>）
_SENSEVOICE_TOKEN_RE = re.compile(r"<\|[^|]*\|>")
_DOWNLOAD_CHUNK_BYTES = 1 << 20

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


class ASRProviderConfig(BaseModel):
    """单个 ASR provider 配置。

    ``type=local`` 走进程内 sherpa-onnx SenseVoice；其余值（默认 ``openai``）
    走 OpenAI 兼容的远端 ``/audio/transcriptions`` 契约。两类字段同模型承载，
    未使用的一侧保持默认值。

    Attributes:
        type: provider 类型（``local`` / ``openai``）
        api_base / api_key / model / language / timeout: 远端字段
        engine: 本地引擎（当前仅 ``sensevoice``）
        model_dir: 本地模型目录（空 = 用户空间默认坐标）
        model_file / tokens_file: 本地模型目录内的文件名
        num_threads: 本地推理线程数
        use_itn: 本地结果是否带逆文本规整（数字/标点）
        auto_download: 模型缺失时是否自动下载
        download_base: 模型文件下载基址（逐文件拼接文件名）
        download_timeout: 单文件下载超时（秒）
    """

    type: str = Field(default="openai", description="provider 类型：local / openai")
    # —— 远端（type=openai）——
    api_base: str = Field(default=DEFAULT_API_BASE, description="ASR 服务商 API 基础地址")
    api_key: str = Field(default="", description="API 密钥")
    model: str = Field(default=DEFAULT_MODEL, description="转写模型名称")
    language: str = Field(default=DEFAULT_LANGUAGE, description="识别语言代码")
    timeout: int = Field(default=DEFAULT_TIMEOUT_SECONDS, description="请求超时（秒）")
    # —— 本地（type=local）——
    engine: str = Field(default="sensevoice", description="本地引擎（当前仅 sensevoice）")
    model_dir: str = Field(default="", description="本地模型目录（空 = 用户空间默认）")
    model_file: str = Field(default=DEFAULT_LOCAL_MODEL_FILE, description="模型文件名")
    tokens_file: str = Field(default=DEFAULT_LOCAL_TOKENS_FILE, description="tokens 文件名")
    num_threads: int = Field(default=2, description="本地推理线程数")
    use_itn: bool = Field(default=True, description="本地结果启用逆文本规整")
    auto_download: bool = Field(default=True, description="模型缺失时自动下载")
    download_base: str = Field(default=DEFAULT_LOCAL_DOWNLOAD_BASE, description="模型下载基址")
    download_timeout: int = Field(default=900, description="单文件下载超时（秒）")


class ASRConfig(BaseModel):
    """ASR 服务配置。

    Attributes:
        api_base: 远端 ASR 服务商 API 基础地址（旧单 provider 形态）
        api_key: API 密钥（Bearer 鉴权；旧单 provider 形态）
        model: 转写模型名称（旧单 provider 形态）
        language: 识别语言代码（如 zh-CN），可被请求参数覆盖
        enabled: 是否启用 ASR（总开关）
        timeout: 单次转写请求超时（秒；旧单 provider 形态）
        default_provider: 首选 provider 名（providers 的键）
        fallback_chain: 备选 provider 名序列（按序尝试，自动去重）
        providers: provider 名 → 配置
    """

    api_base: str = Field(default=DEFAULT_API_BASE, description="ASR 服务商 API 基础地址")
    api_key: str = Field(default="", description="API 密钥")
    model: str = Field(default=DEFAULT_MODEL, description="转写模型名称")
    language: str = Field(default=DEFAULT_LANGUAGE, description="识别语言代码")
    enabled: bool = Field(default=True, description="是否启用 ASR")
    timeout: int = Field(default=DEFAULT_TIMEOUT_SECONDS, description="请求超时（秒）")
    default_provider: str = Field(default="glm", description="首选 provider 名")
    fallback_chain: list[str] = Field(default_factory=list, description="备选 provider 名序列")
    providers: dict[str, ASRProviderConfig] = Field(
        default_factory=dict, description="provider 名 → 配置（空 = 旧单 provider 形态）"
    )


class _SkipProvider(RuntimeError):
    """该 provider 不适用于本次请求（非 WAV / 缺模型等），链上继续下一 provider。"""


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


def _factory_config_path() -> Path:
    """工厂配置路径（项目根 = plugins/shared/multimodal/asr.py 向上回溯四级）。"""
    return Path(__file__).resolve().parents[3] / "config" / "models" / "asr.yaml"


def _default_config_path() -> Path:
    """配置落点解析：用户层存在该文件即用户层，否则工厂路径。

    与内核 ``config_files`` 读写同一套落点语义（``resolve_config_path`` 的
    文件级整体替换），保证设置页写哪份、本服务读哪份。
    """
    user_dir = user_space.user_config_dir()
    if user_dir is not None:
        candidate = user_dir / "models" / "asr.yaml"
        if candidate.is_file():
            return candidate
    return _factory_config_path()


def _stat_signature(path: Path) -> tuple[int, int] | None:
    """文件 stat 签名（mtime_ns, size）；不可 stat（缺失等）返回 None。"""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _parse_provider(name: str, raw: Any) -> ASRProviderConfig:
    """解析单个 provider 段；非映射或字段非法时降级为默认配置并告警。"""
    if not isinstance(raw, dict):
        logger.warning("[ASR] provider %s 配置非映射，按默认值处理", name)
        return ASRProviderConfig()
    known = {key: value for key, value in raw.items() if key in ASRProviderConfig.model_fields}
    try:
        return ASRProviderConfig(**known)
    except ValueError as exc:
        logger.warning("[ASR] provider %s 配置解析失败，按默认值处理: %s", name, exc)
        return ASRProviderConfig()


def _parse_str_list(raw: Any) -> list[str]:
    """解析字符串列表（忽略空项/非字符串项）；非列表返回空。"""
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str) and item.strip()]


def _resolve_env_in_provider(provider: ASRProviderConfig) -> ASRProviderConfig:
    """provider 的 api_key 占位符解析（``${ENV_VAR}`` → 环境变量值）。"""
    return provider.model_copy(update={"api_key": _resolve_env_value(provider.api_key)})


def _legacy_flat_view(
    default_provider: str,
    fallback_chain: list[str],
    providers: dict[str, ASRProviderConfig],
) -> ASRProviderConfig | None:
    """旧扁平字段（api_base/api_key/model/language/timeout）的取值来源。

    语义 = "远端单 provider 视图"：优先默认 provider（远端时），否则按尝试
    顺序取首个远端 provider。全部为本地时退回默认/首个 provider。
    """
    for name in [default_provider, *fallback_chain]:
        provider = providers.get(name)
        if provider is not None and provider.type.strip().lower() != "local":
            return provider
    if default_provider in providers:
        return providers[default_provider]
    for provider in providers.values():
        if provider.type.strip().lower() != "local":
            return provider
    return next(iter(providers.values())) if providers else None


def _service_may_run(providers: dict[str, ASRProviderConfig]) -> bool:
    """配置层可用性（fail-closed）：有本地 provider 或至少一个远端 provider 配了 key。"""
    return any(
        provider.type.strip().lower() == "local" or bool(provider.api_key)
        for provider in providers.values()
    )


def load_asr_config(config_path: Path | None = None) -> ASRConfig:
    """从 YAML 配置文件加载 ASR 配置。

    配置文件结构见模块文档字符串。旧形态（``asr.providers`` 单远端段）与
    新形态（``default_provider`` + ``fallback_chain`` + 多 provider）等价映射：
    旧形态下 provider 链即该单个 provider。

    Args:
        config_path: 配置文件路径；为 None 时使用默认路径

    Returns:
        ASRConfig 实例。未找到配置文件或文件缺失关键字段时，回退到环境变量/默认值。
    """
    if config_path is None:
        config_path = _default_config_path()

    if not config_path.exists():
        logger.debug("[ASR] 配置文件不存在: %s，使用默认值", config_path)
        api_key = _resolve_env_value("${ZHIPU_API_KEY}")
        return ASRConfig(api_key=api_key, enabled=bool(api_key))

    try:
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        logger.warning("[ASR] 配置文件解析失败: %s", exc)
        api_key = _resolve_env_value("${ZHIPU_API_KEY}")
        return ASRConfig(api_key=api_key, enabled=bool(api_key))

    asr_section = (raw or {}).get("asr", {}) if isinstance(raw, dict) else {}
    if not isinstance(asr_section, dict):
        return ASRConfig()

    enabled = bool(asr_section.get("enabled", True))
    default_provider = str(asr_section.get("default_provider", "") or "")
    fallback_chain = _parse_str_list(asr_section.get("fallback_chain"))

    providers: dict[str, ASRProviderConfig] = {}
    raw_providers = asr_section.get("providers") or {}
    if isinstance(raw_providers, dict):
        for name, raw_provider in raw_providers.items():
            providers[str(name)] = _resolve_env_in_provider(_parse_provider(str(name), raw_provider))

    flat = _legacy_flat_view(default_provider, fallback_chain, providers)
    config = ASRConfig(
        api_base=flat.api_base if flat is not None else DEFAULT_API_BASE,
        api_key=flat.api_key if flat is not None else "",
        model=flat.model if flat is not None else DEFAULT_MODEL,
        language=flat.language if flat is not None else DEFAULT_LANGUAGE,
        timeout=flat.timeout if flat is not None else DEFAULT_TIMEOUT_SECONDS,
        enabled=enabled and _service_may_run(providers),
        default_provider=default_provider,
        fallback_chain=fallback_chain,
        providers=providers,
    )
    logger.info(
        "[ASR] 配置已加载: default=%s, chain=%s, enabled=%s, providers=%s",
        default_provider or "(fallback)",
        fallback_chain or "(default)",
        enabled,
        list(providers),
    )
    return config


def _default_model_dir() -> Path | None:
    """本地模型默认目录：``<user_root>/models/asr/<模型目录名>``。"""
    root = user_space.user_root()
    if root is None:
        return None
    return root / "models" / "asr" / SENSEVOICE_MODEL_DIR_NAME


def _resolve_local_paths(provider: ASRProviderConfig) -> tuple[Path, Path] | None:
    """本地模型文件路径（model, tokens）；无法定位目录时返回 None。"""
    raw_dir = provider.model_dir.strip()
    base = Path(raw_dir).expanduser() if raw_dir else _default_model_dir()
    if base is None:
        return None
    return base / provider.model_file, base / provider.tokens_file


def _import_sherpa_onnx() -> Any | None:
    """懒 import sherpa_onnx（未安装/加载失败返回 None，判定为不可用）。"""
    try:
        import sherpa_onnx  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 —— 缺依赖/扩展加载失败均降级为不可用
        logger.debug("[ASR] sherpa_onnx 不可导入: %s", exc)
        return None
    return sherpa_onnx


def _download_ssl_context() -> ssl.SSLContext:
    """下载用 SSL 上下文：certifi CA bundle 优先。

    Python 默认上下文取系统证书存储，系统代理 MITM（Clash 类）或系统证书链
    不全时取不到可信锚，首次自动下载模型即报 CERTIFICATE_VERIFY_FAILED；
    certifi 随包自带完整 CA 集合规避之。certifi 未安装（旧 venv 未刷新）时
    退回默认上下文：下载仍可用，仅证书面退回系统行为，告警不阻断。
    """
    try:
        import certifi  # noqa: PLC0415 —— 懒 import：certifi 缺失不拖垮整个 ASR 模块装载
    except Exception as exc:  # noqa: BLE001 —— 降级路径本身即处理（见 docstring），非吞异常
        logger.warning("[ASR] certifi 不可用（%s），模型下载回退系统默认 CA 上下文", exc)
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _download_file(url: str, dest: Path, timeout: int) -> None:
    """流式下载单文件：``.part`` 临时文件 + ``os.replace`` 原子落盘。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    logger.info("[ASR] 开始下载本地模型文件: %s ← %s", dest, url)
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=_download_ssl_context()) as resp, open(
            part, "wb"
        ) as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            step = max(total // 10, 1) if total else 0
            next_log = step
            downloaded = 0
            while chunk := resp.read(_DOWNLOAD_CHUNK_BYTES):
                fh.write(chunk)
                downloaded += len(chunk)
                if step and downloaded >= next_log:
                    logger.info(
                        "[ASR] 模型下载进度: %s %.0f%% (%d/%d bytes)",
                        dest.name,
                        downloaded * 100 / total,
                        downloaded,
                        total,
                    )
                    while next_log <= downloaded:
                        next_log += step
        os.replace(part, dest)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    logger.info("[ASR] 本地模型文件下载完成: %s", dest)


def _ensure_model_files(provider: ASRProviderConfig) -> tuple[Path, Path]:
    """确保本地模型文件在位（缺失且 auto_download 时下载）。

    Raises:
        _SkipProvider: 目录不可定位或缺失且未开启自动下载（链上继续）
        RuntimeError: 下载失败（链上继续下一 provider）
    """
    paths = _resolve_local_paths(provider)
    if paths is None:
        raise _SkipProvider("无法定位本地模型目录（用户空间不可用）")
    missing = [path for path in paths if not path.is_file()]
    if not missing:
        return paths
    if not provider.auto_download:
        raise _SkipProvider(f"本地模型缺失且未开启自动下载: {', '.join(str(p) for p in missing)}")
    base = provider.download_base.strip()
    if not base:
        raise _SkipProvider("本地模型缺失且未配置 download_base")
    base_url = base.rstrip("/")
    for path in missing:
        url = f"{base_url}/{path.name}"
        try:
            _download_file(url, path, provider.download_timeout)
        except Exception as exc:  # noqa: BLE001 —— 下载失败降级下一 provider（错误聚合进最终 RuntimeError）
            guide = "\n".join(
                f'  curl -L -o "{candidate}" "{base_url}/{candidate.name}"' for candidate in missing
            )
            raise RuntimeError(
                f"本地模型下载失败 ({url}): {exc}\n"
                f"自救：可手动下载下列文件放入模型目录后重试（无需改配置，放好即生效）：\n"
                f"  目标目录: {path.parent}\n{guide}"
            ) from exc
    return paths


def _wav_to_float32(audio_bytes: bytes) -> tuple[int, array.array[float]] | None:
    """解析 16-bit PCM WAV 为 (采样率, 单声道浮点采样)。

    多声道按均值下混；非 WAV / 非 16-bit PCM / 无采样帧返回 None（调用方跳过本地）。
    """
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            sample_rate = wf.getframerate()
            if wf.getcomptype() != "NONE" or sample_width != 2 or channels < 1 or sample_rate <= 0:
                return None
            frames = wf.readframes(wf.getnframes())
    except (wave.Error, EOFError):
        return None
    if not frames:
        return None
    pcm = array.array("h")
    # wave.readframes 已按宿主字节序归一（大端宿主内部换序），此处不二次交换。
    pcm.frombytes(frames)
    frame_count = len(pcm) // channels
    if frame_count == 0:
        return None
    samples = array.array(
        "f",
        (
            sum(pcm[index + ch] for ch in range(channels)) / (channels * 32768.0)
            for index in range(0, frame_count * channels, channels)
        ),
    )
    return sample_rate, samples


def _clean_sensevoice_text(raw_text: str) -> str:
    """清洗 SenseVoice 特殊标记（语言/情感/事件标签）并去首尾空白。"""
    return _SENSEVOICE_TOKEN_RE.sub("", raw_text).strip()


class ASRService:
    """语音识别（ASR）转写服务。

    按 ``default_provider`` + ``fallback_chain`` 顺序尝试 provider：跳过不可用者
    （本地缺模型/sherpa 导入失败、远端缺 api_key），逐个尝试直到成功。本地
    provider 由进程内 sherpa-onnx SenseVoice 解码 WAV，远端走 OpenAI 兼容
    ``POST /audio/transcriptions``。

    配置热生效：无显式配置构造时绑定默认落点（用户层优先），每次公开调用前
    按落点变化 + stat 签名探测配置变更并重载——设置页保存免插件 respawn 即生效。
    本地 recognizer 为单例懒加载，加载与推理持线程锁（sidecar 并发请求安全）。

    Attributes:
        _config: ASR 配置
    """

    def __init__(self, config: ASRConfig | None = None) -> None:
        """初始化 ASRService。

        Args:
            config: ASR 配置；为 None 时自动加载默认配置并启用变更探测
        """
        self._explicit: bool
        self._config: ASRConfig
        self._config_path: Path | None
        self._stat: tuple[int, int] | None
        self._recognizer: Any | None = None
        self._recognizer_key: tuple[str, str, int, str, bool] | None = None
        self._local_lock = threading.Lock()
        if config is not None:
            self._explicit = True
            self._config = config
            self._config_path = None
            self._stat = None
        else:
            self._explicit = False
            self._config = load_asr_config()
            self._config_path = _default_config_path()
            self._stat = _stat_signature(self._config_path)

    @property
    def config(self) -> ASRConfig:
        """当前 ASR 配置（读取前探测配置变更）。"""
        self._maybe_reload()
        return self._config

    def _maybe_reload(self) -> None:
        """探测配置落点/内容变化并重载（显式配置或 stat 失败时保持现值）。"""
        if self._explicit or self._config_path is None:
            return
        path = _default_config_path()
        signature = _stat_signature(path)
        if path == self._config_path and signature == self._stat:
            return
        logger.debug("[ASR] 检测到配置变更: %s → %s", self._config_path, path)
        self._config_path = path
        self._stat = signature
        self._config = load_asr_config(path)

    def _provider_chain(self, cfg: ASRConfig) -> list[tuple[str, ASRProviderConfig]]:
        """尝试顺序：default_provider + fallback_chain 去重；空链取声明顺序。

        旧单 provider 形态（providers 为空）由扁平字段合成一个远端 provider。
        """
        providers = dict(cfg.providers)
        if not providers:
            legacy_name = cfg.default_provider or "default"
            providers = {
                legacy_name: ASRProviderConfig(
                    type="openai",
                    api_base=cfg.api_base,
                    api_key=cfg.api_key,
                    model=cfg.model,
                    language=cfg.language,
                    timeout=cfg.timeout,
                )
            }
        order: list[str] = []
        for name in [cfg.default_provider, *cfg.fallback_chain]:
            if name and name in providers and name not in order:
                order.append(name)
        if not order:
            order = list(providers)
        return [(name, providers[name]) for name in order]

    def _provider_available(self, provider: ASRProviderConfig, cfg: ASRConfig) -> bool:
        """provider 是否可能服务本次请求（本地缺模型但可自动下载时视为可用）。"""
        if not cfg.enabled:
            return False
        if provider.type.strip().lower() == "local":
            if provider.engine.strip().lower() != "sensevoice":
                return False
            if _import_sherpa_onnx() is None:
                return False
            paths = _resolve_local_paths(provider)
            if paths is None:
                return False
            if all(path.is_file() for path in paths):
                return True
            return provider.auto_download and bool(provider.download_base.strip())
        return bool(provider.api_key)

    def is_available(self) -> bool:
        """检查 ASR 服务是否可用（总开关开启且链上至少一个 provider 可服务）。"""
        cfg = self.config
        return any(self._provider_available(provider, cfg) for _, provider in self._provider_chain(cfg))

    async def transcribe(
        self,
        audio_bytes: bytes,
        mime_type: str,
        language: str | None = None,
    ) -> str:
        """将音频字节流转写为文本（按 provider 链顺序尝试，失败自动降级）。

        Args:
            audio_bytes: 音频文件二进制内容
            mime_type: 音频 MIME 类型（如 ``audio/webm``；本地引擎只吃 WAV）
            language: 识别语言代码，覆盖远端配置默认值；为 None 时用 provider 配置值

        Returns:
            转写得到的文本

        Raises:
            RuntimeError: 全链不可用（未配置）或全部 provider 失败；异常信息聚合
                各 provider 失败原因
            ValueError: audio_bytes 为空
        """
        cfg = self.config
        if not cfg.enabled:
            raise RuntimeError("ASR 服务未配置或未启用（缺少 API Key 或 asr.yaml）")

        if not audio_bytes:
            raise ValueError("audio_bytes 不能为空")

        attempts = [
            (name, provider)
            for name, provider in self._provider_chain(cfg)
            if self._provider_available(provider, cfg)
        ]
        if not attempts:
            raise RuntimeError("ASR 服务未配置或未启用（缺少 API Key 或 asr.yaml）")

        failures: list[tuple[str, Exception]] = []
        for name, provider in attempts:
            started = time.monotonic()
            try:
                if provider.type.strip().lower() == "local":
                    text = await asyncio.to_thread(self._recognize_local_sync, provider, audio_bytes)
                else:
                    text = await self._transcribe_remote(provider, audio_bytes, mime_type, language)
            except _SkipProvider as exc:
                logger.warning("[ASR] 跳过 provider=%s: %s", name, exc)
                failures.append((name, exc))
                continue
            except Exception as exc:  # noqa: BLE001 —— 单 provider 失败降级下一 provider，错误聚合进最终异常
                logger.warning("[ASR] provider=%s 转写失败: %s", name, exc)
                failures.append((name, exc))
                continue
            logger.info(
                "[ASR] 转写成功: provider=%s, size=%d bytes, text_length=%d, elapsed=%.2fs",
                name,
                len(audio_bytes),
                len(text),
                time.monotonic() - started,
            )
            return text

        detail = "; ".join(f"{name}: {exc}" for name, exc in failures)
        # 保留底层异常链（单 provider 形态下 __cause__ = 原始网络/业务异常）
        first_cause = next((exc.__cause__ or exc for _, exc in failures), None)
        raise RuntimeError(f"ASR 转写失败（provider 链全部失败）: {detail}") from first_cause

    async def _transcribe_remote(
        self,
        provider: ASRProviderConfig,
        audio_bytes: bytes,
        mime_type: str,
        language: str | None,
    ) -> str:
        """OpenAI 兼容远端转写（multipart 上传，解析 ``{"text": ...}``）。"""
        ext = _MIME_TO_EXT.get(mime_type, "webm")
        filename = f"audio.{ext}"
        lang = language or provider.language
        url = f"{provider.api_base.rstrip('/')}/audio/transcriptions"

        logger.info(
            "[ASR] 提交远端转写: size=%d bytes, mime=%s, model=%s, lang=%s",
            len(audio_bytes),
            mime_type,
            provider.model,
            lang,
        )

        form = aiohttp.FormData()
        form.add_field("file", audio_bytes, filename=filename, content_type=mime_type)
        form.add_field("model", provider.model)
        form.add_field("language", lang)

        headers = {"Authorization": f"Bearer {provider.api_key}"}

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    url,
                    data=form,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=provider.timeout),
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

        return text

    def _get_recognizer(
        self,
        provider: ASRProviderConfig,
        model_path: Path,
        tokens_path: Path,
    ) -> Any:
        """本地 recognizer 单例（调用方持 ``_local_lock``）；配置变更时重载。"""
        key = (str(model_path), str(tokens_path), provider.num_threads, provider.language, provider.use_itn)
        if self._recognizer is not None and self._recognizer_key == key:
            return self._recognizer
        if provider.engine.strip().lower() != "sensevoice":
            raise _SkipProvider(f"不支持的本地引擎: {provider.engine}")
        sherpa = _import_sherpa_onnx()
        if sherpa is None:
            raise _SkipProvider("sherpa_onnx 未安装")
        started = time.monotonic()
        recognizer = sherpa.OfflineRecognizer.from_sense_voice(
            model=str(model_path),
            tokens=str(tokens_path),
            num_threads=provider.num_threads,
            language=provider.language,
            use_itn=provider.use_itn,
            provider="cpu",
        )
        self._recognizer = recognizer
        self._recognizer_key = key
        logger.info(
            "[ASR] 本地 SenseVoice 引擎加载完成: model=%s, threads=%d, lang=%s, elapsed=%.2fs",
            model_path.name,
            provider.num_threads,
            provider.language,
            time.monotonic() - started,
        )
        return recognizer

    def _recognize_local_sync(self, provider: ASRProviderConfig, audio_bytes: bytes) -> str:
        """本地同步转写（由 ``asyncio.to_thread`` 调用；加载与解码持线程锁）。"""
        parsed = _wav_to_float32(audio_bytes)
        if parsed is None:
            raise _SkipProvider("本地引擎仅支持 16-bit PCM WAV 音频")
        sample_rate, samples = parsed
        model_path, tokens_path = _ensure_model_files(provider)
        with self._local_lock:
            recognizer = self._get_recognizer(provider, model_path, tokens_path)
            stream = recognizer.create_stream()
            stream.accept_waveform(sample_rate, samples)
            recognizer.decode_stream(stream)
            raw_text = str(stream.result.text)
        text = _clean_sensevoice_text(raw_text)
        if not text:
            raise RuntimeError("本地转写结果为空")
        logger.info(
            "[ASR] 本地转写完成: rate=%d, frames=%d, text_length=%d",
            sample_rate,
            len(samples),
            len(text),
        )
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
    "ASRProviderConfig",
    "ASRService",
    "get_asr_service",
    "load_asr_config",
    "reset_asr_service",
]
