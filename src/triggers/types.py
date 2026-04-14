"""触发器数据类型定义。

定义触发器系统的核心数据结构，包括触发器类型、状态和配置。

公共 API:
    TriggerType: 触发器类型枚举（延迟/定时/事件/条件）
    TriggerStatus: 触发器状态枚举
    TriggerConfig: 触发器配置数据类
"""

import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TriggerType(Enum):
    """触发器类型。

    Attributes:
        DELAY: 延迟触发 — 经过指定秒数后触发。
        SCHEDULED: 定时触发 — 按 cron 表达式或指定时间触发。
        EVENT: 事件触发 — 监听指定事件名称触发。
        CONDITION: 条件触发 — 布尔表达式求值为 True 时触发。
    """

    DELAY = "delay"
    SCHEDULED = "scheduled"
    EVENT = "event"
    CONDITION = "condition"


class TriggerStatus(Enum):
    """触发器状态。

    Attributes:
        PENDING: 已注册，等待激活。
        ACTIVE: 已激活，可被触发。
        FIRED: 已触发。
        CANCELLED: 已取消。
        EXPIRED: 已过期。
    """

    PENDING = "pending"
    ACTIVE = "active"
    FIRED = "fired"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass
class TriggerConfig:
    """触发器配置。

    Attributes:
        trigger_id: 触发器唯一标识。
        name: 触发器名称。
        trigger_type: 触发器类型。
        status: 触发器当前状态。
        delay_seconds: 延迟触发秒数（DELAY 类型使用）。
        schedule_cron: cron 表达式（SCHEDULED 类型使用）。
        scheduled_at: 定时触发时间（SCHEDULED 类型使用）。
        event_name: 事件名称（EVENT 类型使用）。
        event_filter: 事件数据过滤条件（EVENT 类型使用）。
        condition_expression: Python 布尔表达式（CONDITION 类型使用）。
        action: 触发后执行的动作标识。
        action_params: 动作参数。
        max_fires: 最大触发次数，0 表示无限。
        fire_count: 已触发次数。
        source_agent: 来源 Agent ID。
        metadata: 附加元数据。
    """

    trigger_id: str = ""
    name: str = ""
    trigger_type: TriggerType = TriggerType.EVENT
    status: TriggerStatus = TriggerStatus.PENDING

    # 延迟/定时参数
    delay_seconds: float = 0.0
    schedule_cron: str = ""
    scheduled_at: datetime.datetime | None = None

    # 事件参数
    event_name: str = ""
    event_filter: dict[str, Any] = field(default_factory=dict)

    # 条件参数
    condition_expression: str = ""

    # 通用参数
    action: str = ""
    action_params: dict[str, Any] = field(default_factory=dict)
    max_fires: int = 1
    fire_count: int = 0

    # 来源
    source_agent: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
