"""多模态能力注册表测试 - 验证配置驱动的能力查询"""

import pytest

from multimodal.capabilities import ModelCapabilityRegistry
from multimodal.adapter import OpenAIVisionAdapter, DefaultAdapter


# 测试用 LLM 配置（镜像 llm.yaml 中实际配置的多模态模型）
TEST_LLM_CONFIG = {
    "models": {
        "glm-5.2": {
            "provider": "zhipu_coding",
            "model_name": "glm-5.2",
            "display_name": "GLM-5.2",
            "context_window": 200000,
            "multimodal": {
                "supports_image": True,
                "supported_image_types": [
                    "image/jpeg", "image/png", "image/gif", "image/webp",
                ],
                "max_image_size": 20 * 1024 * 1024,
            },
        },
        "minimax-m3": {
            "provider": "minimax",
            "model_name": "MiniMax-M3",
            "display_name": "MiniMax-M3",
            "multimodal": {
                "supports_image": True,
                "supports_video": True,
                "supported_image_types": [
                    "image/jpeg", "image/png", "image/gif", "image/webp",
                ],
                "max_image_size": 20 * 1024 * 1024,
                "max_video_size": 100 * 1024 * 1024,
            },
        },
        "deepseek-v4-flash": {
            "provider": "deepseek",
            "model_name": "deepseek-v4-flash",
            "display_name": "DeepSeek V4 Flash",
        },
    },
    "defaults": {"chat": "glm-5.2"},
}


@pytest.fixture
def llm_config(monkeypatch):
    """注入测试用 LLM 配置，隔离真实文件加载。"""
    from src.config import llm_config as lc_module
    mgr = lc_module.LLMConfigManager(config=TEST_LLM_CONFIG)
    monkeypatch.setattr(lc_module, "get_llm_config", lambda: mgr)
    return mgr


class TestGLM52Capability:
    """GLM-5.2 能力（从 llm.yaml multimodal 配置读取）"""

    def test_supports_image(self, llm_config):
        cap = ModelCapabilityRegistry.get_capability("glm-5.2")
        assert cap.supports_image is True
        assert "image/png" in cap.supported_image_types
        assert cap.max_image_size == 20 * 1024 * 1024

    def test_multimodal_supported(self, llm_config):
        assert ModelCapabilityRegistry.is_multimodal_supported("glm-5.2") is True

    def test_no_video(self, llm_config):
        cap = ModelCapabilityRegistry.get_capability("glm-5.2")
        assert cap.supports_video is False


class TestMiniMaxM3Capability:
    """MiniMax-M3 支持 图片+视频，且 alias/model_name 双查都能命中"""

    def test_supports_image_and_video_by_alias(self, llm_config):
        cap = ModelCapabilityRegistry.get_capability("minimax-m3")
        assert cap.supports_image is True
        assert cap.supports_video is True
        assert cap.max_video_size == 100 * 1024 * 1024

    def test_lookup_by_model_name(self, llm_config):
        """消费方可能传 model_name（MiniMax-M3）而非 alias（minimax-m3）"""
        cap = ModelCapabilityRegistry.get_capability("MiniMax-M3")
        assert cap.supports_image is True

    def test_multimodal_supported(self, llm_config):
        assert ModelCapabilityRegistry.is_multimodal_supported("minimax-m3") is True


class TestDeepSeekNoImage:
    """DeepSeek 未配 multimodal，默认不支持图片"""

    def test_not_multimodal(self, llm_config):
        assert ModelCapabilityRegistry.is_multimodal_supported("deepseek-v4-flash") is False

    def test_adapter_is_default(self, llm_config):
        adapter = ModelCapabilityRegistry.get_adapter("deepseek")
        assert isinstance(adapter, DefaultAdapter)


class TestUnknownModel:
    """未知模型回退默认空能力"""

    def test_not_multimodal(self, llm_config):
        assert ModelCapabilityRegistry.is_multimodal_supported("nonexistent-model") is False

    def test_capability_defaults(self, llm_config):
        cap = ModelCapabilityRegistry.get_capability("nonexistent-model")
        assert cap.supports_image is False
        assert cap.supported_image_types == []


class TestAdapterForModel:
    """get_adapter_for_model 通过 provider 映射到适配器"""

    def test_glm52_adapter(self, llm_config, monkeypatch):
        from src.llm import router_factory
        monkeypatch.setattr(router_factory, "get_provider_for_model", lambda m: "zhipu_coding")
        adapter = ModelCapabilityRegistry.get_adapter_for_model("glm-5.2")
        assert isinstance(adapter, OpenAIVisionAdapter)

    def test_deepseek_adapter(self, llm_config, monkeypatch):
        from src.llm import router_factory
        monkeypatch.setattr(router_factory, "get_provider_for_model", lambda m: "deepseek")
        adapter = ModelCapabilityRegistry.get_adapter_for_model("deepseek-v4-flash")
        assert isinstance(adapter, DefaultAdapter)
