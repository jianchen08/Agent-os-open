# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""ASR 语音识别服务单元测试。

覆盖场景：
- ASRConfig 默认值
- ASRService.is_available（启用/未启用/key 缺失；provider 链任一可用）
- transcribe 成功路径（mock aiohttp，验证 multipart 上传与 text 解析）
- transcribe HTTP 错误抛 RuntimeError
- transcribe 未配置时抛 RuntimeError
- transcribe 响应缺少文本抛 RuntimeError
- load_asr_config 环境变量回退（asr.yaml 缺失时读 ZHIPU_API_KEY）
- provider 链：默认 provider 优先、fallback_chain 去重、缺 key 跳过、单家失败降级
- 本地 SenseVoice：WAV 解析（单/多声道）、标签清洗、模型缺失跳过、自动下载成功/失败降级
- 旧单 provider 远端配置与新多 provider 配置等价映射
"""

from __future__ import annotations

import array
import asyncio
import io
import os
import ssl
import sys
import types
import urllib.error
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 0.2 平铺布局：multimodal 插件位于 plugins/shared/multimodal/；
# asr.py 内部平铺导入 mm_types（multimodal 目录自身在 sys.path）与
# user_space（plugins/shared 裸模块，配置用户层解析）。
_SHARED = Path(__file__).resolve().parent.parent / "plugins" / "shared"
_MULTIMODAL = _SHARED / "multimodal"
for _p in (_SHARED, _MULTIMODAL):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from multimodal.asr import (  # noqa: E402
    ASRConfig,
    ASRService,
    load_asr_config,
    reset_asr_service,
)


def _async_run(coro):
    """安全执行 async 函数（兼容已有事件循环）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor() as pool:
        return pool.submit(asyncio.run, coro).result(timeout=10)


def test_default_config():
    """ASRConfig 应使用默认值。"""
    cfg = ASRConfig()
    assert cfg.model == "glm-asr"
    assert cfg.language == "zh-CN"
    assert cfg.timeout == 60


def test_is_available_with_key():
    """配置了 api_key 且 enabled 时应可用。"""
    svc = ASRService(ASRConfig(api_key="sk-test", enabled=True))
    assert svc.is_available() is True


def test_not_available_without_key():
    """缺少 api_key 时不可用。"""
    svc = ASRService(ASRConfig(api_key="", enabled=True))
    assert svc.is_available() is False


def test_not_available_when_disabled():
    """enabled=False 时不可用。"""
    svc = ASRService(ASRConfig(api_key="sk-test", enabled=False))
    assert svc.is_available() is False


def _make_mock_response(status: int, json_data: dict | None = None):
    """构造 aiohttp 响应 mock（支持 async with）。"""
    resp = AsyncMock()
    resp.status = status
    if json_data is not None:
        resp.json = AsyncMock(return_value=json_data)
    resp.text = AsyncMock(return_value=str(json_data))
    # async with session.post(...) as resp 的上下文管理协议
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _patch_client_session(mock_resp):
    """patch aiohttp.ClientSession，使其 post 返回 mock_resp 的 async context。

    覆盖 ``async with ClientSession() as session, session.post(...) as resp`` 结构。
    """
    mock_post_cm = MagicMock()
    mock_post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_post_cm.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_post_cm)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    def fake_session_ctor():
        return mock_session

    return patch("multimodal.asr.aiohttp.ClientSession", side_effect=fake_session_ctor), mock_session


def test_transcribe_success():
    """成功转写：应返回响应中的 text 字段。"""
    svc = ASRService(ASRConfig(api_key="sk-test", api_base="https://example.com/v1"))
    mock_resp = _make_mock_response(200, {"text": "你好世界"})
    patcher, mock_session = _patch_client_session(mock_resp)

    with patcher:
        text = _async_run(svc.transcribe(b"fake-audio", "audio/webm"))

    assert text == "你好世界"
    # 验证请求 URL
    called_url = mock_session.post.call_args[0][0]
    assert called_url == "https://example.com/v1/audio/transcriptions"


def test_transcribe_http_error():
    """HTTP 非 200 应抛 RuntimeError。"""
    svc = ASRService(ASRConfig(api_key="sk-test"))
    mock_resp = _make_mock_response(401, {"error": "unauthorized"})
    patcher, _mock_session = _patch_client_session(mock_resp)

    with patcher, pytest.raises(RuntimeError, match="status=401"):
        _async_run(svc.transcribe(b"fake-audio", "audio/webm"))


def test_transcribe_not_configured():
    """未配置时转写应抛 RuntimeError。"""
    svc = ASRService(ASRConfig(api_key="", enabled=False))
    with pytest.raises(RuntimeError, match="未配置"):
        _async_run(svc.transcribe(b"fake-audio", "audio/webm"))


def test_transcribe_empty_audio():
    """空音频应抛 ValueError。"""
    svc = ASRService(ASRConfig(api_key="sk-test"))
    with pytest.raises(ValueError, match="不能为空"):
        _async_run(svc.transcribe(b"", "audio/webm"))


def test_transcribe_missing_text():
    """响应缺少 text 字段应抛 RuntimeError。"""
    svc = ASRService(ASRConfig(api_key="sk-test"))
    mock_resp = _make_mock_response(200, {"unrelated": "data"})
    patcher, _mock_session = _patch_client_session(mock_resp)

    with patcher, pytest.raises(RuntimeError, match="缺少转写文本"):
        _async_run(svc.transcribe(b"fake-audio", "audio/webm"))


def test_load_config_env_fallback(tmp_path, monkeypatch):
    """asr.yaml 缺失时，应从 ZHIPU_API_KEY 环境变量回退。"""
    monkeypatch.setenv("ZHIPU_API_KEY", "sk-from-env")
    cfg = load_asr_config(tmp_path / "nonexistent.yaml")
    assert cfg.api_key == "sk-from-env"
    assert cfg.enabled is True


