"""
思考模型数据模型

提供思考模型的配置数据结构定义
"""

from typing import Literal

from pydantic import BaseModel


class ThinkingModelConfig(BaseModel):
    """思考模型配置"""

    provider: str
    display_name: str
    thinking_mode_type: Literal["model_switch", "parameter_switch"]
    base_model: str
    thinking_model: str
    supports_reasoning_effort: bool = False
    supports_cot: bool = True
    supports_tools: bool = False
    max_context: int = 4096
    thinking_params: dict = {}
    normal_params: dict = {}


# 预定义配置数据
SUPPORTED_THINKING_MODELS: dict[str, ThinkingModelConfig] = {
    # === 模型切换型思考模式 ===
    "deepseek-reasoner": ThinkingModelConfig(
        provider="deepseek_reasoning",
        display_name="DeepSeek R1 思考模型",
        supports_reasoning_effort=False,
        supports_cot=True,
        supports_tools=True,
        max_context=64000,
        thinking_mode_type="model_switch",
        base_model="deepseek-chat",
        thinking_model="deepseek-reasoner",
    ),
    "o1": ThinkingModelConfig(
        provider="openai_reasoning",
        display_name="OpenAI o1 思考模型",
        supports_reasoning_effort=True,
        supports_cot=True,
        supports_tools=True,
        max_context=200000,
        thinking_mode_type="model_switch",
        base_model="gpt-4o",
        thinking_model="o1",
    ),
    "o1-mini": ThinkingModelConfig(
        provider="openai_reasoning",
        display_name="OpenAI o1-mini 思考模型",
        supports_reasoning_effort=True,
        supports_cot=True,
        supports_tools=True,
        max_context=128000,
        thinking_mode_type="model_switch",
        base_model="gpt-4o-mini",
        thinking_model="o1-mini",
    ),
    "o3-mini": ThinkingModelConfig(
        provider="openai_reasoning",
        display_name="OpenAI o3-mini 思考模型",
        supports_reasoning_effort=True,
        supports_cot=True,
        supports_tools=True,
        max_context=200000,
        thinking_mode_type="model_switch",
        base_model="gpt-4o",
        thinking_model="o3-mini",
    ),
    "claude-3-7-sonnet": ThinkingModelConfig(
        provider="anthropic_reasoning",
        display_name="Claude 3.7 Sonnet 思考模式",
        supports_reasoning_effort=True,
        supports_cot=True,
        supports_tools=True,
        max_context=200000,
        thinking_mode_type="model_switch",
        base_model="claude-3-5-sonnet",
        thinking_model="claude-3-7-sonnet-20241022",
    ),
    "claude-3-7-sonnet-20241022": ThinkingModelConfig(
        provider="anthropic_reasoning",
        display_name="Claude 3.7 Sonnet 思考模式",
        supports_reasoning_effort=True,
        supports_cot=True,
        supports_tools=True,
        max_context=200000,
        thinking_mode_type="model_switch",
        base_model="claude-3-5-sonnet",
        thinking_model="claude-3-7-sonnet-20241022",
    ),
    # === 参数切换型思考模式 ===
    "glm-4.7": ThinkingModelConfig(
        provider="zhipu_coding",
        display_name="GLM-4.7 (支持思考模式)",
        supports_reasoning_effort=False,
        supports_cot=True,
        supports_tools=False,
        max_context=128000,
        thinking_mode_type="parameter_switch",
        base_model="glm-4.7",
        thinking_model="glm-4.7",
        thinking_params={
            "thinking": {"type": "enabled"},
            "temperature": 1.0,
            "max_tokens": 65536,
        },
        normal_params={
            "temperature": 0.7,
        },
    ),
}

# reasoning_effort 参数映射
REASONING_EFFORT_LEVELS: dict[str, dict] = {
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
