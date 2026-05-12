"""
SubAgent 管理器

此模块提供向后兼容的导入路径。

新代码应使用:
    from src.orchestration.task_client import TaskClient, SubAgentManager
"""

from src.orchestration.task_client import SubAgentManager, SubAgentManagerFactory

__all__ = ["SubAgentManager", "SubAgentManagerFactory"]
