"""
生命周期状态枚举定义

暴露接口：
- is_terminal(self) -> bool：is_terminal功能
- is_active(self) -> bool：is_active功能
- is_error(self) -> bool：is_error功能
- LifecycleStatus：LifecycleStatus类
"""

from enum import Enum


class LifecycleStatus(str, Enum):
    """
    生命周期状态

    用于 Trigger、Service 等长期运行实体的状态管理。
    描述实体从创建到停止的完整生命周期。

    状态说明:
        - CREATED: 已创建，实体已创建但尚未初始化
        - INITIALIZING: 初始化中，实体正在进行初始化
        - ACTIVE: 活跃，实体正常运行中
        - INACTIVE: 非活跃，实体暂时不活跃
        - PAUSED: 暂停，实体被暂停
        - STOPPING: 停止中，实体正在停止
        - STOPPED: 已停止，实体已停止
        - ERROR: 错误，实体处于错误状态
        - DISABLED: 禁用，实体已被禁用

    Example:
        >>> status = LifecycleStatus.ACTIVE
        >>> status.is_active
        True
        >>> status.is_terminal
        False
    """

    CREATED = "created"
    INITIALIZING = "initializing"
    ACTIVE = "active"
    INACTIVE = "inactive"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"
    DISABLED = "disabled"

    @property
    def is_terminal(self) -> bool:
        """是否为终态"""
        return self in {
            LifecycleStatus.STOPPED,
            LifecycleStatus.DISABLED,
        }

    @property
    def is_active(self) -> bool:
        """是否为活跃状态"""
        return self == LifecycleStatus.ACTIVE

    @property
    def is_error(self) -> bool:
        """是否为错误状态"""
        return self == LifecycleStatus.ERROR


# 生命周期状态转换规则
# 定义每个状态可以合法转换到的目标状态列表
LIFECYCLE_TRANSITIONS: dict[LifecycleStatus, list[LifecycleStatus]] = {
    LifecycleStatus.CREATED: [
        LifecycleStatus.INITIALIZING,
        LifecycleStatus.DISABLED,
    ],
    LifecycleStatus.INITIALIZING: [
        LifecycleStatus.ACTIVE,
        LifecycleStatus.ERROR,
        LifecycleStatus.DISABLED,
    ],
    LifecycleStatus.ACTIVE: [
        LifecycleStatus.INACTIVE,
        LifecycleStatus.PAUSED,
        LifecycleStatus.STOPPING,
        LifecycleStatus.ERROR,
    ],
    LifecycleStatus.INACTIVE: [
        LifecycleStatus.ACTIVE,
        LifecycleStatus.STOPPING,
    ],
    LifecycleStatus.PAUSED: [
        LifecycleStatus.ACTIVE,
        LifecycleStatus.STOPPING,
    ],
    LifecycleStatus.STOPPING: [
        LifecycleStatus.STOPPED,
        LifecycleStatus.ERROR,
    ],
    LifecycleStatus.STOPPED: [
        LifecycleStatus.INITIALIZING,
    ],
    LifecycleStatus.ERROR: [
        LifecycleStatus.INITIALIZING,
        LifecycleStatus.STOPPING,
        LifecycleStatus.DISABLED,
    ],
    LifecycleStatus.DISABLED: [
        LifecycleStatus.INITIALIZING,
    ],
}