def test_load_config_missing_env(tmp_path, monkeypatch):
    """asr.yaml 缺失且环境变量未设置时，enabled 应为 False。"""
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    cfg = load_asr_config(tmp_path / "nonexistent.yaml")
    assert cfg.api_key == ""
    assert cfg.enabled is False


def test_get_asr_service_singleton():
    """get_asr_service 应返回单例。"""
    reset_asr_service()
    svc1 = ASRService.__new__(ASRService)
    svc1._config = ASRConfig(api_key="sk-test")
    with patch("multimodal.asr._asr_service", svc1):
        svc2 = ASRService.__new__(ASRService)
        svc2._config = ASRConfig(api_key="sk-test")
        # 单例已缓存时，应返回缓存的实例
        from multimodal import asr as asr_mod

        asr_mod._asr_service = svc1
        assert asr_mod.get_asr_service() is svc1
    reset_asr_service()


# ── 用户空间真值 + 配置热生效（设置中枢 ASR 配置页的读取契约）──────────────


def _user_yaml(model: str, api_key: str = "sk-user") -> str:
    return (
        "asr:\n"
        "  enabled: true\n"
        "  default_provider: p1\n"
        "  providers:\n"
        "    p1:\n"
        '      api_base: "https://user.example.com/v1"\n'
        f'      api_key: "{api_key}"\n'
        f'      model: "{model}"\n'
        '      language: "zh-CN"\n'
    )


def _clear_user_space_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTOS_USER_CONFIG_DIR", raising=False)
    monkeypatch.delenv("AGENTOS_USER_ROOT", raising=False)


def test_load_config_user_layer_priority(tmp_path, monkeypatch):
    """用户层存在 models/asr.yaml 时优先于工厂配置（文件级整体替换，不合并）。"""
    monkeypatch.setenv("AGENTOS_USER_CONFIG_DIR", str(tmp_path))
    user_yaml = tmp_path / "models" / "asr.yaml"
    user_yaml.parent.mkdir(parents=True)
    user_yaml.write_text(_user_yaml("user-asr-model"), encoding="utf-8")

    cfg = load_asr_config()

    assert cfg.model == "user-asr-model"
    assert cfg.api_key == "sk-user"
    assert cfg.api_base == "https://user.example.com/v1"


def test_load_config_factory_fallback_without_user_layer(tmp_path, monkeypatch):
    """用户层未接管（文件不存在）时回落工厂 config/models/asr.yaml。"""
    _clear_user_space_env(monkeypatch)
    # 空用户根：不消费宿主 %APPDATA% 用户空间（避免测试依赖外部环境配置）
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path))

    cfg = load_asr_config()

    # 工厂配置 = 本地优先 + 免费远端 + 付费兜底的 provider 链
    assert cfg.default_provider == "local"
    assert cfg.fallback_chain == ["local", "siliconflow", "glm"]
    assert cfg.providers["glm"].model == "glm-asr"


def test_service_picks_up_user_layer_created_later(tmp_path, monkeypatch):
    """设置页首次保存（用户层文件从无到有）：既有服务实例下次读取即切用户层。"""
    _clear_user_space_env(monkeypatch)
    # 空用户根：初始读工厂配置，不消费宿主 %APPDATA% 用户空间
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path))
    svc = ASRService()
    assert svc.config.default_provider == "local"

    monkeypatch.setenv("AGENTOS_USER_CONFIG_DIR", str(tmp_path))
    user_yaml = tmp_path / "models" / "asr.yaml"
    user_yaml.parent.mkdir(parents=True)
    user_yaml.write_text(_user_yaml("late-user-model"), encoding="utf-8")

    assert svc.config.model == "late-user-model"
    assert svc.is_available() is True


def test_service_reloads_on_file_content_change(tmp_path, monkeypatch):
    """配置文件内容变更（stat 签名变化）后，既有实例下次读取新值（免重启热生效）。"""
    monkeypatch.setenv("AGENTOS_USER_CONFIG_DIR", str(tmp_path))
    yaml_path = tmp_path / "models" / "asr.yaml"
    yaml_path.parent.mkdir(parents=True)
    yaml_path.write_text(_user_yaml("m1", api_key="k1"), encoding="utf-8")

    svc = ASRService()
    assert svc.config.model == "m1"
    assert svc.config.api_key == "k1"

    yaml_path.write_text(_user_yaml("m2", api_key="k2"), encoding="utf-8")
    # Windows mtime 粒度粗：显式推进 stat 签名，测试不受文件系统时间分辨率影响
    st = yaml_path.stat()
    os.utime(yaml_path, ns=(st.st_atime_ns + 1_000_000_000, st.st_mtime_ns + 1_000_000_000))

    assert svc.config.model == "m2"
    assert svc.config.api_key == "k2"
    assert svc.is_available() is True


def test_service_explicit_config_not_reloaded(tmp_path, monkeypatch):
    """显式传入 ASRConfig（代码内构造，无配置文件语义）时不做文件重载探测。"""
    monkeypatch.setenv("AGENTOS_USER_CONFIG_DIR", str(tmp_path))
    user_yaml = tmp_path / "models" / "asr.yaml"
    user_yaml.parent.mkdir(parents=True)
    user_yaml.write_text(_user_yaml("user-model"), encoding="utf-8")

    svc = ASRService(ASRConfig(api_key="sk-inline", model="inline-model"))
    assert svc.config.model == "inline-model"
    assert svc.config.api_key == "sk-inline"


