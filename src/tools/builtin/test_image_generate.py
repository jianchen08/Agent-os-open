"""图像生成工具单元测试。

测试覆盖：
- 工具定义（名称、描述、输入输出 schema）
- 空提示词验证
- 尺寸验证
- 数量验证
- Provider 正常生成
- Provider 失败回退
- 无注册表时提示
- 多图像生成
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools.builtin.image_generate import ImageGenerateTool, create_image_generate_tool
from tools.media.base import MediaResult, MediaType


class TestImageGenerateToolDefinition:
    """工具定义测试。"""

    def test_tool_name_is_image_generate(self) -> None:
        tool_def = ImageGenerateTool.get_tool_definition()
        assert tool_def.name == "image_generate"

    def test_description_contains_keywords(self) -> None:
        tool_def = ImageGenerateTool.get_tool_definition()
        assert "图像" in tool_def.description or "生成" in tool_def.description

    def test_input_schema_has_required_prompt(self) -> None:
        tool_def = ImageGenerateTool.get_tool_definition()
        required = tool_def.input_schema.get("required", [])
        assert "prompt" in required

    def test_input_schema_has_optional_params(self) -> None:
        tool_def = ImageGenerateTool.get_tool_definition()
        props = tool_def.input_schema.get("properties", {})
        assert "size" in props
        assert "quality" in props
        assert "style" in props
        assert "n" in props
        assert "seed" in props

    def test_output_schema_has_images_list(self) -> None:
        tool_def = ImageGenerateTool.get_tool_definition()
        props = tool_def.output_schema.get("properties", {})
        assert "images" in props
        assert "count" in props

    def test_category_is_media(self) -> None:
        tool_def = ImageGenerateTool.get_tool_definition()
        assert tool_def.category == "media"

    def test_tags_include_image(self) -> None:
        tool_def = ImageGenerateTool.get_tool_definition()
        assert "image" in tool_def.tags


class TestImageGenerateToolExecute:
    """工具执行测试。"""

    @pytest.mark.asyncio
    async def test_empty_prompt_returns_failure(self) -> None:
        tool = ImageGenerateTool()
        result = await tool.execute({"prompt": ""})
        assert result.is_failed
        assert "不能为空" in result.error

    @pytest.mark.asyncio
    async def test_whitespace_only_prompt_returns_failure(self) -> None:
        tool = ImageGenerateTool()
        result = await tool.execute({"prompt": "   "})
        assert result.is_failed

    @pytest.mark.asyncio
    async def test_unsupported_size_returns_failure(self) -> None:
        tool = ImageGenerateTool()
        result = await tool.execute({"prompt": "风景", "size": "999x999"})
        assert result.is_failed
        assert "不支持" in result.error

    @pytest.mark.asyncio
    async def test_supported_sizes_pass_validation(self) -> None:
        """All declared sizes should pass validation."""
        tool = ImageGenerateTool()
        for size in ("256x256", "512x512", "1024x1024", "1024x1792", "1792x1024"):
            result = await tool.execute({"prompt": "测试", "size": size})
            if result.is_failed:
                assert "尺寸" not in result.error

    @pytest.mark.asyncio
    async def test_count_above_limit_returns_failure(self) -> None:
        tool = ImageGenerateTool()
        result = await tool.execute({"prompt": "风景", "n": 5})
        assert result.is_failed
        assert "数量" in result.error or "范围" in result.error

    @pytest.mark.asyncio
    async def test_count_below_limit_returns_failure(self) -> None:
        tool = ImageGenerateTool()
        result = await tool.execute({"prompt": "风景", "n": 0})
        assert result.is_failed

    @pytest.mark.asyncio
    async def test_no_registry_returns_no_provider(self) -> None:
        tool = ImageGenerateTool()
        result = await tool.execute({"prompt": "一个美丽的日落"})
        assert result.is_failed
        assert "未配置" in result.error or "NO_PROVIDER" in (result.error_code or "")

    @pytest.mark.asyncio
    async def test_registry_generate_single_image(self) -> None:
        registry = MagicMock()
        chain = MagicMock()
        chain.execute_generate = AsyncMock(
            return_value=MediaResult(
                file_path=Path("/tmp/img_out.png"),
                media_type=MediaType.IMAGE,
                metadata={"size": "1024x1024"},
                provider_name="mock-image",
            )
        )
        registry.get_chain_for_type.return_value = chain

        tool = ImageGenerateTool(registry=registry)
        result = await tool.execute({
            "prompt": "一个美丽的日落",
            "size": "1024x1024",
            "n": 1,
        })
        assert result.is_completed
        data = result.output
        assert data["count"] == 1
        assert len(data["images"]) == 1
        assert data["images"][0]["file_path"] == "/tmp/img_out.png"

    @pytest.mark.asyncio
    async def test_registry_generate_multiple_images(self) -> None:
        registry = MagicMock()
        chain = MagicMock()
        chain.execute_generate = AsyncMock(
            return_value=MediaResult(
                file_path=Path("/tmp/img_out.png"),
                media_type=MediaType.IMAGE,
                metadata={"size": "1024x1024"},
                provider_name="mock-image",
            )
        )
        registry.get_chain_for_type.return_value = chain

        tool = ImageGenerateTool(registry=registry)
        result = await tool.execute({
            "prompt": "一个美丽的日落",
            "n": 3,
        })
        assert result.is_completed
        data = result.output
        assert data["count"] == 3
        assert len(data["images"]) == 3

    @pytest.mark.asyncio
    async def test_registry_generate_failure_returns_error(self) -> None:
        registry = MagicMock()
        chain = MagicMock()
        chain.execute_generate = AsyncMock(
            side_effect=RuntimeError("所有 Provider 均失败: mock-image: error")
        )
        registry.get_chain_for_type.return_value = chain

        tool = ImageGenerateTool(registry=registry)
        result = await tool.execute({"prompt": "测试"})
        assert result.is_failed

    @pytest.mark.asyncio
    async def test_seed_param_passed_correctly(self) -> None:
        registry = MagicMock()
        chain = MagicMock()
        chain.execute_generate = AsyncMock(
            return_value=MediaResult(
                file_path=Path("/tmp/img_out.png"),
                media_type=MediaType.IMAGE,
                metadata={},
                provider_name="mock-image",
            )
        )
        registry.get_chain_for_type.return_value = chain

        tool = ImageGenerateTool(registry=registry)
        result = await tool.execute({
            "prompt": "测试",
            "seed": 42,
            "n": 2,
        })
        assert result.is_completed
        call_args_list = chain.execute_generate.call_args_list
        assert call_args_list[0].kwargs.get("seed") == 42
        assert call_args_list[1].kwargs.get("seed") == 43


class TestCreateImageGenerateTool:
    """工厂函数测试。"""

    def test_create_without_registry(self) -> None:
        tool = create_image_generate_tool()
        assert isinstance(tool, ImageGenerateTool)

    def test_create_with_registry(self) -> None:
        registry = MagicMock()
        tool = create_image_generate_tool(registry=registry)
        assert isinstance(tool, ImageGenerateTool)
