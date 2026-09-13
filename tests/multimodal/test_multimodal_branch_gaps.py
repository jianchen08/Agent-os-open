# @feature: FP-0.2.二 内部模块manifest | @ci: python-coverage
"""multimodal 三件套（asr / adapter / server）分支缺口补测。

行为驱动（公开 API 入参 → 可观察输出），收口 coverage.xml 2026-09-13 缺行：
- asr.py：load_asr_config 全分支（yaml 成功/文件缺失/OSError/YAMLError/asr 节
  非 dict/default_provider 命中与回退/env 占位符三态/空 provider）、config 属性、
  网络错误异常链、响应回退字段（transcription/result/data）、get_asr_service 单例
- adapter.py：三适配器 convert（base64/url/非图片跳过/无数据跳过/顺序保持）
  与 get_capability
- server.py：MCP 工具面 multimodal.convert（附件循环）、multimodal.supported、
  multimodal.transcribe（未配置/成功）、on_load/on_unload 生命周期

外部依赖 mock：ASR HTTP API（aiohttp.ClientSession）、ASR 服务实例（server 工具面）。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit  # 0.2 TDD 分层：单元测试

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "multimodal"

_MODULE_NAME = "multimodal_branch_gaps_server"


def _run(coro: Any) -> Any:
    """同步执行协程（独立事件循环，不依赖 pytest-asyncio 插桩）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ══════════════════════════════════════════════════════════════════════════
# asr.py — load_asr_config 配置装载分支
# ══════════════════════════════════════════════════════════════════════════


