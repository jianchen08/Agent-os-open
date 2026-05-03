"""video_generate 工具测试"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools.builtin.video_generate import VideoGenerateTool
from tools.media.base import MediaProviderConfig, MediaResult, MediaType


class TestVideoGenerateToolDefinition:
    """video_generate 工具定义测试"""

    @pytest.fixture
    def tool(self) -> VideoGenerateTool:
        """创建无 Provider 的工具实例"""
        return VideoGenerateTool()

    def test_tool_definition_name(self, tool: VideoGenerateTool) -> None:
        """测试工具名称"""
        definition = tool.get_tool_definition()
        assert definition.name == "video_generate"

    def test_tool_definition_source(self, tool: VideoGenerateTool) -> None:
        """测试工具来源"""
        from tools.types import ToolSource

        definition = tool.get_tool_definition()
        assert definition.source == ToolSource.BUILTIN

    def test_tool_definition_has_required_prompt(self, tool: VideoGenerateTool) -> None:
        """测试 input_schema 包含必填的 prompt 字段"""
        definition = tool.get_tool_definition()
        assert "prompt" in definition.input_schema["properties"]
        assert "prompt" in definition.input_schema["required"]

    def test_tool_definition_has_optional_fields(self, tool: VideoGenerateTool) -> None:
        """测试 input_schema 包含可选参数"""
        definition = tool.get_tool_definition()
        props = definition.input_schema["properties"]
        assert "duration" in props
        assert "fps" in props
        assert "resolution" in props
        assert "style" in props

    def test_tool_definition_has_usage_guidance(self, tool: VideoGenerateTool) -> None:
        """测试工具定义包含使用场景说明"""
        definition = tool.get_tool_definition()
        assert len(definition.when_to_use) > 0
        assert len(definition.when_not_to_use) > 0


class TestVideoGenerateExecution:
    """video_generate 工具执行测试"""

    @pytest.fixture
    def tool_no_provider(self) -> VideoGenerateTool:
        """创建无 Provider 的工具实例"""
        return VideoGenerateTool()

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
        self, tool_no_provider: VideoGenerateTool
    ) -> None:
        """测试无注册表时返回友好提示"""
        result = await tool_no_provider.execute({"prompt": "一只猫在跳舞"})

        assert result.success is True
        assert result.data["status"] == "not_configured"
        assert "Provider" in result.data["message"]

    @pytest.mark.asyncio
    async def test_execute_empty_chain_returns_friendly_message(
        self, mock_registry: MagicMock, mock_chain: MagicMock
    ) -> None:
        """测试 ProviderChain 为空时返回友好提示"""
        mock_registry.get_chain_for_type.return_value = mock_chain
        tool = VideoGenerateTool(provider_registry=mock_registry)

        result = await tool.execute({"prompt": "一只猫在跳舞"})

        assert result.success is True
        assert result.data["status"] == "not_configured"
        assert "Provider" in result.data["message"]

    @pytest.mark.asyncio
    async def test_execute_missing_prompt_returns_error(
        self, tool_no_provider: VideoGenerateTool
    ) -> None:
        """测试缺少 prompt 参数时返回错误"""
        result = await tool_no_provider.execute({})

        assert result.success is False
        assert result.error_code == "MISSING_PROMPT"

    @pytest.mark.asyncio
    async def test_execute_empty_prompt_returns_error(
        self, tool_no_provider: VideoGenerateTool
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
        # 创建 Mock Provider
        mock_provider = MagicMock()
        mock_provider.config = MediaProviderConfig(class_name="TestProvider")
        mock_provider.provider_name = "test_video_provider"

        # 创建 Mock Chain
        mock_chain = MagicMock()
        mock_chain.providers = [mock_provider]
        mock_chain.execute_generate = AsyncMock(
            return_value=MediaResult(
                file_path=Path("/tmp/test_video.mp4"),
                media_type=MediaType.VIDEO,
                duration_seconds=5.0,
                provider_name="test_video_provider",
            )
        )

        mock_registry.get_chain_for_type.return_value = mock_chain
        tool = VideoGenerateTool(provider_registry=mock_registry)

        result = await tool.execute({"prompt": "一只猫在跳舞", "duration": 5})

        assert result.success is True
        assert result.data["file_path"] == "/tmp/test_video.mp4"
        assert result.data["media_type"] == "video"
        assert result.data["duration_seconds"] == 5.0
        assert result.data["provider_name"] == "test_video_provider"

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
        tool = VideoGenerateTool(provider_registry=mock_registry)

        result = await tool.execute({"prompt": "一只猫在跳舞"})

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
                file_path=Path("/tmp/test_video.mp4"),
                media_type=MediaType.VIDEO,
                provider_name="test_provider",
            )
        )

        mock_registry.get_chain_for_type.return_value = mock_chain
        tool = VideoGenerateTool(provider_registry=mock_registry)

        await tool.execute({
            "prompt": "测试",
            "duration": 10,
            "fps": 30,
            "resolution": "1920x1080",
            "style": "realistic",
        })

        mock_chain.execute_generate.assert_called_once_with(
            "测试",
            duration=10,
            fps=30,
            resolution="1920x1080",
            style="realistic",
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
                file_path=Path("/tmp/test_video.mp4"),
                media_type=MediaType.VIDEO,
                provider_name="test_provider",
            )
        )

        mock_registry.get_chain_for_type.return_value = mock_chain
        tool = VideoGenerateTool(provider_registry=mock_registry)

        await tool.execute({"prompt": "测试", "duration": None})

        # 只传 prompt，不传 duration（因为它是 None）
        mock_chain.execute_generate.assert_called_once_with("测试")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
