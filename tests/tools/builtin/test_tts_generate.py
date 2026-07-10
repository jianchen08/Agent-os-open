"""tts_generate BuiltinTool 单元测试。

覆盖场景：
- get_tool_definition: 工具定义正确性
- execute: 正常执行 / Provider 不可用 / 参数缺失
- 与 MediaProviderRegistry 的集成
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools.media.base import MediaProviderConfig, MediaResult, MediaType


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _import_tool():
    """动态导入 tts_generate 模块。"""
    from tools.builtin import tts_generate
    return tts_generate


def _make_provider_config() -> MediaProviderConfig:
    """创建测试用 Provider 配置。"""
    return MediaProviderConfig(
        class_name="EdgeTTSProvider",
        enabled=True,
        priority=1,
        config={},
    )


# ========================================================================
# 测试 get_tool_definition
# ========================================================================


class TestGetToolDefinition:
    """工具定义测试。"""

    def test_returns_tool_with_correct_name(self) -> None:
        """工具名称应为 tts_generate。"""
        mod = _import_tool()
        tool_def = mod.TtsGenerateTool.get_tool_definition()
        assert tool_def.name == "tts_generate"

    def test_input_schema_has_required_text(self) -> None:
        """input_schema 中 text 为必填字段。"""
        mod = _import_tool()
        tool_def = mod.TtsGenerateTool.get_tool_definition()
        schema = tool_def.input_schema
        assert "text" in schema.get("properties", {})
        assert "text" in schema.get("required", [])

    def test_input_schema_has_optional_params(self) -> None:
        """input_schema 包含可选参数 voice, format, speed。"""
        mod = _import_tool()
        tool_def = mod.TtsGenerateTool.get_tool_definition()
        schema = tool_def.input_schema
        props = schema.get("properties", {})
        assert "voice" in props
        assert "format" in props
        assert "speed" in props

    def test_source_is_builtin(self) -> None:
        """工具来源应为 builtin。"""
        mod = _import_tool()
        tool_def = mod.TtsGenerateTool.get_tool_definition()
        assert tool_def.source.value == "builtin"

    def test_description_is_non_empty(self) -> None:
        """工具描述非空。"""
        mod = _import_tool()
        tool_def = mod.TtsGenerateTool.get_tool_definition()
        assert len(tool_def.description) > 0


# ========================================================================
# 测试 execute
# ========================================================================


class TestExecute:
    """execute 执行测试。"""

    @pytest.mark.asyncio
    async def test_execute_success_returns_file_path(self, tmp_path: Path) -> None:
        """成功执行时返回包含 file_path 的成功结果。"""
        mod = _import_tool()

        # 创建 mock provider
        mock_provider = AsyncMock()
        mock_provider.is_available.return_value = True
        mock_provider.synthesize.return_value = MediaResult(
            file_path=tmp_path / "output.mp3",
            media_type=MediaType.TTS,
            provider_name="edge_tts",
            metadata={"voice": "zh-CN-XiaoxiaoNeural"},
        )
        # 确保文件存在
        (tmp_path / "output.mp3").write_bytes(b"fake-mp3")

        mock_provider.config = _make_provider_config()
        mock_provider.provider_name = "edge_tts"
        mock_provider.media_type = MediaType.TTS

        # 创建 mock registry
        mock_registry = MagicMock()
        mock_chain = AsyncMock()
        mock_chain.execute_synthesize.return_value = mock_provider.synthesize.return_value
        mock_registry.get_chain_for_type.return_value = mock_chain

        tool = mod.TtsGenerateTool(registry=mock_registry)
        result = await tool.execute(inputs={"text": "你好世界"})

        assert result.success
        assert result.output is not None
        assert "file_path" in result.output or "path" in str(result.output)

    @pytest.mark.asyncio
    async def test_execute_missing_text_returns_failure(self) -> None:
        """缺少 text 参数时返回失败结果。"""
        mod = _import_tool()
        mock_registry = MagicMock()
        tool = mod.TtsGenerateTool(registry=mock_registry)
        result = await tool.execute(inputs={})

        assert result.is_failed
        assert "文本" in (result.error or "") or "不能为空" in (result.error or "")

    @pytest.mark.asyncio
    async def test_execute_provider_failure_returns_failure(self) -> None:
        """Provider 合成失败时返回失败结果。"""
        mod = _import_tool()

        mock_registry = MagicMock()
        mock_chain = AsyncMock()
        mock_chain.execute_synthesize.side_effect = RuntimeError(
            "所有 Provider 均失败: edge_tts: unavailable"
        )
        mock_registry.get_chain_for_type.return_value = mock_chain

        tool = mod.TtsGenerateTool(registry=mock_registry)
        result = await tool.execute(inputs={"text": "测试"})

        assert result.is_failed