def test_stat_signature_missing_file_returns_none(tmp_path):
    """不可 stat（文件缺失）时签名返回 None，调用方保持现值不重载。"""
    from multimodal import asr as asr_mod

    assert asr_mod._stat_signature(tmp_path / "missing.yaml") is None


# ── provider 链（default_provider + fallback_chain）与本地引擎 ──────────────


def _remote_provider(api_base: str, api_key: str = "sk-remote", model: str = "remote-model") -> dict:
    """远端 provider 配置字典（OpenAI 兼容）。"""
    return {
        "type": "openai",
        "api_base": api_base,
        "api_key": api_key,
        "model": model,
        "language": "zh-CN",
    }


def _local_provider(model_dir: Path, *, files: bool = True, **overrides) -> dict:
    """本地 provider 配置字典；files=False 时不落模型文件（模拟缺失）。"""
    provider: dict = {
        "type": "local",
        "engine": "sensevoice",
        "model_dir": str(model_dir),
        "model_file": "model.int8.onnx",
        "tokens_file": "tokens.txt",
        "num_threads": 2,
        "language": "zh",
        "use_itn": True,
        "auto_download": False,
        "download_base": "https://dl.example.test/models",
    }
    if files:
        (model_dir / "model.int8.onnx").write_bytes(b"fake-model")
        (model_dir / "tokens.txt").write_text("fake-tokens", encoding="utf-8")
    provider.update(overrides)
    return provider


def _pcm16_wav(channels: list[list[float]], rate: int = 16000) -> bytes:
    """构造 16-bit PCM WAV（多声道按帧交错）。"""
    frames = array.array("h")
    for index in range(len(channels[0])):
        for channel in channels:
            scaled = round(channel[index] * 32767)
            frames.append(max(-32768, min(32767, scaled)))
    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as wf:
            wf.setnchannels(len(channels))
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(frames.tobytes())
        return buffer.getvalue()


class _FakeStream:
    """假 sherpa OfflineStream：记录 accept_waveform 入参与 result.text。"""

    def __init__(self, text: str) -> None:
        self.result = SimpleNamespace(text=text)
        self.accepted: list[tuple[int, list[float]]] = []

    def accept_waveform(self, sample_rate: int, samples: list[float]) -> None:
        self.accepted.append((sample_rate, list(samples)))


class _FakeRecognizer:
    """假 sherpa OfflineRecognizer：create_stream/decode_stream 最小契约。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.streams: list[_FakeStream] = []
        self.decoded: list[_FakeStream] = []

    def create_stream(self) -> _FakeStream:
        stream = _FakeStream(self.text)
        self.streams.append(stream)
        return stream

    def decode_stream(self, stream: _FakeStream) -> None:
        self.decoded.append(stream)


class _FakeSherpa:
    """假 sherpa_onnx 模块：OfflineRecognizer.from_sense_voice 产出固定文本。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict] = []
        self.recognizers: list[_FakeRecognizer] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _FakeSherpa:
        module = types.ModuleType("sherpa_onnx")
        module.OfflineRecognizer = self  # 实例承载模块属性：from_sense_voice 绑定本实例
        monkeypatch.setitem(sys.modules, "sherpa_onnx", module)
        return self

    def from_sense_voice(self, **kwargs) -> _FakeRecognizer:
        self.calls.append(kwargs)
        recognizer = _FakeRecognizer(self.text)
        self.recognizers.append(recognizer)
        return recognizer


class _FakeHTTPResponse:
    """假 urllib 响应：Content-Length + 流式 read。"""

    def __init__(self, payload: bytes) -> None:
        self._buffer = io.BytesIO(payload)
        self.headers = {"Content-Length": str(len(payload))}

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _patch_client_session_sequence(responses: list):
    """patch ClientSession：按调用顺序返回响应 mock，并记录每次 POST 的 URL。"""
    posted: list[str] = []

    def fake_session_ctor():
        session = MagicMock()

        def post(url, **_kwargs):
            posted.append(url)
            resp = responses[min(len(posted) - 1, len(responses) - 1)]
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        session.post = MagicMock(side_effect=post)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        return session

    return patch("multimodal.asr.aiohttp.ClientSession", side_effect=fake_session_ctor), posted


def test_chain_default_first_then_fallback_deduped():
    """默认 provider 首位，fallback_chain 去重；首家失败降级下一家。"""
    svc = ASRService(
        ASRConfig(
            default_provider="b",
            fallback_chain=["b", "a", "b"],
            providers={
                "a": _remote_provider("https://a.test/v1"),
                "b": _remote_provider("https://b.test/v1"),
            },
        )
    )
    patcher, posted = _patch_client_session_sequence([
        _make_mock_response(500, {"error": "boom"}),
        _make_mock_response(200, {"text": "第二家"}),
    ])

    with patcher:
        text = _async_run(svc.transcribe(b"raw-audio", "audio/webm"))

    assert text == "第二家"
    assert posted == [
        "https://b.test/v1/audio/transcriptions",
        "https://a.test/v1/audio/transcriptions",
    ]


def test_provider_without_api_key_skipped():
    """远端缺 api_key 的 provider 被跳过，不发起请求。"""
    svc = ASRService(
        ASRConfig(
            default_provider="siliconflow",
            fallback_chain=["siliconflow", "glm"],
            providers={
                "siliconflow": _remote_provider("https://sf.test/v1", api_key=""),
                "glm": _remote_provider("https://glm.test/v1", api_key="zhipu-key"),
            },
        )
    )
    patcher, posted = _patch_client_session_sequence([_make_mock_response(200, {"text": "付费兜底"})])

    with patcher:
        text = _async_run(svc.transcribe(b"raw-audio", "audio/webm"))

    assert text == "付费兜底"
    assert posted == ["https://glm.test/v1/audio/transcriptions"]


