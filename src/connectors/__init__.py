"""
连接器模块

提供 Agent 与外部 IDE 的双向通信能力。

暴露接口：
- BaseConnector: 连接器抽象基类
- ConnectorRegistry: 连接器注册表
- DegradationManager: 降级管理器
- ConfigSubscriberMixin: ConfigCenter 配置订阅混入类
- ConnectorContext: 连接器上下文数据
- ConnectorAction: 操作指令
- ActionResult: 操作结果
- ConnectorState: 连接器状态枚举
- ConnectorInfo: 连接器描述信息
- CursorPosition: 光标位置
- AdapterConfig: 适配器配置数据类
- load_adapter_configs: 加载适配器配置
- get_adapter_status_summary: 获取适配器状态摘要
"""

from .adapter_config import (
    AdapterConfig,
    get_adapter_status_summary,
    load_adapter_configs,
)
from .base import BaseConnector
from .config_mixin import ConfigSubscriberMixin
from .degradation import DegradationManager
from .registry import ConnectorRegistry
from .types import (
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
    # 配置订阅
    "ConfigSubscriberMixin",
    # 适配器配置
    "AdapterConfig",
    "load_adapter_configs",
    "get_adapter_status_summary",
    # 类型
    "ActionResult",
    "ConnectorAction",
    "ConnectorContext",
    "ConnectorInfo",
    "ConnectorState",
    "CursorPosition",
]
