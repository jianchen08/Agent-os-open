"""触发器系统公共 API。

提供触发器注册、评估和管理的统一入口。

公共 API:
    TriggerType: 触发器类型枚举
    TriggerStatus: 触发器状态枚举
    TriggerConfig: 触发器配置数据类
    TriggerManager: 触发器管理器
    get_trigger_manager: 获取全局单例
    parse_duration: 时长字符串解析
"""

from .manager import TriggerManager, get_trigger_manager
from .types import TriggerConfig, TriggerStatus, TriggerType, parse_duration

__all__ = [
    "TriggerConfig",
    "TriggerManager",
    "TriggerStatus",
    "TriggerType",
    "get_trigger_manager",
    "parse_duration",
]