def test_local_missing_model_skipped(monkeypatch, tmp_path):
    """本地模型缺失且未开自动下载 → 跳过本地，走远端。"""
    fake = _FakeSherpa("本地引擎").install(monkeypatch)
    svc = ASRService(
        ASRConfig(
            default_provider="local",
            fallback_chain=["local", "glm"],
            providers={
                "local": _local_provider(tmp_path, files=False),
                "glm": _remote_provider("https://glm.test/v1"),
            },
        )
    )
    patcher, posted = _patch_client_session_sequence([_make_mock_response(200, {"text": "远端识别"})])

    with patcher:
        text = _async_run(svc.transcribe(b"raw-audio", "audio/webm"))

    assert text == "远端识别"
    assert fake.recognizers == []
    assert posted == ["https://glm.test/v1/audio/transcriptions"]


def test_local_transcribe_cleans_sensevoice_tokens(monkeypatch, tmp_path):
    """本地识别成功路径：WAV 解码、recognizer 参数、SenseVoice 标签清洗。"""
    fake = _FakeSherpa("<|zh|><|NEUTRAL|><|Speech|><|woitn|>你好，这是语音输入测试。").install(monkeypatch)
    svc = ASRService(
        ASRConfig(
            default_provider="local",
            fallback_chain=["local"],
            providers={"local": _local_provider(tmp_path)},
        )
    )

    text = _async_run(svc.transcribe(_pcm16_wav([[0.1, 0.2, -0.3]]), "audio/wav"))

    assert text == "你好，这是语音输入测试。"
    assert len(fake.recognizers) == 1
    assert fake.calls[0]["model"] == str(tmp_path / "model.int8.onnx")
    assert fake.calls[0]["tokens"] == str(tmp_path / "tokens.txt")
    assert fake.calls[0]["num_threads"] == 2
    assert fake.calls[0]["language"] == "zh"
    assert fake.calls[0]["use_itn"] is True
    rate, samples = fake.recognizers[0].streams[0].accepted[0]
    assert rate == 16000
    assert samples == pytest.approx([0.1, 0.2, -0.3], abs=1e-4)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ([0.5, -0.5], [0.0, 0.5], [0.25, 0.0]),
        ([0.2, 0.6], [0.4, -0.2], [0.3, 0.2]),
    ],
)
def test_local_wav_stereo_downmix_and_rate(monkeypatch, tmp_path, left, right, expected):
    """多声道按均值下混单声道；非 16k 采样率原样交给引擎（内部重采样）。"""
    fake = _FakeSherpa("你好").install(monkeypatch)
    svc = ASRService(
        ASRConfig(
            default_provider="local",
            fallback_chain=["local"],
            providers={"local": _local_provider(tmp_path)},
        )
    )

    _async_run(svc.transcribe(_pcm16_wav([left, right], rate=22050), "audio/wav"))

    rate, samples = fake.recognizers[0].streams[0].accepted[0]
    assert rate == 22050
    assert samples == pytest.approx(expected, abs=1e-4)


def test_non_wav_audio_skips_local_without_loading_model(monkeypatch, tmp_path):
    """非 WAV（如 webm）→ 跳过本地（不加载模型），走远端。"""
    fake = _FakeSherpa("本地引擎").install(monkeypatch)
    svc = ASRService(
        ASRConfig(
            default_provider="local",
            fallback_chain=["local", "glm"],
            providers={
                "local": _local_provider(tmp_path),
                "glm": _remote_provider("https://glm.test/v1"),
            },
        )
    )
    patcher, posted = _patch_client_session_sequence([_make_mock_response(200, {"text": "远端识别"})])

    with patcher:
        text = _async_run(svc.transcribe(b"webm-container-bytes", "audio/webm"))

    assert text == "远端识别"
    assert fake.recognizers == []
    assert posted == ["https://glm.test/v1/audio/transcriptions"]


def test_local_empty_result_falls_back(monkeypatch, tmp_path):
    """本地只产出标签（清洗后为空）视为失败，降级下一 provider。"""
    _FakeSherpa("<|zh|><|NEUTRAL|>").install(monkeypatch)
    svc = ASRService(
        ASRConfig(
            default_provider="local",
            fallback_chain=["local", "glm"],
            providers={
                "local": _local_provider(tmp_path),
                "glm": _remote_provider("https://glm.test/v1"),
            },
        )
    )
    patcher, posted = _patch_client_session_sequence([_make_mock_response(200, {"text": "远端识别"})])

    with patcher:
        text = _async_run(svc.transcribe(_pcm16_wav([[0.1]]), "audio/wav"))

    assert text == "远端识别"
    assert posted == ["https://glm.test/v1/audio/transcriptions"]


def test_local_auto_download_then_transcribe(monkeypatch, tmp_path):
    """模型缺失且 auto_download=true：流式下载两文件后本地识别。"""
    _FakeSherpa("下载后识别").install(monkeypatch)
    downloads: list[str] = []

    def fake_urlopen(url, timeout=0, context=None):
        downloads.append(url)
        return _FakeHTTPResponse(b"payload:" + url.encode())

    from multimodal import asr as asr_mod

    monkeypatch.setattr(asr_mod.urllib.request, "urlopen", fake_urlopen)
    svc = ASRService(
        ASRConfig(
            default_provider="local",
            fallback_chain=["local"],
            providers={
                "local": _local_provider(
                    tmp_path,
                    files=False,
                    auto_download=True,
                    download_base="https://dl.example.test/m",
                )
            },
        )
    )

    text = _async_run(svc.transcribe(_pcm16_wav([[0.1]]), "audio/wav"))

    assert text == "下载后识别"
    assert downloads == [
        "https://dl.example.test/m/model.int8.onnx",
        "https://dl.example.test/m/tokens.txt",
    ]
    assert (tmp_path / "model.int8.onnx").read_bytes() == b"payload:https://dl.example.test/m/model.int8.onnx"
    assert (tmp_path / "tokens.txt").read_bytes() == b"payload:https://dl.example.test/m/tokens.txt"
    assert not list(tmp_path.glob("*.part"))


