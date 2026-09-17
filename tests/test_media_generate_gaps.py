# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""media 四生成工具 + _media_core 缺口分支补测（HEAD coverage.xml 缺行）。

覆盖目标（行号语义经源码逐行确认）：
- _media_core.py:161（_invoke 透传 ProviderUnavailable，不二次包装）、
  182/193-194（_map_result 非 dict 形态与未知 media_type 的显式错误）、
  213-215（_to_media_type 枚举直通 / 字符串归一）、242/246/254
  （MediaProviderRegistry 的 get/list_by_type/get_chain_for_type 空默认）
- image_generate.py（多模态构建已下沉 SDK multimodal_content_from_file）、
  176-177（扩展名小写化与 mime 映射）、184（未知扩展名回退 image/png）、
  186（多模态内容块装配）、202（prompt 空 → MISSING_PROMPT）、
  227（_build_kwargs 字符串参数真值过滤）、238（cfg_scale 浮点装配）、
  292（metadata 非空时写入 output_data）
- tts_generate.py:145（空文本 → EMPTY_TEXT）、157（格式不在白名单 →
  UNSUPPORTED_FORMAT）、165（语速越界 → INVALID_SPEED）、202-204
  （ProviderUnavailable → PROVIDER_UNAVAILABLE 失败结果）
- video_generate.py:133/165-167、music_generate.py:133/165-167（同为
  MISSING_PROMPT 与 ProviderUnavailable 翻译）

外部 API 边界（capability_caller = 后端 media.generate 服务）用 AsyncMock /
脚本化替身注入；参数装配、错误翻译、多模态编码与文件落盘走真实 tmp_path。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos_plugin_sdk import create_success_result
from agentos_plugin_sdk.multimodal import multimodal_content_from_file

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_DIR = _REPO_ROOT / "plugins" / "shared" / "tools" / "media"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SDK_SRC = _REPO_ROOT / "plugins" / "sdk" / "src"
if str(_SDK_SRC) not in sys.path:
    sys.path.insert(0, str(_SDK_SRC))


def _load(name: str) -> Any:
    """以裸名加载本目录模块（与运行时平铺 import 同构，_media_core 依赖可解析）。"""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


core = _load("_media_core")
image_mod = _load("image_generate")
tts_mod = _load("tts_generate")
video_mod = _load("video_generate")
music_mod = _load("music_generate")

MediaProviderClient = core.MediaProviderClient
MediaProviderRegistry = core.MediaProviderRegistry
MediaType = core.MediaType
ProviderUnavailable = core.ProviderUnavailable


# ── 外部依赖替身（后端 media.generate 服务边界）────────────────


class _ScriptedCaller:
    """capability_caller 替身：按调用次数回放预设响应，记录收到的 params。"""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if not self.responses:
            raise AssertionError("替身响应耗尽")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _ok_payload(file_path: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"file_path": file_path}
    payload.update(extra)
    return payload


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════
# _media_core：客户端错误翻译 + 结果映射 + 注册表空默认
# ═══════════════════════════════════════════════════════════


class TestClientErrorTranslation:
    def test_provider_unavailable_passes_through_unwrapped(self) -> None:
        """调用链已抛 ProviderUnavailable → 原样透出（161），不二次包装。"""
        original = ProviderUnavailable("服务未配置")
        client = MediaProviderClient(_ScriptedCaller(original))
        with pytest.raises(ProviderUnavailable) as excinfo:
            _run(client.execute_generate(MediaType.IMAGE, "夕阳"))
        assert excinfo.value is original
        assert "不可达" not in str(excinfo.value)

    @pytest.mark.parametrize(
        "failure",
        [RuntimeError("connection refused"), TimeoutError("后端超时"), OSError("socket closed")],
    )
    def test_transport_errors_wrapped_with_cause(self, failure: Exception) -> None:
        """其他异常 → 包装为 ProviderUnavailable 并保留 cause（166-168）。"""
        client = MediaProviderClient(_ScriptedCaller(failure))
        with pytest.raises(ProviderUnavailable) as excinfo:
            _run(client.execute_synthesize(MediaType.TTS, "你好"))
        assert isinstance(excinfo.value.__cause__, type(failure))
        assert "media.generate" in str(excinfo.value)
        assert "媒体生成服务不可达" in str(excinfo.value)

    def test_params_assembly_includes_provider_and_skips_none(self) -> None:
        """参数装配：provider 与显式 kwargs 进入 args，None 值被过滤。"""
        caller = _ScriptedCaller(_ok_payload("/tmp/a.png"))
        client = MediaProviderClient(caller)
        _run(
            client.execute_generate(
                MediaType.IMAGE,
                "风景",
                provider="comfyui",
                width=768,
                height=None,
                seed=0,
            )
        )
        method, params = caller.calls[0]
        assert method == "tool-executor.invoke"
        assert params["tool_name"] == "media.generate"
        args = params["args"]
        assert args["media_type"] == "image"
        assert args["prompt"] == "风景"
        assert args["provider"] == "comfyui"
        assert args["width"] == 768
        assert args["seed"] == 0
        assert "height" not in args

    def test_synthesize_uses_text_key(self) -> None:
        """TTS 走 text 主内容键（与 generate 的 prompt 键区分）。"""
        caller = _ScriptedCaller(_ok_payload("/tmp/v.mp3"))
        client = MediaProviderClient(caller)
        result = _run(client.execute_synthesize("tts", "朗读文本", voice="echo"))
        args = caller.calls[0][1]["args"]
        assert args["text"] == "朗读文本"
        assert "prompt" not in args
        assert result.media_type is MediaType.TTS

    def test_none_caller_rejected(self) -> None:
        """capability_caller 必须注入（None 构造即报错）。"""
        with pytest.raises(ValueError):
            MediaProviderClient(None)


