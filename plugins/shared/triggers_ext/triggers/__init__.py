"""触发器子系统——trigger_setup 工具自有领域代码。

触发器仅被本工具（``trigger_setup``）消费，按「插件自有的放插件目录」
原则，作为本工具的自有子包直接维护：``tool.py`` 通过 ``from triggers.manager
import ...`` / ``from triggers.types import ...`` 直接导入（本工具目录由
``server.py`` 注入 ``sys.path``）。这是工具自己的真实实现，不是兼容 shim。

公共 API:
    TriggerType / TriggerStatus: 触发器类型/状态枚举
    TriggerConfig: 触发器配置数据类
    parse_duration: 时长字符串解析
    TriggerManager / get_trigger_manager: 触发器管理器及全局单例
"""

from triggers.manager import TriggerManager, get_trigger_manager
from triggers.types import (
    TriggerConfig,
    TriggerStatus,
    TriggerType,
    parse_duration,
)

__all__ = [
    "TriggerConfig",
    "TriggerManager",
    "TriggerStatus",
    "TriggerType",
    "get_trigger_manager",
    "parse_duration",
]
