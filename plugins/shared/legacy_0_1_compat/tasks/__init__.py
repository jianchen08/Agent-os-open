"""任务模块 (0.1 兼容 shim) — 统一导出状态机和服务。

NOTE: 与原始 0.1 ``tasks/__init__.py`` 不同，本 shim 使用 ``tasks.*`` 而非
``src.tasks.*`` 导入路径，因为 compat 包本身就是顶层（其根目录由
``server.py`` 注入到 ``sys.path``）。这避免了 0.1 的 ``src`` 命名空间在
0.2 项目中不存在导致的 ImportError。
"""

from tasks.service import TaskService
from tasks.state_machine import InvalidTransitionError, SimpleStateMachine

__all__ = [
    "SimpleStateMachine",
    "InvalidTransitionError",
    "TaskService",
]