def test_local_auto_download_failure_degrades_to_remote(monkeypatch, tmp_path):
    """下载失败 warning 降级（不抛给用户）：本地跳过，远端兜底。"""
    _FakeSherpa("本地引擎").install(monkeypatch)

    def failing_urlopen(url, timeout=0, context=None):
        raise OSError("network down")

    from multimodal import asr as asr_mod

    monkeypatch.setattr(asr_mod.urllib.request, "urlopen", failing_urlopen)
    svc = ASRService(
        ASRConfig(
            default_provider="local",
            fallback_chain=["local", "glm"],
            providers={
                "local": _local_provider(tmp_path, files=False, auto_download=True),
                "glm": _remote_provider("https://glm.test/v1"),
            },
        )
    )
    patcher, posted = _patch_client_session_sequence([_make_mock_response(200, {"text": "远端识别"})])

    with patcher:
        text = _async_run(svc.transcribe(_pcm16_wav([[0.1]]), "audio/wav"))

    assert text == "远端识别"
    assert posted == ["https://glm.test/v1/audio/transcriptions"]
    assert not (tmp_path / "model.int8.onnx").exists()
    assert not list(tmp_path.glob("*.part"))


def test_all_providers_unavailable_raises_not_configured(monkeypatch, tmp_path):
    """全链不可用（本地缺依赖 + 远端缺 key）→ 未配置语义（server 层 503）。"""
    from multimodal import asr as asr_mod

    monkeypatch.setattr(asr_mod, "_import_sherpa_onnx", lambda: None)
    svc = ASRService(
        ASRConfig(
            default_provider="local",
            fallback_chain=["local", "glm"],
            providers={
                "local": _local_provider(tmp_path, files=False),
                "glm": _remote_provider("https://glm.test/v1", api_key=""),
            },
        )
    )

    assert svc.is_available() is False
    with pytest.raises(RuntimeError, match="未配置"):
        _async_run(svc.transcribe(b"raw-audio", "audio/webm"))


def test_chain_exhausted_aggregates_failures():
    """全部 provider 失败 → RuntimeError 聚合各 provider 失败原因。"""
    svc = ASRService(
        ASRConfig(
            default_provider="glm",
            fallback_chain=["glm"],
            providers={"glm": _remote_provider("https://glm.test/v1")},
        )
    )
    patcher, posted = _patch_client_session_sequence([_make_mock_response(500, {"error": "boom"})])

    with patcher, pytest.raises(RuntimeError, match="provider 链全部失败") as excinfo:
        _async_run(svc.transcribe(b"raw-audio", "audio/webm"))

    assert "status=500" in str(excinfo.value)
    assert posted == ["https://glm.test/v1/audio/transcriptions"]


def test_local_available_when_auto_download_pending(monkeypatch, tmp_path):
    """auto_download=true 时模型未落盘也算可尝试（首次转写触发下载）；关闭则不可用。"""
    _FakeSherpa("本地引擎").install(monkeypatch)
    pending = ASRService(
        ASRConfig(
            default_provider="local",
            fallback_chain=["local"],
            providers={"local": _local_provider(tmp_path, files=False, auto_download=True)},
        )
    )
    manual = ASRService(
        ASRConfig(
            default_provider="local",
            fallback_chain=["local"],
            providers={"local": _local_provider(tmp_path, files=False, auto_download=False)},
        )
    )

    assert pending.is_available() is True
    assert manual.is_available() is False


def test_local_unavailable_without_sherpa(monkeypatch, tmp_path):
    """sherpa_onnx 不可导入 → 本地 provider 不可用（即便模型文件在位）。"""
    from multimodal import asr as asr_mod

    monkeypatch.setattr(asr_mod, "_import_sherpa_onnx", lambda: None)
    svc = ASRService(
        ASRConfig(
            default_provider="local",
            fallback_chain=["local"],
            providers={"local": _local_provider(tmp_path)},
        )
    )

    assert svc.is_available() is False


def test_wav_to_float32_mono_and_stereo():
    """WAV 解析：单声道原样、多声道均值下混、采样率透出。"""
    from multimodal import asr as asr_mod

    rate, mono = asr_mod._wav_to_float32(_pcm16_wav([[0.0, 1.0]], rate=16000))
    assert rate == 16000
    assert mono == pytest.approx([0.0, 1.0], abs=1e-4)

    rate, stereo = asr_mod._wav_to_float32(_pcm16_wav([[0.5, -0.5], [0.0, 0.5]], rate=22050))
    assert rate == 22050
    assert stereo == pytest.approx([0.25, 0.0], abs=1e-4)


def test_wav_to_float32_rejects_non_wav_and_non_16bit():
    """非 WAV / 非 16-bit PCM / 空数据 → None（调用方跳过本地）。"""
    from multimodal import asr as asr_mod

    eight_bit = io.BytesIO()
    with wave.open(eight_bit, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(16000)
        wf.writeframes(b"\x80\x80")

    assert asr_mod._wav_to_float32(b"not a wav") is None
    assert asr_mod._wav_to_float32(eight_bit.getvalue()) is None
    assert asr_mod._wav_to_float32(b"") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<|zh|><|NEUTRAL|><|Speech|><|woitn|>你好，世界。", "你好，世界。"),
        ("<|en|><|HAPPY|> hello <|Speech|>", "hello"),
    ],
)
def test_clean_sensevoice_text(raw, expected):
    """SenseVoice 特殊标记（语言/情感/事件）清洗。"""
    from multimodal import asr as asr_mod

    assert asr_mod._clean_sensevoice_text(raw) == expected


