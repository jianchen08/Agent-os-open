"""
成本控制配置

加载和管理成本控制相关配置
"""

from pathlib import Path  # noqa: F401

import yaml  # noqa: F401
from pydantic import BaseModel, Field

from src.core.constants import CostControl


class AlertThresholds(BaseModel):
    """告警阈值配置"""

    warning_threshold: float = Field(default=CostControl.WARNING_THRESHOLD, description="警告阈值")
    critical_threshold: float = Field(default=CostControl.CRITICAL_THRESHOLD, description="严重阈值")
    exhausted_threshold: float = Field(default=CostControl.EXHAUSTED_THRESHOLD, description="耗尽阈值")


class ProtectionConfig(BaseModel):
    """保护策略配置"""

    auto_save_at_warning: bool = Field(default=True, description="警告时自动保存")
    auto_pause_at_critical: bool = Field(default=True, description="严重时自动暂停")
    auto_stop_at_exhausted: bool = Field(default=True, description="耗尽时自动停止")


class GlobalBudget(BaseModel):
    """全局预算配置"""

    daily_token_limit: int = Field(default=CostControl.DAILY_TOKEN_LIMIT, description="每日 Token 限制")
    monthly_token_limit: int = Field(default=CostControl.MONTHLY_TOKEN_LIMIT, description="每月 Token 限制")
    per_task_token_limit: int = Field(default=50000, description="单任务 Token 限制")
    per_session_token_limit: int = Field(default=100000, description="单会话 Token 限制")


class CostRates(BaseModel):
    """成本费率配置"""

    default: float = Field(default=0.002, description="默认成本率 ($/1K tokens)")
    models: dict[str, float] = Field(default_factory=dict, description="按模型成本率")


class UserBudget(BaseModel):
    """用户预算配置"""

    daily_token_limit: int = Field(default=100000, description="每日 Token 限制")
    monthly_token_limit: int = Field(default=3000000, description="每月 Token 限制")


class CostControlConfig(BaseModel):
    """成本控制完整配置"""

    global_budget: GlobalBudget = Field(default_factory=GlobalBudget)
    alerts: AlertThresholds = Field(default_factory=AlertThresholds)
    protection: ProtectionConfig = Field(default_factory=ProtectionConfig)
    cost_rates: CostRates = Field(default_factory=CostRates)
    user_budgets: dict[str, UserBudget] = Field(default_factory=dict)

    def get_model_cost_rate(self, model_name: str) -> float:
        """获取模型成本率"""
        # 精确匹配
        if model_name in self.cost_rates.models:
            return self.cost_rates.models[model_name]

        # 前缀匹配
        for prefix, rate in self.cost_rates.models.items():
            if model_name.startswith(prefix):
                return rate

        return self.cost_rates.default

    def get_user_budget(self, user_level: str = "default") -> UserBudget:
        """获取用户预算配置"""
        return self.user_budgets.get(user_level, UserBudget())


# 全局配置实例
_config: CostControlConfig | None = None


def load_cost_control_config(config_path: str | None = None) -> CostControlConfig:
    """
    加载成本控制配置

    Args:
        config_path: 配置文件路径，默认为 config/cost_control.yaml

    Returns:
        成本控制配置对象
    """
    global _config  # noqa: PLW0603

    if config_path is None:
        config_path = "config/cost_control.yaml"

    try:
        from config.config_center import get_config_center  # noqa: PLC0415

        rel = config_path.replace("config/", "", 1) if config_path.startswith("config/") else config_path
        data = get_config_center().get(rel) or {}
    except Exception:
        data = {}
    if not data:
        _config = CostControlConfig()
        return _config

    # 解析配置
    global_data = data.get("global", {})
    alerts_data = data.get("alerts", {})
    protection_data = data.get("protection", {})
    cost_rates_data = data.get("cost_rates", {})
    user_budgets_data = data.get("user_budgets", {})

    _config = CostControlConfig(
        global_budget=GlobalBudget(**global_data),
        alerts=AlertThresholds(**alerts_data),
        protection=ProtectionConfig(**protection_data),
        cost_rates=CostRates(
            default=cost_rates_data.get("default", 0.002),
            models=cost_rates_data.get("models", {}),
        ),
        user_budgets={k: UserBudget(**v) for k, v in user_budgets_data.items()},
    )

    return _config


def get_cost_control_config() -> CostControlConfig:
    """获取成本控制配置（单例）"""
    global _config  # noqa: PLW0603
    if _config is None:
        _config = load_cost_control_config()
    return _config


def reset_cost_control_config() -> None:
    """重置配置（用于测试）"""
    global _config  # noqa: PLW0603
    _config = None
