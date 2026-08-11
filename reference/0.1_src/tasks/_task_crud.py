"""任务 CRUD Mixin — 创建、查询、字段更新与基础删除。

从 service.py 拆分出的职责域，提供 TaskService 的数据操作方法。
所有方法通过 ``self._storage`` 访问存储层，由 TaskService.__init__ 初始化。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


class _TaskCrudMixin:
    """任务 CRUD 操作 Mixin。"""

    async def create_task(
        self,
        title: str,
        description: str = "",
        parent_task_id: str | None = None,
        parent_pipeline_id: str | None = None,
        target_type: str | None = None,
        dependencies: list[str] | None = None,
        priority: Any = 5,
        agent_level: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """创建新任务并持久化到存储。

        Args:
            title: 任务标题
            description: 任务描述
            parent_task_id: 父任务 ID
            parent_pipeline_id: 父管道 ID
            target_type: 目标类型
            dependencies: 依赖任务 ID 列表
            priority: 优先级
            agent_level: Agent 层级
            metadata: 扩展元数据

        Returns:
            创建的 TaskModel 实例

        Raises:
            RuntimeError: 存储层未初始化（非门面模式）
        """
        if self._storage is None:
            raise RuntimeError("TaskService.create_task 需要门面模式（task_id=None）")

        from tasks.types import create_task as _create_task  # noqa: PLC0415

        task = _create_task(
            title=title,
            description=description,
            priority=priority,
            agent_level=agent_level,
            parent_task_id=parent_task_id,
            parent_pipeline_id=parent_pipeline_id,
            metadata=metadata,
            dependencies=dependencies,
            target_type=target_type,
        )

        self._storage.save(task)

        if metadata and metadata.get("task_scope") == "container":
            from tasks.types import TaskStatus  # noqa: PLC0415

            task.status = TaskStatus.RUNNING
            task.updated_at = datetime.now().isoformat()
            self._storage.save(task)
            logger.info(
                "[TaskService] 容器任务自动启动 | task_id=%s",
                task.id,
            )

        logger.info(
            "[TaskService] 任务已创建 | task_id=%s | title=%s",
            task.id,
            task.title,
        )
        return task

    async def bind_pipeline_run(self, task_id: str, pipeline_id: str) -> None:
        """将管道实例 ID 绑定到任务。

        Args:
            task_id: 任务 ID
            pipeline_id: 管道实例 ID

        Raises:
            KeyError: 任务不存在
            RuntimeError: 存储层未初始化
        """
        if self._storage is None:
            raise RuntimeError("TaskService.bind_pipeline_run 需要门面模式（task_id=None）")

        task = self._storage.get(task_id)
        if task is None:
            raise KeyError(f"任务不存在: {task_id}")

        task.pipeline_run_id = pipeline_id
        task.updated_at = datetime.now().isoformat()
        self._storage.save(task)
        logger.info(
            "[TaskService] 管道已绑定 | task_id=%s | pipeline_id=%s",
            task_id,
            pipeline_id,
        )

    def list_by_status(self, status: Any) -> list[Any]:
        """按状态筛选任务（委托给 TaskStorage）。

        Args:
            status: TaskStatus 枚举值

        Returns:
            匹配状态的任务列表
        """
        if self._storage is None:
            return []
        return self._storage.list_by_status(status)

    def list_subtasks(self, parent_id: str) -> list[Any]:
        """列出指定父任务的所有直接子任务（委托给 TaskStorage）。

        Args:
            parent_id: 父任务 ID

        Returns:
            子任务列表
        """
        if self._storage is None:
            return []
        return self._storage.list_by_parent(parent_id)

    def get_task(self, task_id: str) -> Any | None:
        """获取单个任务。

        Args:
            task_id: 任务 ID

        Returns:
            TaskModel 实例，不存在返回 None
        """
        if self._storage is None:
            return None
        return self._storage.get(task_id)

    async def list_all(
        self,
        limit: int = 1000,
        session_id: str | None = None,
        reverse: bool = False,
    ) -> list[Any]:
        """列出所有任务。

        Args:
            limit: 返回数量上限
            session_id: 按会话 ID 筛选（匹配 metadata.session_id）
            reverse: 是否按创建时间倒序

        Returns:
            TaskModel 列表
        """
        if self._storage is None:
            return []

        all_tasks = self.get_all_tasks()

        if session_id:
            all_tasks = [t for t in all_tasks if t.metadata.get("session_id") == session_id]

        all_tasks.sort(
            key=lambda t: t.created_at or "",
            reverse=reverse,
        )

        return all_tasks[:limit]

    async def save_task(self, task: Any) -> None:
        """持久化任务对象到存储。

        Args:
            task: TaskModel 实例
        """
        if self._storage is None:
            return

        task.updated_at = datetime.now().isoformat()
        self._storage.save(task)

    async def delete_task(self, task_id: str) -> bool:
        """删除任务（级联清理关联管道与子任务资源）。

        委托 soft_delete_container / hard_delete_task 完成完整级联清理，
        与 task 工具层删除路径保持一致：
          - 容器任务(task_scope=container): 软删除 + 级联清理子任务管道
          - 非容器任务: 硬删除 + 清理自身管道文件 + 级联清理子任务

        BUG-FIX-delete_task_pipeline_cascade:
        原实现仅删除任务记录，不清理 task.pipeline_run_id 对应的管道执行文件、
        不取消运行中的管道引擎、容器任务也不级联清理子任务管道，导致通过 API
        删除任务时管道残留。现统一复用已验证的级联清理路径。

        Args:
            task_id: 任务 ID

        Returns:
            是否删除成功
        """
        if self._storage is None:
            return False

        task = self._storage.get(task_id)
        if task is None:
            return False

        if (task.metadata or {}).get("task_scope") == "container":
            await self.soft_delete_container(task_id)
        else:
            await self.hard_delete_task(task_id)

        # soft/hard_delete 内部不触发 deleting→deleted 信号，
        # 下游 WebSocket/回调依赖此状态变更，需在此保留对外契约。
        await self._emit_state_change(task_id, "deleting", "deleted")
        return True

    async def hard_delete(self, task_id: str) -> bool:
        """硬删除任务记录和 YAML 文件（不检查子任务）。

        替代外部直接调用 service._storage.delete()。

        Args:
            task_id: 任务 ID

        Returns:
            是否删除成功
        """
        if self._storage is None:
            return False
        return self._storage.delete(task_id)

    def hard_delete_sync(self, task_id: str) -> bool:
        """硬删除任务记录（同步版本）。

        替代同步函数中的 service._storage.delete()。

        Args:
            task_id: 任务 ID

        Returns:
            是否删除成功
        """
        if self._storage is None:
            return False
        return self._storage.delete(task_id)

    def update_task_fields_sync(self, task_id: str, **fields) -> Any | None:
        """更新任务字段并持久化（同步版本）。

        替代同步函数中的 service._storage.save(tm)。

        Args:
            task_id: 任务 ID
            **fields: 要更新的字段

        Returns:
            更新后的任务模型
        """
        if self._storage is None:
            return None
        task = self._storage.get(task_id)
        if task is None:
            return None
        task.updated_at = datetime.now().isoformat()
        for k, v in fields.items():
            setattr(task, k, v)
        self._storage.save(task)
        return task

    def get_all_tasks(self) -> list:
        """获取全部任务的内存快照。

        替代外部直接访问 service._storage._tasks.values()。

        Returns:
            TaskModel 列表的浅拷贝
        """
        if self._storage is None:
            return []
        return list(self._storage._tasks.values())

    async def update_task_fields(self, task_id: str, **fields) -> Any | None:
        """更新任务指定字段并持久化。

        替代外部直接调用 service._storage.save(tm)。

        Args:
            task_id: 任务 ID
            **fields: 要更新的字段键值对

        Returns:
            更新后的任务模型，不存在时返回 None
        """
        if self._storage is None:
            return None
        task = self._storage.get(task_id)
        if task is None:
            return None
        task.updated_at = datetime.now().isoformat()
        for k, v in fields.items():
            setattr(task, k, v)
        self._storage.save(task)
        return task

    def get_root_task_id(self, task_id: str) -> str | None:
        """获取任务的根任务 ID。

        Args:
            task_id: 任务 ID

        Returns:
            根任务 ID，任务不存在返回 None
        """
        if self._storage is None:
            return None
        task = self._storage.get(task_id)
        if task is None:
            return None
        return self._storage._find_root_id(task)