def _write_yaml(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _provider_yaml(default_line: str) -> str:
    return (
        "asr:\n"
        "  enabled: true\n"
        f"{default_line}"
        "  providers:\n"
        "    alpha:\n"
        "      api_base: \"https://alpha.example/v1\"\n"
        "      api_key: \"k-alpha\"\n"
        "      model: \"m-alpha\"\n"
        "      language: \"en\"\n"
        "    beta:\n"
        "      api_base: \"https://beta.example/v2\"\n"
        "      api_key: \"k-beta\"\n"
        "      model: \"m-beta\"\n"
        "      language: \"fr\"\n"
    )


class TestLoadAsrConfig:
    """load_asr_config：配置文件各分支。"""

    def test_valid_yaml_with_default_provider_hit(self, tmp_path, monkeypatch):
        """default_provider 命中：取对应 provider，${ENV} 占位符解析为环境值。"""
        import asr

        monkeypatch.setenv("TEST_ASR_KEY_GAP", "sk-from-env")
        cfg_path = _write_yaml(
            tmp_path / "asr.yaml",
            'asr:\n'
            '  enabled: true\n'
            '  default_provider: glm\n'
            '  providers:\n'
            '    glm:\n'
            '      api_base: "https://glm.example/v4"\n'
            '      api_key: "${TEST_ASR_KEY_GAP}"\n'
            '      model: "glm-asr-test"\n'
            '      language: "zh-CN"\n',
        )

        cfg = asr.load_asr_config(cfg_path)

        assert cfg.api_key == "sk-from-env"
        assert cfg.api_base == "https://glm.example/v4"
        assert cfg.model == "glm-asr-test"
        assert cfg.language == "zh-CN"
        assert cfg.enabled is True  # 性质：enabled ⟺ 声明启用且有 key

    @pytest.mark.parametrize(
        "default_line",
        ["", '  default_provider: "nope"\n'],
        ids=["no_default_key", "default_not_in_providers"],
    )
    def test_default_provider_falls_back_to_first(self, tmp_path, default_line):
        """default_provider 缺失/未命中：回退第一个 provider（不取 beta）。"""
        import asr

        cfg = asr.load_asr_config(_write_yaml(tmp_path / "asr.yaml", _provider_yaml(default_line)))

        assert cfg.api_base == "https://alpha.example/v1"
        assert cfg.model == "m-alpha"
        assert cfg.language == "en"
        assert cfg.enabled is True

    def test_provider_without_api_key_disables_service(self, tmp_path):
        """provider 缺 api_key 字段：api_key 空且 enabled 降为 False（fail-closed）。"""
        import asr

        cfg_path = _write_yaml(
            tmp_path / "asr.yaml",
            'asr:\n'
            '  enabled: true\n'
            '  providers:\n'
            '    solo:\n'
            '      api_base: "https://solo.example/v1"\n'
            '      model: "m-solo"\n',
        )

        cfg = asr.load_asr_config(cfg_path)

        assert cfg.api_key == ""
        assert cfg.enabled is False  # 性质：无 key 必不可用
        assert cfg.api_base == "https://solo.example/v1"

    def test_literal_api_key_passthrough(self, tmp_path):
        """api_key 为字面量（非占位符）：原样透传。"""
        import asr

        cfg_path = _write_yaml(
            tmp_path / "asr.yaml",
            'asr:\n'
            '  providers:\n'
            '    p:\n'
            '      api_key: "plain-literal-key"\n',
        )

        cfg = asr.load_asr_config(cfg_path)

        assert cfg.api_key == "plain-literal-key"
        assert cfg.enabled is True

    def test_enabled_false_keeps_key_but_disables(self, tmp_path):
        """enabled: false：即使有 key 服务也不可用。"""
        import asr

        cfg_path = _write_yaml(
            tmp_path / "asr.yaml",
            'asr:\n'
            '  enabled: false\n'
            '  providers:\n'
            '    p:\n'
            '      api_key: "k-have"\n',
        )

        cfg = asr.load_asr_config(cfg_path)

        assert cfg.api_key == "k-have"
        assert cfg.enabled is False

    def test_asr_section_not_dict_returns_defaults(self, tmp_path):
        """asr 节为标量（非 dict）：整体回退默认配置。"""
        import asr

        cfg = asr.load_asr_config(_write_yaml(tmp_path / "asr.yaml", 'asr: "just-a-string"\n'))

        assert cfg == asr.ASRConfig()  # 与全新默认实例逐字段相等

    def test_empty_file_treated_as_empty_config(self, tmp_path):
        """空文件（yaml 解析为 None）：按空配置处理，服务不可用。"""
        import asr

        cfg = asr.load_asr_config(_write_yaml(tmp_path / "asr.yaml", ""))

        assert cfg.api_key == ""
        assert cfg.enabled is False
        assert cfg.model == asr.DEFAULT_MODEL

    def test_unreadable_path_falls_back_to_env(self, tmp_path, monkeypatch):
        """config_path 指向目录（OSError）：回退环境变量。"""
        import asr

        monkeypatch.setenv("ZHIPU_API_KEY", "sk-on-oserror")
        cfg = asr.load_asr_config(tmp_path)  # 目录不可作为文件打开

        assert cfg.api_key == "sk-on-oserror"
        assert cfg.enabled is True
        assert cfg.api_base == asr.DEFAULT_API_BASE

    def test_malformed_yaml_falls_back_to_env(self, tmp_path, monkeypatch):
        """YAML 语法损坏：回退环境变量，不抛异常。"""
        import asr

        monkeypatch.setenv("ZHIPU_API_KEY", "sk-on-yaml-error")
        cfg = asr.load_asr_config(_write_yaml(tmp_path / "asr.yaml", "asr: [unclosed\n"))

        assert cfg.api_key == "sk-on-yaml-error"
        assert cfg.enabled is True

    def test_default_path_reads_repo_config(self):
        """无参调用：按默认路径读仓库 config/models/asr.yaml（真实配置冒烟）。"""
        import asr

        cfg = asr.load_asr_config()

        assert isinstance(cfg, asr.ASRConfig)
        assert cfg.api_base.startswith("https://")
        assert bool(cfg.model)
        assert not (cfg.enabled and not cfg.api_key)  # 性质：enabled ⇒ 有 key


class TestASRServiceConfigProperty:
    """ASRService.config 属性。"""

    def test_config_property_returns_injected_config(self):
        import asr

        cfg = asr.ASRConfig(api_key="k-prop", language="ja")
        svc = asr.ASRService(cfg)

        assert svc.config is cfg
        assert svc.config.language == "ja"


# ══════════════════════════════════════════════════════════════════════════
# asr.py — transcribe 网络错误链与响应回退字段（aiohttp 为外部边界，mock）
# ══════════════════════════════════════════════════════════════════════════


def _patch_session(post_enter: AsyncMock | None = None, resp: Any = None):
    """patch aiohttp.ClientSession：post 上下文进入时返回 resp 或抛 post_enter 异常。"""
    post_cm = MagicMock()
    if post_enter is not None:
        post_cm.__aenter__ = post_enter
    else:
        post_cm.__aenter__ = AsyncMock(return_value=resp)
    post_cm.__aexit__ = AsyncMock(return_value=None)

    session = MagicMock()
    session.post = MagicMock(return_value=post_cm)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    return patch("asr.aiohttp.ClientSession", return_value=session)


def _make_resp(status: int, payload: dict) -> Any:
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=payload)
    resp.text = AsyncMock(return_value=str(payload))
    return resp