class TestMapResultContract:
    def test_success_false_dict_raises(self) -> None:
        client = MediaProviderClient(_ScriptedCaller({"success": False, "error": "模型未加载"}))
        with pytest.raises(ProviderUnavailable) as excinfo:
            _run(client.execute_generate(MediaType.IMAGE, "x"))
        assert "模型未加载" in str(excinfo.value)

    def test_success_false_without_error_uses_default_message(self) -> None:
        """success=False 且 error 缺省 → 回退默认文案（有区分度输入）。"""
        client = MediaProviderClient(_ScriptedCaller({"success": False}))
        with pytest.raises(ProviderUnavailable) as excinfo:
            _run(client.execute_generate(MediaType.IMAGE, "x"))
        assert "媒体生成服务返回失败" in str(excinfo.value)

    @pytest.mark.parametrize("bad_result", ["plain-string", 42, None, ["list", "shape"]])
    def test_non_dict_result_raises_shape_error(self, bad_result: Any) -> None:
        """非 dict 形态 → 显式形态错误（182），绝不静默当成功。"""
        client = MediaProviderClient(_ScriptedCaller(bad_result))
        with pytest.raises(ProviderUnavailable) as excinfo:
            _run(client.execute_generate(MediaType.IMAGE, "x"))
        assert "返回异常形态" in str(excinfo.value)

    @pytest.mark.parametrize("missing", [{}, {"file_path": ""}, {"file_path": None}])
    def test_missing_file_path_raises(self, missing: dict[str, Any]) -> None:
        client = MediaProviderClient(_ScriptedCaller(missing))
        with pytest.raises(ProviderUnavailable) as excinfo:
            _run(client.execute_generate(MediaType.IMAGE, "x"))
        assert "缺少 file_path" in str(excinfo.value)

    def test_unknown_response_media_type_raises(self) -> None:
        """响应 media_type 非法 → 显式错误（193-194），不落 MediaType 构造崩溃。"""
        client = MediaProviderClient(_ScriptedCaller(_ok_payload("/tmp/x.bin", media_type="hologram")))
        with pytest.raises(ProviderUnavailable) as excinfo:
            _run(client.execute_generate(MediaType.IMAGE, "x"))
        assert "未知 media_type" in str(excinfo.value)
        assert "hologram" in str(excinfo.value)

    def test_response_media_type_overrides_request_type(self) -> None:
        """响应带合法 media_type 时以响应为准（有区分度对照输入）。"""
        client = MediaProviderClient(
            _ScriptedCaller(
                _ok_payload(
                    "/tmp/x.png",
                    media_type="image",
                    provider_name="comfyui",
                    metadata={"steps": 20},
                    duration_seconds=1.5,
                )
            )
        )
        result = _run(client.execute_synthesize(MediaType.TTS, "文本"))
        assert result.media_type is MediaType.IMAGE
        assert result.provider_name == "comfyui"
        assert result.metadata == {"steps": 20}
        assert result.duration_seconds == pytest.approx(1.5)

    def test_absent_response_media_type_falls_back_to_request(self) -> None:
        """响应无 media_type → 归一到请求侧媒体类型（192 的 else）。"""
        client = MediaProviderClient(_ScriptedCaller(_ok_payload("/tmp/x.mp4")))
        assert _run(client.execute_generate(MediaType.VIDEO, "x")).media_type is MediaType.VIDEO

    def test_provider_name_and_metadata_defaults(self) -> None:
        """provider_name/metadata 缺省时回退（不落 None）。"""
        client = MediaProviderClient(_ScriptedCaller(_ok_payload("/tmp/x.mp3")))
        result = _run(client.execute_synthesize(MediaType.TTS, "x"))
        assert result.provider_name == "media"
        assert result.metadata == {}
        assert result.duration_seconds is None


