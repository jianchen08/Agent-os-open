"""
任务进度管理模块

跟踪任务执行进度,支持子任务层级、自动保存、断点续传。
"""

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from src.core.states import ExecutionStatus

logger = logging.getLogger(__name__)


class SubTask(BaseModel):
    """子任务"""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    parent_id: str | None = Field(None, description="父任务ID")
    title: str
    description: str | None = None
    status: ExecutionStatus = ExecutionStatus.PENDING

    # 执行信息
    start_time: datetime | None = None
    end_time: datetime | None = None
    error_message: str | None = None

    # 依赖关系
    dependencies: list[str] = Field(
        default_factory=list, description="依赖的任务ID列表"
    )

    # 进度
    progress_percent: float = Field(0.0, ge=0, le=100, description="进度百分比")

    # 元数据
    metadata: dict[str, Any] = Field(default_factory=dict)

    class Config:
        use_enum_values = False


class TaskProgress(BaseModel):
    """任务进度"""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    user_id: str | None = None

    # 任务信息
    title: str
    description: str | None = None
    status: ExecutionStatus = ExecutionStatus.PENDING

    # 时间信息
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    start_time: datetime | None = None
    end_time: datetime | None = None

    # 子任务
    subtasks: list[SubTask] = Field(default_factory=list)

    # 整体进度
    total_steps: int = 0
    completed_steps: int = 0
    progress_percent: float = 0.0

    # 错误信息
    error_message: str | None = None

    # 检查点数据
    checkpoint_data: dict[str, Any] = Field(default_factory=dict)

    class Config:
        use_enum_values = False

    def calculate_progress(self) -> None:
        """计算整体进度并更新时间戳"""
        if self.subtasks:
            self.completed_steps = sum(
                1 for st in self.subtasks if st.status == ExecutionStatus.COMPLETED
            )
            self.total_steps = len(self.subtasks)

        self.progress_percent = (
            (self.completed_steps / self.total_steps) * 100 if self.total_steps > 0 else 0.0
        )
        self.updated_at = datetime.now()