def test_load_config_legacy_single_provider_equivalent(tmp_path, monkeypatch):
    """旧形态（单 provider 远端）→ provider 链即该 provider，转写行为不变。"""
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    config_path = tmp_path / "asr.yaml"
    config_path.write_text(
        "asr:\n"
        "  enabled: true\n"
        "  default_provider: glm\n"
        "  providers:\n"
        "    glm:\n"
        '      api_base: "https://legacy.test/v1"\n'
        '      api_key: "legacy-key"\n'
        '      model: "glm-asr"\n'
        '      language: "zh-CN"\n',
        encoding="utf-8",
    )

    cfg = load_asr_config(config_path)
    assert cfg.default_provider == "glm"
    assert cfg.fallback_chain == []
    assert cfg.api_key == "legacy-key"
    assert cfg.model == "glm-asr"

    svc = ASRService(cfg)
    assert svc.is_available() is True
    patcher, mock_session = _patch_client_session(_make_mock_response(200, {"text": "旧形态"}))
    with patcher:
        text = _async_run(svc.transcribe(b"raw-audio", "audio/webm"))

    assert text == "旧形态"
    assert mock_session.post.call_args[0][0] == "https://legacy.test/v1/audio/transcriptions"


def test_load_config_multi_provider_chain(tmp_path, monkeypatch):
    """新形态：default_provider + fallback_chain + 多 provider 解析。"""
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    config_path = tmp_path / "asr.yaml"
    config_path.write_text(
        "asr:\n"
        "  enabled: true\n"
        "  default_provider: local\n"
        "  fallback_chain: [local, siliconflow, glm]\n"
        "  providers:\n"
        "    local:\n"
        "      type: local\n"
        "      engine: sensevoice\n"
        '      model_dir: ""\n'
        "      model_file: model.int8.onnx\n"
        "      tokens_file: tokens.txt\n"
        "      num_threads: 3\n"
        "      language: zh\n"
        "      use_itn: true\n"
        "      auto_download: true\n"
        '      download_base: "https://dl.example.test/m"\n'
        "      download_timeout: 120\n"
        "    siliconflow:\n"
        "      type: openai\n"
        '      api_base: "https://api.siliconflow.cn/v1"\n'
        '      api_key: "${SILICONFLOW_API_KEY}"\n'
        '      model: "FunAudioLLM/SenseVoiceSmall"\n'
        '      language: "zh-CN"\n'
        "    glm:\n"
        '      api_base: "https://open.bigmodel.cn/api/paas/v4"\n'
        '      api_key: "${ZHIPU_API_KEY}"\n'
        '      model: "glm-asr"\n',
        encoding="utf-8",
    )

    cfg = load_asr_config(config_path)

    assert cfg.enabled is True
    assert cfg.default_provider == "local"
    assert cfg.fallback_chain == ["local", "siliconflow", "glm"]
    local = cfg.providers["local"]
    assert (local.type, local.engine, local.num_threads, local.language) == ("local", "sensevoice", 3, "zh")
    assert local.auto_download is True
    assert local.download_timeout == 120
    # 环境变量占位未设置 → 该远端 provider 的 api_key 为空（链上被跳过）
    assert cfg.providers["siliconflow"].api_key == ""
    assert cfg.providers["glm"].api_key == ""
    # 旧扁平字段 = 链上首个远端 provider（向后兼容视图）
    assert cfg.model == "FunAudioLLM/SenseVoiceSmall"
    assert cfg.api_base == "https://api.siliconflow.cn/v1"


def test_load_config_resolves_provider_env_placeholders(tmp_path, monkeypatch):
    """provider 的 api_key 支持 ${ENV_VAR} 占位（逐个 provider 解析）。"""
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sf-from-env")
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    config_path = tmp_path / "asr.yaml"
    config_path.write_text(
        "asr:\n"
        "  enabled: true\n"
        "  default_provider: siliconflow\n"
        "  fallback_chain: [siliconflow, glm]\n"
        "  providers:\n"
        "    siliconflow:\n"
        '      api_base: "https://api.siliconflow.cn/v1"\n'
        '      api_key: "${SILICONFLOW_API_KEY}"\n'
        '      model: "FunAudioLLM/SenseVoiceSmall"\n'
        "    glm:\n"
        '      api_key: "${ZHIPU_API_KEY}"\n'
        '      model: "glm-asr"\n',
        encoding="utf-8",
    )

    cfg = load_asr_config(config_path)

    assert cfg.providers["siliconflow"].api_key == "sf-from-env"
    assert cfg.providers["glm"].api_key == ""


# ── 配置/路径边界分支（降级、默认坐标、坏配置、无用户空间）──────────────────


def test_load_config_invalid_provider_entries_degrade(tmp_path):
    """provider 段非映射 / 字段类型非法：降级为默认配置，不阻断整份配置。"""
    config_path = tmp_path / "asr.yaml"
    config_path.write_text(
        "asr:\n"
        "  enabled: true\n"
        "  default_provider: broken\n"
        "  providers:\n"
        '    scalar: "not-a-mapping"\n'
        "    broken:\n"
        '      num_threads: "many"\n',
        encoding="utf-8",
    )

    from multimodal.asr import ASRProviderConfig

    cfg = load_asr_config(config_path)

    assert cfg.providers["scalar"] == ASRProviderConfig()
    assert cfg.providers["broken"] == ASRProviderConfig()
    assert cfg.enabled is False  # 全远端无 key → fail-closed