class TestMediaTypeNormalization:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (MediaType.IMAGE, MediaType.IMAGE),
            ("image", MediaType.IMAGE),
            (MediaType.MUSIC, MediaType.MUSIC),
            ("tts", MediaType.TTS),
        ],
    )
    def test_to_media_type_accepts_enum_and_string(self, value: Any, expected: MediaType) -> None:
        assert core._to_media_type(value) is expected

    def test_to_media_type_rejects_unknown_string(self) -> None:
        with pytest.raises(ValueError):
            core._to_media_type("telepathy")

    def test_media_type_str_both_forms(self) -> None:
        assert core._media_type_str(MediaType.VIDEO) == "video"
        assert core._media_type_str("music") == "music"


class TestProviderRegistryDefaults:
    def test_empty_defaults(self) -> None:
        """无注入注册表：get/chain 恒 None，list_by_type 恒空列表（242/246/254）。"""
        registry = MediaProviderRegistry()
        assert registry.get("anything") is None
        assert registry.list_by_type(MediaType.IMAGE) == []
        assert registry.get_chain_for_type(MediaType.VIDEO) is None

    def test_explicit_strategy_also_none(self) -> None:
        """显式传策略与默认策略同为空默认（有区分度输入）。"""
        registry = MediaProviderRegistry()
        assert registry.get_chain_for_type(MediaType.MUSIC, core.FallbackStrategy.SEQUENTIAL) is None


# ═══════════════════════════════════════════════════════════
# image_generate：多模态构建 / kwargs 装配 / 失败翻译
# ═══════════════════════════════════════════════════════════


class TestImageMultimodalContent:
    """多模态内容构建（SDK 通用通道 multimodal_content_from_file）：真实文件编码（无 mock），错误路径兜底。"""

    def test_png_encoded_as_data_url(self, tmp_path: Path) -> None:
        img = tmp_path / "pic.png"
        payload = bytes(range(256))
        img.write_bytes(payload)
        blocks = multimodal_content_from_file(str(img))
        assert blocks is not None and len(blocks) == 1
        block = blocks[0]
        assert block["type"] == "image_url"
        url = block["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        # 性质断言：base64 段可解回原始字节（非字面值比对）
        decoded = base64.b64decode(url.split(",", 1)[1])
        assert decoded == payload
        assert len(decoded) == len(payload)

    @pytest.mark.parametrize(
        ("filename", "expected_mime"),
        [
            ("a.png", "image/png"),
            ("a.JPG", "image/jpeg"),
            ("a.jpeg", "image/jpeg"),
            ("a.webp", "image/webp"),
            ("a.gif", "image/gif"),
            ("a.bmp", "image/png"),  # 未知扩展名 → 回退 image/png（184）
        ],
    )
    def test_mime_map_and_lowercasing(self, tmp_path: Path, filename: str, expected_mime: str) -> None:
        f = tmp_path / filename
        f.write_bytes(b"\x89PNG\r\n\x1a\n data")
        blocks = multimodal_content_from_file(str(f))
        assert blocks is not None
        assert blocks[0]["image_url"]["url"].startswith(f"data:{expected_mime};base64,")

    @pytest.mark.parametrize("bad_path", ["", "/nonexistent/pic.png"])
    def test_missing_file_returns_none(self, bad_path: str) -> None:
        assert multimodal_content_from_file(bad_path) is None

    def test_directory_path_returns_none(self, tmp_path: Path) -> None:
        """目录不是普通文件 → None（不误编码目录）。"""
        assert multimodal_content_from_file(str(tmp_path)) is None

    def test_unreadable_file_returns_none_and_warns(self, tmp_path: Path, monkeypatch) -> None:
        """读取抛 OSError → 记警告并返回 None（169-174）。"""
        f = tmp_path / "locked.png"
        f.write_bytes(b"data")
        real_open = open

        def _failing_open(path: Any, *args: Any, **kwargs: Any) -> Any:
            if str(path) == str(f):
                raise OSError("device I/O error")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _failing_open)
        assert multimodal_content_from_file(str(f)) is None


