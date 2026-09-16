# @feature: FP-MIGR 0.1→0.2迁移 | @vision: V3 可嵌入 | @ci: python-coverage
"""media（image/tts/video/music）工具 0.2 迁移 TDD 测试。

迁移（FP-MIGR）：
1. 四个模块可加载——0.1 的 tools.builtin.base / tools.types / tools.media.*
   已删除，类型与结果工厂改用 agentos_plugin_sdk + 就地 _media_core。
2. F-MEDIA-2（provider 依赖迁移）：0.1 的 infrastructure.service_provider
   （全局服务注册表）已删，media 的 provider 调用改为**经 tool-executor
   capability 调用后端服务**（与 hindsight_memory/memory_backend.py 同款
   模式）——服务契约：tool-executor.invoke → media.generate，args 含
   media_type / prompt|text / provider。调用方未注入或服务不可达时返回
   **显式错误**（error_code=PROVIDER_UNAVAILABLE），**不降级空转**
   （旧的「Provider 未配置」提示与 video/music 的 not_configured 成功态已废除）。
3. server.py 入口注册：media/server.py 的 tts_generate 引用 TtsGenerateTool
   （0.1 残留的 TTSTool 名称已修正），并把内核注入的 tool-executor 能力
   调用方传入工具构造。

2026-08-25 兼容层清理：MediaProviderRegistry 注入路径 / ProviderChain /
_enrich_*_schema / 工厂函数已随 0.1 ProviderChain 兼容残留一并删除，相关
FakeRegistry 测试同步移除；主路径（capability 调用）测试保留。

装配：conftest.py 注入 sdk / media 目录到 sys.path；模块经 importlib 以唯一名加载。
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

_MEDIA_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "tools" / "media"


def _load_module(name: str, filename: str) -> Any:
    """加载 media 插件内的模块（唯一模块名，进程内缓存）。"""
    mod_name = f"{name}_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    module_path = _MEDIA_DIR / filename
    assert module_path.exists(), f"{filename} missing at {module_path}"
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None, f"cannot load {filename}"
    assert spec.loader is not None, f"cannot load {filename}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def image_mod() -> Any:
    return _load_module("media_image", "image_generate.py")


@pytest.fixture(scope="module")
def tts_mod() -> Any:
    return _load_module("media_tts", "tts_generate.py")


@pytest.fixture(scope="module")
def video_mod() -> Any:
    return _load_module("media_video", "video_generate.py")


@pytest.fixture(scope="module")
def music_mod() -> Any:
    return _load_module("media_music", "music_generate.py")


@pytest.fixture(scope="module")
def core_mod() -> Any:
    """_media_core（类型面 + MediaProviderClient + ProviderUnavailable）。"""
    return _load_module("media_core", "_media_core.py")


@pytest.fixture
def caller() -> AsyncMock:
    """注入的 capability_caller 替身（async fn `(method, params) -> Any`）。"""
    return AsyncMock()


# ── 迁移验证：可加载 + 共享类型面 ─────────────────────────


class TestMediaMigration:
    """迁移成功：模块可 import、共享类型来自 _media_core。"""

    def test_all_modules_import_ok(self, image_mod, tts_mod, video_mod, music_mod):
        assert image_mod.ImageGenerateTool is not None
        assert tts_mod.TtsGenerateTool is not None
        assert video_mod.VideoGenerateTool is not None
        assert music_mod.MusicGenerateTool is not None

    @pytest.mark.parametrize("attr", ["MediaType", "FallbackStrategy"])
    def test_media_core_defines_shared_types(self, core_mod, attr):
        assert getattr(core_mod, attr) is not None

    def test_media_core_registry_path_removed(self, image_mod):
        """0.1 ProviderChain 兼容残留已删：模块不再引用 registry/chain 注入面。"""
        assert not hasattr(image_mod, "ProviderChain")
        assert not hasattr(image_mod, "MediaProviderRegistry")

    def test_definitions_are_sdk_tools(self, image_mod, tts_mod, video_mod, music_mod):
        from agentos_plugin_sdk import Tool as SdkTool

        assert isinstance(image_mod.ImageGenerateTool.get_tool_definition(), SdkTool)
        assert isinstance(tts_mod.TtsGenerateTool.get_tool_definition(), SdkTool)
        assert isinstance(video_mod.VideoGenerateTool.get_tool_definition(), SdkTool)
        assert isinstance(music_mod.MusicGenerateTool.get_tool_definition(), SdkTool)

    def test_server_entry_tts_class_name_fixed(self):
        """media/server.py 注册入口引用 TtsGenerateTool（0.1 残留 TTSTool 已修）。"""
        server_src = (_MEDIA_DIR / "server.py").read_text(encoding="utf-8")
        assert "TTSTool" not in server_src
        assert "TtsGenerateTool" in server_src

    def test_server_entry_wires_capability_caller(self):
        """media/server.py 把内核注入的 tool-executor 调用方传入工具（F-MEDIA-2）。"""
        server_src = (_MEDIA_DIR / "server.py").read_text(encoding="utf-8")
        assert "capability_caller" in server_src
        assert "_make_capability_caller" in server_src
        assert "tool-executor" in server_src


# ── F-MEDIA-2：provider 依赖经 capability 调用（不降级空转）──


class TestProviderUnavailable:
    """无 capability 调用方时返回显式「服务不可用」错误，不静默空转。

    意图（产品决定）：0.1 service_provider 已删，0.2 等价物是经 capability
    调用后端服务；调用方未注入/服务不可达时，调用方必须明确知道服务不可用
    （error_code=PROVIDER_UNAVAILABLE），而不是旧的「Provider 未配置」提示
    或 video/music 的 not_configured 成功态（那会让上层误以为生成已排队）。
    """

    @pytest.mark.asyncio
    async def test_image_returns_provider_unavailable(self, image_mod):
        result = await image_mod.ImageGenerateTool().execute({"prompt": "a cat"})
        assert result.success is False
        assert result.error_code == "PROVIDER_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_tts_returns_provider_unavailable(self, tts_mod):
        result = await tts_mod.TtsGenerateTool().execute({"text": "hello"})
        assert result.success is False
        assert result.error_code == "PROVIDER_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_video_returns_provider_unavailable(self, video_mod):
        result = await video_mod.VideoGenerateTool().execute({"prompt": "a dog"})
        assert result.success is False
        assert result.error_code == "PROVIDER_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_music_returns_provider_unavailable(self, music_mod):
        result = await music_mod.MusicGenerateTool().execute({"prompt": "lofi"})
        assert result.success is False
        assert result.error_code == "PROVIDER_UNAVAILABLE"


class TestCapabilityInvocation:
    """工具经 tool-executor capability 调用后端服务（F-MEDIA-2 主路径）。

    意图：provider 依赖已从「本地注册表直调」迁移为「经 capability 调用
    后端服务」——断言 invoke 的服务名/参数形态正确，且后端不可达时返回
    显式错误而非静默空转。
    """

    @pytest.mark.asyncio
    async def test_image_invokes_media_generate_via_capability(self, image_mod, caller):
        caller.return_value = {
            "file_path": "/output/images/x.png",
            "media_type": "image",
            "provider_name": "media",
        }
        tool = image_mod.ImageGenerateTool(capability_caller=caller)
        result = await tool.execute({"prompt": "a cat", "width": 512})
        assert result.success is True
        assert result.output["file_path"] == "/output/images/x.png"
        assert result.output["provider"] == "media"
        caller.assert_awaited_once()
        method, params = caller.call_args.args
        assert method == "tool-executor.invoke"
        assert params["tool_name"] == "media.generate"
        assert params["args"]["media_type"] == "image"
        assert params["args"]["prompt"] == "a cat"
        assert params["args"]["width"] == 512

    @pytest.mark.asyncio
    async def test_tts_invokes_media_generate_via_capability(self, tts_mod, caller):
        caller.return_value = {
            "file_path": "/output/tts/h.mp3",
            "media_type": "tts",
            "provider_name": "media",
            "duration_seconds": 1.5,
        }
        tool = tts_mod.TtsGenerateTool(capability_caller=caller)
        result = await tool.execute({"text": "hello", "voice": "echo", "format": "mp3"})
        assert result.success is True
        assert result.output["file_path"] == "/output/tts/h.mp3"
        caller.assert_awaited_once()
        method, params = caller.call_args.args
        assert method == "tool-executor.invoke"
        assert params["tool_name"] == "media.generate"
        assert params["args"]["media_type"] == "tts"
        assert params["args"]["text"] == "hello"
        assert params["args"]["voice"] == "echo"
        assert params["args"]["format"] == "mp3"

    @pytest.mark.asyncio
    async def test_video_invokes_media_generate_via_capability(self, video_mod, caller):
        caller.return_value = {
            "file_path": "/output/video/v.mp4",
            "media_type": "video",
            "provider_name": "media",
        }
        tool = video_mod.VideoGenerateTool(capability_caller=caller)
        result = await tool.execute({"prompt": "a dog", "duration": 5})
        assert result.success is True
        assert result.output["file_path"] == "/output/video/v.mp4"
        caller.assert_awaited_once()
        method, params = caller.call_args.args
        assert method == "tool-executor.invoke"
        assert params["tool_name"] == "media.generate"
        assert params["args"]["media_type"] == "video"
        assert params["args"]["prompt"] == "a dog"
        assert params["args"]["duration"] == 5

    @pytest.mark.asyncio
    async def test_music_invokes_media_generate_via_capability(self, music_mod, caller):
        caller.return_value = {
            "file_path": "/output/music/m.mp3",
            "media_type": "music",
            "provider_name": "media",
        }
        tool = music_mod.MusicGenerateTool(capability_caller=caller)
        result = await tool.execute({"prompt": "lofi beat", "genre": "jazz"})
        assert result.success is True
        assert result.output["file_path"] == "/output/music/m.mp3"
        caller.assert_awaited_once()
        method, params = caller.call_args.args
        assert method == "tool-executor.invoke"
        assert params["tool_name"] == "media.generate"
        assert params["args"]["media_type"] == "music"
        assert params["args"]["prompt"] == "lofi beat"
        assert params["args"]["genre"] == "jazz"

    @pytest.mark.asyncio
    async def test_image_surfaces_provider_unavailable_on_backend_error(self, image_mod):
        """后端服务不可达（invoke 抛错）时返回显式 PROVIDER_UNAVAILABLE，不静默空转。"""
        caller = AsyncMock(side_effect=RuntimeError("tool not found: media.generate"))
        tool = image_mod.ImageGenerateTool(capability_caller=caller)
        result = await tool.execute({"prompt": "a cat"})
        assert result.success is False
        assert result.error_code == "PROVIDER_UNAVAILABLE"
        assert "media.generate" in result.error


class TestMediaProviderClient:
    """MediaProviderClient：经 tool-executor.invoke 调 media.generate 服务契约。

    意图：与 memory_backend.HindsightBackend 同款模式——唯一外部依赖是注入的
    capability_caller；区别在于调用失败/服务不可达时抛 ProviderUnavailable
    （显式错误），绝不静默返回空结果（产品决定：迁移依赖而非降级空转）。
    """

    def test_client_requires_caller(self, core_mod):
        """capability_caller=None 时抛 ValueError（必须注入，便于测试与解耦）。"""
        with pytest.raises(ValueError):
            core_mod.MediaProviderClient(None)

    @pytest.mark.asyncio
    async def test_client_invokes_media_generate(self, core_mod, caller):
        """execute_generate 调 tool-executor.invoke，tool_name=media.generate。"""
        caller.return_value = {
            "file_path": "/out/x.png",
            "media_type": "image",
            "provider_name": "comfyui",
            "metadata": {"seed": 1},
        }
        client = core_mod.MediaProviderClient(caller)
        result = await client.execute_generate(
            core_mod.MediaType.IMAGE, "a cat", provider="comfyui", width=512
        )
        caller.assert_awaited_once()
        method, params = caller.call_args.args
        assert method == "tool-executor.invoke"
        assert params["tool_name"] == "media.generate"
        args = params["args"]
        assert args["media_type"] == "image"
        assert args["prompt"] == "a cat"
        assert args["provider"] == "comfyui"
        assert args["width"] == 512
        # 结果映射为统一 MediaResult 形态
        assert result.file_path == "/out/x.png"
        assert result.media_type == core_mod.MediaType.IMAGE
        assert result.provider_name == "comfyui"
        assert result.metadata == {"seed": 1}

    @pytest.mark.asyncio
    async def test_client_invokes_synthesize_for_tts(self, core_mod, caller):
        """execute_synthesize 以 text 为主内容调 media.generate（tts 契约）。"""
        caller.return_value = {
            "file_path": "/out/h.mp3",
            "media_type": "tts",
            "provider_name": "media",
            "duration_seconds": 1.5,
        }
        client = core_mod.MediaProviderClient(caller)
        result = await client.execute_synthesize(
            core_mod.MediaType.TTS, "hello", voice="echo", format="mp3"
        )
        caller.assert_awaited_once()
        method, params = caller.call_args.args
        assert method == "tool-executor.invoke"
        assert params["tool_name"] == "media.generate"
        assert params["args"]["media_type"] == "tts"
        assert params["args"]["text"] == "hello"
        assert params["args"]["voice"] == "echo"
        assert params["args"]["format"] == "mp3"
        assert result.duration_seconds == 1.5

    @pytest.mark.asyncio
    async def test_client_raises_provider_unavailable_on_call_error(self, core_mod, caller):
        """invoke 抛异常（服务不可达）→ 抛 ProviderUnavailable（显式，非静默）。"""
        caller.side_effect = RuntimeError("tool not found")
        client = core_mod.MediaProviderClient(caller)
        with pytest.raises(core_mod.ProviderUnavailable):
            await client.execute_generate(core_mod.MediaType.IMAGE, "a cat")

    @pytest.mark.asyncio
    async def test_client_raises_provider_unavailable_on_failed_result(self, core_mod, caller):
        """内核返回 {success: false, error}（服务未注册）→ 抛 ProviderUnavailable。"""
        caller.return_value = {
            "success": False,
            "error": "tool execution failed: media.generate not registered",
        }
        client = core_mod.MediaProviderClient(caller)
        with pytest.raises(core_mod.ProviderUnavailable) as exc_info:
            await client.execute_generate(core_mod.MediaType.IMAGE, "a cat")
        assert "media.generate" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_client_raises_provider_unavailable_on_missing_file_path(self, core_mod, caller):
        """结果缺少 file_path（契约不满足）→ 抛 ProviderUnavailable，不伪造成功。"""
        caller.return_value = {"ok": True}
        client = core_mod.MediaProviderClient(caller)
        with pytest.raises(core_mod.ProviderUnavailable):
            await client.execute_generate(core_mod.MediaType.IMAGE, "a cat")


    @pytest.mark.asyncio
    async def test_client_reraises_provider_unavailable_untouched(self, core_mod, caller):
        """调用方已抛 ProviderUnavailable → 原样上抛（不被包成「服务不可达」）。"""
        caller.side_effect = core_mod.ProviderUnavailable("服务未配置")
        client = core_mod.MediaProviderClient(caller)
        with pytest.raises(core_mod.ProviderUnavailable) as exc_info:
            await client.execute_generate(core_mod.MediaType.IMAGE, "a cat")
        # 原异常不被二次包装（__cause__ 为空 = 没走 except Exception 分支）
        assert str(exc_info.value) == "服务未配置"
        assert exc_info.value.__cause__ is None

    @pytest.mark.asyncio
    async def test_client_raises_provider_unavailable_on_non_dict_result(self, core_mod, caller):
        """返回值不是 dict（契约形态异常）→ 抛 ProviderUnavailable，不猜字段。"""
        caller.return_value = ["file_path", "/out/x.png"]
        client = core_mod.MediaProviderClient(caller)
        with pytest.raises(core_mod.ProviderUnavailable) as exc_info:
            await client.execute_generate(core_mod.MediaType.IMAGE, "a cat")
        assert "异常形态" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_client_raises_provider_unavailable_on_unknown_media_type(self, core_mod, caller):
        """后端回的 media_type 不在枚举内 → 抛 ProviderUnavailable（不静默按入参兜底）。"""
        caller.return_value = {
            "file_path": "/out/x.bin",
            "media_type": "hologram",
            "provider_name": "media",
        }
        client = core_mod.MediaProviderClient(caller)
        with pytest.raises(core_mod.ProviderUnavailable) as exc_info:
            await client.execute_generate(core_mod.MediaType.IMAGE, "a cat")
        assert "hologram" in str(exc_info.value)
        # 未知类型被 from None 掐断链，避免把 ValueError 噪音透给上游
        assert exc_info.value.__cause__ is None

    @pytest.mark.parametrize(
        ("given", "expected_value"),
        [("music", "music"), ("video", "video")],
    )
    @pytest.mark.asyncio
    async def test_client_normalizes_media_type_input(
        self, core_mod, caller, given: Any, expected_value: str
    ):
        """入参 media_type 字符串形态归一为 MediaType（结果与请求参数同源同值）。"""
        caller.return_value = {"file_path": "/out/x", "provider_name": "media"}
        client = core_mod.MediaProviderClient(caller)
        result = await client.execute_generate(given, "a cat")
        assert result.media_type == core_mod.MediaType(expected_value)
        assert caller.call_args.args[1]["args"]["media_type"] == expected_value

    def test_default_registry_returns_empty_face(self, core_mod):
        """MediaProviderRegistry 空默认行为（0.2 类型面）：get/chain 恒 None、list 恒空。"""
        registry = core_mod.MediaProviderRegistry()
        assert registry.get("comfyui") is None
        assert registry.get_chain_for_type(core_mod.MediaType.IMAGE) is None
        assert registry.get_chain_for_type(core_mod.MediaType.TTS, core_mod.FallbackStrategy.SEQUENTIAL) is None
        assert registry.list_by_type(core_mod.MediaType.VIDEO) == []

    @pytest.mark.asyncio
    async def test_result_media_type_falls_back_to_enum_request(self, core_mod, caller):
        """后端不回 media_type 键时，MediaType 枚举入参原样带进结果（不做字符串往返）。"""
        caller.return_value = {"file_path": "/out/x", "provider_name": "media"}
        client = core_mod.MediaProviderClient(caller)
        result = await client.execute_generate(core_mod.MediaType.MUSIC, "a tune")
        assert result.media_type is core_mod.MediaType.MUSIC
        # 发往接口的仍是契约字符串（枚举不外泄到 capability 参数）
        assert caller.call_args.args[1]["args"]["media_type"] == "music"

    @pytest.mark.asyncio
    async def test_result_media_type_falls_back_to_str_request(self, core_mod, caller):
        """后端不回 media_type 键且入参是字符串 → 归一为对应 MediaType。"""
        caller.return_value = {"file_path": "/out/x", "provider_name": "media"}
        client = core_mod.MediaProviderClient(caller)
        result = await client.execute_generate("video", "a clip")
        assert result.media_type is core_mod.MediaType.VIDEO


class TestInputValidationGaps:
    """四工具入口校验分支：空内容 / 不支持格式 / 越界语速的显式失败契约。

    意图：校验失败必须返回结构化 error_code（LLM 与前端按码路由）；
    传入 caller 的用例同时钉「校验失败不触达后端」——不浪费一次能力调用。
    """

    @pytest.mark.asyncio
    async def test_image_empty_prompt_rejected(self, image_mod, caller):
        tool = image_mod.ImageGenerateTool(capability_caller=caller)
        result = await tool.execute({"prompt": "   "})
        assert result.success is False
        assert result.error_code == "MISSING_PROMPT"
        assert caller.await_count == 0

    @pytest.mark.asyncio
    async def test_image_missing_prompt_key_rejected(self, image_mod):
        """缺省 prompt 键同样被拒（不是仅空白串特例）。"""
        result = await image_mod.ImageGenerateTool().execute({})
        assert result.success is False
        assert result.error_code == "MISSING_PROMPT"

    @pytest.mark.asyncio
    async def test_music_empty_prompt_rejected(self, music_mod, caller):
        tool = music_mod.MusicGenerateTool(capability_caller=caller)
        result = await tool.execute({"prompt": ""})
        assert result.success is False
        assert result.error_code == "MISSING_PROMPT"
        assert caller.await_count == 0

    @pytest.mark.asyncio
    async def test_video_empty_prompt_rejected(self, video_mod, caller):
        tool = video_mod.VideoGenerateTool(capability_caller=caller)
        result = await tool.execute({"prompt": " "})
        assert result.success is False
        assert result.error_code == "MISSING_PROMPT"
        assert caller.await_count == 0

    @pytest.mark.asyncio
    async def test_tts_empty_text_rejected(self, tts_mod, caller):
        tool = tts_mod.TtsGenerateTool(capability_caller=caller)
        result = await tool.execute({"text": "  "})
        assert result.success is False
        assert result.error_code == "EMPTY_TEXT"
        assert caller.await_count == 0

    @pytest.mark.asyncio
    async def test_tts_unsupported_format_rejected(self, tts_mod, caller):
        """不在 SUPPORTED_FORMATS 内的 format → UNSUPPORTED_FORMAT，且不触达后端。"""
        tool = tts_mod.TtsGenerateTool(capability_caller=caller)
        result = await tool.execute({"text": "hello", "format": "flac"})
        assert result.success is False
        assert result.error_code == "UNSUPPORTED_FORMAT"
        assert "flac" in result.error
        assert caller.await_count == 0

    @pytest.mark.parametrize("audio_format", ["mp3", "wav", "ogg"])
    @pytest.mark.asyncio
    async def test_tts_supported_formats_accepted(self, tts_mod, caller, audio_format: str):
        """声明支持的三种格式都放行到后端并透传（与 SUPPORTED_FORMATS 同源）。"""
        caller.return_value = {
            "file_path": f"/out/h.{audio_format}",
            "media_type": "tts",
            "provider_name": "media",
        }
        tool = tts_mod.TtsGenerateTool(capability_caller=caller)
        result = await tool.execute({"text": "hello", "format": audio_format})
        assert result.success is True
        assert result.output["format"] == audio_format

    @pytest.mark.parametrize("speed", [0.4, 2.5])
    @pytest.mark.asyncio
    async def test_tts_speed_out_of_range_rejected(self, tts_mod, caller, speed: float):
        """语速越界（下界外与上界外两侧）→ INVALID_SPEED，不触达后端。"""
        tool = tts_mod.TtsGenerateTool(capability_caller=caller)
        result = await tool.execute({"text": "hello", "speed": speed})
        assert result.success is False
        assert result.error_code == "INVALID_SPEED"
        assert caller.await_count == 0

    @pytest.mark.parametrize("speed", [0.5, 2.0])
    @pytest.mark.asyncio
    async def test_tts_speed_boundaries_accepted(self, tts_mod, caller, speed: float):
        """边界值 0.5 / 2.0 属闭区间内 → 放行到后端并透传原值。"""
        caller.return_value = {"file_path": "/out/h.mp3", "media_type": "tts", "provider_name": "media"}
        tool = tts_mod.TtsGenerateTool(capability_caller=caller)
        result = await tool.execute({"text": "hello", "speed": speed})
        assert result.success is True
        assert caller.call_args.args[1]["args"]["speed"] == speed


class TestBackendErrorSurface:
    """后端可用性错误在各工具上的回显契约（error_code + 后端原文透传）。"""

    @pytest.mark.asyncio
    async def test_tts_surfaces_provider_unavailable(self, tts_mod):
        backend = AsyncMock(side_effect=RuntimeError("media.generate 未注册"))
        tool = tts_mod.TtsGenerateTool(capability_caller=backend)
        result = await tool.execute({"text": "hello"})
        assert result.success is False
        assert result.error_code == "PROVIDER_UNAVAILABLE"
        assert "media.generate" in result.error

    @pytest.mark.asyncio
    async def test_video_surfaces_provider_unavailable(self, video_mod):
        backend = AsyncMock(side_effect=RuntimeError("media.generate 未注册"))
        tool = video_mod.VideoGenerateTool(capability_caller=backend)
        result = await tool.execute({"prompt": "a dog"})
        assert result.success is False
        assert result.error_code == "PROVIDER_UNAVAILABLE"
        assert "media.generate" in result.error

    @pytest.mark.asyncio
    async def test_music_surfaces_provider_unavailable(self, music_mod):
        backend = AsyncMock(side_effect=RuntimeError("media.generate 未注册"))
        tool = music_mod.MusicGenerateTool(capability_caller=backend)
        result = await tool.execute({"prompt": "lofi"})
        assert result.success is False
        assert result.error_code == "PROVIDER_UNAVAILABLE"
        assert "media.generate" in result.error

    @pytest.mark.asyncio
    async def test_music_success_maps_backend_payload(self, music_mod, caller):
        """music 成功面：file_path/media_type/duration/provider_name/metadata 全量投影。"""
        caller.return_value = {
            "file_path": "/out/m.wav",
            "media_type": "music",
            "provider_name": "suno",
            "duration_seconds": 30.0,
            "metadata": {"bpm": 120},
        }
        tool = music_mod.MusicGenerateTool(capability_caller=caller)
        result = await tool.execute({"prompt": "lofi"})
        assert result.success is True
        assert result.output["file_path"] == "/out/m.wav"
        assert result.output["duration_seconds"] == 30.0
        assert result.output["provider_name"] == "suno"
        assert result.output["metadata"] == {"bpm": 120}


class TestImageResultAssembly:
    """image_generate 结果组装面：可选参数过滤、cfg_scale 浮点、多模态内容块。

    断言全部落在「送到后端的 args」与「返回给管道的 output/metadata」两个
    可观察出口上，不读私有属性。
    """

    @pytest.mark.parametrize(
        ("inputs", "expected_keys"),
        [
            # 有效字符串参数入选
            (
                {"prompt": "p", "negative_prompt": "bad", "style": "anime", "workflow_template": "w"},
                {"negative_prompt", "style", "workflow_template"},
            ),
            # 空串 / 非字符串被过滤（不把无效值传给后端）
            ({"prompt": "p", "negative_prompt": "", "style": 123, "workflow_template": None}, set()),
            # 数值参数转 int；cfg_scale 转 float
            (
                {"prompt": "p", "width": 768, "height": 512, "seed": 42, "steps": 20, "cfg_scale": 7.5},
                {"width", "height", "seed", "steps", "cfg_scale"},
            ),
            # 非法数值类型被过滤
            ({"prompt": "p", "width": "768", "seed": None, "cfg_scale": "1.0"}, set()),
        ],
    )
    @pytest.mark.asyncio
    async def test_optional_params_filtered_before_backend(self, image_mod, caller, inputs, expected_keys):
        """可选参数按类型过滤后到达后端 args；数值参数以数值类型透传。"""
        caller.return_value = {"file_path": "/out/x.png", "media_type": "image", "provider_name": "media"}
        tool = image_mod.ImageGenerateTool(capability_caller=caller)
        await tool.execute(inputs)

        args = caller.call_args.args[1]["args"]
        assert set(args) - {"media_type", "prompt"} == expected_keys
        if "width" in expected_keys:
            assert isinstance(args["width"], int)
            assert isinstance(args["cfg_scale"], float)

    @pytest.mark.asyncio
    async def test_cfg_scale_float_passthrough_to_backend(self, image_mod, caller):
        """cfg_scale 浮点值原样到达后端 args（不被截成 int）。"""
        caller.return_value = {"file_path": "/out/x.png", "media_type": "image", "provider_name": "media"}
        tool = image_mod.ImageGenerateTool(capability_caller=caller)
        await tool.execute({"prompt": "p", "cfg_scale": 7.5})
        assert caller.call_args.args[1]["args"]["cfg_scale"] == 7.5

    @pytest.mark.asyncio
    async def test_backend_metadata_projected_into_output(self, image_mod, caller):
        """后端 metadata 非空 → 投影进 output（前端/管道可读 seed 等生成信息）。"""
        caller.return_value = {
            "file_path": "/out/x.png",
            "media_type": "image",
            "provider_name": "comfyui",
            "metadata": {"seed": 42},
        }
        tool = image_mod.ImageGenerateTool(capability_caller=caller)
        result = await tool.execute({"prompt": "p"})
        assert result.success is True
        assert result.output["metadata"] == {"seed": 42}

    @pytest.mark.asyncio
    async def test_backend_empty_metadata_absent_from_output(self, image_mod, caller):
        """后端无 metadata → output 不带该键（不写空字典占位）。"""
        caller.return_value = {"file_path": "/out/x.png", "media_type": "image", "provider_name": "media"}
        tool = image_mod.ImageGenerateTool(capability_caller=caller)
        result = await tool.execute({"prompt": "p"})
        assert "metadata" not in result.output

    @pytest.mark.parametrize(
        ("suffix", "expected_mime"),
        [
            (".png", "image/png"),
            (".jpg", "image/jpeg"),
            (".JPEG", "image/jpeg"),
            (".webp", "image/webp"),
            (".gif", "image/gif"),
        ],
    )
    @pytest.mark.asyncio
    async def test_multimodal_block_mime_mapping(
        self, image_mod, caller, tmp_path, suffix: str, expected_mime: str
    ):
        """真实文件 → 成功结果 metadata 的 vision 数据块按扩展名给 MIME。"""
        raw = b"\x89PNG\r\n\x1a\n-fake-bytes"
        img = tmp_path / f"gen{suffix}"
        img.write_bytes(raw)
        caller.return_value = {
            "file_path": str(img),
            "media_type": "image",
            "provider_name": "media",
        }
        tool = image_mod.ImageGenerateTool(capability_caller=caller)
        result = await tool.execute({"prompt": "p"})

        blocks = result.metadata["multimodal_content"]
        assert blocks == [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{expected_mime};base64," + base64.b64encode(raw).decode("utf-8")
                },
            }
        ]

    @pytest.mark.asyncio
    async def test_unknown_extension_defaults_to_png(self, image_mod, caller, tmp_path):
        """未知扩展名（.bmp 不在 mime_map）→ 缺省 image/png，不误标其它类型。"""
        img = tmp_path / "pic.bmp"
        img.write_bytes(b"bytes")
        caller.return_value = {
            "file_path": str(img),
            "media_type": "image",
            "provider_name": "media",
        }
        tool = image_mod.ImageGenerateTool(capability_caller=caller)
        result = await tool.execute({"prompt": "p"})
        url = result.metadata["multimodal_content"][0]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")

    @pytest.mark.asyncio
    async def test_multimodal_content_omitted_for_empty_path(self, image_mod, caller):
        """后端返回空 file_path → 空路径不构块（省略元数据键，不抛异常）。"""
        caller.return_value = {"file_path": "", "media_type": "image", "provider_name": "media"}
        tool = image_mod.ImageGenerateTool(capability_caller=caller)
        # MediaProviderClient 对空 file_path 已判契约不满足，此处断言显式失败而非静默成功
        result = await tool.execute({"prompt": "p"})
        assert result.success is False
        assert result.error_code == "PROVIDER_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_multimodal_content_omitted_when_file_unreadable(self, image_mod, caller, tmp_path):
        """文件在盘但读取抛 OSError → 成功结果不带 multimodal_content，不向上抛。"""
        img = tmp_path / "locked.png"
        img.write_bytes(b"bytes")

        real_open = open

        def _boom(path, *args, **kwargs):
            if str(path) == str(img):
                raise OSError("device busy")
            return real_open(path, *args, **kwargs)

        caller.return_value = {
            "file_path": str(img),
            "media_type": "image",
            "provider_name": "media",
        }
        tool = image_mod.ImageGenerateTool(capability_caller=caller)

        import builtins

        original = builtins.open
        builtins.open = _boom
        try:
            result = await tool.execute({"prompt": "p"})
        finally:
            builtins.open = original

        assert result.success is True
        assert "multimodal_content" not in result.metadata

    @pytest.mark.asyncio
    async def test_multimodal_content_omitted_when_file_missing(self, image_mod, caller, tmp_path):
        """生成成功但文件不在盘上（远端产物）→ 不带 multimodal_content 键，不报错。"""
        caller.return_value = {
            "file_path": str(tmp_path / "ghost.png"),
            "media_type": "image",
            "provider_name": "media",
        }
        tool = image_mod.ImageGenerateTool(capability_caller=caller)
        result = await tool.execute({"prompt": "p"})
        assert result.success is True
        assert "multimodal_content" not in result.metadata