class TaskProgressManager:
    """
    任务进度管理器

    功能:
    - 管理任务和子任务的执行状态
    - 自动保存进度到数据库
    - 支持断点续传
    - 提供进度查询接口

    BUG-FIX-fix_20260226_session_leak: 修复会话泄漏问题
    问题根因: 手动调用 __aenter__ 而没有对应的 __aexit__，导致连接泄漏
    修复方案: 追踪上下文管理器，在 cleanup 中正确调用 __aexit__
    影响范围: 数据库连接池
    """

    def __init__(
        self,
        session_id: str,
        user_id: str | None = None,
        auto_save: bool = True,
        save_interval: int = 30,
    ):
        self.session_id = session_id
        self.user_id = user_id
        self.auto_save = auto_save
        self.save_interval = save_interval

        self._tasks: dict[str, TaskProgress] = {}
        self._current_task_id: str | None = None
        self._auto_save_task: asyncio.Task | None = None
        self._db_session: Any | None = None
        self._session_context: Any | None = None

    async def _get_db_session(self):
        """
        获取数据库会话

        使用上下文管理器追踪会话生命周期，确保连接正确释放。
        """
        if self._db_session is None:
            from src.db.session_manager import managed_session

            self._session_context = managed_session()
            self._db_session = await self._session_context.__aenter__()
        return self._db_session

    async def create_task(
        self,
        title: str,
        description: str | None = None,
        subtasks: list[dict[str, Any]] | None = None,
    ) -> TaskProgress:
        """
        创建新任务

        Args:
            title: 任务标题
            description: 任务描述
            subtasks: 子任务列表

        Returns:
            任务进度对象
        """
        task = TaskProgress(
            session_id=self.session_id,
            user_id=self.user_id,
            title=title,
            description=description,
        )

        # 添加子任务
        if subtasks:
            for st_data in subtasks:
                subtask = SubTask(parent_id=task.id, **st_data)
                task.subtasks.append(subtask)

        # 计算初始进度
        task.calculate_progress()

        # 保存
        self._tasks[task.id] = task
        self._current_task_id = task.id

        # 持久化
        if self.auto_save:
            await self._save_to_db(task)

        return task

    async def update_subtask(
        self,
        task_id: str,
        subtask_id: str,
        status: ExecutionStatus,
        progress_percent: float | None = None,
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """
        更新子任务状态

        Args:
            task_id: 任务 ID
            subtask_id: 子任务 ID
            status: 新状态
            progress_percent: 进度百分比
            error_message: 错误信息
            metadata: 元数据
        """
        task = self._tasks.get(task_id)
        if not task:
            raise ValueError(f"任务不存在: {task_id}")

        # 查找子任务
        subtask = None
        for st in task.subtasks:
            if st.id == subtask_id:
                subtask = st
                break

        if not subtask:
            raise ValueError(f"子任务不存在: {subtask_id}")

        # 更新状态
        subtask.status = status

        if progress_percent is not None:
            subtask.progress_percent = progress_percent

        if error_message:
            subtask.error_message = error_message

        if metadata:
            subtask.metadata.update(metadata)

        # 更新时间
        if status == ExecutionStatus.RUNNING and not subtask.start_time:
            subtask.start_time = datetime.now()
        elif status.is_terminal:
            subtask.end_time = datetime.now()

        # 重新计算整体进度
        task.calculate_progress()

        # 自动保存
        if self.auto_save:
            await self._save_to_db(task)

    async def update_task_status(
        self,
        task_id: str,
        status: ExecutionStatus,
        error_message: str | None = None,
    ) -> None:
        """
        更新任务状态

        Args:
            task_id: 任务 ID
            status: 新状态
            error_message: 错误信息
        """
        task = self._tasks.get(task_id)
        if not task:
            raise ValueError(f"任务不存在: {task_id}")

        task.status = status

        if error_message:
            task.error_message = error_message

        # 更新时间
        if status == ExecutionStatus.RUNNING and not task.start_time:
            task.start_time = datetime.now()
        elif status.is_terminal:
            task.end_time = datetime.now()

        # 自动保存
        if self.auto_save:
            await self._save_to_db(task)

    async def save_checkpoint(
        self,
        task_id: str,
        checkpoint_data: dict[str, Any],
    ) -> None:
        """
        保存检查点

        Args:
            task_id: 任务 ID
            checkpoint_data: 检查点数据
        """
        task = self._tasks.get(task_id)
        if not task:
            raise ValueError(f"任务不存在: {task_id}")

        task.checkpoint_data = checkpoint_data
        task.updated_at = datetime.now()

        # 持久化
        if self.auto_save:
            await self._save_to_db(task)

    async def get_task(self, task_id: str) -> TaskProgress | None:
        """获取任务进度"""
        return self._tasks.get(task_id)

    async def get_current_task(self) -> TaskProgress | None:
        """获取当前任务"""
        if self._current_task_id:
            return self._tasks.get(self._current_task_id)
        return None

    def list_tasks(self) -> list[TaskProgress]:
        """列出所有任务"""
        return list(self._tasks.values())

    async def resume_task(self, task_id: str) -> TaskProgress | None:
        """
        从内存缓存恢复任务

        注意：任务进度数据已通过 Task.progress_percent 和 ExecutionRecord 表持久化，
        不需要单独的数据库存储。此方法仅从内存缓存返回。

        Args:
            task_id: 任务 ID

        Returns:
            恢复的任务进度对象
        """
        return self._tasks.get(task_id)

    async def _save_to_db(self, task: TaskProgress) -> None:
        """保存到数据库（已废弃）

        注意：此方法已废弃，因为：
        1. Task.progress_percent 已存储任务整体进度
        2. ExecutionRecord 表记录了所有执行细节
        3. 进度可以从执行记录实时计算
        4. 避免数据冗余和维护成本

        此方法保留为空实现以保持向后兼容。
        """
        pass

    async def cleanup(self) -> None:
        """
        清理资源

        BUG-FIX-fix_20260226_session_leak: 正确释放数据库连接
        """
        if self._auto_save_task:
            self._auto_save_task.cancel()
            self._auto_save_task = None

        if self._session_context:
            try:
                await self._session_context.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"关闭数据库会话时出错: {e}")
            finally:
                self._session_context = None
                self._db_session = None
