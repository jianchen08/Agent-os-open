"""TTS 生成工具单元测试。

测试覆盖：
- 工具定义（名称、描述、输入输出 schema）
- 空文本验证
- 音频格式验证
- 语速范围验证
- Provider 正常合成
- Provider 失败回退
- 无注册表时提示
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools.builtin.tts_generate import TtsGenerateTool, create_tts_generate_tool
from tools.media.base import MediaProvider, MediaProviderConfig, MediaResult, MediaType


def _make_mock_provider(
    available: bool = True,
    result: MediaResult | None = None,
    error: Exception | None = None,
) -> MediaProvider:
    """创建 Mock MediaProvider。

    Args:
        available: Provider 是否可用
        result: synthesize 返回结果
        error: synthesize 抛出的异常

    Returns:
        Mock MediaProvider 实例
    """
    config = MediaProviderConfig(
        class_name="MockProvider",
        enabled=True,
        priority=1,
    )
    provider = MediaProvider.__new__(MediaProvider)
    provider._provider_name = "mock-tts"
    provider._media_type = MediaType.TTS
    provider._config = config
    provider.is_available = AsyncMock(return_value=available)  # type: ignore[method-assign]

    if error:
        provider.synthesize = AsyncMock(side_effect=error)  # type: ignore[method-assign]
    elif result:
        provider.synthesize = AsyncMock(return_value=result)  # type: ignore[method-assign]
    else:
        default_result = MediaResult(
            file_path=Path("/tmp/tts_output.mp3"),
            media_type=MediaType.TTS,
            duration_seconds=3.5,
            metadata={"voice": "alloy"},
            provider_name="mock-tts",
        )
        provider.synthesize = AsyncMock(return_value=default_result)  # type: ignore[method-assign]

    return provider  # type: ignore[return-value]


class TestTtsGenerateToolDefinition:
    """工具定义测试。"""

    def test_tool_name_is_tts_generate(self) -> None:
        tool_def = TtsGenerateTool.get_tool_definition()
        assert tool_def.name == "tts_generate"

    def test_description_contains_keywords(self) -> None:
        tool_def = TtsGenerateTool.get_tool_definition()
        assert "文本" in tool_def.description
        assert "语音" in tool_def.description

    def test_input_schema_has_required_text(self) -> None:
        tool_def = TtsGenerateTool.get_tool_definition()
        required = tool_def.input_schema.get("required", [])
        assert "text" in required

    def test_input_schema_has_optional_params(self) -> None:
        tool_def = TtsGenerateTool.get_tool_definition()
        props = tool_def.input_schema.get("properties", {})
        assert "voice" in props
        assert "format" in props
        assert "speed" in props

    def test_output_schema_has_file_path_and_duration(self) -> None:
        tool_def = TtsGenerateTool.get_tool_definition()
        props = tool_def.output_schema.get("properties", {})
        assert "file_path" in props
        assert "duration_seconds" in props

    def test_category_is_media(self) -> None:
        tool_def = TtsGenerateTool.get_tool_definition()
        assert tool_def.category == "media"

    def test_tags_include_tts(self) -> None:
        tool_def = TtsGenerateTool.get_tool_definition()
        assert "tts" in tool_def.tags


class TestTtsGenerateToolExecute:
    """工具执行测试。"""

    @pytest.mark.asyncio
    async def test_empty_text_returns_failure(self) -> None:
        tool = TtsGenerateTool()
        result = await tool.execute({"text": ""})
        assert result.is_failed
        assert "不能为空" in result.error

    @pytest.mark.asyncio
    async def test_whitespace_only_text_returns_failure(self) -> None:
        tool = TtsGenerateTool()
        result = await tool.execute({"text": "   "})
        assert result.is_failed

    @pytest.mark.asyncio
    async def test_unsupported_format_returns_failure(self) -> None:
        tool = TtsGenerateTool()
        result = await tool.execute({"text": "你好", "format": "flac"})
        assert result.is_failed
        assert "不支持" in result.error

    @pytest.mark.asyncio
    async def test_supported_formats_pass_validation(self) -> None:
        """mp3, wav, ogg should all pass format validation."""
        tool = TtsGenerateTool()
        for fmt in ("mp3", "wav", "ogg"):
            result = await tool.execute({"text": "测试", "format": fmt})
            if result.is_failed:
                assert "格式" not in result.error

    @pytest.mark.asyncio
    async def test_speed_above_range_returns_failure(self) -> None:
        tool = TtsGenerateTool()
        result = await tool.execute({"text": "你好", "speed": 3.0})
        assert result.is_failed
        assert "语速" in result.error

    @pytest.mark.asyncio
    async def test_speed_below_range_returns_failure(self) -> None:
        tool = TtsGenerateTool()
        result = await tool.execute({"text": "你好", "speed": 0.1})
        assert result.is_failed

    @pytest.mark.asyncio
    async def test_no_registry_returns_no_provider(self) -> None:
        tool = TtsGenerateTool()
        result = await tool.execute({"text": "你好世界"})
        assert result.is_failed
        assert "未配置" in result.error or "NO_PROVIDER" in (result.error_code or "")

    @pytest.mark.asyncio
    async def test_registry_synthesize_success(self) -> None:
        registry = MagicMock()
        chain = MagicMock()
        chain.execute_synthesize = AsyncMock(
            return_value=MediaResult(
                file_path=Path("/tmp/tts_out.mp3"),
                media_type=MediaType.TTS,
                duration_seconds=5.0,
                metadata={"voice": "alloy"},
                provider_name="mock-tts",
            )
        )
        registry.get_chain_for_type.return_value = chain

        tool = TtsGenerateTool(registry=registry)
        result = await tool.execute({"text": "你好世界", "voice": "alloy"})
        assert result.is_completed
        data = result.output
        assert data["file_path"] == "/tmp/tts_out.mp3"
        assert data["duration_seconds"] == 5.0

    @pytest.mark.asyncio
    async def test_registry_synthesize_failure_returns_error(self) -> None:
        registry = MagicMock()
        chain = MagicMock()
        chain.execute_synthesize = AsyncMock(
            side_effect=RuntimeError("所有 Provider 均失败: mock-tts: error")
        )
        registry.get_chain_for_type.return_value = chain

        tool = TtsGenerateTool(registry=registry)
        result = await tool.execute({"text": "测试"})
        assert result.is_failed


class TestCreateTtsGenerateTool:
    """工厂函数测试。"""

    def test_create_without_registry(self) -> None:
        tool = create_tts_generate_tool()
        assert isinstance(tool, TtsGenerateTool)

    def test_create_with_registry(self) -> None:
        registry = MagicMock()
        tool = create_tts_generate_tool(registry=registry)
        assert isinstance(tool, TtsGenerateTool)
