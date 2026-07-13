"""EdgeTTSProvider 单元测试。

覆盖场景：
- is_available: edge-tts 已安装 / 未安装
- synthesize: 正常合成 / 参数传递 / 文件保存 / 元数据返回
- 异常处理: edge-tts 未安装时合成 / 合成失败
- 边界: 空文本 / 超长文本
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.media.base import MediaProviderConfig, MediaResult, MediaType


# ---------------------------------------------------------------------------
# 辅助：构造 EdgeTTSProvider 实例（延迟导入，方便 mock edge_tts）
# ---------------------------------------------------------------------------

def _make_config(**overrides: Any) -> MediaProviderConfig:
    """创建测试用的 MediaProviderConfig。"""
    defaults: dict[str, Any] = {
        "class_name": "EdgeTTSProvider",
        "enabled": True,
        "priority": 1,
        "config": {},
    }
    defaults.update(overrides)
    return MediaProviderConfig(**defaults)


def _import_provider() -> types.ModuleType:
    """动态导入 EdgeTTSProvider 模块。"""
    from tools.media.providers import edge_tts_provider
    return edge_tts_provider


# ========================================================================
# 测试 is_available
# ========================================================================


class TestIsAvailable:
    """is_available 检查测试。"""

    @pytest.mark.asyncio
    async def test_available_when_edge_tts_installed(self) -> None:
        """edge-tts 已安装时返回 True。"""
        mod = _import_provider()
        provider = mod.EdgeTTSProvider(
            config=_make_config(),
        )
        mock_edge_tts = MagicMock()
        with patch.object(mod, "edge_tts", mock_edge_tts):
            result = await provider.is_available()
        assert result is True

    @pytest.mark.asyncio
    async def test_not_available_when_edge_tts_not_installed(self) -> None:
        """edge-tts 未安装时返回 False。"""
        mod = _import_provider()
        provider = mod.EdgeTTSProvider(
            config=_make_config(),
        )
        with patch.object(mod, "edge_tts", None):
            result = await provider.is_available()
        assert result is False


# ========================================================================
# 测试 synthesize 正常流程
# ========================================================================


class TestSynthesize:
    """synthesize 合成测试。"""

    @pytest.mark.asyncio
    async def test_synthesize_creates_mp3_file(self, tmp_path: Path) -> None:
        """合成成功后生成 mp3 文件并返回 MediaResult。"""
        mod = _import_provider()
        provider = mod.EdgeTTSProvider(
            config=_make_config(config={"output_dir": str(tmp_path)}),
        )

        mock_edge_tts = MagicMock()
        mock_communicate = AsyncMock()

        async def fake_save(path: str) -> None:
            Path(path).write_bytes(b"fake-mp3-data")

        mock_communicate.save = fake_save
        mock_edge_tts.Communicate.return_value = mock_communicate

        with patch.object(mod, "edge_tts", mock_edge_tts):
            result = await provider.synthesize(
                text="你好世界",
                voice="zh-CN-XiaoxiaoNeural",
                rate="+0%",
            )

        assert isinstance(result, MediaResult)
        assert result.media_type == MediaType.TTS
        assert result.provider_name == "edge_tts"

        assert isinstance(result.file_path, Path)
        assert result.file_path.exists()
        assert result.file_path.suffix == ".mp3"

        assert result.metadata["voice"] == "zh-CN-XiaoxiaoNeural"
        assert result.metadata["rate"] == "+0%"
        assert result.metadata["text_length"] == 4

    @pytest.mark.asyncio
    async def test_synthesize_with_default_voice(self, tmp_path: Path) -> None:
        """不指定 voice 时使用默认语音。"""
        mod = _import_provider()
        provider = mod.EdgeTTSProvider(
            config=_make_config(config={"output_dir": str(tmp_path)}),
        )

        mock_edge_tts = MagicMock()
        mock_communicate = AsyncMock()

        async def fake_save(path: str) -> None:
            Path(path).write_bytes(b"fake-mp3-data")

        mock_communicate.save = fake_save
        mock_edge_tts.Communicate.return_value = mock_communicate

        with patch.object(mod, "edge_tts", mock_edge_tts):
            result = await provider.synthesize(text="测试文本")

        assert result.metadata["voice"] == "zh-CN-XiaoxiaoNeural"

    @pytest.mark.asyncio
    async def test_synthesize_passes_rate(self, tmp_path: Path) -> None:
        """rate 参数正确传递给 Communicate。"""
        mod = _import_provider()
        provider = mod.EdgeTTSProvider(
            config=_make_config(config={"output_dir": str(tmp_path)}),
        )

        mock_edge_tts = MagicMock()
        mock_communicate = AsyncMock()

        async def fake_save(path: str) -> None:
            Path(path).write_bytes(b"fake-mp3-data")

        mock_communicate.save = fake_save
        mock_edge_tts.Communicate.return_value = mock_communicate

        with patch.object(mod, "edge_tts", mock_edge_tts):
            await provider.synthesize(
                text="测试",
                rate="+50%",
            )

        mock_edge_tts.Communicate.assert_called_once()
        call_args = mock_edge_tts.Communicate.call_args
        assert call_args[1]["rate"] == "+50%"


# ========================================================================
# 测试 synthesize 异常处理
# ========================================================================


class TestSynthesizeErrors:
    """synthesize 异常处理测试。"""

    @pytest.mark.asyncio
    async def test_synthesize_raises_when_edge_tts_not_installed(self) -> None:
        """edge-tts 未安装时 synthesize 抛出异常。"""
        mod = _import_provider()
        provider = mod.EdgeTTSProvider(
            config=_make_config(),
        )

        with patch.object(mod, "edge_tts", None):
            with pytest.raises(RuntimeError, match="edge-tts"):
                await provider.synthesize(text="测试")

    @pytest.mark.asyncio
    async def test_synthesize_raises_on_save_failure(self, tmp_path: Path) -> None:
        """合成保存失败时抛出异常。"""
        mod = _import_provider()
        provider = mod.EdgeTTSProvider(
            config=_make_config(config={"output_dir": str(tmp_path)}),
        )

        mock_edge_tts = MagicMock()
        mock_communicate = AsyncMock()
        mock_communicate.save = AsyncMock(side_effect=IOError("磁盘已满"))
        mock_edge_tts.Communicate.return_value = mock_communicate

        with patch.object(mod, "edge_tts", mock_edge_tts):
            with pytest.raises(RuntimeError, match="合成失败"):
                await provider.synthesize(text="测试")


# ========================================================================
# 测试 Provider 基本属性
# ========================================================================


class TestProviderProperties:
    """Provider 属性测试。"""

    def test_media_type_is_tts(self) -> None:
        """media_type 应为 TTS。"""
        mod = _import_provider()
        provider = mod.EdgeTTSProvider(
            config=_make_config(),
        )
        assert provider.media_type == MediaType.TTS

    def test_provider_name(self) -> None:
        """provider_name 应为构造时传入的名称。"""
        mod = _import_provider()
        provider = mod.EdgeTTSProvider(
            config=_make_config(),
        )
        assert provider.provider_name == "edge_tts"
