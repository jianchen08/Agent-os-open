"""
连接器模块

提供 Agent 与外部 IDE 的双向通信能力。

暴露接口：
- BaseConnector: 连接器抽象基类
- ConnectorRegistry: 连接器注册表
- DegradationManager: 降级管理器
- ConnectorContext: 连接器上下文数据
- ConnectorAction: 操作指令
- ActionResult: 操作结果
- ConnectorState: 连接器状态枚举
- ConnectorInfo: 连接器描述信息
- CursorPosition: 光标位置
"""

from connectors.base import BaseConnector
from connectors.degradation import DegradationManager
from connectors.registry import ConnectorRegistry
from connectors.types import (
    ActionResult,
    ConnectorAction,
    ConnectorContext,
    ConnectorInfo,
    ConnectorState,
    CursorPosition,
)

__all__ = [
    # 基类
    "BaseConnector",
    # 注册表
    "ConnectorRegistry",
    # 降级管理
    "DegradationManager",
    # 类型
    "ActionResult",
    "ConnectorAction",
    "ConnectorContext",
    "ConnectorInfo",
    "ConnectorState",
    "CursorPosition",
]
