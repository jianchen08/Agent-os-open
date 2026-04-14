"""触发器系统公共 API。

提供触发器注册、评估和管理的统一入口。

公共 API:
    TriggerType: 触发器类型枚举
    TriggerStatus: 触发器状态枚举
    TriggerConfig: 触发器配置数据类
    TriggerManager: 触发器管理器
"""

from .manager import TriggerManager
from .types import TriggerConfig, TriggerStatus, TriggerType

__all__ = [
    "TriggerConfig",
    "TriggerManager",
    "TriggerStatus",
    "TriggerType",
]
