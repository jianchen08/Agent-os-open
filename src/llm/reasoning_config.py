"""
思考模型配置管理

提供思考模型的配置管理和参数优化功能

注意：所有模型的思考参数通过 config/models/llm.yaml 的 default_params 透传，
此类保留空配置字典，仅提供配置管理接口。
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ReasoningConfig:
    """思考模型配置类"""

    # 支持的思考模型列表（参数透传，保留空字典）
    SUPPORTED_MODELS: dict[str, dict[str, Any]] = {}

    # reasoning_effort 参数映射（参数透传模式）
    REASONING_EFFORT_LEVELS: dict[str, dict[str, Any]] = {}

    @classmethod
    def get_model_info(cls, model_name: str) -> dict[str, Any] | None:
        """获取模型信息"""
        if model_name in cls.SUPPORTED_MODELS:
            return cls.SUPPORTED_MODELS[model_name]
        return None

    @classmethod
    def get_thinking_mode_type(cls, model_name: str) -> str | None:
        """获取思考模式类型"""
        model_info = cls.get_model_info(model_name)
        return model_info.get("thinking_mode_type") if model_info else None

    @classmethod
    def supports_thinking_mode(cls, model_name: str) -> bool:
        """检查模型是否支持思考模式"""
        return cls.get_thinking_mode_type(model_name) is not None

    @classmethod
    def get_thinking_config(
        cls, model_name: str, enable_thinking: bool = True  # noqa: ARG003
    ) -> tuple[str, dict[str, Any]]:
        """获取思考模式配置"""
        return model_name, {}

    @classmethod
    def get_base_model_for_thinking(cls, thinking_model: str) -> str | None:  # noqa: ARG003
        """根据思考模型获取对应的基础模型"""
        return None

    @classmethod
    def get_thinking_model_for_base(cls, base_model: str) -> str | None:  # noqa: ARG003
        """根据基础模型获取对应的思考模型"""
        return None

    @classmethod
    def supports_reasoning_effort(cls, model_name: str) -> bool:  # noqa: ARG003
        """检查模型是否支持 reasoning_effort 参数"""
        return False

    @classmethod
    def supports_tools(cls, model_name: str) -> bool:  # noqa: ARG003
        """检查思考模型是否支持工具调用"""
        return False

    @classmethod
    def get_optimal_params(
        cls, model_name: str, task_type: str = "general", complexity: str = "medium"  # noqa: ARG003
    ) -> dict[str, Any]:
        """获取最优参数配置"""
        return {}

    @classmethod
    def validate_params(cls, model_name: str, params: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG003
        """验证和修正参数"""
        return params

    @classmethod
    def get_usage_recommendations(cls, model_name: str) -> dict[str, Any]:  # noqa: ARG003
        """获取使用建议"""
        return {}


def get_reasoning_config() -> ReasoningConfig:
    """获取思考模型配置实例"""
    return ReasoningConfig()
