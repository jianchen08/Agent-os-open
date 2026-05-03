"""music_generate 工具测试"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools.builtin.music_generate import MusicGenerateTool
from tools.media.base import MediaProviderConfig, MediaResult, MediaType


class TestMusicGenerateToolDefinition:
    """music_generate 工具定义测试"""

    @pytest.fixture
    def tool(self) -> MusicGenerateTool:
        """创建无 Provider 的工具实例"""
        return MusicGenerateTool()

    def test_tool_definition_name(self, tool: MusicGenerateTool) -> None:
        """测试工具名称"""
        definition = tool.get_tool_definition()
        assert definition.name == "music_generate"

    def test_tool_definition_source(self, tool: MusicGenerateTool) -> None:
        """测试工具来源"""
        from tools.types import ToolSource

        definition = tool.get_tool_definition()
        assert definition.source == ToolSource.BUILTIN

    def test_tool_definition_has_required_prompt(self, tool: MusicGenerateTool) -> None:
        """测试 input_schema 包含必填的 prompt 字段"""
        definition = tool.get_tool_definition()
        assert "prompt" in definition.input_schema["properties"]
        assert "prompt" in definition.input_schema["required"]

    def test_tool_definition_has_optional_fields(self, tool: MusicGenerateTool) -> None:
        """测试 input_schema 包含可选参数"""
        definition = tool.get_tool_definition()
        props = definition.input_schema["properties"]
        assert "genre" in props
        assert "mood" in props
        assert "duration_seconds" in props
        assert "tempo" in props

    def test_tool_definition_has_usage_guidance(self, tool: MusicGenerateTool) -> None:
        """测试工具定义包含使用场景说明"""
        definition = tool.get_tool_definition()
        assert len(definition.when_to_use) > 0
        assert len(definition.when_not_to_use) > 0


class TestMusicGenerateExecution:
    """music_generate 工具执行测试"""

    @pytest.fixture
    def tool_no_provider(self) -> MusicGenerateTool:
        """创建无 Provider 的工具实例"""
        return MusicGenerateTool()

    @pytest.fixture
    def mock_registry(self) -> MagicMock:
        """创建 Mock 的 MediaProviderRegistry"""
        return MagicMock()

    @pytest.fixture
    def mock_chain(self) -> MagicMock:
        """创建 Mock 的 ProviderChain"""
        chain = MagicMock()
        chain.providers = []
        return chain

    @pytest.mark.asyncio
    async def test_execute_no_registry_returns_friendly_message(
        self, tool_no_provider: MusicGenerateTool
    ) -> None:
        """测试无注册表时返回友好提示"""
        result = await tool_no_provider.execute({"prompt": "一段欢快的旋律"})

        assert result.success is True
        assert result.data["status"] == "not_configured"
        assert "Provider" in result.data["message"]

    @pytest.mark.asyncio
    async def test_execute_empty_chain_returns_friendly_message(
        self, mock_registry: MagicMock, mock_chain: MagicMock
    ) -> None:
        """测试 ProviderChain 为空时返回友好提示"""
        mock_registry.get_chain_for_type.return_value = mock_chain
        tool = MusicGenerateTool(provider_registry=mock_registry)

        result = await tool.execute({"prompt": "一段欢快的旋律"})

        assert result.success is True
        assert result.data["status"] == "not_configured"
        assert "Provider" in result.data["message"]

    @pytest.mark.asyncio
    async def test_execute_missing_prompt_returns_error(
        self, tool_no_provider: MusicGenerateTool
    ) -> None:
        """测试缺少 prompt 参数时返回错误"""
        result = await tool_no_provider.execute({})

        assert result.success is False
        assert result.error_code == "MISSING_PROMPT"

    @pytest.mark.asyncio
    async def test_execute_empty_prompt_returns_error(
        self, tool_no_provider: MusicGenerateTool
    ) -> None:
        """测试空 prompt 时返回错误"""
        result = await tool_no_provider.execute({"prompt": ""})

        assert result.success is False
        assert result.error_code == "MISSING_PROMPT"

    @pytest.mark.asyncio
    async def test_execute_with_provider_success(
        self, mock_registry: MagicMock
    ) -> None:
        """测试有 Provider 时成功执行"""
        mock_provider = MagicMock()
        mock_provider.config = MediaProviderConfig(class_name="TestProvider")
        mock_provider.provider_name = "test_music_provider"

        mock_chain = MagicMock()
        mock_chain.providers = [mock_provider]
        mock_chain.execute_generate = AsyncMock(
            return_value=MediaResult(
                file_path=Path("/tmp/test_music.mp3"),
                media_type=MediaType.MUSIC,
                duration_seconds=30.0,
                provider_name="test_music_provider",
            )
        )

        mock_registry.get_chain_for_type.return_value = mock_chain
        tool = MusicGenerateTool(provider_registry=mock_registry)

        result = await tool.execute({"prompt": "一段欢快的旋律", "genre": "pop"})

        assert result.success is True
        assert result.data["file_path"] == "/tmp/test_music.mp3"
        assert result.data["media_type"] == "music"
        assert result.data["duration_seconds"] == 30.0
        assert result.data["provider_name"] == "test_music_provider"

    @pytest.mark.asyncio
    async def test_execute_provider_failure_returns_error(
        self, mock_registry: MagicMock
    ) -> None:
        """测试 Provider 执行失败时返回错误"""
        mock_provider = MagicMock()
        mock_provider.config = MediaProviderConfig(class_name="TestProvider")

        mock_chain = MagicMock()
        mock_chain.providers = [mock_provider]
        mock_chain.execute_generate = AsyncMock(
            side_effect=RuntimeError("Provider 生成失败")
        )

        mock_registry.get_chain_for_type.return_value = mock_chain
        tool = MusicGenerateTool(provider_registry=mock_registry)

        result = await tool.execute({"prompt": "一段欢快的旋律"})

        assert result.success is False
        assert result.error_code == "GENERATE_FAILED"

    @pytest.mark.asyncio
    async def test_execute_passes_optional_kwargs(
        self, mock_registry: MagicMock
    ) -> None:
        """测试可选参数正确传递给 Provider"""
        mock_provider = MagicMock()
        mock_provider.config = MediaProviderConfig(class_name="TestProvider")

        mock_chain = MagicMock()
        mock_chain.providers = [mock_provider]
        mock_chain.execute_generate = AsyncMock(
            return_value=MediaResult(
                file_path=Path("/tmp/test_music.mp3"),
                media_type=MediaType.MUSIC,
                provider_name="test_provider",
            )
        )

        mock_registry.get_chain_for_type.return_value = mock_chain
        tool = MusicGenerateTool(provider_registry=mock_registry)

        await tool.execute({
            "prompt": "测试",
            "genre": "jazz",
            "mood": "happy",
            "duration_seconds": 60,
            "tempo": 120,
        })

        mock_chain.execute_generate.assert_called_once_with(
            "测试",
            genre="jazz",
            mood="happy",
            duration_seconds=60,
            tempo=120,
        )

    @pytest.mark.asyncio
    async def test_execute_skips_none_kwargs(
        self, mock_registry: MagicMock
    ) -> None:
        """测试不传递 None 值的可选参数"""
        mock_provider = MagicMock()
        mock_provider.config = MediaProviderConfig(class_name="TestProvider")

        mock_chain = MagicMock()
        mock_chain.providers = [mock_provider]
        mock_chain.execute_generate = AsyncMock(
            return_value=MediaResult(
                file_path=Path("/tmp/test_music.mp3"),
                media_type=MediaType.MUSIC,
                provider_name="test_provider",
            )
        )

        mock_registry.get_chain_for_type.return_value = mock_chain
        tool = MusicGenerateTool(provider_registry=mock_registry)

        await tool.execute({"prompt": "测试", "genre": None})

        # 只传 prompt，不传 genre（因为它是 None）
        mock_chain.execute_generate.assert_called_once_with("测试")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
