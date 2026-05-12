"""
任务存储模块

提供任务存储的抽象接口和多种存储后端实现。

使用示例：

    from src.tasks.storage import ITaskStorage, TaskModel, get_task_storage

    # 获取存储实例（根据配置自动选择）
    storage = get_task_storage(session=db_session)

    # 保存任务
    task = TaskModel(id="task-001", title="示例任务", status="pending")
    await storage.save(task)

    # 加载任务
    loaded = await storage.load("task-001")

    # 按状态列出任务
    pending_tasks = await storage.list_by_status("pending")

    # 更新状态
    await storage.update_status("task-001", "completed")

    # 删除任务
    await storage.delete("task-001")
"""

from src.tasks.storage.base import ITaskStorage, StorageError, TaskModel
from src.tasks.storage.db_storage import DatabaseTaskStorage
from src.tasks.storage.factory import (
    StorageType,
    TaskStorageFactory,
    get_task_storage,
)
from src.tasks.storage.file_storage import FileTaskStorage

__all__ = [
    # 抽象接口和数据模型
    "ITaskStorage",
    "TaskModel",
    "StorageError",
    # 存储实现
    "FileTaskStorage",
    "DatabaseTaskStorage",
    # 工厂
    "TaskStorageFactory",
    "get_task_storage",
    # 类型
    "StorageType",
]
