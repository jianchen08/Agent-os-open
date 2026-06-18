"""
思考模式切换服务

提供统一的思考模式切换接口，支持：
1. 参数切换型：GLM-4.7 等通过参数启用思考模式
2. 模型切换型：DeepSeek 等需要切换到专门的思考模型
"""

import logging
from typing import Any

from src.core.di import get_global_container
from src.llm.base import LLMResponse, Message
from src.llm.config import ThinkingModelService
from src.llm.models import SUPPORTED_THINKING_MODELS

logger = logging.getLogger(__name__)


class ThinkingModeService:
    """思考模式切换服务"""

    def __init__(self):
        container = get_global_container()
        self.factory = container.get("llm_factory")
        self.model_service = ThinkingModelService()

    def get_available_thinking_models(self) -> list[dict[str, Any]]:
        """
        获取所有支持思考模式的模型

        Returns:
            支持思考模式的模型列表
        """
        models = []
        for model_name, model_config in SUPPORTED_THINKING_MODELS.items():
            thinking_type = model_config.thinking_mode_type

            models.append(
                {
                    "model_name": model_name,
                    "display_name": model_config.display_name,
                    "thinking_type": thinking_type,
                    "base_model": model_config.base_model,
                    "thinking_model": model_config.thinking_model,
                    "is_same_model": model_config.base_model == model_config.thinking_model,
                    "supports_reasoning_effort": model_config.supports_reasoning_effort,
                    "description": self._get_model_description(model_config.model_dump()),
                }
            )

        return models

    def can_enable_thinking_mode(self, model_name: str) -> bool:
        """
        检查模型是否支持思考模式

        Args:
            model_name: 模型名称

        Returns:
            是否支持思考模式
        """
        return self.model_service.supports_thinking_mode(model_name)

    def get_thinking_mode_info(self, model_name: str) -> dict[str, Any] | None:
        """
        获取模型的思考模式信息

        Args:
            model_name: 模型名称

        Returns:
            思考模式信息
        """
        if not self.can_enable_thinking_mode(model_name):
            return None

        model_info = self.model_service.get_model_info(model_name)
        if not model_info:
            return None

        thinking_type = model_info.get("thinking_mode_type")

        return {
            "model_name": model_name,
            "thinking_type": thinking_type,
            "display_name": model_info["display_name"],
            "base_model": model_info.get("base_model", model_name),
            "thinking_model": model_info.get("thinking_model", model_name),
            "is_same_model": (
                model_info.get("base_model") == model_info.get("thinking_model")
            ),
            "switch_description": self._get_switch_description(thinking_type),
            "thinking_params": model_info.get("thinking_params", {}),
            "normal_params": model_info.get("normal_params", {}),
        }

    async def generate_with_thinking_mode(
        self,
        model_name: str,
        messages: list[Message],
        enable_thinking: bool = True,
        **extra_params: Any,
    ) -> LLMResponse:
        """
        使用思考模式生成响应

        Args:
            model_name: 基础模型名称
            messages: 消息列表
            enable_thinking: 是否启用思考模式
            **extra_params: 额外参数

        Returns:
            LLM 响应
        """
        # 获取思考模式配置
        actual_model, thinking_params = self.model_service.get_thinking_config(
            model_name, enable_thinking
        )

        # 合并参数
        final_params = thinking_params.copy()
        final_params.update(extra_params)

        # 准备消息（参数切换型可能需要添加思考提示）
        final_messages = self._prepare_messages_for_thinking(
            messages, model_name, enable_thinking
        )

        # 获取客户端并生成响应
        try:
            client = self.factory.get_client(actual_model)
            response = await client.generate(final_messages, **final_params)

            # 后处理响应（提取思考过程等）
            processed_response = self._process_thinking_response(
                response, model_name, enable_thinking
            )

            logger.info(
                f"思考模式生成完成: {model_name} -> {actual_model} "
                f"(思考模式: {'开启' if enable_thinking else '关闭'})"
            )

            return processed_response

        except Exception as e:
            logger.error(f"思考模式生成失败: {e}")
            raise

    def get_thinking_mode_params(
        self, model_name: str, enable_thinking: bool = True
    ) -> dict[str, Any]:
        """
        获取思考模式参数

        Args:
            model_name: 模型名称
            enable_thinking: 是否启用思考模式

        Returns:
            思考模式参数字典
        """
        if not self.can_enable_thinking_mode(model_name):
            return {}

        _, params = self.model_service.get_thinking_config(model_name, enable_thinking)
        return params

    def switch_thinking_mode(
        self, current_model: str, enable_thinking: bool
    ) -> tuple[str, dict[str, Any]]:
        """
        切换思考模式

        Args:
            current_model: 当前模型名称
            enable_thinking: 是否启用思考模式

        Returns:
            (目标模型名称, 参数配置)
        """
        if not self.can_enable_thinking_mode(current_model):
            logger.warning(f"模型 {current_model} 不支持思考模式")
            return current_model, {}

        target_model, params = self.model_service.get_thinking_config(
            current_model, enable_thinking
        )

        model_info = self.model_service.get_model_info(current_model)
        thinking_type = model_info.get("thinking_mode_type") if model_info else None

        logger.info(
            f"思考模式切换: {current_model} -> {target_model} "
            f"(类型: {thinking_type}, 思考模式: {'开启' if enable_thinking else '关闭'})"
        )

        return target_model, params

    def get_thinking_mode_recommendations(
        self, task_type: str = "general", complexity: str = "medium"
    ) -> list[dict[str, Any]]:
        """
        获取思考模式推荐

        Args:
            task_type: 任务类型
            complexity: 复杂度

        Returns:
            推荐的思考模型列表
        """
        recommendations = []

        for model_name in SUPPORTED_THINKING_MODELS:
            model_info = self.model_service.get_model_info(model_name)
            if not model_info:
                continue

            # 获取最优参数
            optimal_params = self.model_service.get_optimal_params(
                model_name, task_type, complexity
            )

            # 获取使用建议
            usage_recommendations = self.model_service.get_usage_recommendations(model_name)

            # 计算适合度评分
            suitability_score = self._calculate_suitability_score(
                model_info, task_type, complexity
            )

            recommendations.append(
                {
                    "model_name": model_name,
                    "display_name": model_info["display_name"],
                    "thinking_type": model_info.get("thinking_mode_type"),
                    "suitability_score": suitability_score,
                    "optimal_params": optimal_params,
                    "best_for": usage_recommendations.get("best_for", []),
                    "tips": usage_recommendations.get("tips", []),
                    "cost_estimate": self._estimate_cost(model_info, optimal_params),
                }
            )

        # 按适合度评分排序
        recommendations.sort(key=lambda x: x["suitability_score"], reverse=True)

        return recommendations

    def _prepare_messages_for_thinking(
        self, messages: list[Message], model_name: str, enable_thinking: bool
    ) -> list[Message]:
        """
        为思考模式准备消息

        Args:
            messages: 原始消息列表
            model_name: 模型名称
            enable_thinking: 是否启用思考模式

        Returns:
            处理后的消息列表
        """
        if not enable_thinking:
            return messages

        return messages

    def _process_thinking_response(
        self, response: LLMResponse, model_name: str, enable_thinking: bool
    ) -> LLMResponse:
        """
        处理思考模式响应

        Args:
            response: 原始响应
            model_name: 模型名称
            enable_thinking: 是否启用思考模式

        Returns:
            处理后的响应
        """
        return response

    def _get_model_description(self, model_info: dict[str, Any]) -> str:
        """获取模型描述"""
        thinking_type = model_info.get("thinking_mode_type", "unknown")

        if thinking_type == "parameter_switch":
            return "通过参数启用思考模式，同一模型支持普通和思考两种模式"
        if thinking_type == "model_switch":
            return "需要切换到专门的思考模型，提供更强的推理能力"
        return "支持思考模式"

    def _get_switch_description(self, thinking_type: str) -> str:
        """获取切换方式描述"""
        if thinking_type == "parameter_switch":
            return "通过参数切换，保持同一模型"
        if thinking_type == "model_switch":
            return "切换到专门的思考模型"
        return "未知切换方式"

    def _calculate_suitability_score(
        self, model_info: dict[str, Any], task_type: str, complexity: str
    ) -> float:
        """
        计算模型适合度评分

        Args:
            model_info: 模型信息
            task_type: 任务类型
            complexity: 复杂度

        Returns:
            适合度评分 (0-100)
        """
        score = 50.0  # 基础分数

        provider = model_info.get("provider", "")

        # 根据任务类型调整分数
        if task_type == "math":
            if "deepseek" in provider or "openai" in provider:
                score += 20
        elif task_type == "coding":
            if "deepseek" in provider or "openai" in provider:
                score += 15
        elif task_type == "creative" and "anthropic" in provider:
            score += 15

        # 根据复杂度调整分数
        if complexity == "complex":
            if model_info.get("supports_reasoning_effort"):
                score += 10
            if model_info.get("max_context", 0) > 100000:
                score += 5

        return min(100.0, max(0.0, score))

    def _estimate_cost(self, model_info: dict[str, Any], params: dict[str, Any]) -> str:
        """
        估算成本

        Args:
            model_info: 模型信息
            params: 参数配置

        Returns:
            成本估算描述
        """
        provider = model_info.get("provider", "")
        reasoning_effort = params.get("reasoning_effort", "medium")

        if "openai" in provider:
            if reasoning_effort == "high":
                return "高成本"
            if reasoning_effort == "low":
                return "中等成本"
            return "中高成本"
        if "deepseek" in provider:
            return "低成本"
        if "anthropic" in provider:
            return "中等成本"
        return "未知成本"


def get_thinking_mode_service() -> ThinkingModeService:
    """获取思考模式服务实例"""
    return ThinkingModeService()