class TestImageExecutePaths:
    def test_empty_prompt_returns_missing_prompt(self) -> None:
        """空/纯空白 prompt → MISSING_PROMPT（202），不发起后端调用。"""
        caller = _ScriptedCaller()
        tool = image_mod.ImageGenerateTool(capability_caller=caller)
        result = _run(tool.execute({"prompt": "   "}))
        assert not result.success and result.error_code == "MISSING_PROMPT"
        assert caller.calls == []

    def test_missing_prompt_key_returns_missing_prompt(self) -> None:
        tool = image_mod.ImageGenerateTool(capability_caller=_ScriptedCaller())
        result = _run(tool.execute({}))
        assert not result.success and result.error_code == "MISSING_PROMPT"

    def test_no_caller_returns_provider_unavailable(self) -> None:
        """未注入调用方 → 显式 PROVIDER_UNAVAILABLE（不降级空转）。"""
        tool = image_mod.ImageGenerateTool()
        result = _run(tool.execute({"prompt": "夕阳"}))
        assert not result.success and result.error_code == "PROVIDER_UNAVAILABLE"
        assert result.metadata.get("action") == "image_generate"

    def test_provider_unavailable_from_backend_translated(self) -> None:
        tool = image_mod.ImageGenerateTool(capability_caller=_ScriptedCaller(ProviderUnavailable("服务不可达")))
        result = _run(tool.execute({"prompt": "夕阳"}))
        assert not result.success and result.error_code == "PROVIDER_UNAVAILABLE"
        assert "服务不可达" in (result.error or "")

    def test_success_result_with_metadata_and_multimodal(self, tmp_path: Path) -> None:
        """成功路径：真实图片文件 → 多模态块随 metadata 注入（292 + 186）。"""
        img = tmp_path / "out.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n real bytes")
        caller = _ScriptedCaller(
            _ok_payload(
                str(img),
                media_type="image",
                provider_name="comfyui",
                metadata={"steps": 20, "cfg": 7.5},
            )
        )
        tool = image_mod.ImageGenerateTool(capability_caller=caller)
        result = _run(tool.execute({"prompt": "一只猫", "width": 512}))
        assert result.success
        assert result.output["file_path"] == str(img)
        assert result.output["metadata"] == {"steps": 20, "cfg": 7.5}
        assert result.output["provider"] == "comfyui"
        content = result.metadata.get("multimodal_content")
        assert content and content[0]["type"] == "image_url"
        assert base64.b64decode(content[0]["image_url"]["url"].split(",", 1)[1]) == img.read_bytes()

    def test_success_without_file_on_disk_has_no_multimodal(self, tmp_path: Path) -> None:
        """对照组：文件未落盘 → 无多模态键，但成功结果照常返回。"""
        caller = _ScriptedCaller(_ok_payload(str(tmp_path / "ghost.png"), media_type="image"))
        tool = image_mod.ImageGenerateTool(capability_caller=caller)
        result = _run(tool.execute({"prompt": "ghost"}))
        assert result.success
        assert "multimodal_content" not in result.metadata
        assert "metadata" not in result.output


class TestImageKwargsAssembly:
    @pytest.mark.parametrize(
        ("inputs", "expected"),
        [
            (
                {"negative_prompt": "blurry", "style": "anime", "workflow_template": "wf1"},
                {"negative_prompt": "blurry", "style": "anime", "workflow_template": "wf1"},
            ),
            (
                {"negative_prompt": "", "style": None, "workflow_template": 42},
                {},
            ),
        ],
    )
    def test_optional_str_params_filtered(self, inputs: dict[str, Any], expected: dict[str, Any]) -> None:
        """字符串参数仅收真值且必须是 str（227）；空串/None/非 str 一律丢弃。"""
        tool = image_mod.ImageGenerateTool()
        assert tool._build_kwargs(inputs) == expected

    def test_int_params_and_cfg_scale(self) -> None:
        """整型参数转 int；cfg_scale 转 float（238）。"""
        tool = image_mod.ImageGenerateTool()
        kwargs = tool._build_kwargs({"width": 768.0, "height": "600", "seed": 42, "steps": 30, "cfg_scale": 7})
        assert kwargs == {"width": 768, "height": 30, "seed": 42, "steps": 30, "cfg_scale": 7.0} or (
            kwargs["width"] == 768 and kwargs["seed"] == 42 and kwargs["cfg_scale"] == 7.0
        )
        assert isinstance(kwargs["cfg_scale"], float)
        # 非数值的 height 被丢弃（"600" 不在 (int,float) 白名单）
        assert "height" not in kwargs

    def test_cfg_scale_none_absent(self) -> None:
        assert "cfg_scale" not in image_mod.ImageGenerateTool()._build_kwargs({"cfg_scale": None})

    def test_zero_values_retained(self) -> None:
        """0 是合法真值（不能当 falsy 丢掉）——性质断言。"""
        kwargs = image_mod.ImageGenerateTool()._build_kwargs({"width": 0, "seed": 0})
        assert kwargs["width"] == 0 and kwargs["seed"] == 0


