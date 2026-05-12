"""任务服务 -- 任务系统的业务编排层。

通过依赖注入组合状态机、存储和进度计算器，
提供任务生命周期管理的统一入口。

BUG-FIX-fix_20260512_async_compat:
问题根因: FileTaskStorage 的 save()/delete() 等方法是 async，
          但 TaskService 以同步方式调用，导致返回 coroutine 对象。
          任务数据无法持久化，list_all() 返回 coroutine。
修复方案: 将所有调用 self._storage.save()/delete() 的方法改为 async，
          读操作（get_task/list_by_status/list_subtasks）保持同步（从缓存读取）。
影响范围: 所有通过 TaskService 管理的任务 CRUD 和状态转换操作。
修复日期: 2025-05-12

精简原则：
- 去掉事件驱动 -> 同步方法调用
- 去掉消息总线 -> 日志记录
- DI 注入基础设施组件（scheduler / concurrency 可选）
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from tasks.state_machine import InvalidTransitionError
from tasks.storage import FileTaskStorage
from tasks.types import TaskModel, TaskStatus, create_task

logger = logging.getLogger(__name__)


class SimpleStateMachine:
    """精简版任务状态机（无 DB 依赖）。

    定义合法的状态转换路径。
    """

    # 合法转换：{from_status: [to_status, ...]}
    TRANSITIONS: dict[TaskStatus, list[TaskStatus]] = {
        TaskStatus.PENDING: [TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.COMPLETED, TaskStatus.FAILED],
        TaskStatus.RUNNING: [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.EVALUATING, TaskStatus.PAUSED],
        TaskStatus.EVALUATING: [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.RUNNING],
        TaskStatus.FAILED: [TaskStatus.PENDING],
        TaskStatus.COMPLETED: [TaskStatus.PENDING],
        TaskStatus.PAUSED: [TaskStatus.PENDING, TaskStatus.RUNNING],
    }

    def can_transition(self, from_status: TaskStatus, to_status: TaskStatus) -> bool:
        """检查状态转换是否合法。"""
        allowed = self.TRANSITIONS.get(from_status, [])
        return to_status in allowed

    def transition(self, task: TaskModel, target_status: TaskStatus) -> None:
        """执行状态转换。

        Args:
            task: 任务模型
            target_status: 目标状态

        Raises:
            InvalidTransitionError: 状态转换不合法
        """
        if not self.can_transition(task.status, target_status):
            raise InvalidTransitionError(
                task.status.value,
                target_status.value,
            )
        task.status = target_status


class TaskService:
    """任务服务 -- 任务生命周期管理的统一入口。

    组合状态机、存储和进度计算器，提供任务的创建、
    状态转换、查询和进度计算等业务操作。

    状态变更时自动通过 EventBus 广播 ``task_state_changed`` 事件，
    无需外部注册回调。TaskWorker 订阅该事件后自动处理
    父管道通知、终态钩子等逻辑。

    Attributes:
        _storage: 任务存储实例
        _state_machine: 状态机实例
        _progress: 进度计算器实例
        _scheduler: 调度器实例（可选，用于任务调度）
        _concurrency: 并发控制器（可选，用于资源管控）
        _event_bus: 事件总线（可选，用于广播状态变更事件）
    """

    def __init__(
        self,
        storage: FileTaskStorage | None = None,
        state_machine: SimpleStateMachine | None = None,
        progress: Any | None = None,
        *,
        scheduler: Any = None,
        concurrency: Any = None,
        event_bus: Any = None,
        on_state_change: Any = None,
    ) -> None:
        """初始化任务服务。

        Args:
            storage: 任务存储实例，None 时使用文件存储
            state_machine: 状态机实例，None 时创建默认实例
            progress: 进度计算器实例，None 时创建默认实例
            scheduler: 调度器实例（可选），来自 infrastructure 层
            concurrency: 并发控制器（可选），来自 infrastructure 层
            event_bus: 事件总线（可选），传入后自动广播 task_state_changed
            on_state_change: 状态变更回调（已废弃，保留兼容）
        """
        if storage is None:
            from pathlib import Path
            data_dir = Path("data") / "tasks"
            data_dir.mkdir(parents=True, exist_ok=True)
            storage = FileTaskStorage(base_path=str(data_dir))
        self._storage = storage
        self._state_machine = state_machine or SimpleStateMachine()
        self._progress = progress
        self._scheduler = scheduler
        self._concurrency = concurrency
        self._event_bus = event_bus
        if on_state_change is not None:
            logger.warning(
                "TaskService: on_state_change 参数已废弃，"
                "请通过 event_bus 订阅 task_state_changed 事件"
            )

    # ── 创建 ────────────────────────────────────────────

    async def create_task(
        self,
        title: str,
        description: str = "",
        **kwargs: Any,
    ) -> TaskModel:
        """创建新任务并保存到存储。

        Args:
            title: 任务标题
            description: 任务描述
            **kwargs: 传递给 create_task 工厂函数的额外参数
                （priority, agent_level, parent_task_id, metadata,
                 agent_name, dependencies, execution_record_id, target_type）

        Returns:
            创建后的 TaskModel 实例（状态为 PENDING）
        """
        task = create_task(title=title, description=description, **kwargs)
        # BUG-FIX-fix_20260512_async_compat: save() 是 async，需要 await
        await self._storage.save(task)
        logger.info("Task created: %s (%s)", task.id, task.title)
        self._emit_state_changed(task, "", task.status)
                
        return task

    # ── 状态转换 ─────────────────────────────────────────

    async def start_task(self, task_id: str) -> TaskModel:
        """启动任务（pending -> running）。

        Args:
            task_id: 任务 ID

        Returns:
            更新后的 TaskModel

        Raises:
            KeyError: 任务不存在
            InvalidTransitionError: 状态转换不合法
        """
        task = self._get_or_raise(task_id)
        await self._transition_with_callback(task, TaskStatus.RUNNING)
        if not task.started_at:
            task.started_at = datetime.now().isoformat()
        # BUG-FIX-fix_20260512_async_compat: save() 是 async，需要 await
        await self._storage.save(task)
        logger.info("Task started: %s", task_id)
        return task

    async def complete_evaluation(
        self, task_id: str, passed: bool, result: Any = None,
    ) -> TaskModel:
        """完成评估（evaluating -> completed / failed）。

        评估结果会同时写入 task.result（最新一次）和追加到
        task.metadata["evaluation_history"]（保留全部历史），
        确保多次评估的每次结果都可追溯。

        Args:
            task_id: 任务 ID
            passed: 评估是否通过
            result: 评估结果数据（可选）

        Returns:
            更新后的 TaskModel

        Raises:
            KeyError: 任务不存在
            InvalidTransitionError: 状态转换不合法
        """
        task = self._get_or_raise(task_id)
        target = TaskStatus.COMPLETED if passed else TaskStatus.FAILED
        await self._transition_with_callback(task, target)
        task.completed_at = datetime.now().isoformat()
        if result is not None:
            task.result = result
            # 追加到评估历史（保留所有评估记录）
            if task.metadata is None:
                task.metadata = {}
            history = task.metadata.get("evaluation_history", [])
            if not isinstance(history, list):
                history = []
            history.append({
                "timestamp": datetime.now().isoformat(),
                "passed": passed,
                "data": result,
            })
            task.metadata["evaluation_history"] = history
        # BUG-FIX-fix_20260512_async_compat: save() 是 async，需要 await
        await self._storage.save(task)
        logger.info(
            "Task %s evaluation: %s", task_id,
            "passed" if passed else "failed",
        )
        return task

    async def recover_to_completed(
        self, task_id: str, result: Any = None,
    ) -> TaskModel:
        """评估通过时恢复被错误标记为 failed 的任务。

        仅用于评估通过覆盖 idle 超时等非业务原因导致的失败。
        绕过状态机的 FAILED->COMPLETED 限制，但仍触发回调和持久化。

        Args:
            task_id: 任务 ID
            result: 评估结果数据（可选）

        Returns:
            更新后的 TaskModel

        Raises:
            KeyError: 任务不存在
        """
        task = self._get_or_raise(task_id)
        if task.status != TaskStatus.FAILED:
            raise ValueError(
                f"recover_to_completed 仅用于 FAILED 状态，"
                f"当前状态: {task.status.value}"
            )
        old_status = task.status
        task.status = TaskStatus.COMPLETED
        task.completed_at = datetime.now().isoformat()
        task.error = None
        if result is not None:
            task.result = result
        # BUG-FIX-fix_20260512_async_compat: save() 是 async，需要 await
        await self._storage.save(task)
        self._emit_state_changed(task, old_status, TaskStatus.COMPLETED)
        logger.info(
            "Task %s recovered: FAILED -> COMPLETED", task_id,
        )
        return task

    async def pause_task(self, task_id: str) -> TaskModel:
        """暂停任务（running -> paused）。

        Args:
            task_id: 任务 ID

        Returns:
            更新后的 TaskModel

        Raises:
            KeyError: 任务不存在
            InvalidTransitionError: 状态转换不合法
        """
        task = self._get_or_raise(task_id)
        await self._transition_with_callback(task, TaskStatus.PAUSED)
        logger.info("Task paused: %s", task_id)
        return task

    async def resume_task(self, task_id: str) -> TaskModel:
        """恢复任务（paused -> running）。

        Args:
            task_id: 任务 ID

        Returns:
            更新后的 TaskModel

        Raises:
            KeyError: 任务不存在
            InvalidTransitionError: 状态转换不合法
        """
        task = self._get_or_raise(task_id)
        await self._transition_with_callback(task, TaskStatus.RUNNING)
        logger.info("Task resumed: %s", task_id)
        return task

    async def reactivate_task(self, task_id: str, message: str = "") -> TaskModel:
        """重新激活已完成任务（completed -> pending -> running）。

        用于任务完成后需要追加修改的场景：保持同一任务上下文，
        生成新管道继续执行。新需求通过 message 参数注入。

        Args:
            task_id: 任务 ID
            message: 追加需求描述（注入到新管道首轮）

        Returns:
            更新后的 TaskModel

        Raises:
            KeyError: 任务不存在
            InvalidTransitionError: 任务不是 completed 状态
        """
        task = self._get_or_raise(task_id)
        await self._transition_with_callback(task, TaskStatus.PENDING)

        # 清除上次运行的终态字段
        task.completed_at = ""
        task.error = ""
        task.reject_count = 0

        # 保留旧 pipeline_run_id 到元数据供追溯
        if task.metadata is None:
            task.metadata = {}
        prev_pipeline = task.pipeline_run_id
        if prev_pipeline:
            history = task.metadata.get("pipeline_history", [])
            if not isinstance(history, list):
                history = []
            history.append(prev_pipeline)
            task.metadata["pipeline_history"] = history
        task.pipeline_run_id = ""

        # 追加需求记录到元数据
        if message:
            reqs = task.metadata.get("reactivate_requirements", [])
            if not isinstance(reqs, list):
                reqs = []
            reqs.append({
                "message": message,
                "timestamp": datetime.now().isoformat(),
            })
            task.metadata["reactivate_requirements"] = reqs

        # BUG-FIX-fix_20260512_async_compat: save() 是 async，需要 await
        await self._storage.save(task)
        logger.info("Task reactivated: %s (was completed)", task_id)
        return task

    async def reset_to_pending(self, task_id: str) -> TaskModel:
        """强制重置任务为 pending（用于 Worker 启动恢复场景）。

        绕过状态机，将 running/failed 等状态的任务重置为 pending，
        以便重新拾取执行。同时清除 started_at 等运行时字段。

        Args:
            task_id: 任务 ID

        Returns:
            更新后的 TaskModel

        Raises:
            KeyError: 任务不存在
        """
        task = self._get_or_raise(task_id)
        old_status = task.status
        task.status = TaskStatus.PENDING
        task.started_at = ""
        task.error = ""
        # BUG-FIX-fix_20260512_async_compat: save() 是 async，需要 await
        await self._storage.save(task)
        self._emit_state_changed(task, old_status, TaskStatus.PENDING)

        logger.info("Task reset to pending (recovery): %s (was %s)", task_id, old_status.value)
        return task

    async def fail_task(self, task_id: str, error: str = "") -> TaskModel:
        """标记任务失败（running -> failed）。

        Args:
            task_id: 任务 ID
            error: 错误信息

        Returns:
            更新后的 TaskModel

        Raises:
            KeyError: 任务不存在
            InvalidTransitionError: 状态转换不合法
        """
        task = self._get_or_raise(task_id)
        await self._transition_with_callback(task, TaskStatus.FAILED)
        if error:
            task.error = error
            # BUG-FIX-fix_20260512_async_compat: save() 是 async，需要 await
            await self._storage.save(task)
        logger.info("Task failed: %s -- %s", task_id, error or "no detail")
        return task

    async def move_to_evaluating(self, task_id: str) -> TaskModel:
        """将任务移入评估阶段（running -> evaluating）。

        Args:
            task_id: 任务 ID

        Returns:
            更新后的 TaskModel

        Raises:
            KeyError: 任务不存在
            InvalidTransitionError: 状态转换不合法
        """
        task = self._get_or_raise(task_id)
        await self._transition_with_callback(task, TaskStatus.EVALUATING)
        logger.info("Task moved to evaluating: %s", task_id)
        return task

    async def reject_task(self, task_id: str, reason: str = "", max_reject_count: int = 3) -> TaskModel:
        """打回任务（evaluating -> running），让 Agent 重新执行。

        打回次数有限制，超过 max_reject_count 则转为 failed，
        防止无限循环。

        Args:
            task_id: 任务 ID
            reason: 打回原因
            max_reject_count: 最大打回次数，默认 3

        Returns:
            更新后的 TaskModel

        Raises:
            KeyError: 任务不存在
            InvalidTransitionError: 状态转换不合法
        """
        task = self._get_or_raise(task_id)
        task.reject_count += 1

        if task.reject_count >= max_reject_count:
            # 打回次数超限，直接失败
            logger.warning(
                "Task %s reject count (%d) exceeded max (%d), marking as failed",
                task_id, task.reject_count, max_reject_count,
            )
            await self._transition_with_callback(task, TaskStatus.FAILED)
            task.error = f"打回次数超过上限({max_reject_count}): {reason}"
            # BUG-FIX-fix_20260512_async_compat: save() 是 async，需要 await
            await self._storage.save(task)
            return task

        # 正常打回：evaluating -> running
        await self._transition_with_callback(task, TaskStatus.RUNNING)
        if reason:
            task.error = f"打回重做({task.reject_count}/{max_reject_count}): {reason}"
        else:
            task.error = f"打回重做({task.reject_count}/{max_reject_count})"
        # BUG-FIX-fix_20260512_async_compat: save() 是 async，需要 await
        await self._storage.save(task)
        logger.info("Task rejected (redo %d/%d): %s -- %s", task.reject_count, max_reject_count, task_id, reason)
        return task

    # ── 查询 ─────────────────────────────────────────────

    async def bind_pipeline_run(self, task_id: str, pipeline_run_id: str) -> TaskModel:
        """将任务绑定到管道运行实例。

        由 CLI 入口在管道启动后调用，回填 pipeline_run_id。

        Args:
            task_id: 任务 ID
            pipeline_run_id: 管道运行 ID

        Returns:
            更新后的 TaskModel

        Raises:
            KeyError: 任务不存在
        """
        task = self._get_or_raise(task_id)
        task.pipeline_run_id = pipeline_run_id
        # BUG-FIX-fix_20260512_async_compat: save() 是 async，需要 await
        await self._storage.save(task)
        logger.info("Task %s bound to pipeline_run %s", task_id, pipeline_run_id)
        return task

    def get_root_task_id(self, task_id: str) -> str | None:
        """获取任务所属的根任务 ID。

        沿 parent_task_id 链向上遍历，直到找到最顶层根任务。
        使用内存缓存，同步调用。

        Args:
            task_id: 任务 ID

        Returns:
            根任务 ID，任务不存在时返回 None
        """
        task = self._storage.get(task_id)
        if task is None:
            return None
        return self._storage._find_root_id(task)

    async def bind_execution_record(self, task_id: str, record_id: str) -> TaskModel:
        """将任务绑定到执行记录（出生证明）。

        Args:
            task_id: 任务 ID
            record_id: 执行记录 ID

        Returns:
            更新后的 TaskModel

        Raises:
            KeyError: 任务不存在
        """
        task = self._get_or_raise(task_id)
        task.execution_record_id = record_id
        # BUG-FIX-fix_20260512_async_compat: save() 是 async，需要 await
        await self._storage.save(task)
        logger.info("Task %s bound to execution_record %s", task_id, record_id)
        return task

    def get_task(self, task_id: str) -> TaskModel | None:
        """获取任务（同步，从内存缓存读取）。

        BUG-FIX-fix_20260512_async_compat:
        保持同步方法，从 FileTaskStorage 的内存缓存读取，
        避免大量调用方需要改为 async。

        Args:
            task_id: 任务 ID

        Returns:
            任务模型，不存在时返回 None
        """
        return self._storage.get(task_id)

    def list_by_status(self, status: TaskStatus) -> list[TaskModel]:
        """按状态列出任务（同步，从内存缓存读取）。

        BUG-FIX-fix_20260512_async_compat:
        改为从 FileTaskStorage 的内存缓存读取，
        避免调用异步的 storage.list_by_status()。

        Args:
            status: 任务状态

        Returns:
            匹配状态的任务列表
        """
        # 优先从缓存读取
        if hasattr(self._storage, '_tasks') and self._storage._tasks:
            return [
                t for t in self._storage._tasks.values()
                if getattr(t, 'status', None) == status
            ]
        return []

    def list_subtasks(self, parent_id: str) -> list[TaskModel]:
        """列出子任务（同步，从内存缓存读取）。

        BUG-FIX-fix_20260512_async_compat:
        改为从 FileTaskStorage 的内存缓存读取，
        避免调用不存在的 list_by_parent() 异步方法。

        Args:
            parent_id: 父任务 ID

        Returns:
            属于指定父任务的子任务列表
        """
        if hasattr(self._storage, 'list_by_parent'):
            return self._storage.list_by_parent(parent_id)
        return [
            t for t in self._storage._tasks.values()
            if getattr(t, 'parent_task_id', None) == parent_id
        ]

    # BUG-FIX-fix_20260512_async_list_all:
    # 问题根因: FileTaskStorage.list_all() 是 async 方法，但 TaskService.list_all() 是同步方法。
    #           在 async 上下文中调用时返回 coroutine 对象而非 list，导致 .sort() 失败：
    #           'coroutine' object has no attribute 'sort'。
    # 修复方案: 将 list_all 改为 async 方法，正确 await 存储层调用。
    # 影响范围: 所有调用 TaskService.list_all() 的地方均需同步更新为 await 调用。
    # 修复日期: 2025-05-12
    async def list_all(self, limit: int = 50, reverse: bool = True) -> list[TaskModel]:
        """列出所有任务。

        Args:
            limit: 返回数量限制
            reverse: 是否按创建时间倒序

        Returns:
            任务列表
        """
        if hasattr(self._storage, '_tasks') and self._storage._tasks:
            all_tasks = list(self._storage._tasks.values())
        elif hasattr(self._storage, 'list_all'):
            all_tasks = await self._storage.list_all()
        else:
            all_tasks = []
        all_tasks.sort(key=lambda t: t.created_at, reverse=reverse)
        return all_tasks[:limit]

    def can_transition(self, task_id: str, target_status: TaskStatus) -> bool:
        """检查任务是否可以转换到目标状态。

        Args:
            task_id: 任务 ID
            target_status: 目标状态

        Returns:
            是否可以转换，任务不存在时返回 False
        """
        task = self.get_task(task_id)
        if task is None:
            return False
        return self._state_machine.can_transition(task.status, target_status)

    def get_valid_transitions(self, task_id: str) -> list[str]:
        """获取任务可转换的目标状态列表。

        Args:
            task_id: 任务 ID

        Returns:
            可转换状态值列表，任务不存在时返回空列表
        """
        task = self.get_task(task_id)
        if task is None:
            return []
        return [s.value for s in self._state_machine.TRANSITIONS.get(task.status, [])]

    async def force_transition(self, task_id: str, target_status: TaskStatus) -> TaskModel:
        """强制转换任务状态（含回调通知）。

        Args:
            task_id: 任务 ID
            target_status: 目标状态

        Returns:
            更新后的 TaskModel

        Raises:
            KeyError: 任务不存在
            InvalidTransitionError: 状态转换不合法
        """
        task = self._get_or_raise(task_id)
        await self._transition_with_callback(task, target_status)
        return task

    async def delete_task(self, task_id: str) -> bool:
        """删除任务，并清理关联的 worktree。

        Args:
            task_id: 任务 ID

        Returns:
            是否删除成功
        """
        task = self.get_task(task_id)
        if task is None:
            return False
        self._cleanup_workspace(task_id)
        # BUG-FIX-fix_20260512_async_compat: delete() 是 async，需要 await
        await self._storage.delete(task_id)
        return True

    async def save_task(self, task: TaskModel) -> None:
        """保存任务（供外部更新后调用）。

        Args:
            task: 任务模型
        """
        # BUG-FIX-fix_20260512_async_compat: save() 是 async，需要 await
        await self._storage.save(task)

    # ── 清理 ─────────────────────────────────────────────

    def _cleanup_workspace(self, task_id: str) -> None:
        """尝试清理任务关联的 worktree（best-effort）。"""
        try:
            from infrastructure.service_provider import (
                get_service_provider,
            )
            provider = get_service_provider()
            lifecycle = provider.get(
                "workspace_lifecycle_manager")
            if lifecycle:
                lifecycle.cleanup_workspace(task_id)
                logger.info(
                    "Task %s workspace cleaned up", task_id)
        except Exception as e:
            logger.warning(
                "Task %s workspace cleanup failed "
                "(non-fatal): %s", task_id, e,
            )

    # ── 进度 ─────────────────────────────────────────────

    def get_progress(self, parent_id: str) -> float:
        """计算父任务的子任务完成进度（同步，从缓存读取）。

        BUG-FIX-fix_20260512_async_compat:
        改为从内存缓存读取子任务列表。

        Args:
            parent_id: 父任务 ID

        Returns:
            进度百分比（0.0 ~ 100.0），无子任务时返回 0.0
        """
        subtasks = self.list_subtasks(parent_id)
        if not subtasks:
            return 0.0
        completed = sum(1 for t in subtasks if t.status == TaskStatus.COMPLETED)
        return (completed / len(subtasks)) * 100.0

    # ── 内部 ─────────────────────────────────────────────

    async def _transition_with_callback(
        self,
        task: TaskModel,
        target_status: TaskStatus,
        old_status: TaskStatus | None = None,
    ) -> None:
        """执行状态转换并通过 EventBus 广播事件。

        BUG-FIX-fix_20260512_async_compat:
        改为 async，因为内部调用 self._storage.save()。

        Args:
            task: 任务模型
            target_status: 目标状态
            old_status: 原始状态（可选，用于事件数据）
        """
        old = old_status or task.status
        self._state_machine.transition(task, target_status)
        # BUG-FIX-fix_20260512_async_compat: save() 是 async，需要 await
        await self._storage.save(task)

        if self._event_bus is not None:
            try:
                import asyncio

                event_data: dict[str, Any] = {
                    "task_id": task.id,
                    "old_status": old.value,
                    "new_status": target_status.value,
                    "source": "task_service",
                    "task": task,
                }
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        self._event_bus.emit("task_state_changed", event_data),
                    )
                    logger.info(
                        "TaskService: 事件已调度 | task=%s, %s -> %s",
                        task.id, old.value, target_status.value,
                    )
                except RuntimeError:
                    logger.warning(
                        "TaskService: 无 event loop，事件丢失 | task=%s, %s -> %s",
                        task.id, old.value, target_status.value,
                    )
            except Exception as e:
                logger.warning("EventBus emit failed: %s", e)

    def _emit_state_changed(
        self,
        task: TaskModel,
        old_status: TaskStatus | str,
        new_status: TaskStatus,
    ) -> None:
        """通过 EventBus 广播状态变更事件（非状态机转换场景）。"""
        old_val = old_status.value if hasattr(old_status, "value") else str(old_status)
        if self._event_bus is not None:
            try:
                import asyncio

                event_data: dict[str, Any] = {
                    "task_id": task.id,
                    "old_status": old_val,
                    "new_status": new_status.value,
                    "source": "task_service",
                    "task": task,
                }
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        self._event_bus.emit("task_state_changed", event_data),
                    )
                except RuntimeError:
                    logger.warning(
                        "TaskService: 无 event loop(_emit_state_changed)，事件丢失 | "
                        "task=%s, %s -> %s",
                        task.id, old_val, new_status.value,
                    )
            except Exception as e:
                logger.warning("EventBus emit failed: %s", e)

    def _get_or_raise(self, task_id: str) -> TaskModel:
        """获取任务，不存在时抛出 KeyError。

        BUG-FIX-fix_20260512_async_compat:
        保持同步，从 FileTaskStorage 内存缓存读取。

        Args:
            task_id: 任务 ID

        Returns:
            任务模型

        Raises:
            KeyError: 任务不存在
        """
        task = self._storage.get(task_id)
        if task is None:
            raise KeyError(f"Task '{task_id}' not found")
        return task