def test_load_config_local_only_flat_view(tmp_path):
    """只有本地 provider：扁平旧字段退回该 provider 的默认值，服务仍可用。"""
    from multimodal import asr as asr_mod

    config_path = tmp_path / "asr.yaml"
    config_path.write_text(
        "asr:\n"
        "  enabled: true\n"
        "  default_provider: local\n"
        "  providers:\n"
        "    local:\n"
        "      type: local\n"
        f'      model_dir: "{tmp_path.as_posix()}"\n',
        encoding="utf-8",
    )

    cfg = load_asr_config(config_path)

    assert cfg.enabled is True
    assert cfg.api_base == asr_mod.DEFAULT_API_BASE
    assert cfg.model == asr_mod.DEFAULT_MODEL


def test_default_model_dir_uses_user_root(tmp_path, monkeypatch):
    """model_dir 留空 → <user_root>/models/asr/<模型目录>/ 下的两个文件。"""
    from multimodal import asr as asr_mod
    from multimodal.asr import ASRProviderConfig

    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path))
    model_dir = tmp_path / "models" / "asr" / asr_mod.SENSEVOICE_MODEL_DIR_NAME

    assert asr_mod._default_model_dir() == model_dir
    assert asr_mod._resolve_local_paths(ASRProviderConfig(type="local")) == (
        model_dir / "model.int8.onnx",
        model_dir / "tokens.txt",
    )


def test_local_paths_none_without_user_root(monkeypatch):
    """用户空间不可得 → 本地模型路径不可定位（provider 不可用/跳过）。"""
    from multimodal import asr as asr_mod
    from multimodal.asr import ASRProviderConfig

    monkeypatch.setattr(asr_mod.user_space, "user_root", lambda: None)
    provider = ASRProviderConfig(type="local")

    assert asr_mod._default_model_dir() is None
    assert asr_mod._resolve_local_paths(provider) is None
    with pytest.raises(asr_mod._SkipProvider, match="无法定位本地模型目录"):
        asr_mod._ensure_model_files(provider)


def test_import_sherpa_onnx_returns_none_on_import_error(monkeypatch):
    """import 抛异常（扩展加载失败）→ 返回 None，不向上抛。"""
    from multimodal import asr as asr_mod

    monkeypatch.setitem(sys.modules, "sherpa_onnx", None)

    assert asr_mod._import_sherpa_onnx() is None


def test_ensure_model_files_rejects_disabled_download_and_empty_base(tmp_path):
    """缺失模型：未开自动下载 / 未配 download_base → 跳过本地（链上继续）。"""
    from multimodal import asr as asr_mod
    from multimodal.asr import ASRProviderConfig

    disabled = ASRProviderConfig(type="local", model_dir=str(tmp_path), auto_download=False)
    with pytest.raises(asr_mod._SkipProvider, match="未开启自动下载"):
        asr_mod._ensure_model_files(disabled)

    no_base = ASRProviderConfig(type="local", model_dir=str(tmp_path), auto_download=True, download_base="")
    with pytest.raises(asr_mod._SkipProvider, match="download_base"):
        asr_mod._ensure_model_files(no_base)