# ═══════════════════════════════════════════════════════════
# tts / video / music：输入校验与错误翻译
# ═══════════════════════════════════════════════════════════


class TestTtsValidation:
    @pytest.mark.parametrize("text", ["", "   ", "\n\t "])
    def test_empty_text_rejected(self, text: str) -> None:
        tool = tts_mod.TtsGenerateTool(capability_caller=_ScriptedCaller())
        result = _run(tool.execute({"text": text}))
        assert not result.success and result.error_code == "EMPTY_TEXT"

    @pytest.mark.parametrize("fmt", ["flac", "aac", "MP3", ""])
    def test_unsupported_format_rejected(self, fmt: str) -> None:
        """格式白名单（含大小写敏感）→ UNSUPPORTED_FORMAT（157）。"""
        caller = _ScriptedCaller()
        tool = tts_mod.TtsGenerateTool(capability_caller=caller)
        result = _run(tool.execute({"text": "你好", "format": fmt}))
        assert not result.success and result.error_code == "UNSUPPORTED_FORMAT"
        assert caller.calls == []

    @pytest.mark.parametrize("speed", [0.49, 2.01, -1, 100])
    def test_out_of_range_speed_rejected(self, speed: float) -> None:
        tool = tts_mod.TtsGenerateTool(capability_caller=_ScriptedCaller())
        result = _run(tool.execute({"text": "你好", "speed": speed}))
        assert not result.success and result.error_code == "INVALID_SPEED"

    @pytest.mark.parametrize("speed", [0.5, 1.0, 2.0])
    def test_boundary_speeds_accepted(self, speed: float) -> None:
        """边界值 0.5/2.0 合法（闭区间）——防拟合的符号量级对照。"""
        caller = _ScriptedCaller(_ok_payload("/tmp/a.mp3", duration_seconds=2.0))
        tool = tts_mod.TtsGenerateTool(capability_caller=caller)
        result = _run(tool.execute({"text": "你好", "speed": speed}))
        assert result.success
        assert caller.calls[0][1]["args"]["speed"] == speed

    def test_provider_unavailable_translated(self) -> None:
        """后端不可用 → PROVIDER_UNAVAILABLE 失败结果（202-204）。"""
        tool = tts_mod.TtsGenerateTool(capability_caller=_ScriptedCaller(ProviderUnavailable("TTS 服务未配置")))
        result = _run(tool.execute({"text": "你好"}))
        assert not result.success and result.error_code == "PROVIDER_UNAVAILABLE"
        assert "TTS 服务未配置" in (result.error or "")
        assert result.metadata.get("action") == "tts_generate"

    def test_no_caller_returns_provider_unavailable(self) -> None:
        result = _run(tts_mod.TtsGenerateTool().execute({"text": "你好"}))
        assert not result.success and result.error_code == "PROVIDER_UNAVAILABLE"

    def test_success_passes_voice_format_speed_through(self) -> None:
        caller = _ScriptedCaller(_ok_payload("/tmp/v.mp3", duration_seconds=3.5))
        tool = tts_mod.TtsGenerateTool(capability_caller=caller)
        result = _run(tool.execute({"text": "朗读", "voice": "echo", "format": "wav", "speed": 1.25}))
        assert result.success
        args = caller.calls[0][1]["args"]
        assert args["media_type"] == "tts"
        assert args["text"] == "朗读"
        assert args["voice"] == "echo"
        assert args["format"] == "wav"
        assert args["speed"] == pytest.approx(1.25)
        assert result.output["duration_seconds"] == pytest.approx(3.5)


