"""
思考模型配置服务

提供思考模型的配置管理和参数优化功能
"""

import logging
from typing import Any

from src.llm.models import (
    REASONING_EFFORT_LEVELS,
    SUPPORTED_THINKING_MODELS,
    ThinkingModelConfig,
)

logger = logging.getLogger(__name__)


class ThinkingModelService:
    """思考模型配置服务"""

    @staticmethod
    def get_model_config(model_name: str) -> ThinkingModelConfig | None:
        """
        获取模型配置（支持模糊匹配和基础模型查找）

        Args:
            model_name: 模型名称

        Returns:
            模型配置，如果模型不存在则返回 None
        """
        # 精确匹配
        if model_name in SUPPORTED_THINKING_MODELS:
            return SUPPORTED_THINKING_MODELS[model_name]

        # 模糊匹配（支持模型名称的变体，如 "glm-4.7" 匹配 "GLM-4.7"）
        model_lower = model_name.lower()
        for supported_model, config in SUPPORTED_THINKING_MODELS.items():
            if supported_model.lower() == model_lower:
                return config
            # 部分匹配（如 "glm-4" 匹配 "glm-4.7"）
            if (
                supported_model.lower() in model_lower
                or model_lower in supported_model.lower()
            ):
                return config

        # 基础模型查找：如果 model_name 是某个思考模型的 base_model，返回该配置
        for supported_model, config in SUPPORTED_THINKING_MODELS.items():
            if config.base_model == model_name:
                return config
            # 不区分大小写的基础模型匹配
            if config.base_model.lower() == model_lower:
                return config

        return None

    @staticmethod
    def get_model_info(model_name: str) -> dict[str, Any] | None:
        """
        获取模型信息字典（兼容旧接口）

        Args:
            model_name: 模型名称

        Returns:
            模型信息字典，如果模型不存在则返回 None
        """
        config = ThinkingModelService.get_model_config(model_name)
        if config:
            return config.model_dump()
        return None

    @staticmethod
    def get_thinking_mode_type(model_name: str) -> str | None:
        """
        获取思考模式类型

        Args:
            model_name: 模型名称

        Returns:
            思考模式类型: "model_switch" 或 "parameter_switch"，如果不支持则返回 None
        """
        config = ThinkingModelService.get_model_config(model_name)
        return config.thinking_mode_type if config else None

    @staticmethod
    def supports_thinking_mode(model_name: str) -> bool:
        """
        检查模型是否支持思考模式

        Args:
            model_name: 模型名称

        Returns:
            是否支持思考模式
        """
        return ThinkingModelService.get_thinking_mode_type(model_name) is not None

    @staticmethod
    def get_thinking_config(
        model_name: str, enable_thinking: bool = True
    ) -> tuple[str, dict[str, Any]]:
        """
        获取思考模式配置

        Args:
            model_name: 基础模型名称
            enable_thinking: 是否启用思考模式

        Returns:
            (实际使用的模型名称, 参数配置)
        """
        config = ThinkingModelService.get_model_config(model_name)
        if not config:
            return model_name, {}

        thinking_type = config.thinking_mode_type

        if not enable_thinking:
            # 关闭思考模式，使用基础模型
            if thinking_type == "model_switch":
                return config.base_model, config.normal_params
            elif thinking_type == "parameter_switch":
                return model_name, config.normal_params
            else:
                return model_name, {}

        # 启用思考模式
        if thinking_type == "model_switch":
            # 模型切换型：切换到思考模型
            return config.thinking_model, config.thinking_params
        elif thinking_type == "parameter_switch":
            # 参数切换型：同一模型，不同参数
            return model_name, config.thinking_params
        else:
            return model_name, {}

    @staticmethod
    def get_base_model_for_thinking(thinking_model: str) -> str | None:
        """
        根据思考模型获取对应的基础模型

        Args:
            thinking_model: 思考模型名称

        Returns:
            基础模型名称，如果找不到则返回 None
        """
        for config in SUPPORTED_THINKING_MODELS.values():
            if config.thinking_model == thinking_model:
                return config.base_model
        return None

    @staticmethod
    def get_thinking_model_for_base(base_model: str) -> str | None:
        """
        根据基础模型获取对应的思考模型

        Args:
            base_model: 基础模型名称

        Returns:
            思考模型名称，如果找不到则返回 None
        """
        for config in SUPPORTED_THINKING_MODELS.values():
            if config.base_model == base_model:
                return config.thinking_model
        return None

    @staticmethod
    def supports_reasoning_effort(model_name: str) -> bool:
        """
        检查模型是否支持 reasoning_effort 参数

        Args:
            model_name: 模型名称

        Returns:
            是否支持 reasoning_effort
        """
        config = ThinkingModelService.get_model_config(model_name)
        return config.supports_reasoning_effort if config else False

    @staticmethod
    def supports_tools(model_name: str) -> bool:
        """
        检查思考模型是否支持工具调用

        Args:
            model_name: 模型名称

        Returns:
            是否支持工具调用
        """
        config = ThinkingModelService.get_model_config(model_name)
        return config.supports_tools if config else False

    @staticmethod
    def get_optimal_params(
        model_name: str, task_type: str = "general", complexity: str = "medium"
    ) -> dict[str, Any]:
        """
        获取最优参数配置

        Args:
            model_name: 模型名称
            task_type: 任务类型 (math, coding, analysis, creative, general)
            complexity: 复杂度 (simple, medium, complex)

        Returns:
            最优参数配置
        """
        config = ThinkingModelService.get_model_config(model_name)
        if not config:
            return {}

        params = {}

        # 根据任务类型和复杂度设置 reasoning_effort
        if config.supports_reasoning_effort:
            if task_type in ["math", "coding"] and complexity == "complex":
                params["reasoning_effort"] = "high"
            elif task_type == "creative" or complexity == "simple":
                params["reasoning_effort"] = "low"
            else:
                params["reasoning_effort"] = "medium"

        # 根据模型类型设置其他参数
        provider = config.provider

        if provider == "deepseek_reasoning":
            params.update(
                {
                    "temperature": 0.7 if task_type == "creative" else 0.3,
                    "max_tokens": 8192 if complexity == "complex" else 4096,
                }
            )
        elif provider == "openai_reasoning":
            params.update(
                {
                    "max_completion_tokens": (
                        32768 if complexity == "complex" else 16384
                    ),
                }
            )
        elif provider == "anthropic_reasoning":
            params.update(
                {
                    "temperature": 0.7 if task_type == "creative" else 0.3,
                    "max_tokens": 8192 if complexity == "complex" else 4096,
                }
            )

        return params

    @staticmethod
    def validate_params(model_name: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        验证和修正参数

        Args:
            model_name: 模型名称
            params: 参数字典

        Returns:
            验证后的参数字典
        """
        config = ThinkingModelService.get_model_config(model_name)
        if not config:
            logger.warning(f"未知的思考模型: {model_name}")
            return params

        validated_params = params.copy()

        # 验证 reasoning_effort
        if "reasoning_effort" in validated_params:
            if not config.supports_reasoning_effort:
                logger.warning(
                    f"模型 {model_name} 不支持 reasoning_effort 参数，已移除"
                )
                validated_params.pop("reasoning_effort")
            elif validated_params["reasoning_effort"] not in REASONING_EFFORT_LEVELS:
                logger.warning("无效的 reasoning_effort 值，使用默认值 'medium'")
                validated_params["reasoning_effort"] = "medium"

        # 验证 token 限制
        max_context = config.max_context

        if "max_tokens" in validated_params:
            if validated_params["max_tokens"] > max_context:
                logger.warning(f"max_tokens 超过模型限制，调整为 {max_context}")
                validated_params["max_tokens"] = max_context

        if "max_completion_tokens" in validated_params:
            if validated_params["max_completion_tokens"] > max_context:
                logger.warning(
                    f"max_completion_tokens 超过模型限制，调整为 {max_context}"
                )
                validated_params["max_completion_tokens"] = max_context

        return validated_params

    @staticmethod
    def get_usage_recommendations(model_name: str) -> dict[str, Any]:
        """
        获取使用建议

        Args:
            model_name: 模型名称

        Returns:
            使用建议
        """
        config = ThinkingModelService.get_model_config(model_name)
        if not config:
            return {}

        provider = config.provider

        recommendations = {
            "best_for": [],
            "avoid_for": [],
            "tips": [],
        }

        if provider == "deepseek_reasoning":
            recommendations.update(
                {
                    "best_for": ["数学问题", "逻辑推理", "代码分析", "开源友好"],
                    "avoid_for": ["实时对话", "简单问答"],
                    "tips": [
                        "支持中文思考过程",
                        "适合复杂推理任务",
                        "响应时间较长，请耐心等待",
                    ],
                }
            )
        elif provider == "openai_reasoning":
            recommendations.update(
                {
                    "best_for": ["科学计算", "复杂编程", "学术研究"],
                    "avoid_for": ["创意写作", "日常聊天"],
                    "tips": [
                        "使用 reasoning_effort 控制思考深度",
                        "高质量推理但成本较高",
                        "适合需要严谨逻辑的任务",
                    ],
                }
            )
        elif provider == "anthropic_reasoning":
            recommendations.update(
                {
                    "best_for": ["分析写作", "道德推理", "复杂决策"],
                    "avoid_for": ["数学计算", "代码生成"],
                    "tips": [
                        "擅长多角度分析",
                        "注重安全和道德考量",
                        "适合需要平衡考虑的场景",
                    ],
                }
            )

        return recommendations

    @staticmethod
    def get_all_supported_models() -> dict[str, ThinkingModelConfig]:
        """
        获取所有支持的思考模型

        Returns:
            所有支持的思考模型字典
        """
        return SUPPORTED_THINKING_MODELS.copy()


def get_thinking_model_service() -> ThinkingModelService:
    """获取思考模型服务实例"""
    return ThinkingModelService()
