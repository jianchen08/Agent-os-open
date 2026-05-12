"""
内置Agent配置模块

提供系统内置的Agent配置和加载器
"""

from .loader import (
    AgentNames,
    BuiltinAgentLoader,
    get_loader,
    load_agent,
    load_all_agents,
)

__all__ = [
    "BuiltinAgentLoader",
    "AgentNames",
    "get_loader",
    "load_agent",
    "load_all_agents",
]
