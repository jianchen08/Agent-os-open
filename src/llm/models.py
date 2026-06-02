"""
思考模型数据模型

提供思考模型的配置数据结构定义

注意：所有模型的思考参数通过 config/models/llm.yaml 的 default_params 透传，
此文件仅定义数据结构，不应包含硬编码的模型配置。
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


# 预定义配置数据（参数透传，保留空字典）
SUPPORTED_THINKING_MODELS: dict[str, ThinkingModelConfig] = {}