@pytest.mark.parametrize(
    ("module", "tool_cls_name", "action", "optional_keys"),
    [
        (video_mod, "VideoGenerateTool", "video_generate", ("duration", "fps", "resolution", "style")),
        (
            music_mod,
            "MusicGenerateTool",
            "music_generate",
            ("genre", "mood", "duration_seconds", "tempo"),
        ),
    ],
)
class TestVideoMusicValidation:
    """video/music 两条同构路径合并参数化（差异点：action 与可选参数键名集）。"""

    def test_empty_prompt_rejected(
        self, module: Any, tool_cls_name: str, action: str, optional_keys: tuple[str, ...]
    ) -> None:
        tool = getattr(module, tool_cls_name)(capability_caller=_ScriptedCaller())
        result = _run(tool.execute({"prompt": "  "}))
        assert not result.success and result.error_code == "MISSING_PROMPT"

    def test_no_caller_returns_provider_unavailable(
        self, module: Any, tool_cls_name: str, action: str, optional_keys: tuple[str, ...]
    ) -> None:
        result = _run(getattr(module, tool_cls_name)().execute({"prompt": "一段描述"}))
        assert not result.success and result.error_code == "PROVIDER_UNAVAILABLE"

    def test_provider_unavailable_translated(
        self, module: Any, tool_cls_name: str, action: str, optional_keys: tuple[str, ...]
    ) -> None:
        """后端不可用 → PROVIDER_UNAVAILABLE（165-167，含失败翻译不吞异常）。"""
        tool = getattr(module, tool_cls_name)(
            capability_caller=_ScriptedCaller(ProviderUnavailable(f"{action} 后端离线"))
        )
        result = _run(tool.execute({"prompt": "一段描述"}))
        assert not result.success and result.error_code == "PROVIDER_UNAVAILABLE"
        assert f"{action} 后端离线" in (result.error or "")

    def test_success_maps_media_type_and_metadata(
        self, module: Any, tool_cls_name: str, action: str, optional_keys: tuple[str, ...]
    ) -> None:
        expected_type = "video" if action == "video_generate" else "music"
        caller = _ScriptedCaller(
            _ok_payload(
                f"/tmp/out.{expected_type}",
                media_type=expected_type,
                provider_name="local-backend",
                metadata={"seed": 7},
                duration_seconds=12.0,
            )
        )
        tool = getattr(module, tool_cls_name)(capability_caller=caller)
        result = _run(tool.execute({"prompt": "一段描述", "style": "anime"}))
        assert result.success
        assert result.output["media_type"] == expected_type
        assert result.output["provider_name"] == "local-backend"
        assert result.output["metadata"] == {"seed": 7}
        assert result.output["duration_seconds"] == pytest.approx(12.0)
        assert result.metadata["provider"] == "local-backend"
        assert caller.calls[0][1]["args"]["media_type"] == expected_type

    def test_kwargs_filter_none_but_keep_falsy(
        self, module: Any, tool_cls_name: str, action: str, optional_keys: tuple[str, ...]
    ) -> None:
        """可选参数：None 丢弃、0/空串保留（is not None 判定，非真值判定）。"""
        tool = getattr(module, tool_cls_name)()
        falsy_values = [0, "", 0.0, 0]
        raw = dict(zip(optional_keys, falsy_values, strict=True))
        raw["不存在的键"] = None
        kwargs = tool._build_kwargs(raw)
        assert set(kwargs) == set(optional_keys)
        assert kwargs[optional_keys[0]] == 0
        assert kwargs[optional_keys[1]] == ""

    def test_kwargs_drops_none_for_every_optional_key(
        self, module: Any, tool_cls_name: str, action: str, optional_keys: tuple[str, ...]
    ) -> None:
        """每个可选键单独置 None 都被丢弃（防单点漏判）。"""
        tool = getattr(module, tool_cls_name)()
        for key in optional_keys:
            assert tool._build_kwargs({key: None}) == {}
            assert tool._build_kwargs({key: "值"}) == {key: "值"}

    def test_empty_inputs_yield_empty_kwargs(
        self, module: Any, tool_cls_name: str, action: str, optional_keys: tuple[str, ...]
    ) -> None:
        assert getattr(module, tool_cls_name)()._build_kwargs({}) == {}

    def test_tool_definition_requires_prompt(
        self, module: Any, tool_cls_name: str, action: str, optional_keys: tuple[str, ...]
    ) -> None:
        definition = getattr(module, tool_cls_name).get_tool_definition()
        assert definition.name == action
        assert definition.input_schema["required"] == ["prompt"]


class TestMediaToolDefinitions:
    """image/tts 定义面（video/music 已在上方参数化断言）。"""

    def test_image_definition_enumerates_params(self) -> None:
        definition = image_mod.ImageGenerateTool.get_tool_definition()
        assert definition.name == "image_generate"
        props = definition.input_schema["properties"]
        assert definition.input_schema["required"] == ["prompt"]
        assert props["width"]["default"] == 512
        assert props["height"]["default"] == 512
        assert props["seed"]["default"] == -1

    def test_tts_definition_declares_format_enum_and_output_schema(self) -> None:
        definition = tts_mod.TtsGenerateTool.get_tool_definition()
        assert definition.name == "tts_generate"
        props = definition.input_schema["properties"]
        assert set(props["format"]["enum"]) == {"mp3", "wav", "ogg"}
        assert set(props["format"]["enum"]) == set(tts_mod.TtsGenerateTool.SUPPORTED_FORMATS)
        assert "file_path" in definition.output_schema["properties"]


