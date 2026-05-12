"""
文件系统任务存储实现

将任务数据存储为 JSON 文件，支持异步文件操作和文件锁。
"""

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import aiofiles
import aiofiles.os

from src.tasks.storage.base import ITaskStorage, StorageError

# BUG-FIX-fix_20260512_task_tree_500:
# 问题根因: file_storage 从 JSON 反序列化时使用 storage.base.TaskModel（Pydantic），
#           但 JSON 数据是 tasks.types.TaskModel（dataclass）格式存储的。
#           Pydantic TaskModel 缺少 metadata、pipeline_run_id、parent_pipeline_id、
#           agent_name、agent_level、error 等字段，导致 routes_missing.py 中
#           t.metadata.get("session_id") 抛出 AttributeError。
# 修复方案: 反序列化使用 dataclass 版本的 TaskModel，与 JSON 存储格式一致。
# 影响范围: FileTaskStorage 的 load/list_all/list_by_status 等读取方法。
from tasks.types import TaskModel

logger = logging.getLogger(__name__)


class FileTaskStorage(ITaskStorage):
    """
    文件系统任务存储

    将任务数据存储为 JSON 文件，每个任务一个文件。
    存储路径格式：{base_path}/{task_id}.json

    特性：
    - 使用 aiofiles 进行异步文件操作
    - 使用 asyncio.Lock 实现文件锁，防止并发写入冲突
    - 支持按状态分目录存储（可选）
    """

    # BUG-FIX-fix_20260512_async_compat:
    # 问题根因: TaskService 同步调用 FileTaskStorage 的异步方法，
    #           save()/get()/delete()/list_all() 等全部是 async，
    #           但 TaskService 全部以同步方式调用，导致返回 coroutine 对象。
    #           任务数据无法持久化到文件，list_all() 返回 coroutine。
    # 修复方案: 添加 _tasks 内存缓存，save() 时同步更新缓存，
    #           提供 get()/list_by_parent()/_find_root_id() 等同步方法，
    #           TaskService 读操作保持同步（从缓存），写操作改为 async。
    # 影响范围: 所有通过 TaskService 管理的任务 CRUD 和状态转换操作
    # 修复日期: 2025-05-12

    def __init__(self, base_path: str = "data/tasks"):
        """
        初始化文件存储

        Args:
            base_path: 存储根目录路径
        """
        self.base_path = Path(base_path)
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._initialized = False
        # 内存缓存：存储 TaskService 层的 TaskModel 对象（dataclass）
        self._tasks: dict[str, Any] = {}

    async def _ensure_initialized(self) -> None:
        """
        确保存储目录已初始化

        创建必要的目录结构。
        """
        if self._initialized:
            return

        async with self._global_lock:
            if self._initialized:
                return

            try:
                await aiofiles.os.makedirs(str(self.base_path), exist_ok=True)
                self._initialized = True
                logger.info("文件存储初始化完成: %s", self.base_path)
            except Exception as e:
                raise StorageError(f"初始化存储目录失败: {self.base_path}", e)

    def _get_task_path(self, task_id: str) -> Path:
        """
        获取任务文件路径

        Args:
            task_id: 任务ID

        Returns:
            任务文件路径
        """
        # 清理任务ID中的非法字符
        safe_id = "".join(c for c in task_id if c.isalnum() or c in "-_")
        return self.base_path / f"{safe_id}.json"

    async def _get_lock(self, task_id: str) -> asyncio.Lock:
        """
        获取任务对应的文件锁

        Args:
            task_id: 任务ID

        Returns:
            文件锁
        """
        async with self._global_lock:
            if task_id not in self._locks:
                self._locks[task_id] = asyncio.Lock()
            return self._locks[task_id]

    async def save(self, task: TaskModel) -> TaskModel:
        """
        保存任务到文件

        同时更新内存缓存，确保后续的同步 get() 调用能立即获取最新数据。
        兼容 dataclass 和 Pydantic 两种 TaskModel 类型。

        Args:
            task: 任务数据模型

        Returns:
            保存后的任务数据模型

        Raises:
            StorageError: 存储操作失败时抛出
        """
        # 先同步更新内存缓存（确保后续 get() 立即可用）
        self._tasks[task.id] = task

        await self._ensure_initialized()

        task_path = self._get_task_path(task.id)
        lock = await self._get_lock(task.id)

        async with lock:
            try:
                # BUG-FIX-fix_20260512_async_compat:
                # 兼容 dataclass（tasks.types.TaskModel）和
                # Pydantic（storage.base.TaskModel）两种模型
                if hasattr(task, 'model_dump'):
                    # Pydantic 模型
                    now = datetime.now(UTC)
                    if task.created_at is None:
                        task.created_at = now
                    task.updated_at = now
                    task_json = task.model_dump(mode="json")
                else:
                    # dataclass 模型（tasks.types.TaskModel）
                    task.updated_at = datetime.now().isoformat()
                    task_json = asdict(task)

                # 写入文件
                async with aiofiles.open(task_path, mode="w", encoding="utf-8") as f:
                    await f.write(json.dumps(
                        task_json,
                        ensure_ascii=False,
                        indent=2,
                        default=_task_json_default,
                    ))

                logger.debug("任务已保存: %s", task.id)
                return task

            except Exception as e:
                logger.error("保存任务失败: %s, 错误: %s", task.id, str(e))
                raise StorageError(f"保存任务失败: {task.id}", e)

    async def load(self, task_id: str) -> TaskModel | None:
        """
        从文件加载任务

        Args:
            task_id: 任务ID

        Returns:
            任务数据模型，如果不存在则返回 None
        """
        await self._ensure_initialized()

        task_path = self._get_task_path(task_id)
        lock = await self._get_lock(task_id)

        async with lock:
            try:
                if not await aiofiles.os.path.exists(task_path):
                    return None

                async with aiofiles.open(task_path, encoding="utf-8") as f:
                    content = await f.read()

                task_data = json.loads(content)
                return TaskModel(**task_data)

            except json.JSONDecodeError as e:
                logger.error("解析任务文件失败: %s, 错误: %s", task_path, str(e))
                raise StorageError(f"解析任务文件失败: {task_path}", e)
            except Exception as e:
                logger.error("加载任务失败: %s, 错误: %s", task_id, str(e))
                raise StorageError(f"加载任务失败: {task_id}", e)

    async def load_by_id(self, task_id: str) -> TaskModel | None:
        """
        根据ID加载任务（load 的别名）

        Args:
            task_id: 任务ID

        Returns:
            任务数据模型，如果不存在则返回 None
        """
        return await self.load(task_id)

    async def list_by_status(
        self,
        status: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TaskModel]:
        """
        按状态列出任务

        Args:
            status: 任务状态
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            任务列表
        """
        await self._ensure_initialized()

        tasks: list[TaskModel] = []

        try:
            # 遍历目录中的所有 JSON 文件
            entries = await aiofiles.os.listdir(str(self.base_path))

            for entry in entries:
                if not entry.endswith(".json"):
                    continue

                task_path = self.base_path / entry
                try:
                    async with aiofiles.open(task_path, encoding="utf-8") as f:
                        content = await f.read()

                    task_data = json.loads(content)
                    task = TaskModel(**task_data)

                    if task.status == status:
                        tasks.append(task)

                except (json.JSONDecodeError, Exception) as e:
                    logger.warning("跳过无效任务文件: %s, 错误: %s", entry, str(e))
                    continue

            # 按创建时间排序
            tasks.sort(key=lambda t: t.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)

            # 应用分页
            return tasks[offset : offset + limit]

        except Exception as e:
            logger.error("列出任务失败: %s", str(e))
            raise StorageError("列出任务失败", e)

    async def update_status(
        self,
        task_id: str,
        status: str,
        error_message: str | None = None,
    ) -> bool:
        """
        更新任务状态

        Args:
            task_id: 任务ID
            status: 新状态
            error_message: 错误信息（可选）

        Returns:
            是否更新成功
        """
        task = await self.load(task_id)
        if task is None:
            return False

        task.status = status
        task.updated_at = datetime.now(UTC)

        # 根据状态设置时间字段
        if status == "running" and task.started_at is None:
            task.started_at = datetime.now(UTC)
        elif status in ("completed", "failed"):
            task.completed_at = datetime.now(UTC)

        # 存储错误信息
        if error_message:
            metadata = task.task_metadata or {}
            metadata["error_message"] = error_message
            task.task_metadata = metadata

        await self.save(task)
        return True

    async def delete(self, task_id: str) -> bool:
        """
        删除任务文件

        同时从内存缓存中移除。

        Args:
            task_id: 任务ID

        Returns:
            是否删除成功
        """
        # 先从缓存移除
        self._tasks.pop(task_id, None)

        await self._ensure_initialized()

        task_path = self._get_task_path(task_id)
        lock = await self._get_lock(task_id)

        async with lock:
            try:
                if not await aiofiles.os.path.exists(task_path):
                    return False

                await aiofiles.os.remove(task_path)
                logger.debug("任务已删除: %s", task_id)
                return True

            except Exception as e:
                logger.error("删除任务失败: %s, 错误: %s", task_id, str(e))
                raise StorageError(f"删除任务失败: {task_id}", e)

    async def list_all(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TaskModel]:
        """
        列出所有任务

        Args:
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            任务列表
        """
        await self._ensure_initialized()

        tasks: list[TaskModel] = []

        try:
            entries = await aiofiles.os.listdir(str(self.base_path))

            for entry in entries:
                if not entry.endswith(".json"):
                    continue

                task_path = self.base_path / entry
                try:
                    async with aiofiles.open(task_path, encoding="utf-8") as f:
                        content = await f.read()

                    task_data = json.loads(content)
                    task = TaskModel(**task_data)
                    tasks.append(task)

                except (json.JSONDecodeError, Exception) as e:
                    logger.warning("跳过无效任务文件: %s, 错误: %s", entry, str(e))
                    continue

            # 按创建时间排序
            tasks.sort(key=lambda t: t.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)

            # 应用分页
            return tasks[offset : offset + limit]

        except Exception as e:
            logger.error("列出所有任务失败: %s", str(e))
            raise StorageError("列出所有任务失败", e)

    async def count_all(self) -> int:
        """
        统计任务总数

        Returns:
            任务总数
        """
        await self._ensure_initialized()

        try:
            entries = await aiofiles.os.listdir(str(self.base_path))
            return sum(1 for e in entries if e.endswith(".json"))
        except Exception as e:
            logger.error("统计任务数量失败: %s", str(e))
            raise StorageError("统计任务数量失败", e)

    async def count_by_status(self, status: str) -> int:
        """
        按状态统计任务数量

        Args:
            status: 任务状态

        Returns:
            该状态的任务数量
        """
        tasks = await self.list_by_status(status, limit=10000)
        return len(tasks)

    # ── 同步缓存方法（供 TaskService 的同步读操作使用） ──

    def get(self, task_id: str) -> Any | None:
        """同步获取任务（从内存缓存）。

        BUG-FIX-fix_20260512_async_compat:
        TaskService.get_task() 需要同步调用，但 FileTaskStorage 的 load()
        是异步的。添加此同步方法从内存缓存读取，避免 TaskService 需要改为 async。

        Args:
            task_id: 任务 ID

        Returns:
            任务模型（dataclass），不存在时返回 None
        """
        return self._tasks.get(task_id)

    def list_by_parent(self, parent_id: str) -> list[Any]:
        """同步列出子任务（从内存缓存）。

        BUG-FIX-fix_20260512_async_compat:
        TaskService.list_subtasks() 和 get_progress() 需要同步调用。

        Args:
            parent_id: 父任务 ID

        Returns:
            属于指定父任务的子任务列表
        """
        return [
            t for t in self._tasks.values()
            if getattr(t, 'parent_task_id', None) == parent_id
        ]

    def list_by_status_sync(self, status: Any) -> list[Any]:
        """同步按状态列出任务（从内存缓存）。

        BUG-FIX-fix_20260512_async_compat:
        TaskService.list_by_status() 和 TaskWorker 恢复任务时需要同步调用。

        Args:
            status: 任务状态（TaskStatus 枚举或字符串）

        Returns:
            匹配状态的任务列表
        """
        result = []
        for t in self._tasks.values():
            task_status = getattr(t, 'status', None)
            if task_status == status:
                result.append(t)
            elif hasattr(task_status, 'value') and task_status.value == status:
                result.append(t)
            elif task_status == status.value if hasattr(status, 'value') else False:
                result.append(t)
        return result

    def _find_root_id(self, task: Any) -> str:
        """沿 parent_task_id 链向上查找根任务 ID。

        BUG-FIX-fix_20260512_async_compat:
        TaskService.get_root_task_id() 需要同步调用。

        Args:
            task: 起始任务模型

        Returns:
            根任务 ID
        """
        visited: set[str] = set()
        current = task
        while current and getattr(current, 'parent_task_id', None):
            current_id = getattr(current, 'id', None)
            if current_id in visited:
                break
            visited.add(current_id)
            parent_id = current.parent_task_id
            parent = self._tasks.get(parent_id)
            if parent is None:
                break
            current = parent
        return getattr(current, 'id', getattr(task, 'id', ''))


def _task_json_default(obj: Any) -> Any:
    """JSON 序列化辅助函数，处理 dataclass 中的枚举等特殊类型。

    将枚举转为值，其他不可序列化对象转为字符串。
    """
    if isinstance(obj, Enum):
        return obj.value
    return str(obj)
