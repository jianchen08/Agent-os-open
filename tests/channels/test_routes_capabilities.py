"""capabilities 端点测试。

验证 /api/v1/files/capabilities 端点接真实能力源后：
- 按模型返回真实多模态能力（image/audio/video）
- 不再返回 document/code 字段（文本无需声明能力）
- is_multimodal 正确计算
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

# 确保 src 在 sys.path 中
_src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if _src not in sys.path:
    sys.path.insert(0, os.path.abspath(_src))

from multimodal.types import ModelCapability  # noqa: E402


@pytest.fixture(autouse=True)
def _mock_registry():
    """注入测试用模型能力，隔离真实 llm.yaml 加载。"""
    capabilities = {
        "glm-5.2": ModelCapability(
            model_name="glm-5.2",
            supports_image=True,
            supported_image_types=["image/jpeg", "image/png"],
            max_image_size=20 * 1024 * 1024,
            supported_audio_types=[],
            supported_video_types=[],
        ),
        "minimax-m3": ModelCapability(
            model_name="minimax-m3",
            supports_image=True,
            supported_image_types=["image/jpeg"],
            max_image_size=20 * 1024 * 1024,
            supports_video=True,
            supported_video_types=["video/mp4"],
            max_video_size=100 * 1024 * 1024,
            supported_audio_types=[],
        ),
        "deepseek-v4-flash": ModelCapability(model_name="deepseek-v4-flash"),
    }

    def fake_get_capability(model_name: str) -> ModelCapability:
        return capabilities.get(model_name, ModelCapability(model_name=model_name))

    with patch(
        "multimodal.capabilities.ModelCapabilityRegistry.get_capability",
        side_effect=fake_get_capability,
    ):
        yield


@pytest.mark.asyncio
class TestCapabilitiesEndpoint:
    """capabilities 端点接真实能力源后的行为。"""

    async def test_image_model_returns_image_capability(self) -> None:
        """支持图片的模型返回 image 能力。"""
        from channels.api.routes_missing import get_model_file_capabilities

        result = await get_model_file_capabilities(model_name="glm-5.2")

        assert result["model_name"] == "glm-5.2"
        assert result["supports_image"] is True
        assert "image/jpeg" in result["supported_image_types"]
        assert result["max_image_size"] == 20 * 1024 * 1024
        assert result["is_multimodal"] is True

    async def test_no_document_code_fields(self) -> None:
        """端点不再返回 document/code 字段（文本无需声明能力）。"""
        from channels.api.routes_missing import get_model_file_capabilities

        result = await get_model_file_capabilities(model_name="glm-5.2")

        # 文本相关维度已删除
        assert "supports_document" not in result
        assert "supported_document_types" not in result
        assert "supports_code" not in result
        assert "supported_code_types" not in result
        assert "max_document_size" not in result
        assert "max_code_size" not in result

    async def test_video_model_returns_video_capability(self) -> None:
        """支持视频的模型返回 video 能力。"""
        from channels.api.routes_missing import get_model_file_capabilities

        result = await get_model_file_capabilities(model_name="minimax-m3")

        assert result["supports_image"] is True
        assert result["supports_video"] is True
        assert "video/mp4" in result["supported_video_types"]
        assert result["max_video_size"] == 100 * 1024 * 1024

    async def test_text_only_model_all_false(self) -> None:
        """纯文本模型（无多模态）返回全 False，但仍可发文本附件。"""
        from channels.api.routes_missing import get_model_file_capabilities

        result = await get_model_file_capabilities(model_name="deepseek-v4-flash")

        assert result["supports_image"] is False
        assert result["supports_audio"] is False
        assert result["supports_video"] is False
        assert result["is_multimodal"] is False
        assert result["supported_image_types"] == []

    async def test_different_models_return_different_capabilities(self) -> None:
        """不同模型返回差异化能力（验证非硬编码）。"""
        from channels.api.routes_missing import get_model_file_capabilities

        glm = await get_model_file_capabilities(model_name="glm-5.2")
        minimax = await get_model_file_capabilities(model_name="minimax-m3")

        # glm-5.2 不支持视频，minimax-m3 支持 → 差异化
        assert glm["supports_video"] is False
        assert minimax["supports_video"] is True
