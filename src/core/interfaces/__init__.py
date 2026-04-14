"""核心接口定义。

提供依赖注入所需的抽象接口：
- IEventBus: 事件总线接口
- IToolRegistry: 工具注册表接口
- IStateMachine: 状态机接口
"""

from core.interfaces.event_bus import IEventBus
from core.interfaces.tool_registry import IToolRegistry
from core.interfaces.state_machine import IStateMachine

__all__ = [
    "IEventBus",
    "IToolRegistry",
    "IStateMachine",
]