class TestTranscribeErrorAndFallback:
    """transcribe：网络错误链与非常规响应字段的容错。"""

    def test_client_error_wrapped_as_runtime_error(self):
        import aiohttp
        import asr

        svc = asr.ASRService(asr.ASRConfig(api_key="sk-test"))
        patcher = _patch_session(post_enter=AsyncMock(side_effect=aiohttp.ClientError("net down")))

        with patcher, pytest.raises(RuntimeError, match="ASR 网络请求失败") as exc_info:
            _run(svc.transcribe(b"audio", "audio/webm"))

        assert isinstance(exc_info.value.__cause__, aiohttp.ClientError)  # 异常链保留

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"transcription": "  alpha transcription  "}, "alpha transcription"),
            ({"text": "", "result": "beta result"}, "beta result"),
            ({"transcription": 123, "data": "gamma data"}, "gamma data"),
        ],
        ids=["transcription_key", "result_after_empty_text", "non_str_skipped"],
    )
    def test_fallback_fields_when_text_missing_or_empty(self, payload, expected):
        """text 缺失/为空时按 transcription/result/data 顺序容错，非字符串跳过。"""
        import asr

        svc = asr.ASRService(asr.ASRConfig(api_key="sk-test"))
        patcher = _patch_session(resp=_make_resp(200, payload))

        with patcher:
            text = _run(svc.transcribe(b"audio", "audio/webm"))

        assert text == expected
        assert text == text.strip()  # 性质：前后空白已归一


class TestGetAsrServiceSingleton:
    """get_asr_service 单例工厂。"""

    def test_creates_then_returns_cached_instance(self):
        import asr

        asr.reset_asr_service()
        try:
            svc1 = asr.get_asr_service()
            svc2 = asr.get_asr_service()

            assert isinstance(svc1, asr.ASRService)
            assert svc1 is svc2  # 二次调用返回缓存实例
        finally:
            asr.reset_asr_service()


# ══════════════════════════════════════════════════════════════════════════
# adapter.py — 三适配器 convert / get_capability
# ══════════════════════════════════════════════════════════════════════════


def _att(mm: Any, media_type: Any, *, b64: str | None = None, url: str | None = None,
         mime: str = "image/png") -> Any:
    return mm.AttachmentInfo(
        file_id="fid000000001",
        filename="file.bin",
        mime_type=mime,
        size=128,
        media_type=media_type,
        base64_data=b64,
        url=url,
    )


# 不可渲染附件矩阵：非图片（即使带数据）或图片但 base64/url 双缺。
_UNRENDERABLE = [
    ("AUDIO", "QUJD", None, "audio/webm"),
    ("IMAGE", None, None, "image/png"),
    ("DOCUMENT", None, "https://cdn.example/d.pdf", "application/pdf"),
]


