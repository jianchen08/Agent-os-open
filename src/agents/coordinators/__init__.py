"""
协调器模块

提供各种协调器来解耦 AgentLoop 的职责
"""

from src.agents.coordinators.isolation_config import IsolationConfig, load_config
from src.agents.coordinators.isolation_coordinator import (
    IsolationCoordinator,
    create_isolation_coordinator,
    get_isolation_coordinator,
)
from src.agents.coordinators.isolation_tool_wrapper import (
    IsolationToolWrapper,
    wrap_executor_with_isolation,
)
from src.agents.coordinators.llm_coordinator import LLMCoordinator
from src.agents.coordinators.memory_coordinator import MemoryCoordinator
from src.agents.coordinators.monitoring_coordinator import MonitoringCoordinator
from src.agents.coordinators.tool_coordinator import ToolCoordinator

__all__ = [
    "LLMCoordinator",
    "MemoryCoordinator",
    "MonitoringCoordinator",
    "ToolCoordinator",
    # 隔离系统
    "IsolationConfig",
    "load_config",
    "IsolationCoordinator",
    "create_isolation_coordinator",
    "get_isolation_coordinator",
    "IsolationToolWrapper",
    "wrap_executor_with_isolation",
]