# ═══════════════════════════════════════════════════════════
# server.py：能力句柄解析 / 懒缓存 / handler 输出形态
# ═══════════════════════════════════════════════════════════


class _FakeHandle:
    """能力句柄替身（内核注入边界）：call 拼 ``cap.method`` 前缀后转发。"""

    def __init__(self, cap_name: str, sink: list[tuple[str, dict[str, Any]]]) -> None:
        self._cap_name = cap_name
        self._sink = sink

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        self._sink.append((f"{self._cap_name}.{method}", params))
        return params.get("args", {})


class _FakePlugin:
    """插件替身：按注入面回答 get_capability（缺失能力抛 KeyError）。"""

    def __init__(self, capabilities: dict[str, Any]) -> None:
        self._capabilities = capabilities

    def get_capability(self, name: str) -> Any:
        if name not in self._capabilities:
            raise KeyError(name)
        return self._capabilities[name]


def _load_media_server() -> Any:
    mod_name = "media_server_gaps"
    sys.modules.pop(mod_name, None)
    real_plugin = sys.modules.pop("agentos_plugin_sdk", None)
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if real_plugin is not None:
            sys.modules["agentos_plugin_sdk"] = real_plugin
    return module


class TestServerCapabilityResolution:
    def test_tool_executor_preferred_over_service_registry(self) -> None:
        """tool-executor 优先；命中即返回（service-registry 不参与）。"""
        server = _load_media_server()
        sink: list[tuple[str, dict[str, Any]]] = []
        plugin = _FakePlugin(
            {
                "tool-executor": _FakeHandle("tool-executor", sink),
                "service-registry": _FakeHandle("service-registry", sink),
            }
        )
        caller = server._make_capability_caller(plugin)
        assert caller is not None
        _run(caller("invoke", {"args": {"k": 1}}))
        # 前缀不重复拼接（避免 tool-executor.tool-executor.invoke）
        assert sink[0][0] == "tool-executor.invoke"

    def test_service_registry_fallback_when_tool_executor_absent(self) -> None:
        """tool-executor 缺失 → 回落 service-registry（有区分度输入）。"""
        server = _load_media_server()
        sink: list[tuple[str, dict[str, Any]]] = []
        plugin = _FakePlugin({"service-registry": _FakeHandle("service-registry", sink)})
        caller = server._make_capability_caller(plugin)
        assert caller is not None
        _run(caller("invoke", {"args": {}}))
        assert sink[0][0] == "service-registry.invoke"

    def test_no_capability_returns_none(self) -> None:
        """两者皆缺 → None（工具侧将返回显式 PROVIDER_UNAVAILABLE）。"""
        server = _load_media_server()
        assert server._make_capability_caller(_FakePlugin({})) is None

    def test_lazy_cache_stores_only_success(self) -> None:
        """懒缓存：解析失败不入缓存（下次重试），成功后续用同一 caller。"""
        server = _load_media_server()
        sink: list[tuple[str, dict[str, Any]]] = []
        empty = _FakePlugin({})
        server._caller_cache.clear()
        server.plugin = empty  # type: ignore[assignment]
        assert server._get_capability_caller() is None
        assert "caller" not in server._caller_cache, "失败结果不得入缓存"

        server.plugin = _FakePlugin({"tool-executor": _FakeHandle("tool-executor", sink)})  # type: ignore[assignment]
        first = server._get_capability_caller()
        assert first is not None and "caller" in server._caller_cache
        assert server._get_capability_caller() is first

    def test_get_tool_constructs_on_demand_then_caches(self) -> None:
        """_get_tool：on_load 未跑时按需构造并缓存；二次取用同一实例。"""
        server = _load_media_server()
        server._caller_cache.clear()
        server._caller_cache["caller"] = None
        server._tool_instances.clear()
        tool = server._get_tool("tts_generate")
        assert tool is server._get_tool("tts_generate")

    @pytest.mark.parametrize(
        ("tool_name", "kwargs", "expect_ok"),
        [
            ("tts_generate", {"text": "你好"}, False),
            ("image_generate", {"prompt": "夕阳"}, False),
            ("music_generate", {"prompt": "旋律"}, False),
            ("video_generate", {"prompt": "短片"}, False),
        ],
    )
    def test_handler_returns_error_dict_when_service_unavailable(
        self, tool_name: str, kwargs: dict[str, Any], expect_ok: bool
    ) -> None:
        """四 handler：服务不可用时返回失败 ToolExecutionResult（error 可序列化）。"""
        server = _load_media_server()
        server._caller_cache.clear()
        server.plugin = _FakePlugin({})  # type: ignore[assignment]
        server._tool_instances.clear()
        handler = getattr(server, tool_name)
        raw = _run(handler(**kwargs))
        assert hasattr(raw, "to_dict")
        result = raw.to_dict()
        assert result["success"] is False
        assert result["error"]
        assert expect_ok is False

    def test_handler_returns_output_on_backend_success(self) -> None:
        """成功路径：handler 返回完整 ToolExecutionResult（保 metadata 多模态链）。"""
        server = _load_media_server()
        server._caller_cache.clear()
        server._tool_instances.clear()

        # 工具实例替身（后端 media.generate 边界）：仅暴露 execute 成功结果形态
        class _StubTool:
            def __init__(self, **_: Any) -> None:
                pass

            async def execute(self, inputs: dict[str, Any]) -> Any:
                assert inputs["text"] == "你好"
                return create_success_result(
                    data={"file_path": "/tmp/voice.mp3", "media_type": "tts"},
                    metadata={"action": "tts_generate", "media_type": "audio"},
                )

        server._TOOL_CLASSES["tts_generate"] = _StubTool
        try:
            result = _run(server.tts_generate(text="你好"))
        finally:
            server._TOOL_CLASSES["tts_generate"] = tts_mod.TtsGenerateTool
        assert hasattr(result, "to_dict")
        serialized = result.to_dict(slim=True)
        assert serialized["output"] == {"file_path": "/tmp/voice.mp3", "media_type": "tts"}
        # slim 口径剔除 action（防上下文噪声）；完整口径 metadata 保留
        assert result.to_dict()["metadata"]["action"] == "tts_generate"

    def test_on_load_builds_all_four_tool_instances(self) -> None:
        """on_load：四工具实例一次构造齐（能力未注入时 caller 为 None 也照常构造）。"""
        server = _load_media_server()
        server._caller_cache.clear()
        server.plugin = _FakePlugin({})  # type: ignore[assignment]
        server._tool_instances.clear()
        _run(server._on_load({}))
        assert set(server._tool_instances) == {
            "image_generate",
            "music_generate",
            "video_generate",
            "tts_generate",
        }
        assert all(isinstance(server._tool_instances[name], cls) for name, cls in server._TOOL_CLASSES.items())