class TestOpenAIVisionAdapter:
    """OpenAIVisionAdapter：base64 优先、url 兜底、非可渲染跳过。"""

    def test_convert_text_only(self):
        import adapter

        out = adapter.OpenAIVisionAdapter().convert("hello", [])

        assert out == [{"type": "text", "text": "hello"}]

    def test_convert_base64_image_builds_data_uri(self):
        import adapter
        import mm_types

        att = _att(mm_types, mm_types.MediaType.IMAGE, b64="QUJD", mime="image/png")

        out = adapter.OpenAIVisionAdapter().convert("describe", [att])

        assert out[0] == {"type": "text", "text": "describe"}
        assert out[1]["type"] == "image_url"
        url = out[1]["image_url"]["url"]
        assert url == "data:image/png;base64,QUJD"
        assert url.startswith("data:image/png;base64,")  # 性质：data URI 形态
        assert adapter.OpenAIVisionAdapter.degraded is False

    def test_convert_url_image_used_when_no_base64(self):
        import adapter
        import mm_types

        att = _att(mm_types, mm_types.MediaType.IMAGE, url="https://cdn.example/pic.png")

        out = adapter.OpenAIVisionAdapter().convert("describe", [att])

        assert out[1]["image_url"]["url"] == "https://cdn.example/pic.png"

    @pytest.mark.parametrize(
        ("media", "b64", "url", "mime"),
        _UNRENDERABLE,
        ids=["audio_with_data", "image_without_data", "document_with_url"],
    )
    def test_convert_skips_unrenderable_attachments(self, media, b64, url, mime):
        import adapter
        import mm_types

        att = _att(mm_types, getattr(mm_types.MediaType, media), b64=b64, url=url, mime=mime)

        out = adapter.OpenAIVisionAdapter().convert("hi", [att])

        assert out == [{"type": "text", "text": "hi"}]

    def test_convert_mixed_attachments_preserve_order(self):
        import adapter
        import mm_types

        audio = _att(mm_types, mm_types.MediaType.AUDIO, b64="RkFLRQ==", mime="audio/webm")
        img_b64 = _att(mm_types, mm_types.MediaType.IMAGE, b64="QUJD")
        img_url = _att(mm_types, mm_types.MediaType.IMAGE, url="https://cdn.example/x.png")

        out = adapter.OpenAIVisionAdapter().convert("q", [audio, img_b64, img_url])

        assert [m["type"] for m in out] == ["text", "image_url", "image_url"]
        assert out[1]["image_url"]["url"].startswith("data:")  # base64 优先于 url
        assert out[2]["image_url"]["url"] == "https://cdn.example/x.png"

    def test_get_capability(self):
        import adapter

        cap = adapter.OpenAIVisionAdapter().get_capability()

        assert cap.model_name == "gpt-4o"
        assert cap.supports_image is True
        assert cap.supports_audio is True
        assert cap.supports_video is False
        assert cap.max_image_size == 20 * 1024 * 1024 > 0
        assert "image/png" in cap.supported_image_types


class TestClaudeVisionAdapter:
    """ClaudeVisionAdapter：仅 base64 图片块，url-only 图片同样跳过。"""

    def test_convert_base64_image_builds_source_block(self):
        import adapter
        import mm_types

        att = _att(mm_types, mm_types.MediaType.IMAGE, b64="QUJD", mime="image/webp")

        out = adapter.ClaudeVisionAdapter().convert("describe", [att])

        assert out[0] == {"type": "text", "text": "describe"}
        assert out[1] == {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/webp", "data": "QUJD"},
        }

    @pytest.mark.parametrize(
        ("media", "b64", "url", "mime"),
        [*_UNRENDERABLE, ("IMAGE", None, "https://cdn.example/pic.png", "image/png")],
        ids=["audio_with_data", "image_without_data", "document_with_url", "image_url_only"],
    )
    def test_convert_skips_attachments_without_base64(self, media, b64, url, mime):
        """Claude 无 url 分支：凡无 base64_data 一律不产图片块。"""
        import adapter
        import mm_types

        att = _att(mm_types, getattr(mm_types.MediaType, media), b64=b64, url=url, mime=mime)

        out = adapter.ClaudeVisionAdapter().convert("hi", [att])

        assert out == [{"type": "text", "text": "hi"}]

    def test_get_capability(self):
        import adapter

        cap = adapter.ClaudeVisionAdapter().get_capability()

        assert cap.model_name == "claude-3-opus"
        assert cap.supports_image is True
        assert cap.supports_audio is False
        assert cap.max_image_size == 20 * 1024 * 1024 > 0


class TestDefaultAdapter:
    """DefaultAdapter：降级面（degraded=True）与纯文本能力。"""

    def test_get_capability_all_false(self):
        import adapter

        cap = adapter.DefaultAdapter().get_capability()

        assert cap.model_name == "default"
        assert cap.supports_image is False
        assert cap.supports_audio is False
        assert cap.supports_video is False
        assert cap.supported_image_types == []

    def test_convert_ignores_attachments_and_marks_degraded(self):
        import adapter
        import mm_types

        att = _att(mm_types, mm_types.MediaType.IMAGE, b64="QUJD")

        out = adapter.DefaultAdapter().convert("text only", [att])

        assert out == [{"type": "text", "text": "text only"}]
        assert adapter.DefaultAdapter.degraded is True  # 附件丢弃必须可感知


# ══════════════════════════════════════════════════════════════════════════
# server.py — MCP 工具面（经 importlib 独立加载，见既有 test_multimodal_http 模式）
# ══════════════════════════════════════════════════════════════════════════


def _load_server() -> Any:
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, str(_PLUGIN_DIR / "server.py"))
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server() -> Any:
    mod = _load_server()
    yield mod
    sys.modules.pop(_MODULE_NAME, None)


