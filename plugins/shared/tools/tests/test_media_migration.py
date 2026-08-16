# @feature: FP-MIGR 0.1→0.2迁移 | @vision: V3 可嵌入 | @ci: python-coverage
"""media（image/tts/video/music）工具 0.2 迁移 TDD 测试。

迁移（FP-MIGR）：
1. 四个模块可加载——0.1 的 tools.builtin.base / tools.types / tools.media.*
   已删除，类型与结果工厂改用 agentos_plugin_sdk + 就地 _media_core。
2. ProviderChain 已从正确来源（media/_media_core.py）真实 import，
   `# noqa: F821` 消音已删除——用户显式指定 provider 时不再 NameError。
3. F-MEDIA-2（provider 依赖迁移）：0.1 的 infrastructure.service_provider
   （全局服务注册表）已删，media 的 provider 调用改为**经 tool-executor
   capability 调用后端服务**（与 hindsight_memory/memory_backend.py 同款
   模式）——服务契约：tool-executor.invoke → media.generate，args 含
   media_type / prompt|text / provider。调用方未注入或服务不可达时返回
   **显式错误**（error_code=PROVIDER_UNAVAILABLE），**不降级空转**
   （旧的「Provider 未配置」提示与 video/music 的 not_configured 成功态已废除）。
4. server.py 入口注册：media/server.py 的 tts_generate 引用 TtsGenerateTool
   （0.1 残留的 TTSTool 名称已修正），并把内核注入的 tool-executor 能力
   调用方传入工具构造。

装配：conftest.py 注入 sdk / media 目录到 sys.path；模块经 importlib 以唯一名加载。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

_MEDIA_DIR = Path(__file__).resolve().parent.parent / "media"


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


# ── 迁移验证：可加载 + ProviderChain 真实定义 ───────────────


class TestMediaMigration:
    """迁移成功：模块可 import、ProviderChain 来自 _media_core。"""

    def test_all_modules_import_ok(self, image_mod, tts_mod, video_mod, music_mod):
        assert image_mod.ImageGenerateTool is not None
        assert tts_mod.TtsGenerateTool is not None
        assert video_mod.VideoGenerateTool is not None
        assert music_mod.MusicGenerateTool is not None

    @pytest.mark.parametrize("attr", ["MediaType", "FallbackStrategy", "ProviderChain"])
    def test_media_core_defines_shared_types(self, image_mod, attr):
        assert getattr(image_mod, attr) is not None

    def test_provider_chain_same_origin_across_tools(self, image_mod, tts_mod, video_mod, music_mod):
        """四工具引用的 ProviderChain 同源（media/_media_core.py）。"""
        assert image_mod.ProviderChain is tts_mod.ProviderChain
        assert video_mod.ProviderChain is music_mod.ProviderChain
        assert image_mod.ProviderChain is video_mod.ProviderChain

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


# ── 安全/健壮随迁：传 provider 不 NameError；注册表注入路径可用 ──


class _FakeResult:
    """Provider 返回结果替身（与 0.1 MediaResult 字段对齐）。"""

    def __init__(self, media_type: Any):
        self.file_path = "out.bin"
        self.media_type = media_type
        self.duration_seconds = 1.0
        self.provider_name = "fake_provider"
        self.metadata = {}


class _FakeProvider:
    """异步 Provider 替身（is_available + execute_generate/execute_synthesize）。"""

    provider_name = "fake_provider"

    def __init__(self, media_type: Any):
        self._media_type = media_type

    async def is_available(self) -> bool:
        return True

    async def execute_generate(self, prompt: str, **kwargs: Any) -> _FakeResult:
        return _FakeResult(self._media_type)

    async def execute_synthesize(self, text: str, **kwargs: Any) -> _FakeResult:
        return _FakeResult(self._media_type)


class _FakeRegistry:
    """MediaProviderRegistry 替身（get / get_chain_for_type）。"""

    def __init__(self, provider: _FakeProvider):
        self._provider = provider

    def get(self, provider_name: str) -> _FakeProvider:
        return self._provider

    def get_chain_for_type(self, media_type: Any, strategy: Any = None) -> Any:
        from _media_core import ProviderChain

        return ProviderChain(providers=[self._provider], strategy=strategy)


class TestProviderSpecifiedNoNameError:
    """用户显式指定 provider 时执行不 NameError（原 F821 消音缺陷）。

    注入式注册表路径保留（0.1 ProviderChain 语义）——测试注入 FakeRegistry
    时仍走链式执行，与 capability 路径并行不悖。
    """

    @pytest.mark.asyncio
    async def test_image_with_provider_succeeds(self, image_mod):
        tool = image_mod.ImageGenerateTool(
            registry=_FakeRegistry(_FakeProvider(image_mod.MediaType.IMAGE))
        )
        result = await tool.execute({"prompt": "a cat", "provider": "fake_provider"})
        assert result.success is True
        assert result.output["provider"] == "fake_provider"

    @pytest.mark.asyncio
    async def test_tts_with_provider_succeeds(self, tts_mod):
        tool = tts_mod.TtsGenerateTool(
            registry=_FakeRegistry(_FakeProvider(tts_mod.MediaType.TTS))
        )
        result = await tool.execute({"text": "hello", "provider": "fake_provider"})
        assert result.success is True
        assert result.output["provider"] == "fake_provider"

    @pytest.mark.asyncio
    async def test_video_with_provider_succeeds(self, video_mod):
        tool = video_mod.VideoGenerateTool(
            provider_registry=_FakeRegistry(_FakeProvider(video_mod.MediaType.VIDEO))
        )
        result = await tool.execute({"prompt": "a dog", "provider": "fake_provider"})
        assert result.success is True
        assert result.output["provider_name"] == "fake_provider"

    @pytest.mark.asyncio
    async def test_music_with_provider_succeeds(self, music_mod):
        tool = music_mod.MusicGenerateTool(
            provider_registry=_FakeRegistry(_FakeProvider(music_mod.MediaType.MUSIC))
        )
        result = await tool.execute({"prompt": "lofi beat", "provider": "fake_provider"})
        assert result.success is True
        assert result.output["provider_name"] == "fake_provider"


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
