"""
思考模型配置服务

提供思考模型的配置管理和参数优化功能

注意：所有模型的思考参数通过 config/models/llm.yaml 的 default_params 透传，
此类保留简化接口，不依赖硬编码模型配置。
"""

import logging
from typing import Any

from src.llm.models import ThinkingModelConfig

logger = logging.getLogger(__name__)


class ThinkingModelService:
    """思考模型配置服务（参数透传模式）"""

    @staticmethod
    def get_model_config(model_name: str) -> ThinkingModelConfig | None:
        """获取模型配置（参数透传模式始终返回 None）"""
        return None

    @staticmethod
    def get_model_info(model_name: str) -> dict[str, Any] | None:
        """获取模型信息字典（参数透传模式始终返回 None）"""
        return None

    @staticmethod
    def get_thinking_mode_type(model_name: str) -> str | None:
        """获取思考模式类型（参数透传模式始终返回 None）"""
        return None

    @staticmethod
    def supports_thinking_mode(model_name: str) -> bool:
        """检查模型是否支持思考模式（参数透传模式始终返回 True）"""
        return True

    @staticmethod
    def get_thinking_config(
        model_name: str, enable_thinking: bool = True
    ) -> tuple[str, dict[str, Any]]:
        """获取思考模式配置（参数透传模式直接返回原始参数）"""
        return model_name, {}

    @staticmethod
    def get_base_model_for_thinking(thinking_model: str) -> str | None:
        """根据思考模型获取对应的基础模型"""
        return None

    @staticmethod
    def get_thinking_model_for_base(base_model: str) -> str | None:
        """根据基础模型获取对应的思考模型"""
        return None

    @staticmethod
    def supports_reasoning_effort(model_name: str) -> bool:
        """检查模型是否支持 reasoning_effort 参数"""
        return False

    @staticmethod
    def supports_tools(model_name: str) -> bool:
        """检查思考模型是否支持工具调用"""
        return True

    @staticmethod
    def get_optimal_params(
        model_name: str, task_type: str = "general", complexity: str = "medium"
    ) -> dict[str, Any]:
        """获取最优参数配置（参数透传模式返回空字典）"""
        return {}

    @staticmethod
    def validate_params(model_name: str, params: dict[str, Any]) -> dict[str, Any]:
        """验证和修正参数（参数透传模式直接返回）"""
        return params

    @staticmethod
    def get_usage_recommendations(model_name: str) -> dict[str, Any]:
        """获取使用建议（参数透传模式返回空字典）"""
        return {}

    @staticmethod
    def get_all_supported_models() -> dict[str, ThinkingModelConfig]:
        """获取所有支持的思考模型（参数透传模式返回空字典）"""
        return {}


def get_thinking_model_service() -> ThinkingModelService:
    """获取思考模型服务实例"""
    return ThinkingModelService()