class _FakeCap:
    """能力替身：脚本化 supports_* 标志。"""

    def __init__(self, model_name: str, *, image: bool = False, audio: bool = False,
                 video: bool = False) -> None:
        self.model_name = model_name
        self.supports_image = image
        self.supports_audio = audio
        self.supports_video = video


class _RecordingASR:
    """可用 ASR 替身：记录调用参数，返回脚本化文本。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[tuple[bytes, str, str]] = []

    def is_available(self) -> bool:
        return True

    async def transcribe(self, audio_bytes: bytes, mime_type: str, language: str) -> str:
        self.calls.append((audio_bytes, mime_type, language))
        return self.text


class _UnavailableASR:
    def is_available(self) -> bool:
        return False


class TestServerLifecycle:
    """on_load / on_unload 生命周期钩子。"""

    def test_on_load_and_on_unload_complete(self, server):
        assert _run(server._on_load({})) is None
        assert _run(server._on_unload({})) is None


class TestServerMultimodalConvert:
    """multimodal.convert 工具：附件循环与 degraded 标记。"""

    def test_openai_with_image_and_audio_attachments(self, server):
        result = _run(server.multimodal_convert(
            content="描述这张图",
            provider="openai",
            attachments=[
                {"filename": "pic.png", "mime_type": "image/png", "media_type": "image",
                 "base64_data": "QUJD"},
                {"filename": "note.webm", "mime_type": "audio/webm", "media_type": "audio"},
            ],
        ))

        assert result["degraded"] is False
        assert result["count"] == len(result["messages"]) == 2  # 性质：count 与消息数一致
        assert result["messages"][0] == {"type": "text", "text": "描述这张图"}
        assert result["messages"][1]["type"] == "image_url"
        assert result["messages"][1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_default_adapter_reports_degraded_with_attachment(self, server):
        result = _run(server.multimodal_convert(
            content="hi",
            provider="deepseek",
            attachments=[
                {"filename": "pic.png", "mime_type": "image/png", "media_type": "image",
                 "base64_data": "QQ=="},
            ],
        ))

        assert result["degraded"] is True  # 附件被丢弃必须可感知
        assert result["messages"] == [{"type": "text", "text": "hi"}]
        assert result["count"] == 1


class TestServerMultimodalSupported:
    """multimodal.supported 工具：能力注册表 → 布尔 + 回显。"""

    def test_supported_true_when_image(self, server, monkeypatch):
        monkeypatch.setattr(
            server.ModelCapabilityRegistry, "get_capability",
            lambda name: _FakeCap(name, image=True),
        )

        out = _run(server.multimodal_supported("glm-5.2"))

        assert out == {"model_name": "glm-5.2", "multimodal_supported": True}

    def test_supported_false_when_text_only(self, server, monkeypatch):
        monkeypatch.setattr(
            server.ModelCapabilityRegistry, "get_capability",
            lambda name: _FakeCap(name),
        )

        out = _run(server.multimodal_supported("deepseek-chat"))

        assert out == {"model_name": "deepseek-chat", "multimodal_supported": False}


class TestServerMultimodalTranscribe:
    """multimodal.transcribe 工具：未配置短路 + base64 解码转发。"""

    def test_not_configured_returns_error_shape(self, server, monkeypatch):
        import asr as asr_mod

        monkeypatch.setattr(asr_mod, "get_asr_service", lambda: _UnavailableASR())

        out = _run(server.multimodal_transcribe(
            audio_base64="QUJD", mime_type="audio/webm", language="zh-CN"))

        assert out["text"] == ""
        assert "not configured" in out["error"]

    @pytest.mark.parametrize(
        ("raw", "mime", "lang", "expected"),
        [
            (b"webm-bytes-01", "audio/webm", "zh-CN", "转写文本"),
            (b"mp3-bytes-999", "audio/mpeg", "en-US", "transcribed"),
        ],
        ids=["webm_zh", "mp3_en"],
    )
    def test_success_decodes_base64_and_forwards(self, server, monkeypatch, raw, mime, lang,
                                                 expected):
        import asr as asr_mod

        fake = _RecordingASR(expected)
        monkeypatch.setattr(asr_mod, "get_asr_service", lambda: fake)

        out = _run(server.multimodal_transcribe(
            audio_base64=base64.b64encode(raw).decode("ascii"), mime_type=mime, language=lang))

        assert out == {"text": expected}
        assert fake.calls == [(raw, mime, lang)]  # base64 已解码、参数原样转发
