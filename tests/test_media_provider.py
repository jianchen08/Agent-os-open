"""媒体 Provider 抽象层和配置体系单元测试。

覆盖：
- MediaProvider 抽象基类接口
- MediaResult 数据类
- MediaProviderConfig 配置模型
- MediaType 枚举
- FallbackStrategy 枚举
- ProviderChain Fallback 链
- MediaProviderRegistry 注册表
- YAML 配置加载
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tools.media.base import (
    MediaProvider,
    MediaProviderConfig,
    MediaResult,
    MediaType,
)
from tools.media.fallback import FallbackStrategy, ProviderChain
from tools.media.provider_registry import MediaProviderRegistry


# ============================================================
# 辅助：测试用 Mock Provider
# ============================================================


class MockTTSProvider(MediaProvider):
    """Mock TTS Provider，用于测试。"""

    def __init__(
        self,
        *,
        provider_name: str = "mock_tts",
        available: bool = True,
        should_fail: bool = False,
    ) -> None:
        super().__init__(
            provider_name=provider_name,
            media_type=MediaType.TTS,
            config=MediaProviderConfig(
                class_name="MockTTSProvider",
                enabled=True,
                priority=1,
                config={"voice": "test"},
            ),
        )
        self._available = available
        self._should_fail = should_fail

    async def is_available(self) -> bool:
        """检查 Provider 是否可用。"""
        return self._available

    async def synthesize(self, text: str, **kwargs: Any) -> MediaResult:
        """合成音频（TTS）。"""
        if self._should_fail:
            raise RuntimeError(f"{self.provider_name} synthesis failed")
        return MediaResult(
            file_path=Path(f"/tmp/{self.provider_name}_output.mp3"),
            media_type=MediaType.TTS,
            duration_seconds=3.5,
            metadata={"voice": kwargs.get("voice", "default")},
            provider_name=self.provider_name,
        )


class MockImageProvider(MediaProvider):
    """Mock Image Provider，用于测试。"""

    def __init__(
        self,
        *,
        provider_name: str = "mock_image",
        available: bool = True,
        should_fail: bool = False,
    ) -> None:
        super().__init__(
            provider_name=provider_name,
            media_type=MediaType.IMAGE,
            config=MediaProviderConfig(
                class_name="MockImageProvider",
                enabled=True,
                priority=1,
                config={"api_url": "http://localhost:8080"},
            ),
        )
        self._available = available
        self._should_fail = should_fail

    async def is_available(self) -> bool:
        """检查 Provider 是否可用。"""
        return self._available

    async def generate(self, prompt: str, **kwargs: Any) -> MediaResult:
        """生成图像。"""
        if self._should_fail:
            raise RuntimeError(f"{self.provider_name} generation failed")
        return MediaResult(
            file_path=Path(f"/tmp/{self.provider_name}_output.png"),
            media_type=MediaType.IMAGE,
            metadata={"prompt": prompt, "size": kwargs.get("size", "512x512")},
            provider_name=self.provider_name,
        )


# ============================================================
# 测试 MediaType 枚举
# ============================================================


class TestMediaType:
    """MediaType 枚举测试。"""

    def test_media_types_defined(self) -> None:
        """所有媒体类型都已定义。"""
        assert MediaType.TTS == "tts"
        assert MediaType.IMAGE == "image"
        assert MediaType.VIDEO == "video"
        assert MediaType.MUSIC == "music"

    def test_media_type_from_string(self) -> None:
        """从字符串创建 MediaType。"""
        assert MediaType("tts") == MediaType.TTS
        assert MediaType("image") == MediaType.IMAGE


# ============================================================
# 测试 MediaResult 数据类
# ============================================================


class TestMediaResult:
    """MediaResult 数据类测试。"""

    def test_basic_creation(self) -> None:
        """基本创建。"""
        result = MediaResult(
            file_path=Path("/tmp/test.mp3"),
            media_type=MediaType.TTS,
            metadata={"voice": "test"},
            provider_name="mock_tts",
        )
        assert result.file_path == Path("/tmp/test.mp3")
        assert result.media_type == MediaType.TTS
        assert result.provider_name == "mock_tts"
        assert result.duration_seconds is None
        assert result.metadata == {"voice": "test"}

    def test_with_duration(self) -> None:
        """带时长的创建。"""
        result = MediaResult(
            file_path=Path("/tmp/test.mp3"),
            media_type=MediaType.TTS,
            duration_seconds=5.2,
            provider_name="mock",
        )
        assert result.duration_seconds == 5.2

    def test_default_values(self) -> None:
        """默认值。"""
        result = MediaResult(
            file_path=Path("/tmp/test.png"),
            media_type=MediaType.IMAGE,
            provider_name="mock",
        )
        assert result.metadata == {}
        assert result.duration_seconds is None
        assert result.error is None


# ============================================================
# 测试 MediaProviderConfig
# ============================================================


class TestMediaProviderConfig:
    """MediaProviderConfig Pydantic 模型测试。"""

    def test_basic_config(self) -> None:
        """基本配置。"""
        config = MediaProviderConfig(
            class_name="EdgeTTSProvider",
            enabled=True,
            priority=1,
            config={"voice": "zh-CN-XiaoxiaoNeural"},
        )
        assert config.class_name == "EdgeTTSProvider"
        assert config.enabled is True
        assert config.priority == 1
        assert config.config == {"voice": "zh-CN-XiaoxiaoNeural"}

    def test_default_values(self) -> None:
        """默认值。"""
        config = MediaProviderConfig(class_name="TestProvider")
        assert config.enabled is True
        assert config.priority == 99
        assert config.config == {}

    def test_disabled_config(self) -> None:
        """禁用配置。"""
        config = MediaProviderConfig(
            class_name="TestProvider",
            enabled=False,
        )
        assert config.enabled is False


# ============================================================
# 测试 MediaProvider 抽象基类
# ============================================================


class TestMediaProvider:
    """MediaProvider 抽象基类测试。"""

    def test_cannot_instantiate_directly(self) -> None:
        """不能直接实例化抽象基类。"""
        with pytest.raises(TypeError):
            MediaProvider(  # type: ignore[abstract]
                provider_name="test",
                media_type=MediaType.TTS,
                config=MediaProviderConfig(class_name="Test"),
            )

    def test_mock_tts_provider_creation(self) -> None:
        """Mock TTS Provider 可以正常创建。"""
        provider = MockTTSProvider()
        assert provider.provider_name == "mock_tts"
        assert provider.media_type == MediaType.TTS

    def test_mock_image_provider_creation(self) -> None:
        """Mock Image Provider 可以正常创建。"""
        provider = MockImageProvider()
        assert provider.provider_name == "mock_image"
        assert provider.media_type == MediaType.IMAGE

    @pytest.mark.asyncio
    async def test_tts_synthesize(self) -> None:
        """TTS synthesize 正常工作。"""
        provider = MockTTSProvider()
        result = await provider.synthesize("hello world")
        assert result.file_path == Path("/tmp/mock_tts_output.mp3")
        assert result.media_type == MediaType.TTS
        assert result.duration_seconds == 3.5

    @pytest.mark.asyncio
    async def test_image_generate(self) -> None:
        """Image generate 正常工作。"""
        provider = MockImageProvider()
        result = await provider.generate("a cat")
        assert result.file_path == Path("/tmp/mock_image_output.png")
        assert result.media_type == MediaType.IMAGE
        assert result.metadata["prompt"] == "a cat"

    @pytest.mark.asyncio
    async def test_is_available_true(self) -> None:
        """is_available 返回 True。"""
        provider = MockTTSProvider(available=True)
        assert await provider.is_available() is True

    @pytest.mark.asyncio
    async def test_is_available_false(self) -> None:
        """is_available 返回 False。"""
        provider = MockTTSProvider(available=False)
        assert await provider.is_available() is False


# ============================================================
# 测试 FallbackStrategy 枚举
# ============================================================


class TestFallbackStrategy:
    """FallbackStrategy 枚举测试。"""

    def test_strategies_defined(self) -> None:
        """所有策略已定义。"""
        assert FallbackStrategy.SEQUENTIAL == "sequential"
        assert FallbackStrategy.RANDOM == "random"
        assert FallbackStrategy.WEIGHTED == "weighted"


# ============================================================
# 测试 ProviderChain Fallback 链
# ============================================================


class TestProviderChain:
    """ProviderChain Fallback 链测试。"""

    def test_creation(self) -> None:
        """创建 ProviderChain。"""
        providers = [MockTTSProvider(), MockTTSProvider(provider_name="backup_tts")]
        chain = ProviderChain(providers=providers, strategy=FallbackStrategy.SEQUENTIAL)
        assert len(chain.providers) == 2
        assert chain.strategy == FallbackStrategy.SEQUENTIAL

    @pytest.mark.asyncio
    async def test_first_provider_succeeds(self) -> None:
        """第一个 Provider 成功，不触发 fallback。"""
        provider1 = MockTTSProvider(provider_name="primary", available=True)
        provider2 = MockTTSProvider(provider_name="backup", available=True)
        chain = ProviderChain(
            providers=[provider1, provider2],
            strategy=FallbackStrategy.SEQUENTIAL,
        )
        result = await chain.execute_synthesize("hello")
        assert result.provider_name == "primary"

    @pytest.mark.asyncio
    async def test_fallback_on_failure(self) -> None:
        """第一个 Provider 失败，自动切换到备用。"""
        provider1 = MockTTSProvider(
            provider_name="primary", available=True, should_fail=True
        )
        provider2 = MockTTSProvider(provider_name="backup", available=True)
        chain = ProviderChain(
            providers=[provider1, provider2],
            strategy=FallbackStrategy.SEQUENTIAL,
        )
        result = await chain.execute_synthesize("hello")
        assert result.provider_name == "backup"

    @pytest.mark.asyncio
    async def test_fallback_on_unavailable(self) -> None:
        """第一个 Provider 不可用，自动切换到备用。"""
        provider1 = MockTTSProvider(provider_name="primary", available=False)
        provider2 = MockTTSProvider(provider_name="backup", available=True)
        chain = ProviderChain(
            providers=[provider1, provider2],
            strategy=FallbackStrategy.SEQUENTIAL,
        )
        result = await chain.execute_synthesize("hello")
        assert result.provider_name == "backup"

    @pytest.mark.asyncio
    async def test_all_providers_fail(self) -> None:
        """所有 Provider 均失败，抛出明确异常。"""
        provider1 = MockTTSProvider(
            provider_name="p1", available=True, should_fail=True
        )
        provider2 = MockTTSProvider(
            provider_name="p2", available=True, should_fail=True
        )
        chain = ProviderChain(
            providers=[provider1, provider2],
            strategy=FallbackStrategy.SEQUENTIAL,
        )
        with pytest.raises(RuntimeError, match="所有 Provider 均失败"):
            await chain.execute_synthesize("hello")

    @pytest.mark.asyncio
    async def test_image_chain_generate(self) -> None:
        """Image Provider Chain 的 generate 方法。"""
        provider1 = MockImageProvider(provider_name="img1", available=True)
        chain = ProviderChain(
            providers=[provider1],
            strategy=FallbackStrategy.SEQUENTIAL,
        )
        result = await chain.execute_generate("a beautiful sunset")
        assert result.provider_name == "img1"

    @pytest.mark.asyncio
    async def test_empty_chain_raises(self) -> None:
        """空链执行时抛出异常。"""
        chain = ProviderChain(providers=[], strategy=FallbackStrategy.SEQUENTIAL)
        with pytest.raises(RuntimeError, match="没有可用的 Provider"):
            await chain.execute_synthesize("hello")


# ============================================================
# 测试 MediaProviderRegistry
# ============================================================


class TestMediaProviderRegistry:
    """MediaProviderRegistry 注册表测试。"""

    def setup_method(self) -> None:
        """每个测试方法前重置注册表。"""
        self.registry = MediaProviderRegistry()

    def test_register_provider(self) -> None:
        """注册 Provider。"""
        provider = MockTTSProvider()
        self.registry.register(provider)
        assert self.registry.has("mock_tts")

    def test_get_provider(self) -> None:
        """获取已注册的 Provider。"""
        provider = MockTTSProvider()
        self.registry.register(provider)
        retrieved = self.registry.get("mock_tts")
        assert retrieved is provider

    def test_get_nonexistent_provider(self) -> None:
        """获取不存在的 Provider 返回 None。"""
        result = self.registry.get("nonexistent")
        assert result is None

    def test_unregister_provider(self) -> None:
        """注销 Provider。"""
        provider = MockTTSProvider()
        self.registry.register(provider)
        self.registry.unregister("mock_tts")
        assert not self.registry.has("mock_tts")

    def test_list_by_media_type(self) -> None:
        """按 media_type 查询可用 Provider。"""
        tts_provider = MockTTSProvider()
        image_provider = MockImageProvider()
        self.registry.register(tts_provider)
        self.registry.register(image_provider)

        tts_providers = self.registry.list_by_type(MediaType.TTS)
        image_providers = self.registry.list_by_type(MediaType.IMAGE)

        assert len(tts_providers) == 1
        assert len(image_providers) == 1
        assert tts_providers[0].provider_name == "mock_tts"
        assert image_providers[0].provider_name == "mock_image"

    def test_list_all(self) -> None:
        """列出所有 Provider。"""
        self.registry.register(MockTTSProvider())
        self.registry.register(MockImageProvider())
        all_providers = self.registry.list_all()
        assert len(all_providers) == 2

    def test_register_duplicate_overwrite(self) -> None:
        """注册重名 Provider 覆盖旧值。"""
        provider1 = MockTTSProvider(provider_name="same_name")
        provider2 = MockTTSProvider(provider_name="same_name", available=False)
        self.registry.register(provider1)
        self.registry.register(provider2)
        assert self.registry.get("same_name") is provider2

    def test_get_chain_for_type(self) -> None:
        """按 media_type 获取 Fallback Chain。"""
        primary = MockTTSProvider(provider_name="primary_tts")
        backup = MockTTSProvider(provider_name="backup_tts")
        self.registry.register(primary)
        self.registry.register(backup)

        chain = self.registry.get_chain_for_type(MediaType.TTS)
        assert chain is not None
        assert len(chain.providers) == 2
        # 按 priority 排序
        assert chain.providers[0].provider_name == "primary_tts"

    def test_get_chain_empty_type(self) -> None:
        """不存在的 media_type 获取 Chain 返回空链。"""
        chain = self.registry.get_chain_for_type(MediaType.VIDEO)
        assert chain is not None
        assert len(chain.providers) == 0


# ============================================================
# 测试 YAML 配置加载
# ============================================================


class TestYAMLConfigLoading:
    """YAML 配置加载测试。"""

    def setup_method(self) -> None:
        """每个测试方法前重置注册表。"""
        self.registry = MediaProviderRegistry()

    def test_parse_yaml_config(self, tmp_path: Path) -> None:
        """解析 YAML 配置文件。"""
        config_data = {
            "media": {
                "tts": {
                    "default_provider": "edge_tts",
                    "providers": {
                        "edge_tts": {
                            "class": "EdgeTTSProvider",
                            "enabled": True,
                            "priority": 1,
                            "config": {
                                "voice": "zh-CN-XiaoxiaoNeural",
                                "rate": "+0%",
                            },
                        }
                    },
                },
                "image": {
                    "default_provider": "comfyui",
                    "providers": {},
                },
            }
        }
        config_file = tmp_path / "media_providers.yaml"
        config_file.write_text(yaml.dump(config_data, allow_unicode=True), encoding="utf-8")

        loaded = self.registry.load_config(config_file)
        assert "tts" in loaded
        assert loaded["tts"]["default_provider"] == "edge_tts"
        assert "edge_tts" in loaded["tts"]["providers"]

    def test_load_disabled_provider_not_instantiated(self, tmp_path: Path) -> None:
        """禁用的 Provider 不会被实例化。"""
        config_data = {
            "media": {
                "tts": {
                    "default_provider": "edge_tts",
                    "providers": {
                        "edge_tts": {
                            "class": "EdgeTTSProvider",
                            "enabled": True,
                            "priority": 1,
                            "config": {"voice": "zh-CN-XiaoxiaoNeural"},
                        },
                        "disabled_tts": {
                            "class": "DisabledTTSProvider",
                            "enabled": False,
                            "priority": 2,
                            "config": {},
                        },
                    },
                }
            }
        }
        config_file = tmp_path / "media_providers.yaml"
        config_file.write_text(yaml.dump(config_data, allow_unicode=True), encoding="utf-8")

        loaded = self.registry.load_config(config_file)
        # 只有 enabled 的 provider 被保留
        enabled_providers = loaded["tts"]["providers"]
        assert "edge_tts" in enabled_providers
        assert "disabled_tts" not in enabled_providers

    def test_empty_yaml_config(self, tmp_path: Path) -> None:
        """空 YAML 配置。"""
        config_file = tmp_path / "media_providers.yaml"
        config_file.write_text("media: {}", encoding="utf-8")

        loaded = self.registry.load_config(config_file)
        assert loaded == {}
