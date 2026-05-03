"""多模态能力注册表测试 - 验证新增模型能力注册正确"""

import pytest

from multimodal.capabilities import ModelCapabilityRegistry
from multimodal.adapter import OpenAIVisionAdapter, DefaultAdapter


class TestGLM5Capabilities:
    """GLM-5 系列模型能力"""

    def test_glm51_supports_image(self):
        cap = ModelCapabilityRegistry.get_capability("glm-5.1")
        assert cap.supports_image is True
        assert "image/png" in cap.supported_image_types
        assert cap.max_image_size == 20 * 1024 * 1024

    def test_glm5_turbo_supports_image(self):
        cap = ModelCapabilityRegistry.get_capability("glm-5-turbo")
        assert cap.supports_image is True

    def test_glm51_multimodal_supported(self):
        assert ModelCapabilityRegistry.is_multimodal_supported("glm-5.1") is True

    def test_glm5_turbo_multimodal_supported(self):
        assert ModelCapabilityRegistry.is_multimodal_supported("glm-5-turbo") is True

    def test_glm51_provider_mapping(self):
        adapter = ModelCapabilityRegistry.get_adapter_for_model("glm-5.1")
        assert isinstance(adapter, OpenAIVisionAdapter)

    def test_glm5_turbo_provider_mapping(self):
        adapter = ModelCapabilityRegistry.get_adapter_for_model("glm-5-turbo")
        assert isinstance(adapter, OpenAIVisionAdapter)


class TestMiniMaxCapabilities:
    """MiniMax M2.7 模型能力"""

    def test_minimax_no_image(self):
        cap = ModelCapabilityRegistry.get_capability("MiniMax-M2.7")
        assert cap.supports_image is False
        assert cap.supported_image_types == []

    def test_minimax_not_multimodal(self):
        assert ModelCapabilityRegistry.is_multimodal_supported("MiniMax-M2.7") is False

    def test_minimax_adapter_is_default(self):
        adapter = ModelCapabilityRegistry.get_adapter("minimax")
        assert isinstance(adapter, DefaultAdapter)


class TestDeepSeekStillNoImage:
    """DeepSeek 模型确认仍不支持图片"""

    def test_deepseek_chat_no_image(self):
        assert ModelCapabilityRegistry.is_multimodal_supported("deepseek-chat") is False

    def test_deepseek_adapter_is_default(self):
        adapter = ModelCapabilityRegistry.get_adapter("deepseek")
        assert isinstance(adapter, DefaultAdapter)


class TestExistingModelsUnchanged:
    """确认已有模型注册未被破坏"""

    @pytest.mark.parametrize("model", [
        "gpt-4o", "claude-3-5-sonnet", "glm-4v", "gemini-1.5-pro",
    ])
    def test_still_supports_image(self, model):
        assert ModelCapabilityRegistry.is_multimodal_supported(model) is True

    def test_glm47_no_image(self):
        assert ModelCapabilityRegistry.is_multimodal_supported("glm-4.7") is False

    def test_unknown_model_defaults_no_image(self):
        assert ModelCapabilityRegistry.is_multimodal_supported("nonexistent-model") is False
