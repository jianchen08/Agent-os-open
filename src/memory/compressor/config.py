"""
压缩配置模块

管理上下文压缩器的配置参数
"""

from dataclasses import dataclass
from typing import Any


def load_context_window_config() -> dict[str, Any]:
    """
    从 config/system/context_window_config.yaml 加载上下文窗口配置

    Returns:
        配置字典

    Raises:
        KeyError: 配置缺失时抛出明确错误
    """
    from src.config.system_config import get_system_config_manager

    manager = get_system_config_manager()
    config = manager.load_context_window_config()

    # 验证必要配置项存在（无硬编码兜底）
    if not config:
        raise KeyError(
            "上下文窗口配置缺失。请确保 config/system/context_window_config.yaml 文件存在且内容正确。"
        )

    # 验证压缩触发比例
    if "compress_trigger_ratio" not in config:
        raise KeyError("配置缺失: compress_trigger_ratio")

    # 验证预算配置
    if "budgets" not in config:
        raise KeyError("配置缺失: budgets")

    required_budget_keys = [
        "system_prompt", "tools_description", "dynamic_variables",
        "l1", "l2", "l3", "retrieval", "recent", "response_reserve"
    ]
    budgets = config["budgets"]
    for key in required_budget_keys:
        if key not in budgets:
            raise KeyError(f"配置缺失: budgets.{key}")

    return config


@dataclass
class CompressionConfig:
    """
    压缩配置

    所有比例基于 context_window 计算实际 token 数

    注意：context_window 必须从模型配置中传入，无硬编码兜底值
    """

    # 模型上下文窗口大小（必须从模型配置传入，无默认值）
    context_window: int = None

    # ========== 触发阈值 ==========
    compress_trigger_ratio: float = None

    # ========== 固定预算（KV Cache 最稳定）==========
    system_prompt_ratio: float = None  # 系统提示
    tools_description_ratio: float = None  # 工具描述

    # ========== 半稳定层 ==========
    dynamic_variables_ratio: float = None  # 动态变量
    knowledge_ratio: float = None  # 知识库
    experience_ratio: float = None  # 跨会话经验
    l3_ratio: float = None  # L3 关键词

    # ========== 动态层 ==========
    l2_ratio: float = None  # L2 三元组
    l1_ratio: float = None  # L1 八段摘要
    retrieval_ratio: float = None  # 检索召回
    recent_ratio: float = None  # 最近原文（L0）
    response_reserve_ratio: float = None  # 预留回复

    def __post_init__(self):
        """初始化后从配置文件加载比例参数"""
        # 如果配置项未设置，从配置文件加载
        if self.context_window is None:
            raise KeyError(
                "CompressionConfig 初始化失败: context_window 必须提供。"
                "请从模型配置中读取 context_window 并传入。"
            )

        # 加载全局配置（会验证配置完整性）
        config = load_context_window_config()
        budgets = config["budgets"]

        # 验证所有必需的 budgets 配置项存在
        required_budget_keys = [
            "system_prompt", "tools_description", "dynamic_variables",
            "l1", "l2", "l3", "retrieval", "recent", "response_reserve"
        ]
        for key in required_budget_keys:
            if key not in budgets:
                raise KeyError(
                    f"配置缺失: budgets.{key}。"
                    f"请在 config/system/context_window_config.yaml 的 budgets 部分添加 '{key}' 配置项。"
                )

        # 加载触发阈值
        if self.compress_trigger_ratio is None:
            self.compress_trigger_ratio = config["compress_trigger_ratio"]

        # 加载预算比例（使用 budgets[key] 强制要求配置存在）
        if self.system_prompt_ratio is None:
            self.system_prompt_ratio = budgets["system_prompt"]
        if self.tools_description_ratio is None:
            self.tools_description_ratio = budgets["tools_description"]

        # 半稳定层
        if self.dynamic_variables_ratio is None:
            self.dynamic_variables_ratio = budgets["dynamic_variables"]
        if self.l3_ratio is None:
            self.l3_ratio = budgets["l3"]

        # 动态层
        if self.l2_ratio is None:
            self.l2_ratio = budgets["l2"]
        if self.l1_ratio is None:
            self.l1_ratio = budgets["l1"]
        if self.retrieval_ratio is None:
            self.retrieval_ratio = budgets["retrieval"]
        if self.recent_ratio is None:
            self.recent_ratio = budgets["recent"]
        if self.response_reserve_ratio is None:
            self.response_reserve_ratio = budgets["response_reserve"]

        # 验证配置
        if not self.validate():
            raise ValueError(
                f"配置验证失败: 预算比例总和超过 75%。"
                f"当前比例: system_prompt={self.system_prompt_ratio}, "
                f"tools_description={self.tools_description_ratio}, "
                f"dynamic_variables={self.dynamic_variables_ratio}, "
                f"l3={self.l3_ratio}, l2={self.l2_ratio}, l1={self.l1_ratio}, "
                f"retrieval={self.retrieval_ratio}, "
                f"reserve={self.response_reserve_ratio}, recent={self.recent_ratio}"
            )

    def get_budgets(self) -> dict[str, int]:
        """
        计算各部分实际 token 预算

        Returns:
            各层预算字典
        """
        return {
            "system_prompt": int(self.context_window * self.system_prompt_ratio),
            "tools_description": int(self.context_window * self.tools_description_ratio),
            "dynamic_variables": int(self.context_window * self.dynamic_variables_ratio),
            "response_reserve": int(self.context_window * self.response_reserve_ratio),
            "recent": int(self.context_window * self.recent_ratio),
            "L1": int(self.context_window * self.l1_ratio),
            "L2": int(self.context_window * self.l2_ratio),
            "L3": int(self.context_window * self.l3_ratio),
            "retrieval": int(self.context_window * self.retrieval_ratio),
        }

    def get_trigger_threshold(self) -> int:
        """
        获取触发压缩的 token 阈值

        Returns:
            触发阈值（token 数）
        """
        return int(self.context_window * self.compress_trigger_ratio)

    def validate(self) -> bool:
        """
        验证配置是否合理

        Returns:
            配置是否有效
        """
        total_ratio = (
            self.system_prompt_ratio
            + self.tools_description_ratio
            + self.dynamic_variables_ratio
            + self.response_reserve_ratio
            + self.recent_ratio
            + self.l1_ratio
            + self.l2_ratio
            + self.l3_ratio
            + self.retrieval_ratio
        )
        # 总比例不应超过 75%，留 25% 安全余量
        return total_ratio <= 0.75


@dataclass
class ContextBudget:
    """上下文预算状态"""

    # 固定层
    system_prompt_tokens: int = 0
    tools_description_tokens: int = 0
    dynamic_variables_tokens: int = 0

    # 记忆层
    l0_tokens: int = 0  # 最近原文
    l1_tokens: int = 0  # L1 八段摘要
    l2_tokens: int = 0  # L2 三元组
    l3_tokens: int = 0  # L3 关键词

    # 动态层
    retrieval_tokens: int = 0
    user_message_tokens: int = 0

    def total(self) -> int:
        """总使用量"""
        return (
            self.system_prompt_tokens
            + self.tools_description_tokens
            + self.dynamic_variables_tokens
            + self.l0_tokens
            + self.l1_tokens
            + self.l2_tokens
            + self.l3_tokens
            + self.retrieval_tokens
            + self.user_message_tokens
        )
