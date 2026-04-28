"""任务服务 — 任务系统的业务编排层。

通过依赖注入组合状态机、存储和进度计算器，
提供任务生命周期管理的统一入口。

精简原则：
- 去掉事件驱动 → 同步方法调用
- 去掉消息总线 → 日志记录
- DI 注入基础设施组件（scheduler / concurrency 可选）
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from tasks.state_machine import InvalidTransitionError
from tasks.storage import TaskStorage
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
        TaskStatus.COMPLETED: [],
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
    """任务服务 — 任务生命周期管理的统一入口。

    组合状态机、存储和进度计算器，提供任务的创建、
    状态转换、查询和进度计算等业务操作。

    Attributes:
        _storage: 任务存储实例
        _state_machine: 状态机实例
        _progress: 进度计算器实例
        _scheduler: 调度器实例（可选，用于任务调度）
        _concurrency: 并发控制器（可选，用于资源管控）
    """

    def __init__(
        self,
        storage: TaskStorage | None = None,
        state_machine: SimpleStateMachine | None = None,
        progress: Any | None = None,
        *,
        scheduler: Any = None,
        concurrency: Any = None,
        on_state_change: Any = None,
    ) -> None:
        """初始化任务服务。

        Args:
            storage: 任务存储实例，None 时使用内存存储
            state_machine: 状态机实例，None 时创建默认实例
            progress: 进度计算器实例，None 时创建默认实例
            scheduler: 调度器实例（可选），来自 infrastructure 层
            concurrency: 并发控制器（可选），来自 infrastructure 层
            on_state_change: 状态变更回调函数(task_id, old_status, new_status)
        """
        # 默认使用文件存储，确保数据持久化
        if storage is None:
            from pathlib import Path
            data_dir = Path("data") / "tasks"
            data_dir.mkdir(parents=True, exist_ok=True)
            storage = TaskStorage(data_dir=data_dir)
        self._storage = storage
        self._state_machine = state_machine or SimpleStateMachine()
        self._progress = progress  # 可选，不再强制依赖 ProgressCalculator
        self._scheduler = scheduler
        self._concurrency = concurrency
        self._on_state_change = on_state_change

    # ── 创建 ────────────────────────────────────────────

    def create_task(
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
        self._storage.save(task)
        logger.info("Task created: %s (%s)", task.id, task.title)
        
        # 触发状态变更回调（PENDING 状态）
        if self._on_state_change:
            try:
                self._on_state_change(task.id, "", task.status.value, task=task)
            except Exception as e:
                logger.warning("State change callback failed: %s", e)
                
        return task

    # ── 状态转换 ─────────────────────────────────────────

    def start_task(self, task_id: str) -> TaskModel:
        """启动任务（pending → running）。

        Args:
            task_id: 任务 ID

        Returns:
            更新后的 TaskModel

        Raises:
            KeyError: 任务不存在
            InvalidTransitionError: 状态转换不合法
        """
        task = self._get_or_raise(task_id)
        self._transition_with_callback(task, TaskStatus.RUNNING)
        if not task.started_at:
            task.started_at = datetime.now().isoformat()
        self._storage.save(task)
        logger.info("Task started: %s", task_id)
        return task

    def complete_evaluation(
        self, task_id: str, passed: bool, result: Any = None,
    ) -> TaskModel:
        """完成评估（evaluating → completed / failed）。

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
        self._transition_with_callback(task, target)
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
        self._storage.save(task)
        logger.info(
            "Task %s evaluation: %s", task_id,
            "passed" if passed else "failed",
        )
        return task

    def recover_to_completed(
        self, task_id: str, result: Any = None,
    ) -> TaskModel:
        """评估通过时恢复被错误标记为 failed 的任务。

        仅用于评估通过覆盖 idle 超时等非业务原因导致的失败。
        绕过状态机的 FAILED→COMPLETED 限制，但仍触发回调和持久化。

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
        self._storage.save(task)

        if self._on_state_change:
            try:
                self._on_state_change(
                    task.id,
                    old_status.value,
                    TaskStatus.COMPLETED.value,
                    task=task,
                )
            except Exception as e:
                logger.warning(
                    "State change callback failed: %s", e
                )
        logger.info(
            "Task %s recovered: FAILED → COMPLETED", task_id,
        )
        return task

    def pause_task(self, task_id: str) -> TaskModel:
        """暂停任务（running → paused）。

        Args:
            task_id: 任务 ID

        Returns:
            更新后的 TaskModel

        Raises:
            KeyError: 任务不存在
            InvalidTransitionError: 状态转换不合法
        """
        task = self._get_or_raise(task_id)
        self._transition_with_callback(task, TaskStatus.PAUSED)
        logger.info("Task paused: %s", task_id)
        return task

    def resume_task(self, task_id: str) -> TaskModel:
        """恢复任务（paused → running）。

        Args:
            task_id: 任务 ID

        Returns:
            更新后的 TaskModel

        Raises:
            KeyError: 任务不存在
            InvalidTransitionError: 状态转换不合法
        """
        task = self._get_or_raise(task_id)
        self._transition_with_callback(task, TaskStatus.RUNNING)
        logger.info("Task resumed: %s", task_id)
        return task

    def reset_to_pending(self, task_id: str) -> TaskModel:
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
        self._storage.save(task)

        if self._on_state_change:
            try:
                self._on_state_change(task.id, old_status.value, TaskStatus.PENDING.value)
            except Exception as e:
                logger.warning("State change callback failed: %s", e)

        logger.info("Task reset to pending (recovery): %s (was %s)", task_id, old_status.value)
        return task

    def fail_task(self, task_id: str, error: str = "") -> TaskModel:
        """标记任务失败（running → failed）。

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
        self._transition_with_callback(task, TaskStatus.FAILED)
        if error:
            task.error = error
            self._storage.save(task)
        logger.info("Task failed: %s — %s", task_id, error or "no detail")
        return task

    def move_to_evaluating(self, task_id: str) -> TaskModel:
        """将任务移入评估阶段（running → evaluating）。

        Args:
            task_id: 任务 ID

        Returns:
            更新后的 TaskModel

        Raises:
            KeyError: 任务不存在
            InvalidTransitionError: 状态转换不合法
        """
        task = self._get_or_raise(task_id)
        self._transition_with_callback(task, TaskStatus.EVALUATING)
        logger.info("Task moved to evaluating: %s", task_id)
        return task

    def reject_task(self, task_id: str, reason: str = "", max_reject_count: int = 3) -> TaskModel:
        """打回任务（evaluating → running），让 Agent 重新执行。

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
            self._transition_with_callback(task, TaskStatus.FAILED)
            task.error = f"打回次数超过上限({max_reject_count}): {reason}"
            self._storage.save(task)
            return task

        # 正常打回：evaluating → running
        self._transition_with_callback(task, TaskStatus.RUNNING)
        if reason:
            task.error = f"打回重做({task.reject_count}/{max_reject_count}): {reason}"
        else:
            task.error = f"打回重做({task.reject_count}/{max_reject_count})"
        self._storage.save(task)
        logger.info("Task rejected (redo %d/%d): %s — %s", task.reject_count, max_reject_count, task_id, reason)
        return task

    # ── 查询 ─────────────────────────────────────────────

    def bind_pipeline_run(self, task_id: str, pipeline_run_id: str) -> TaskModel:
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
        self._storage.save(task)
        logger.info("Task %s bound to pipeline_run %s", task_id, pipeline_run_id)
        return task

    def get_root_task_id(self, task_id: str) -> str | None:
        """获取任务所属的根任务 ID。

        沿 parent_task_id 链向上遍历，直到找到最顶层根任务。

        Args:
            task_id: 任务 ID

        Returns:
            根任务 ID，任务不存在时返回 None
        """
        task = self._storage.get(task_id)
        if task is None:
            return None
        return self._storage._find_root_id(task)

    def bind_execution_record(self, task_id: str, record_id: str) -> TaskModel:
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
        self._storage.save(task)
        logger.info("Task %s bound to execution_record %s", task_id, record_id)
        return task

    def get_task(self, task_id: str) -> TaskModel | None:
        """获取任务。

        Args:
            task_id: 任务 ID

        Returns:
            任务模型，不存在时返回 None
        """
        return self._storage.get(task_id)

    def list_by_status(self, status: TaskStatus) -> list[TaskModel]:
        """按状态列出任务。

        Args:
            status: 任务状态

        Returns:
            匹配状态的任务列表
        """
        return self._storage.list_by_status(status)

    def list_subtasks(self, parent_id: str) -> list[TaskModel]:
        """列出子任务。

        Args:
            parent_id: 父任务 ID

        Returns:
            属于指定父任务的子任务列表
        """
        return self._storage.list_by_parent(parent_id)

    def list_all(self, limit: int = 50, reverse: bool = True) -> list[TaskModel]:
        """列出所有任务。

        Args:
            limit: 返回数量限制
            reverse: 是否按创建时间倒序

        Returns:
            任务列表
        """
        all_tasks = list(self._storage._tasks.values()) if hasattr(self._storage, '_tasks') else self._storage.list_all()
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

    def force_transition(self, task_id: str, target_status: TaskStatus) -> TaskModel:
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
        self._transition_with_callback(task, target_status)
        return task

    def delete_task(self, task_id: str) -> bool:
        """删除任务。

        Args:
            task_id: 任务 ID

        Returns:
            是否删除成功
        """
        task = self.get_task(task_id)
        if task is None:
            return False
        self._storage.delete(task_id)
        return True

    def save_task(self, task: TaskModel) -> None:
        """保存任务（供外部更新后调用）。

        Args:
            task: 任务模型
        """
        self._storage.save(task)

    # ── 进度 ─────────────────────────────────────────────

    def get_progress(self, parent_id: str) -> float:
        """计算父任务的子任务完成进度。

        Args:
            parent_id: 父任务 ID

        Returns:
            进度百分比（0.0 ~ 100.0），无子任务时返回 0.0
        """
        subtasks = self._storage.list_by_parent(parent_id)
        if not subtasks:
            return 0.0
        completed = sum(1 for t in subtasks if t.status == TaskStatus.COMPLETED)
        return (completed / len(subtasks)) * 100.0

    # ── 内部 ─────────────────────────────────────────────

    def _transition_with_callback(
        self,
        task: TaskModel,
        target_status: TaskStatus,
        old_status: TaskStatus | None = None,
    ) -> None:
        """执行状态转换并触发回调。

        Args:
            task: 任务模型
            target_status: 目标状态
            old_status: 原始状态（可选，用于回调）
        """
        old = old_status or task.status
        self._state_machine.transition(task, target_status)
        self._storage.save(task)

        if self._on_state_change:
            try:
                self._on_state_change(
                    task.id,
                    old.value,
                    target_status.value,
                    task=task,
                )
            except Exception as e:
                logger.warning("State change callback failed: %s", e)

    def _get_or_raise(self, task_id: str) -> TaskModel:
        """获取任务，不存在时抛出 KeyError。

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
