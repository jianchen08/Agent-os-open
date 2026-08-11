"""触发器数据类型定义。

定义触发器系统的核心数据结构，包括触发器类型、状态和配置。

公共 API:
    TriggerType: 触发器类型枚举（延迟/定时/周期/事件/条件）
    TriggerStatus: 触发器状态枚举
    TriggerConfig: 触发器配置数据类
    parse_duration: 解析时长字符串为秒数
"""

import datetime
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TriggerType(Enum):
    """触发器类型。

    Attributes:
        DELAY: 延迟触发 — 经过指定秒数后触发。
        SCHEDULED: 定时触发 — 按指定时间触发。
        INTERVAL: 周期触发 — 按固定间隔重复触发。
        EVENT: 事件触发 — 监听指定事件名称触发。
        CONDITION: 条件触发 — 布尔表达式求值为 True 时触发。
    """

    DELAY = "delay"
    SCHEDULED = "scheduled"
    INTERVAL = "interval"
    EVENT = "event"
    CONDITION = "condition"


class TriggerStatus(Enum):
    """触发器状态。

    Attributes:
        PENDING: 已注册，等待激活。
        ACTIVE: 已激活，可被触发。
        FIRED: 已触发（达到停止条件）。
        CANCELLED: 已取消。
        EXPIRED: 已过期。
    """

    PENDING = "pending"
    ACTIVE = "active"
    FIRED = "fired"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


_DURATION_PATTERN = re.compile(
    r"""^
    (?:
        (?P<days>\d+)\s*d(?:ays?)?|
        (?P<hours>\d+)\s*h(?:ours?)?|
        (?P<minutes>\d+)\s*m(?:in(?:utes?)?)?|
        (?P<seconds>\d+)\s*s(?:ec(?:onds?)?)?
    )
    (?:\s*,?\s*)*
    $""",
    re.IGNORECASE | re.VERBOSE,
)

_DURATION_MULTI_PATTERN = re.compile(
    r"(?:(\d+)\s*d(?:ays?)?)"
    r"|(?:\s*,?\s*)"
    r"|(?:(\d+)\s*h(?:ours?)?)"
    r"|(?:\s*,?\s*)"
    r"|(?:(\d+)\s*m(?:in(?:utes?)?)?)"
    r"|(?:\s*,?\s*)"
    r"|(?:(\d+)\s*s(?:ec(?:onds?)?)?)",
    re.IGNORECASE,
)


def parse_duration(duration_str: str) -> float:
    """将时长字符串解析为秒数。

    支持格式：
        - "30s" / "30sec" / "30 seconds"  → 30秒
        - "5m" / "5min" / "5 minutes"     → 300秒
        - "2h" / "2hours"                  → 7200秒
        - "3d" / "3days"                   → 259200秒
        - 组合："1h30m" / "2d 6h" / "1h, 30m, 15s"

    Args:
        duration_str: 时长字符串

    Returns:
        对应的秒数

    Raises:
        ValueError: 无法解析的格式
    """
    if not duration_str or not duration_str.strip():
        raise ValueError("时长字符串不能为空")

    duration_str = duration_str.strip()
    total_seconds = 0.0
    remaining = duration_str

    unit_map = [
        (re.compile(r"(\d+)\s*d(?:ays?)?", re.I), 86400),
        (re.compile(r"(\d+)\s*h(?:ours?)?", re.I), 3600),
        (re.compile(r"(\d+)\s*m(?:in(?:utes?)?)?", re.I), 60),
        (re.compile(r"(\d+)\s*s(?:ec(?:onds?)?)?", re.I), 1),
    ]

    for pattern, multiplier in unit_map:
        match = pattern.search(remaining)
        if match:
            total_seconds += int(match.group(1)) * multiplier
            remaining = remaining[: match.start()] + remaining[match.end() :]

    remaining = remaining.strip().strip(", ")
    if remaining:
        raise ValueError(f"无法解析的时长格式: '{duration_str}'，未识别部分: '{remaining}'")

    if total_seconds <= 0:
        raise ValueError(f"时长必须大于 0: '{duration_str}'")

    return total_seconds


@dataclass
class TriggerConfig:
    """触发器配置。

    Attributes:
        trigger_id: 触发器唯一标识。
        name: 触发器名称。
        trigger_type: 触发器类型。
        status: 触发器当前状态。
        delay_seconds: 延迟触发秒数（DELAY 类型使用）。
        schedule_cron: cron 表达式（保留字段）。
        scheduled_at: 定时触发时间（SCHEDULED 类型使用）。
        interval_seconds: 周期间隔秒数（INTERVAL 类型使用）。
        event_name: 事件名称（EVENT 类型使用）。
        event_filter: 事件数据过滤条件（EVENT 类型使用）。
        condition_expression: Python 布尔表达式（CONDITION 类型使用）。
        action: 触发后执行的动作标识。
        action_params: 动作参数。
        max_fires: 最大触发次数，0 表示无限。
        max_time_seconds: 最长运行时间（秒），0 表示无限。
        fire_count: 已触发次数。
        source_agent: 来源 Agent ID。
        message: 触发时注入的消息内容。
        pipeline_id: 所属管道 ID（用于唤醒）。
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

    # 周期触发参数
    interval_seconds: float = 0.0

    # 事件参数
    event_name: str = ""
    event_filter: dict[str, Any] = field(default_factory=dict)

    # 条件参数
    condition_expression: str = ""

    # 通用参数
    action: str = ""
    action_params: dict[str, Any] = field(default_factory=dict)
    max_fires: int = 1
    max_time_seconds: float = 0.0
    fire_count: int = 0

    # 来源
    source_agent: str = ""
    message: str = ""
    pipeline_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
