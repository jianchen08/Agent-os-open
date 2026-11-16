"""
成本控制配置

执法面配置模型。唯一取数源 = 内核按 manifest ``config_files[id=cost]`` 注入的
配置命名空间（``plugin.get_config()["cost"]``，即设置页真值文件
``config/plugins/cost_control/cost_control.yaml`` 的内容，内核每次工具调用
现算携带，SDK ``on_config_changed`` 感知变更）。B2 收敛后本模块不读任何
yaml 文件——设置页 PUT 落盘 → 内核下次调用携带新视图 → server.py 钩子重建
配置对象，执法面随之。
"""

from typing import Any

from constants import CostControl
from pydantic import BaseModel, Field


class AlertThresholds(BaseModel):
    """告警阈值配置（小数刻度 0-1；百分形态经 from_percent 折算）"""

    warning_threshold: float = Field(
        default=CostControl.WARNING_THRESHOLD_PERCENT / 100, description="警告阈值"
    )
    critical_threshold: float = Field(
        default=CostControl.CRITICAL_THRESHOLD_PERCENT / 100, description="严重阈值"
    )
    exhausted_threshold: float = Field(
        default=CostControl.EXHAUSTED_THRESHOLD_PERCENT / 100, description="耗尽阈值"
    )

    @classmethod
    def from_percent(cls, warning: float, critical: float, exhausted: float) -> "AlertThresholds":
        """设置页百分刻度(0-100) → 执法面小数刻度(0-1)，折算单点。"""
        return cls(
            warning_threshold=warning / 100,
            critical_threshold=critical / 100,
            exhausted_threshold=exhausted / 100,
        )


class ProtectionConfig(BaseModel):
    """保护策略配置"""

    auto_save_at_warning: bool = Field(default=True, description="警告时自动保存")
    auto_pause_at_critical: bool = Field(default=True, description="严重时自动暂停")
    auto_stop_at_exhausted: bool = Field(default=True, description="耗尽时自动停止")


class GlobalBudget(BaseModel):
    """全局预算配置"""

    daily_token_limit: int = Field(default=CostControl.DAILY_TOKEN_LIMIT, description="每日 Token 限制")
    monthly_token_limit: int = Field(default=CostControl.MONTHLY_TOKEN_LIMIT, description="每月 Token 限制")
    per_task_token_limit: int = Field(default=CostControl.PER_TASK_TOKEN_LIMIT, description="单任务 Token 限制")
    per_session_token_limit: int = Field(
        default=CostControl.PER_SESSION_TOKEN_LIMIT, description="单会话 Token 限制"
    )


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


def load_cost_control_config(injected: dict[str, Any] | None = None) -> CostControlConfig:
    """
    从 manifest 注入的 cost 配置命名空间构建执法面配置。

    Args:
        injected: ``plugin.get_config()["cost"]``（设置页表面形态：
            ``{enabled, global_config, alerts, protection}``，alerts 为百分刻度）；
            None/非 dict/空 dict/缺段时该段回落内置默认（constants，与设置页
            缺省回落表同值）。``enabled`` 开关执法面暂不消费（模型无此字段）。

    Returns:
        成本控制配置对象（同时落位单例）
    """
    global _config  # noqa: PLW0603

    data = injected if isinstance(injected, dict) else {}
    global_data = data.get("global_config") or {}
    alerts_data = data.get("alerts") or {}
    protection_data = data.get("protection") or {}

    _config = CostControlConfig(
        global_budget=GlobalBudget(**global_data),
        alerts=AlertThresholds.from_percent(
            alerts_data.get("warning_threshold", CostControl.WARNING_THRESHOLD_PERCENT),
            alerts_data.get("critical_threshold", CostControl.CRITICAL_THRESHOLD_PERCENT),
            alerts_data.get("exhausted_threshold", CostControl.EXHAUSTED_THRESHOLD_PERCENT),
        ),
        protection=ProtectionConfig(**protection_data),
    )
    return _config


def get_cost_control_config() -> CostControlConfig:
    """获取成本控制配置（单例；未播种时为默认表）"""
    global _config  # noqa: PLW0603
    if _config is None:
        _config = load_cost_control_config()
    return _config
