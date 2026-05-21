"""
任务服务模块 - 提供任务的业务逻辑处理。

重构说明：
- 状态机逻辑已迁移到 state_machine.py
- 本模块包含 TaskService，支持两种使用模式：
  1. 门面模式（task_id=None）：单例，提供全部任务的 CRUD 和状态操作，供 API 层使用
  2. 单任务模式（task_id 指定）：包装单个任务的状态机，向后兼容旧代码
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from src.tasks.state_machine import (
    InvalidTransitionError,
    SimpleStateMachine,
    _TASK_TRANSITIONS,
)

logger = logging.getLogger(__name__)


DEFAULT_TASK_TRANSITIONS: dict[str, list[str]] = {
    "pending": ["running"],
    "running": ["completed", "failed", "cancelled"],
    "completed": [],
    "failed": ["pending"],
    "cancelled": [],
}


def _default_data_dir() -> str:
    """推断任务 YAML 数据目录。

    service.py 位于 src/tasks/，data 目录位于项目根目录下 data/tasks/。
    """
    # src/tasks/service.py → src/tasks/ → src/ → project_root/
    return str(Path(__file__).resolve().parent.parent.parent / "data" / "tasks")


class TaskService:
    """任务服务类。

    支持两种使用模式：
    1. 门面模式（task_id=None）：单例，提供全部任务的 CRUD 和状态操作，供 API 层使用
    2. 单任务模式（task_id 指定）：包装单个任务的状态机，向后兼容旧 api/routes/tasks.py

    Args:
        task_id: 任务 ID（指定时进入单任务模式）
        initial_state: 初始状态（单任务模式使用，默认 pending）
        event_bus: 可选的事件总线实例，用于发布任务状态变更事件
        data_dir: YAML 存储目录（门面模式使用，None 时自动推断）
    """

    def __init__(
        self,
        task_id: str | None = None,
        initial_state: str = "pending",
        event_bus: Any | None = None,
        data_dir: str | None = None,
    ) -> None:
        self.task_id = task_id
        self._event_bus = event_bus

        # 单任务状态机（向后兼容：旧 api/routes/tasks.py 逐任务创建）
        self._state_machine = SimpleStateMachine(
            initial_state=initial_state,
            transitions=DEFAULT_TASK_TRANSITIONS,
        )

        # 门面模式的存储层（仅 task_id=None 时初始化）
        self._storage: Any = None
        if task_id is None:
            from tasks.storage import TaskStorage

            _dir = data_dir or _default_data_dir()
            self._storage = TaskStorage(data_dir=_dir)

    # ── 旧接口（单任务模式向后兼容）──────────────────────────────

    @property
    def state(self) -> str:
        """获取当前任务状态（单任务模式）。"""
        return self._state_machine.current_state

    def advance(self, target_state: str) -> None:
        """推进任务到目标状态（单任务模式）。

        Args:
            target_state: 目标状态。

        Raises:
            InvalidTransitionError: 当状态转换不被允许时。
        """
        self._state_machine.transition(target_state)

    # ── 门面模式：创建方法 ──────────────────────────────────────

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

        from tasks.types import create_task as _create_task

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
        logger.info(
            "[TaskService] 任务已创建 | task_id=%s | title=%s",
            task.id, task.title,
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
            task_id, pipeline_id,
        )


    # ── 门面模式：查询方法 ──────────────────────────────────────

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

        all_tasks = list(self._storage._tasks.values())

        if session_id:
            all_tasks = [
                t for t in all_tasks
                if t.metadata.get("session_id") == session_id
            ]

        all_tasks.sort(
            key=lambda t: t.created_at or "",
            reverse=reverse,
        )

        return all_tasks[:limit]

    # ── 门面模式：状态操作 ──────────────────────────────────────

    def can_transition(self, task_id: str, target_status: Any) -> bool:
        """检查任务是否可以转换到目标状态。

        BUG-FIX-fix_20260521_missing_method: 补充缺失的 can_transition 方法。
        问题根因: TaskService 缺少此方法，导致 task_manage 调用时 AttributeError。

        Args:
            task_id: 任务 ID
            target_status: 目标状态（TaskStatus 枚举或字符串）

        Returns:
            是否允许状态转换
        """
        if self._storage is None:
            return False

        task = self._storage.get(task_id)
        if task is None:
            return False

        current = task.status.value if hasattr(task.status, "value") else str(task.status)
        target = target_status.value if hasattr(target_status, "value") else str(target_status)
        allowed = _TASK_TRANSITIONS.get(current, [])
        return target in allowed

    def get_valid_transitions(self, task_id: str) -> list[str]:
        """获取任务当前状态可转换的目标状态列表。

        BUG-FIX-fix_20260521_missing_method: 补充缺失的 get_valid_transitions 方法。
        问题根因: TaskService 缺少此方法，导致 task_manage 调用时 AttributeError。

        Args:
            task_id: 任务 ID

        Returns:
            可转换的目标状态列表
        """
        if self._storage is None:
            return []

        task = self._storage.get(task_id)
        if task is None:
            return []

        current = task.status.value if hasattr(task.status, "value") else str(task.status)
        return _TASK_TRANSITIONS.get(current, [])

    async def force_transition(self, task_id: str, target_status: Any) -> None:
        """强制执行任务状态转换并持久化。

        BUG-FIX-fix_20260521_missing_method: 补充缺失的 force_transition 方法。
        问题根因: TaskService 缺少此方法，导致容器完成/失败/重试等操作 AttributeError。

        与 start_task / complete_task 等具体方法不同，此方法接受任意 TaskStatus，
        通过 _TASK_TRANSITIONS 校验合法性后执行转换。

        Args:
            task_id: 任务 ID
            target_status: 目标状态（TaskStatus 枚举）

        Raises:
            KeyError: 任务不存在
            InvalidTransitionError: 当前状态不允许转换到目标状态
        """
        if self._storage is None:
            raise KeyError(f"任务不存在: {task_id}")

        task = self._storage.get(task_id)
        if task is None:
            raise KeyError(f"任务不存在: {task_id}")

        from tasks.types import TaskStatus

        current = task.status.value if hasattr(task.status, "value") else str(task.status)
        target = target_status.value if hasattr(target_status, "value") else str(target_status)

        allowed = _TASK_TRANSITIONS.get(current, [])
        if target not in allowed:
            raise InvalidTransitionError(
                current, target,
                f"不允许从 '{current}' 转换到 '{target}'，合法目标: {allowed}",
            )

        task.status = TaskStatus(target)
        task.updated_at = datetime.now().isoformat()
        self._storage.save(task)

        await self._emit_state_change(task_id, current, target)

    async def pause_task(self, task_id: str) -> None:
        """暂停任务。

        将任务状态设置为 paused 并持久化到 YAML。

        Args:
            task_id: 任务 ID

        Raises:
            KeyError: 任务不存在
            InvalidTransitionError: 当前状态不允许暂停
        """
        if self._storage is None:
            raise KeyError(f"任务不存在: {task_id}")

        task = self._storage.get(task_id)
        if task is None:
            raise KeyError(f"任务不存在: {task_id}")

        from tasks.types import TaskStatus

        current = task.status.value if hasattr(task.status, "value") else str(task.status)
        allowed = {"running", "pending", "scheduled", "evaluating"}
        if current not in allowed:
            raise InvalidTransitionError(
                current, "paused",
                f"不允许从 '{current}' 暂停任务",
            )

        old_status = current
        task.status = TaskStatus.PAUSED
        task.updated_at = datetime.now().isoformat()
        self._storage.save(task)

        await self._emit_state_change(task_id, old_status, "paused")

    async def resume_task(self, task_id: str) -> Any:
        """恢复暂停的任务。

        将任务状态从 paused 变为 pending 并持久化。

        Args:
            task_id: 任务 ID

        Returns:
            恢复后的 TaskModel

        Raises:
            KeyError: 任务不存在
            InvalidTransitionError: 当前状态不允许恢复
        """
        if self._storage is None:
            raise KeyError(f"任务不存在: {task_id}")

        task = self._storage.get(task_id)
        if task is None:
            raise KeyError(f"任务不存在: {task_id}")

        from tasks.types import TaskStatus

        current = task.status.value if hasattr(task.status, "value") else str(task.status)
        if current != "paused":
            raise InvalidTransitionError(
                current, "pending",
                f"只有 paused 状态的任务可以恢复，当前: '{current}'",
            )

        old_status = current
        task.status = TaskStatus.PENDING
        task.updated_at = datetime.now().isoformat()
        self._storage.save(task)

        await self._emit_state_change(task_id, old_status, "pending")
        return task

    # ── 门面模式：TaskWorker 依赖的操作 ──────────────────────────

    async def start_task(self, task_id: str) -> None:
        """将任务从 pending 状态推进到 running。

        Args:
            task_id: 任务 ID

        Raises:
            KeyError: 任务不存在
            InvalidTransitionError: 当前状态不允许启动
        """
        if self._storage is None:
            raise KeyError(f"任务不存在: {task_id}")

        task = self._storage.get(task_id)
        if task is None:
            raise KeyError(f"任务不存在: {task_id}")

        from tasks.types import TaskStatus

        old_status = task.status.value if hasattr(task.status, "value") else str(task.status)
        if old_status not in ("pending", "running"):
            raise InvalidTransitionError(
                old_status, "running",
                f"不允许从 '{old_status}' 启动任务",
            )

        task.status = TaskStatus.RUNNING
        task.updated_at = datetime.now().isoformat()
        self._storage.save(task)

        await self._emit_state_change(task_id, old_status, "running")

    async def fail_task(self, task_id: str, reason: str = "") -> None:
        """将任务标记为失败。

        Args:
            task_id: 任务 ID
            reason: 失败原因
        """
        if self._storage is None:
            return

        task = self._storage.get(task_id)
        if task is None:
            return

        from tasks.types import TaskStatus

        old_status = task.status.value if hasattr(task.status, "value") else str(task.status)
        task.status = TaskStatus.FAILED
        task.updated_at = datetime.now().isoformat()
        if reason:
            task.metadata["fail_reason"] = reason
        self._storage.save(task)

        await self._emit_state_change(task_id, old_status, "failed")

    async def complete_task(self, task_id: str) -> None:
        """将任务标记为完成。

        Args:
            task_id: 任务 ID
        """
        if self._storage is None:
            return

        task = self._storage.get(task_id)
        if task is None:
            return

        from tasks.types import TaskStatus

        old_status = task.status.value if hasattr(task.status, "value") else str(task.status)
        task.status = TaskStatus.COMPLETED
        task.updated_at = datetime.now().isoformat()
        self._storage.save(task)

        await self._emit_state_change(task_id, old_status, "completed")

    async def complete_evaluation(
        self, task_id: str, passed: bool, result: dict | None = None
    ) -> None:
        """评估完成后更新任务状态。

        BUG-FIX-fix_20260521_missing_method: 补充缺失的 complete_evaluation 方法。
        问题根因: TaskService 缺少此方法，导致 task_evaluate 工具调用时 AttributeError。
        修复方案: 根据 passed 参数决定调用 complete_task 或 fail_task，并写入评估结果。

        Args:
            task_id: 任务 ID
            passed: 评估是否通过
            result: 评估结果数据
        """
        if self._storage is None:
            return

        task = self._storage.get(task_id)
        if task is None:
            return

        if result is not None:
            task.result = result

        if passed:
            await self.complete_task(task_id)
        else:
            await self.fail_task(task_id)

    async def reset_to_pending(self, task_id: str) -> None:
        """将任务重置为 pending 状态（用于恢复/重试）。

        Args:
            task_id: 任务 ID
        """
        if self._storage is None:
            return

        task = self._storage.get(task_id)
        if task is None:
            return

        from tasks.types import TaskStatus

        old_status = task.status.value if hasattr(task.status, "value") else str(task.status)
        task.status = TaskStatus.PENDING
        task.updated_at = datetime.now().isoformat()
        self._storage.save(task)

        await self._emit_state_change(task_id, old_status, "pending")

    async def save_task(self, task: Any) -> None:
        """持久化任务对象到存储。

        Args:
            task: TaskModel 实例
        """
        if self._storage is None:
            return

        task.updated_at = datetime.now().isoformat()
        self._storage.save(task)

    # ── 门面模式：删除操作 ──────────────────────────────────────

    async def delete_task(self, task_id: str) -> bool:
        """删除任务。

        容器任务（有子任务）执行软删除（标记 soft_deleted），
        非容器任务执行硬删除（从存储中移除）。

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

        # 检查是否为容器任务（有子任务）
        children = self._storage.list_by_parent(task_id)
        if children:
            # 容器任务：软删除
            task.metadata["soft_deleted"] = True
            task.updated_at = datetime.now().isoformat()
            self._storage.save(task)
        else:
            # 非容器任务：硬删除
            self._storage.delete(task_id)

        await self._emit_state_change(task_id, "deleting", "deleted")
        return True

    # ── 辅助方法 ────────────────────────────────────────────────

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

    async def _emit_state_change(
        self,
        task_id: str,
        old_status: str,
        new_status: str,
    ) -> None:
        """通过 EventBus 发布任务状态变更事件（best-effort）。

        Args:
            task_id: 任务 ID
            old_status: 原状态
            new_status: 新状态
        """
        if self._event_bus is None:
            return
        try:
            await self._event_bus.emit("task_state_changed", {
                "task_id": task_id,
                "old_status": old_status,
                "new_status": new_status,
            })
        except Exception as exc:
            logger.debug("emit task_state_changed 失败: %s", exc)