def test_wav_to_float32_rejects_empty_frames_and_partial_frame():
    """无采样帧 / 声道交错不完整（截断帧）→ None。"""
    from multimodal import asr as asr_mod

    empty = io.BytesIO()
    with wave.open(empty, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"")

    stereo = io.BytesIO()
    with wave.open(stereo, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x01\x00\x02\x00\x03\x00\x04\x00")
    truncated = stereo.getvalue()[:-6]  # 头声明 2 帧，实际只剩半帧

    assert asr_mod._wav_to_float32(empty.getvalue()) is None
    assert asr_mod._wav_to_float32(truncated) is None


def test_chain_without_default_uses_declared_order():
    """default_provider 与 fallback_chain 均空 → 按 providers 声明顺序尝试。"""
    svc = ASRService(
        ASRConfig(
            default_provider="",
            fallback_chain=[],
            providers={
                "first": _remote_provider("https://first.test/v1"),
                "second": _remote_provider("https://second.test/v1"),
            },
        )
    )
    patcher, posted = _patch_client_session_sequence([_make_mock_response(200, {"text": "第一家"})])

    with patcher:
        text = _async_run(svc.transcribe(b"raw-audio", "audio/webm"))

    assert text == "第一家"
    assert posted == ["https://first.test/v1/audio/transcriptions"]


def test_local_unavailable_for_unsupported_engine_and_unresolvable_dir(monkeypatch, tmp_path):
    """本地引擎名不支持 / 模型目录不可定位 → provider 不可用。"""
    from multimodal import asr as asr_mod
    from multimodal.asr import ASRProviderConfig

    _FakeSherpa("本地").install(monkeypatch)
    cfg = ASRConfig(default_provider="local", fallback_chain=["local"])
    svc = ASRService(cfg)

    unsupported = ASRProviderConfig(type="local", engine="whisper", model_dir=str(tmp_path))
    (tmp_path / "model.int8.onnx").write_bytes(b"fake")
    (tmp_path / "tokens.txt").write_text("fake", encoding="utf-8")
    assert svc._provider_available(unsupported, cfg) is False

    monkeypatch.setattr(asr_mod, "_default_model_dir", lambda: None)
    assert svc._provider_available(ASRProviderConfig(type="local"), cfg) is False


def test_recognizer_cached_across_calls(monkeypatch, tmp_path):
    """同一配置下二次转写复用已加载 recognizer（不重复 from_sense_voice）。"""
    fake = _FakeSherpa("你好").install(monkeypatch)
    svc = ASRService(
        ASRConfig(
            default_provider="local",
            fallback_chain=["local"],
            providers={"local": _local_provider(tmp_path)},
        )
    )
    audio = _pcm16_wav([[0.1, 0.2]])

    first = _async_run(svc.transcribe(audio, "audio/wav"))
    second = _async_run(svc.transcribe(audio, "audio/wav"))

    assert first == second == "你好"
    assert len(fake.calls) == 1
    assert len(fake.recognizers) == 1


def test_get_recognizer_skips_unsupported_engine_and_missing_sherpa(monkeypatch, tmp_path):
    """本地引擎加载的跳过分支：引擎名不支持 / sherpa 未安装。"""
    from multimodal import asr as asr_mod
    from multimodal.asr import ASRProviderConfig

    svc = ASRService(ASRConfig(api_key="sk"))
    model_path = tmp_path / "model.int8.onnx"
    tokens_path = tmp_path / "tokens.txt"

    with pytest.raises(asr_mod._SkipProvider, match="不支持的本地引擎"):
        svc._get_recognizer(ASRProviderConfig(type="local", engine="whisper"), model_path, tokens_path)

    monkeypatch.setattr(asr_mod, "_import_sherpa_onnx", lambda: None)
    with pytest.raises(asr_mod._SkipProvider, match="sherpa_onnx 未安装"):
        svc._get_recognizer(ASRProviderConfig(type="local"), model_path, tokens_path)


# ── 下载器证书加固（certifi CA bundle + 失败自救指引）────────────────────────


def _raise_cert_verify_urlopen(url, timeout=0, **_kwargs):
    """模拟系统代理 MITM/证书链不全：urlopen 抛证书验证失败（无真网络连接）。

    消息格式复刻 _ssl C 层对验证失败的包装（纯 Python 构造不带该前缀）。
    """
    raise urllib.error.URLError(
        ssl.SSLCertVerificationError(
            20,
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "unable to get local issuer certificate (_ssl.c:992)",
        )
    )


@pytest.mark.parametrize(
    ("download_base", "model_file", "tokens_file"),
    [
        ("https://hf-mirror.test/voice", "model.int8.onnx", "tokens.txt"),
        ("https://mirror.test/sense", "custom-model.onnx", "vocab.txt"),
    ],
    ids=["default-files", "custom-files"],
)
def test_download_failure_guides_manual_rescue(
    monkeypatch, tmp_path, download_base, model_file, tokens_file
):
    """下载失败 → 异常信息含原始原因 + 全部缺失文件的手动下载 URL 与目标目录绝对路径。"""
    from multimodal import asr as asr_mod
    from multimodal.asr import ASRProviderConfig

    monkeypatch.setattr(asr_mod.urllib.request, "urlopen", _raise_cert_verify_urlopen)
    provider = ASRProviderConfig(
        type="local",
        model_dir=str(tmp_path),
        model_file=model_file,
        tokens_file=tokens_file,
        auto_download=True,
        download_base=download_base,
    )

    with pytest.raises(RuntimeError, match="本地模型下载失败") as excinfo:
        asr_mod._ensure_model_files(provider)

    message = str(excinfo.value)
    assert str(tmp_path) in message  # 目标目录绝对路径
    assert "CERTIFICATE_VERIFY_FAILED" in message  # 原始错误原因保留（可诊断）
    assert "curl -L -o" in message  # 指引可原样复制执行
    for name in (model_file, tokens_file):
        assert f"{download_base.rstrip('/')}/{name}" in message  # 手动下载 URL
        assert str(tmp_path / name) in message  # 各文件落盘目标


@pytest.mark.parametrize(
    ("payload", "filename"),
    [
        (b"onnx-bytes-alpha", "model.int8.onnx"),
        (b"tokens-beta", "tokens.txt"),
    ],
    ids=["model-file", "tokens-file"],
)
def test_download_success_keeps_atomic_part_and_uses_certifi_ca(
    monkeypatch, tmp_path, payload, filename
):
    """下载成功路径不受影响：certifi CA bundle 驱动 SSL 上下文传入 urlopen；.part 原子落盘语义保持。"""
    import certifi
    from multimodal import asr as asr_mod

    ca_records: list[dict] = []
    real_create_default_context = ssl.create_default_context

    def recording_create_default_context(**kwargs):
        ca_records.append(kwargs)
        return real_create_default_context(**kwargs)

    opened: list[dict] = []

    def fake_urlopen(url, timeout=0, context=None):
        opened.append({"url": url, "context": context})
        return _FakeHTTPResponse(payload)

    monkeypatch.setattr(ssl, "create_default_context", recording_create_default_context)
    monkeypatch.setattr(asr_mod.urllib.request, "urlopen", fake_urlopen)

    dest = tmp_path / filename
    asr_mod._download_file(f"https://dl.example.test/{filename}", dest, 30)

    assert dest.read_bytes() == payload  # 内容落盘正确
    assert not list(tmp_path.glob("*.part"))  # os.replace 后无 .part 残留
    assert ca_records and ca_records[0].get("cafile") == certifi.where()
    assert isinstance(opened[0]["context"], ssl.SSLContext)  # 上下文确实传入 urlopen
    assert opened[0]["url"] == f"https://dl.example.test/{filename}"


def test_download_ssl_context_falls_back_without_certifi(monkeypatch):
    """certifi 未安装（旧 venv 未刷新）→ 回退系统默认上下文，不阻断下载能力。"""
    from multimodal import asr as asr_mod

    monkeypatch.setitem(sys.modules, "certifi", None)  # None in sys.modules = import 失败语义

    ctx = asr_mod._download_ssl_context()

    assert isinstance(ctx, ssl.SSLContext)
