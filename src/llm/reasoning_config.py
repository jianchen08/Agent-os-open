"""
思考模型配置管理

提供思考模型的配置管理和参数优化功能
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ReasoningConfig:
    """思考模型配置类"""

    # 支持的思考模型列表
    SUPPORTED_MODELS = {
        # === 模型切换型思考模式 ===
        "deepseek-reasoner": {
            "provider": "deepseek_reasoning",
            "display_name": "DeepSeek R1 思考模型",
            "supports_reasoning_effort": False,
            "supports_cot": True,
            "supports_tools": True,  # DeepSeek R1 支持工具调用
            "max_context": 64000,
            "thinking_mode_type": "model_switch",  # 需要切换模型
            "base_model": "deepseek-chat",  # 基础模型
            "thinking_model": "deepseek-reasoner",  # 思考模型
        },
        "o1": {
            "provider": "openai_reasoning",
            "display_name": "OpenAI o1 思考模型",
            "supports_reasoning_effort": True,
            "supports_cot": True,
            "supports_tools": True,  # OpenAI o1 支持工具调用
            "max_context": 200000,
            "thinking_mode_type": "model_switch",
            "base_model": "gpt-4o",
            "thinking_model": "o1",
        },
        "o1-mini": {
            "provider": "openai_reasoning",
            "display_name": "OpenAI o1-mini 思考模型",
            "supports_reasoning_effort": True,
            "supports_cot": True,
            "supports_tools": True,  # OpenAI o1-mini 支持工具调用
            "max_context": 128000,
            "thinking_mode_type": "model_switch",
            "base_model": "gpt-4o-mini",
            "thinking_model": "o1-mini",
        },
        "o3-mini": {
            "provider": "openai_reasoning",
            "display_name": "OpenAI o3-mini 思考模型",
            "supports_reasoning_effort": True,
            "supports_cot": True,
            "supports_tools": True,  # OpenAI o3-mini 支持工具调用
            "max_context": 200000,
            "thinking_mode_type": "model_switch",
            "base_model": "gpt-4o",
            "thinking_model": "o3-mini",
        },
        "claude-3-7-sonnet": {
            "provider": "anthropic_reasoning",
            "display_name": "Claude 3.7 Sonnet 思考模式",
            "supports_reasoning_effort": True,
            "supports_cot": True,
            "supports_tools": True,  # Claude 3.7 Sonnet 支持工具调用
            "max_context": 200000,
            "thinking_mode_type": "model_switch",
            "base_model": "claude-3-5-sonnet",
            "thinking_model": "claude-3-7-sonnet-20241022",
        },
        "claude-3-7-sonnet-20241022": {
            "provider": "anthropic_reasoning",
            "display_name": "Claude 3.7 Sonnet 思考模式",
            "supports_reasoning_effort": True,
            "supports_cot": True,
            "max_context": 200000,
            "thinking_mode_type": "model_switch",
            "base_model": "claude-3-5-sonnet",
            "thinking_model": "claude-3-7-sonnet-20241022",
        },
        # === 参数切换型思考模式 ===
        "glm-4.7": {
            "provider": "zhipu_coding",
            "display_name": "GLM-4.7 (支持思考模式)",
            "supports_reasoning_effort": False,
            "supports_cot": True,
            "max_context": 128000,
            "thinking_mode_type": "parameter_switch",  # 通过参数切换
            "base_model": "glm-4.7",  # 同一个模型
            "thinking_model": "glm-4.7",  # 同一个模型
            "thinking_params": {
                "thinking": {"type": "enabled"},  # 智谱 API 格式
                "temperature": 1.0,  # 思考模式推荐温度
                "max_tokens": 65536,  # 思考模式最大 tokens
            },
            "normal_params": {
                "temperature": 0.7,
            },
        },
    }

    # reasoning_effort 参数映射
    REASONING_EFFORT_LEVELS = {
        "low": {
            "description": "低思考强度 - 快速响应，较少思考步骤",
            "use_cases": ["简单问答", "基础总结", "快速决策"],
            "cost_multiplier": 0.5,
        },
        "medium": {
            "description": "中等思考强度 - 平衡的思考深度和响应速度",
            "use_cases": ["一般分析", "代码调试", "逻辑推理"],
            "cost_multiplier": 1.0,
        },
        "high": {
            "description": "高思考强度 - 深度思考，复杂问题解决",
            "use_cases": ["复杂数学", "深度分析", "创新设计"],
            "cost_multiplier": 2.0,
        },
    }

    @classmethod
    def get_model_info(cls, model_name: str) -> dict[str, Any] | None:
        """
        获取模型信息（支持模糊匹配和基础模型查找）

        Args:
            model_name: 模型名称

        Returns:
            模型信息字典，如果模型不存在则返回 None
        """
        # 精确匹配
        if model_name in cls.SUPPORTED_MODELS:
            return cls.SUPPORTED_MODELS[model_name]

        # 模糊匹配（支持模型名称的变体，如 "glm-4.7" 匹配 "GLM-4.7"）
        model_lower = model_name.lower()
        for supported_model, info in cls.SUPPORTED_MODELS.items():
            if supported_model.lower() == model_lower:
                return info
            # 部分匹配（如 "glm-4" 匹配 "glm-4.7"）
            if (
                supported_model.lower() in model_lower
                or model_lower in supported_model.lower()
            ):
                return info

        # 基础模型查找：如果 model_name 是某个思考模型的 base_model，返回该配置
        for supported_model, info in cls.SUPPORTED_MODELS.items():
            if info.get("base_model") == model_name:
                return info
            # 不区分大小写的基础模型匹配
            if info.get("base_model", "").lower() == model_lower:
                return info

        return None

    @classmethod
    def get_thinking_mode_type(cls, model_name: str) -> str | None:
        """
        获取思考模式类型

        Args:
            model_name: 模型名称

        Returns:
            思考模式类型: "model_switch" 或 "parameter_switch"，如果不支持则返回 None
        """
        model_info = cls.get_model_info(model_name)
        return model_info.get("thinking_mode_type") if model_info else None

    @classmethod
    def supports_thinking_mode(cls, model_name: str) -> bool:
        """
        检查模型是否支持思考模式

        Args:
            model_name: 模型名称

        Returns:
            是否支持思考模式
        """
        return cls.get_thinking_mode_type(model_name) is not None

    @classmethod
    def get_thinking_config(
        cls, model_name: str, enable_thinking: bool = True
    ) -> tuple[str, dict[str, Any]]:
        """
        获取思考模式配置

        Args:
            model_name: 基础模型名称
            enable_thinking: 是否启用思考模式

        Returns:
            (实际使用的模型名称, 参数配置)
        """
        model_info = cls.get_model_info(model_name)
        if not model_info:
            return model_name, {}

        thinking_type = model_info.get("thinking_mode_type")

        if not enable_thinking:
            # 关闭思考模式，使用基础模型
            if thinking_type == "model_switch":
                return model_info["base_model"], model_info.get("normal_params", {})
            elif thinking_type == "parameter_switch":
                return model_name, model_info.get("normal_params", {})
            else:
                return model_name, {}

        # 启用思考模式
        if thinking_type == "model_switch":
            # 模型切换型：切换到思考模型
            return model_info["thinking_model"], model_info.get("thinking_params", {})
        elif thinking_type == "parameter_switch":
            # 参数切换型：同一模型，不同参数
            return model_name, model_info.get("thinking_params", {})
        else:
            return model_name, {}

    @classmethod
    def get_base_model_for_thinking(cls, thinking_model: str) -> str | None:
        """
        根据思考模型获取对应的基础模型

        Args:
            thinking_model: 思考模型名称

        Returns:
            基础模型名称，如果找不到则返回 None
        """
        for _model_name, config in cls.SUPPORTED_MODELS.items():
            if config.get("thinking_model") == thinking_model:
                return config.get("base_model")
        return None

    @classmethod
    def get_thinking_model_for_base(cls, base_model: str) -> str | None:
        """
        根据基础模型获取对应的思考模型

        Args:
            base_model: 基础模型名称

        Returns:
            思考模型名称，如果找不到则返回 None
        """
        for _model_name, config in cls.SUPPORTED_MODELS.items():
            if config.get("base_model") == base_model:
                return config.get("thinking_model")
        return None

    @classmethod
    def supports_reasoning_effort(cls, model_name: str) -> bool:
        """
        检查模型是否支持 reasoning_effort 参数

        Args:
            model_name: 模型名称

        Returns:
            是否支持 reasoning_effort
        """
        model_info = cls.get_model_info(model_name)
        return (
            model_info.get("supports_reasoning_effort", False) if model_info else False
        )

    @classmethod
    def supports_tools(cls, model_name: str) -> bool:
        """
        检查思考模型是否支持工具调用

        Args:
            model_name: 模型名称

        Returns:
            是否支持工具调用
        """
        model_info = cls.get_model_info(model_name)
        return model_info.get("supports_tools", False) if model_info else False

    @classmethod
    def get_optimal_params(
        cls, model_name: str, task_type: str = "general", complexity: str = "medium"
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
        model_info = cls.get_model_info(model_name)
        if not model_info:
            return {}

        params = {}

        # 根据任务类型和复杂度设置 reasoning_effort
        if model_info.get("supports_reasoning_effort"):
            if task_type in ["math", "coding"] and complexity == "complex":
                params["reasoning_effort"] = "high"
            elif task_type == "creative" or complexity == "simple":
                params["reasoning_effort"] = "low"
            else:
                params["reasoning_effort"] = "medium"

        # 根据模型类型设置其他参数
        provider = model_info["provider"]

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

    @classmethod
    def validate_params(cls, model_name: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        验证和修正参数

        Args:
            model_name: 模型名称
            params: 参数字典

        Returns:
            验证后的参数字典
        """
        model_info = cls.get_model_info(model_name)
        if not model_info:
            logger.warning(f"未知的思考模型: {model_name}")
            return params

        validated_params = params.copy()

        # 验证 reasoning_effort
        if "reasoning_effort" in validated_params:
            if not model_info.get("supports_reasoning_effort"):
                logger.warning(
                    f"模型 {model_name} 不支持 reasoning_effort 参数，已移除"
                )
                validated_params.pop("reasoning_effort")
            elif (
                validated_params["reasoning_effort"] not in cls.REASONING_EFFORT_LEVELS
            ):
                logger.warning("无效的 reasoning_effort 值，使用默认值 'medium'")
                validated_params["reasoning_effort"] = "medium"

        # 验证 token 限制
        max_context = model_info.get("max_context", 4096)

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

    @classmethod
    def get_usage_recommendations(cls, model_name: str) -> dict[str, Any]:
        """
        获取使用建议

        Args:
            model_name: 模型名称

        Returns:
            使用建议
        """
        model_info = cls.get_model_info(model_name)
        if not model_info:
            return {}

        provider = model_info["provider"]

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


def get_reasoning_config() -> ReasoningConfig:
    """获取思考模型配置实例"""
    return ReasoningConfig()