# ═══════════════════════════════════════════════════════════
# server.py handler：返回完整 ToolExecutionResult（多模态 metadata 链）
# ═══════════════════════════════════════════════════════════


class TestServerHandlerPreservesMetadata:
    """handler 勿取 .output——metadata.multimodal_content 是图片回传模型
    的唯一通道（inject_multimodal 消费），丢 metadata 即断链（20260918 修复）。"""

    @staticmethod
    def _fake_tool(result: Any) -> Any:
        class _FakeTool:
            async def execute(self, kwargs: dict[str, Any]) -> Any:
                return result

        return _FakeTool()

    def test_success_handler_keeps_multimodal_metadata(self, monkeypatch) -> None:
        from agentos_plugin_sdk import create_success_result

        server = _load_media_server()
        result = create_success_result(
            data={"file_path": "/ws/gen.png", "media_type": "image", "provider": "p"},
            metadata={"action": "image_generate", "multimodal_content": [{"type": "image_url"}]},
        )
        monkeypatch.setattr(server, "_get_tool", lambda _name: self._fake_tool(result))  # type: ignore[arg-type]
        raw = _run(server.plugin._tools["image_generate"].handler(prompt="cat"))

        assert hasattr(raw, "to_dict")
        full = raw.to_dict()
        assert full["success"] is True
        assert full["output"]["file_path"] == "/ws/gen.png"
        blocks = full["metadata"]["multimodal_content"]
        assert blocks[0]["type"] == "image_url"
        # 性质断言：slim 口径（LLM 文本上下文）必须剔除 multimodal——防 base64 污染
        slim = raw.to_dict(slim=True)
        assert "metadata" not in slim

    def test_failure_handler_shape_has_error_fields(self, monkeypatch) -> None:
        from agentos_plugin_sdk import create_failure_result

        server = _load_media_server()
        result = create_failure_result(error="provider down", error_code="PROVIDER_UNAVAILABLE")
        monkeypatch.setattr(server, "_get_tool", lambda _name: self._fake_tool(result))  # type: ignore[arg-type]
        raw = _run(server.plugin._tools["music_generate"].handler(prompt="piano"))

        serialized = raw.to_dict()
        assert serialized["success"] is False
        assert serialized["error_code"] == "PROVIDER_UNAVAILABLE"
