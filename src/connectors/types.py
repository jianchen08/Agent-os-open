"""
连接器协议类型定义

定义连接器系统中使用的所有数据类型，包括上下文、操作、结果和状态。

暴露接口：
- CursorPosition: 光标位置
- ConnectorContext: 连接器上下文数据
- ConnectorAction: 操作指令
- ActionResult: 操作结果
- ConnectorState: 连接器状态枚举
- ConnectorInfo: 连接器描述信息
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass(frozen=True)
class CursorPosition:
    """光标位置。

    Attributes:
        line: 行号（从 0 开始）
        column: 列号（从 0 开始）
    """

    line: int
    column: int


@dataclass
class ConnectorContext:
    """连接器上下文数据。

    从 IDE 获取的当前上下文信息。

    Attributes:
        active_file: 活动文件路径
        selected_text: 选中的文本
        cursor_position: 光标位置
        open_files: 所有打开的文件列表
        metadata: 额外元数据
    """

    active_file: str | None = None
    selected_text: str | None = None
    cursor_position: CursorPosition | None = None
    open_files: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConnectorAction:
    """操作指令。

    向 IDE 发送的操作指令。

    Attributes:
        action_type: 操作类型（open_file/insert_content/jump_to/show_diff）
        parameters: 操作参数
        action_id: 操作唯一标识
    """

    action_type: str
    parameters: dict[str, Any] = field(default_factory=dict)
    action_id: str = ""


@dataclass
class ActionResult:
    """操作结果。

    Attributes:
        success: 是否成功
        data: 返回数据
        error: 错误信息
    """

    success: bool
    data: Any = None
    error: str | None = None


class ConnectorState(str, Enum):
    """连接器状态枚举。

    定义连接器的生命周期状态。
    """

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ACTIVE = "active"
    DISCONNECTING = "disconnecting"
    ERROR = "error"


@dataclass
class ConnectorInfo:
    """连接器描述信息。

    Attributes:
        connector_type: 连接器类型标识
        display_name: 显示名称
        capabilities: 支持的能力列表
        priority: 优先级（数值越大优先级越高）
    """

    connector_type: str
    display_name: str
    capabilities: list[str] = field(default_factory=list)
    priority: int = 0
