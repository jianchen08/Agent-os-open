"""工具 multimodal_content 元数据字段输出测试

验证工具（image_generate、playwright_test 等）的输出结果中
metadata.multimodal_content 字段的正确性。

覆盖场景：
- ToolExecutionResult metadata 包含 multimodal_content
- slim 模式排除 multimodal_content（不污染 LLM 上下文）
- multimodal_content 格式验证（OpenAI / Claude 格式）
- 边界：空列表、None metadata、多个内容块
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from core.results.tool import ToolExecutionResult


class TestMultimodalContentInToolResult:
    """工具结果中 multimodal_content 字段。"""

    def test_success_result_with_multimodal_content(self):
        """成功结果携带 multimodal_content。"""
        result = ToolExecutionResult.create_completed(
            output="图片生成成功",
            metadata={
                "action": "image_generate",
                "media_type": "image",
                "multimodal_content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR"}},
                ],
            },
        )
        assert result.success is True
        assert result.metadata is not None
        mm = result.metadata.get("multimodal_content")
        assert mm is not None, "multimodal_content 不应为 None"
        assert len(mm) == 1
        assert mm[0]["type"] == "image_url"

    def test_result_without_multimodal_content(self):
        """无 multimodal_content 的结果正常。"""
        result = ToolExecutionResult.create_completed(
            output="任务完成",
            metadata={"action": "file_write"},
        )
        assert result.success is True
        assert result.metadata.get("multimodal_content") is None

    def test_multiple_items_in_multimodal_content(self):
        """multimodal_content 可含多个内容块。"""
        result = ToolExecutionResult.create_completed(
            output="生成 3 张图片",
            metadata={
                "multimodal_content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,BBB"}},
                    {"type": "image_url", "image_url": {"url": "data:image/webp;base64,CCC"}},
                ],
            },
        )
        mm = result.metadata["multimodal_content"]
        assert len(mm) == 3

    def test_claude_format_source(self):
        """Claude 格式 source 类型。"""
        result = ToolExecutionResult.create_completed(
            output="截图",
            metadata={
                "multimodal_content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "iVBORw0KGgo=",
                        },
                    },
                ],
            },
        )
        mm = result.metadata["multimodal_content"]
        assert mm[0]["source"]["type"] == "base64"
        assert "data" in mm[0]["source"]

    def test_empty_multimodal_content_list(self):
        """空列表。"""
        result = ToolExecutionResult.create_completed(
            output="no images",
            metadata={"multimodal_content": []},
        )
        assert result.metadata["multimodal_content"] == []

    def test_result_to_dict_preserves_metadata(self):
        """to_dict() 保留 metadata。"""
        result = ToolExecutionResult.create_completed(
            output="ok",
            metadata={"key": "value", "multimodal_content": [{"x": 1}]},
        )
        d = result.to_dict()
        assert d["metadata"]["key"] == "value"
        assert "multimodal_content" in d["metadata"]


class TestSlimModeExclusion:
    """slim 模式排除大体积字段。"""

    def test_slim_excludes_multimodal_content(self):
        """slim 模式下 multimodal_content 不面向 LLM。"""
        result = ToolExecutionResult.create_completed(
            output="这是给 LLM 看的文本",
            metadata={
                "action": "image_generate",
                "media_type": "image",
                "multimodal_content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,VERY_LONG_BASE64_DATA"}},
                ],
                "hint": "图片已保存",
            },
        )
        slim = result.to_dict(slim=True)
        # slim 模式下 metadata 不包含 multimodal_content 和 action
        assert "multimodal_content" not in slim.get("metadata", {})
        assert "action" not in slim.get("metadata", {})
        # 但 hint 应该保留
        assert slim["metadata"]["hint"] == "图片已保存"

    def test_slim_mode_preserves_output(self):
        """slim 模式保留 output。"""
        result = ToolExecutionResult.create_completed(
            output="LLM 需要的输出",
            metadata={"multimodal_content": [{"x": 1}]},
        )
        slim = result.to_dict(slim=True)
        assert slim["output"] == "LLM 需要的输出"


class TestEdgeCases:
    """边界场景。"""

    def test_none_metadata_default(self):
        """不传 metadata 默认为空 dict。"""
        result = ToolExecutionResult.create_completed(output="test")
        assert result.metadata == {}

    def test_to_dict_full_mode_includes_all(self):
        """完整模式包含所有字段。"""
        result = ToolExecutionResult.create_completed(
            output="test",
            tool_name="image_generate",
            metadata={"multimodal_content": [{"type": "image_url"}]},
        )
        d = result.to_dict()
        assert d["status"] == "completed"
        assert d["success"] is True
        assert d["output"] == "test"
        assert "multimodal_content" in d["metadata"]
        assert d["tool_name"] == "image_generate"

    def test_slim_strips_tool_metadata(self):
        """slim 模式去除 tool_name/tool_id/input_params。"""
        result = ToolExecutionResult.create_completed(
            output="test",
            tool_name="image_generate",
            tool_id="tool-001",
            input_params={"prompt": "cat"},
            metadata={"multimodal_content": [{"type": "image_url"}]},
        )
        slim = result.to_dict(slim=True)
        assert "tool_name" not in slim
        assert "tool_id" not in slim
        assert "input_params" not in slim
