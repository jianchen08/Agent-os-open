"""
任务服务模块 - 提供任务的业务逻辑处理。

重构说明：
- 状态机逻辑已迁移到 state_machine.py
- 本模块包含 TaskService，提供全部任务的 CRUD 和状态操作。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.tasks.state_machine import (
    InvalidTransitionError,
    _TASK_TRANSITIONS,
)

logger = logging.getLogger(__name__)

StateChangeCallback = Callable[[str, str, str], Awaitable[None]]


def _default_data_dir() -> str:
    """推断任务 YAML 数据目录。

    service.py 位于 src/tasks/，data 目录位于项目根目录下 data/tasks/。
    """
    # src/tasks/service.py → src/tasks/ → src/ → project_root/
    return str(Path(__file__).resolve().parent.parent.parent / "data" / "tasks")


class TaskService:
    """任务服务类。

    门面模式服务，提供全部任务的 CRUD 和状态操作。

    Args:
        task_id: 任务 ID（保留参数，兼容外部调用签名）
        initial_state: 初始状态（已弃用，保留参数签名兼容）
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
        self._state_callbacks: list[StateChangeCallback] = []

        # 门面模式的存储层（仅 task_id=None 时初始化）
        self._storage: Any = None
        if task_id is None:
            from tasks.storage import TaskStorage

            _dir = data_dir or _default_data_dir()
            self._storage = TaskStorage(data_dir=_dir)

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

        all_tasks = self.get_all_tasks()

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

    async def pause_task(self, task_id: str, paused_by: str = "user") -> None:
        """暂停任务。

        将任务状态设置为 paused 并持久化到 YAML。

        Args:
            task_id: 任务 ID
            paused_by: 暂停来源，"user"（用户手动暂停）或 "system"（系统关闭时暂停），
                       用于重启时区分是否需要恢复。

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
                current, "suspended",
                f"不允许从 '{current}' 暂停任务",
            )

        old_status = current
        task.status = TaskStatus.SUSPENDED
        task.updated_at = datetime.now().isoformat()
        # BUG-FIX-fix_20260603_pause_metadata:
        # 记录暂停来源，重启时区分用户暂停（应保持 SUSPENDED）和系统暂停（应恢复）
        if task.metadata is None:
            task.metadata = {}
        task.metadata["paused_by"] = paused_by
        self._storage.save(task)

        await self._emit_state_change(task_id, old_status, "suspended")

    async def resume_task(self, task_id: str) -> Any:
        """恢复暂停的任务。

        将任务状态从 paused 变为 running 并持久化，同时唤醒挂起的管道引擎。

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
        if current != "suspended":
            raise InvalidTransitionError(
                current, "running",
                f"只有 paused 状态的任务可以恢复，当前: '{current}'",
            )

        old_status = current
        # BUG-FIX-fix_20260603_resume_wake_engine:
        # resume 后应设为 RUNNING（而非 PENDING），因为挂起的管道引擎需要继续执行，
        # 不能等 TaskWorker 重新拾取 PENDING 任务再起新管道。
        task.status = TaskStatus.RUNNING
        task.updated_at = datetime.now().isoformat()
        # 清除暂停来源标记（恢复后不再需要）
        if task.metadata:
            task.metadata.pop("paused_by", None)
        self._storage.save(task)

        await self._emit_state_change(task_id, old_status, "running")

        # 唤醒挂起的管道引擎：任务状态已恢复为 RUNNING，pause_guard 下次检查
        # 会看到非 SUSPENDED 状态，管道自动继续执行。
        try:
            from pipeline.registry import get_engine_registry
            entries = get_engine_registry().find_by_tag("task_id", task_id)
            for entry in entries:
                if entry.engine is not None and entry.engine.is_suspended:
                    entry.engine.wake()
                    logger.info(
                        "TaskService: 唤醒挂起引擎 task_id=%s pipeline=%s",
                        task_id, entry.pipeline_id[:12],
                    )
        except Exception as exc:
            logger.debug(
                "TaskService: 唤醒引擎失败（非致命）task_id=%s: %s",
                task_id, exc,
            )

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
            # BUG-FIX-fix_20260524_error_not_propagated:
            # 问题根因: fail_task 只将失败原因写入 metadata["fail_reason"]，
            #   不写入 task.error 字段。前端和 API 读取 task.error 展示错误信息，
            #   导致任务失败后前端看不到失败原因，状态显示不完整。
            # 修复方案: 同时写入 task.error 字段，确保前端能正确展示失败原因。
            # 影响范围: 所有调用 fail_task 的场景（管道异常、超时、工作空间失败等）。
            # 修复日期: 2026-05-24
            #
            # BUG-FIX-fix_20260606_error_accumulate:
            # 问题根因: fail_task 在链路中可能被多次调用（引擎异常退出 →
            #   _mark_task_failed_on_engine_exit → _fail_after_pipeline_exit →
            #   cleanup_ghost_tasks），每次调用 task.error = reason 会覆盖之前的诊断。
            #   修复方案: 追加而非覆盖，保留完整错误链。
            #   修复日期: 2026-06-06
            if task.error and task.error != reason:
                task.error = f"{task.error} → {reason}"
            else:
                task.error = reason
        self._storage.save(task)

        await self._emit_state_change(task_id, old_status, "failed")

        # BUG-FIX-fix_20260603_fail_task_cascade:
        # 问题根因: fail_task 只将当前任务标记为 FAILED，不处理子任务，
        #   导致父任务失败后子任务仍然在运行（状态为 RUNNING/EVALUATING），
        #   出现"父任务已失败但子任务还在跑"的不一致状态。
        # 修复方案: 父任务失败时级联取消所有非终态的子任务（递归），
        #   子任务标记为 CANCELLED（而非 FAILED），因为子任务本身未执行失败，
        #   而是因父任务失败被终止。
        # 影响范围: 所有调用 fail_task 的场景（管道异常、超时、工作空间失败等）。
        # 修复日期: 2026-06-03
        _cascade_count = await self.fail_task_cascade(task_id, reason=reason)
        if _cascade_count > 0:
            logger.info(
                "TaskService: fail_task cascade 完成 | parent=%s, cancelled_subtasks=%d",
                task_id, _cascade_count,
            )

    # BUG-FIX-fix_20260523_cancel_task:
    # 问题根因: cancel_task_cascade 复用 fail_task 将子任务状态设为 FAILED，
    #           无法区分"用户主动取消"和"执行失败"，前端无法正确展示取消状态。
    # 修复方案: 新增 cancel_task 方法，设置状态为 CANCELLED 而非 FAILED，
    #           记录取消原因到 metadata["cancel_reason"]。
    # 影响范围: 任务取消功能，routes_tasks.py 的 cancel 端点。
    # 修复日期: 2026-05-23
    async def cancel_task(self, task_id: str, reason: str = "") -> None:
        """将任务标记为已取消。

        与 fail_task 不同，此方法将状态设为 CANCELLED，
        用于区分用户主动取消和任务执行失败。

        Args:
            task_id: 任务 ID
            reason: 取消原因
        """
        if self._storage is None:
            return

        task = self._storage.get(task_id)
        if task is None:
            return

        from tasks.types import TaskStatus

        old_status = task.status.value if hasattr(task.status, "value") else str(task.status)
        task.status = TaskStatus.CANCELLED
        task.updated_at = datetime.now().isoformat()
        if reason:
            task.metadata["cancel_reason"] = reason
            # BUG-FIX-fix_20260606_error_accumulate: 追加而非覆盖
            if task.error and task.error != reason:
                task.error = f"{task.error} → {reason}"
            else:
                task.error = reason
        self._storage.save(task)

        await self._emit_state_change(task_id, old_status, "cancelled")

    async def cancel_task_cascade(self, task_id: str, reason: str = "") -> int:
        """级联取消指定任务的所有子任务。

        BUG-FIX-fix_20260522_cancel_task_cascade:
        问题根因: TaskTool._cancel_task() 调用 service.cancel_task_cascade()，
                  但 TaskService 中从未定义此方法，导致 AttributeError。
        修复方案: 参照 fail_task() 的实现模式，遍历 storage 查找
                  parent_task_id == task_id 的所有子任务，对每个子任务
                  递归调用 fail_task，并递归处理更深层级。
        影响范围: 任务取消功能，tool.py 和 routes_tasks.py 的 cancel 端点。
        修复日期: 2026-05-22

        Args:
            task_id: 父任务 ID
            reason: 取消原因

        Returns:
            被级联取消的子任务数量
        """
        if self._storage is None:
            return 0

        # 查找 parent_task_id == task_id 的所有直接子任务
        subtasks = self._storage.list_by_parent(task_id)
        cancelled_count = 0

        for subtask in subtasks:
            # BUG-FIX-fix_20260523_cancel_task: 改用 cancel_task 替代 fail_task，
            # 使子任务状态为 CANCELLED 而非 FAILED，与父任务保持一致。
            await self.cancel_task(
                subtask.id,
                reason=f"父任务取消，级联取消: {reason}" if reason else "父任务取消，级联取消",
            )
            cancelled_count += 1

            # 递归处理更深层级的子任务
            deeper_count = await self.cancel_task_cascade(subtask.id, reason=reason)
            cancelled_count += deeper_count

        return cancelled_count

    async def fail_task_cascade(self, task_id: str, reason: str = "") -> int:
        """级联取消父任务失败时的所有子任务。

        BUG-FIX-fix_20260603_fail_task_cascade:
        问题根因: fail_task 只处理当前任务，不处理子任务，
          导致父任务失败后子任务仍保持 RUNNING/EVALUATING 状态继续执行。
        修复方案: 参照 cancel_task_cascade()，递归遍历所有后代子任务，
          对非终态的子任务调用 cancel_task 标记为 CANCELLED，
          跳过已处于终态（COMPLETED/FAILED/CANCELLED）的子任务。
        修复日期: 2026-06-03

        Args:
            task_id: 父任务 ID
            reason: 失败原因（会传递给子任务的取消原因）

        Returns:
            被级联取消的子任务数量
        """
        if self._storage is None:
            return 0

        from tasks.types import TaskStatus

        _TERMINAL = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})

        subtasks = self._storage.list_by_parent(task_id)
        cancelled_count = 0

        for subtask in subtasks:
            if subtask.status in _TERMINAL:
                continue

            await self.cancel_task(
                subtask.id,
                reason=f"父任务失败，级联取消: {reason}" if reason else "父任务失败，级联取消",
            )
            cancelled_count += 1

            # 递归处理更深层级的子任务
            deeper_count = await self.fail_task_cascade(subtask.id, reason=reason)
            cancelled_count += deeper_count

        return cancelled_count

    @staticmethod
    async def cleanup_ghost_tasks(data_dir: str) -> tuple[int, int]:
        """清理服务重启后残留的幽灵任务（running/evaluating）。

        服务重启后，内存中的引擎已不存在，但磁盘上可能残留
        status=running 或 status=evaluating 的任务 YAML。
        此方法将它们标记为 failed，并级联取消所有非终态的子任务。

        Args:
            data_dir: TaskStorage 的数据目录路径

        Returns:
            (清理的幽灵任务数, 级联取消的子任务数)
        """
        from pathlib import Path as _Path
        from datetime import datetime, timezone
        from tasks.storage import TaskStorage
        from tasks.types import TaskStatus

        _data_dir = str(_Path(data_dir).resolve())
        try:
            storage = TaskStorage(data_dir=_data_dir)
        except Exception as exc:
            logger.warning("[GhostCleanup] 初始化 TaskStorage 失败: %s", exc)
            return (0, 0)

        _GHOST_STATES = frozenset({TaskStatus.RUNNING, TaskStatus.EVALUATING})
        _TERMINAL = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})
        _REASON = "服务重启后引擎状态丢失"

        ghost_tasks: list = []
        for state in _GHOST_STATES:
            ghost_tasks.extend(storage.list_by_status(state))

        if not ghost_tasks:
            return (0, 0)

        def _cancel_descendants(parent_id: str) -> int:
            subtasks = storage.list_by_parent(parent_id)
            count = 0
            for st in subtasks:
                if st.status in _TERMINAL:
                    continue
                st.status = TaskStatus.CANCELLED
                st.updated_at = datetime.now(timezone.utc).isoformat()
                st.metadata["cancel_reason"] = "父任务因服务重启失败，级联取消"
                st.error = "父任务因服务重启失败，级联取消"
                storage.save(st)
                count += 1
                count += _cancel_descendants(st.id)
            return count

        cleaned = 0
        cascaded = 0
        for task in ghost_tasks:
            try:
                task.status = TaskStatus.FAILED
                task.updated_at = datetime.now(timezone.utc).isoformat()
                task.metadata["fail_reason"] = _REASON
                # BUG-FIX-fix_20260606_error_accumulate:
                # 如果有原始错误（如 LLM timeout、迭代耗尽等），追加而非覆盖
                if task.error:
                    task.error = f"{task.error} → {_REASON}"
                else:
                    task.error = _REASON
                storage.save(task)
                cleaned += 1
                cascaded += _cancel_descendants(task.id)
            except Exception as exc:
                logger.warning(
                    "[GhostCleanup] 清理失败: task=%s err=%s",
                    task.id[:12] if hasattr(task, "id") else "?", exc,
                )

        logger.info(
            "[GhostCleanup] 完成: cleaned=%d, cascaded=%d",
            cleaned, cascaded,
        )
        return (cleaned, cascaded)

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

        # 注入上下文使用率到 task.metadata（供通知链路使用）
        self._inject_context_usage(task)
        self._storage.save(task)

        if passed:
            await self.complete_task(task_id)
        else:
            # BUG-FIX-fix_20260524_error_not_propagated:
            # 问题根因: complete_evaluation 评估不通过时调用 fail_task(task_id) 不传 reason，
            #   导致任务被标记为 FAILED 但没有任何失败原因记录（error 和 metadata 都为空）。
            # 修复方案: 从 result 中提取失败摘要作为 reason，确保有可追溯的失败原因。
            # 影响范围: task_evaluate 工具评估不通过时的任务状态。
            # 修复日期: 2026-05-24
            _eval_reason = ""
            if isinstance(result, dict):
                summary = result.get("summary", "")
                if summary:
                    _eval_reason = f"评估未通过: {summary}"
                else:
                    failed_metrics = []
                    for m in result.get("metrics", []):
                        if isinstance(m, dict) and not m.get("passed", True):
                            mid = m.get("metric_id", "unknown")
                            msg = m.get("message", m.get("error", ""))
                            failed_metrics.append(f"{mid}: {msg}" if msg else mid)
                    if failed_metrics:
                        _eval_reason = f"评估未通过: {', '.join(failed_metrics)}"
            if not _eval_reason:
                _eval_reason = "评估未通过"
            await self.fail_task(task_id, reason=_eval_reason)

    @staticmethod
    def _inject_context_usage(task: Any) -> None:
        """计算并注入当前 Agent 的上下文使用率到 task.metadata。

        通过 task.pipeline_run_id 查找引擎注册表中的 PipelineEngine，
        读取 _current_state 中的 context_window 和 llm_usage.input_tokens，
        计算出上下文使用百分比，写入 task.metadata["context_usage"]。

        此方法在 complete_evaluation() 中被调用，运行时机在引擎生命周期内，
        保证一定能获取到上下文状态（零降级）。

        Args:
            task: TaskModel 实例（会被原地修改 metadata）
        """
        pipeline_run_id = getattr(task, "pipeline_run_id", None)
        if not pipeline_run_id:
            return

        try:
            from pipeline.registry import get_engine_registry
            _reg = get_engine_registry()
            _entry = _reg.get(pipeline_run_id)
            if not _entry or not _entry.engine:
                return

            _engine = _entry.engine
            _state = getattr(_engine, "_current_state", None)
            if not _state:
                return

            _cw = _state.get("context_window", 0)
            _usage = _state.get("llm_usage", {})
            _input_tokens = _usage.get("input_tokens", 0)

            if _cw <= 0:
                return

            _pct = round((_input_tokens / _cw) * 100, 1)
            if task.metadata is None:
                task.metadata = {}
            task.metadata["context_usage"] = {
                "pct": _pct,
                "input_tokens": _input_tokens,
                "context_window": _cw,
            }
        except Exception:
            # 上下文使用率计算不是关键路径，异常不影响任务完成
            pass

    async def recover_to_completed(
        self, task_id: str, result: dict | None = None,
    ) -> None:
        """将已 failed 的任务恢复为 completed（评估通过但 idle timer 先杀掉了任务）。

        Args:
            task_id: 任务 ID
            result: 评估结果数据
        """
        if self._storage is None:
            return

        task = self._storage.get(task_id)
        if task is None:
            return

        if result is not None:
            task.result = result

        task.status = TaskStatus.COMPLETED
        task.completed_at = datetime.now(UTC)
        logger.info(
            "[TaskService] 任务已从 failed 恢复为 completed | task_id=%s",
            task_id,
        )

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

    # ── 职责归一化包装方法 ──────────────────────────────────────

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

    # ── 资源清理方法 ──────────────────────────────────────────────

    def _get_execution_record_storage(self):
        """获取全局 ExecutionRecordStorage 实例。

        通过 ServiceProvider 统一获取。

        Returns:
            ExecutionRecordStorage 实例，获取失败时返回 None
        """
        try:
            from infrastructure.service_provider import get_service_provider
            provider = get_service_provider()
            return provider.get("execution_record_storage")
        except Exception:
            return None

    def _cancel_pipeline(self, task_id: str) -> None:
        """取消任务关联的运行中管道（best-effort）。

        BUG-FIX-fix_20260514_task_delete_pipeline:
        通过 TaskWorker.cancel_pipeline 强制取消 asyncio.Task，
        在删除任务前确保管道停止执行。
        """
        try:
            from infrastructure.service_provider import get_service_provider

            provider = get_service_provider()
            task_worker = provider.get("task_worker")
            if task_worker is None:
                return
            cancelled = task_worker.cancel_pipeline(task_id)
            if cancelled:
                logger.info(
                    "[TaskService] 任务 %s 管道已取消", task_id,
                )
        except Exception as e:
            logger.warning(
                "[TaskService] 任务 %s 管道取消失败 (non-fatal): %s",
                task_id, e,
            )

    def _cancel_pipeline_recursive(self, task_id: str) -> None:
        """递归取消任务及其所有子任务的运行中管道。

        BUG-FIX-fix_20260514_task_delete_pipeline:
        删除任务时需要同时取消所有下级子任务的管道，避免孤立管道继续执行。
        """
        self._cancel_pipeline(task_id)
        subtasks = self.list_subtasks(task_id)
        for subtask in subtasks:
            self._cancel_pipeline_recursive(subtask.id)

    def _is_child_of_container(self, task: Any) -> bool:
        """判断非容器任务是否属于某个容器任务的子树。

        BUG-FIX-fix_20260514_task_delete_pipeline:
        向上追溯 parent_task_id 链，检查根任务是否为 container 类型。
        容器的子任务删除时不需要清理工作空间（工作空间由容器管理）。
        """
        root_id = self.get_root_task_id(task.id)
        if root_id is None or root_id == task.id:
            return False
        root_task = self.get_task(root_id)
        if root_task is None:
            return False
        return root_task.metadata.get("task_scope") == "container"

    async def _cleanup_task_resources(
        self,
        task_id: str,
        workspace: str | None,
    ) -> dict[str, Any]:
        """清理任务相关的资源（容器和工作空间）。

        容器清理委托给 IsolationManager，不再直接操作 Docker SDK。

        Args:
            task_id: 任务 ID
            workspace: 工作空间路径
            isolation_level: 隔离级别

        Returns:
            清理结果字典
        """
        cleanup_results: dict[str, Any] = {
            "container_destroyed": False,
            "workspace_cleaned": False,
            "errors": [],
        }

        try:
            from isolation.manager import get_isolation_manager

            manager = await get_isolation_manager()
            destroyed = await manager.destroy_environment(task_id)
            cleanup_results["container_destroyed"] = destroyed
            if destroyed:
                logger.info("[TaskService] 已通过 IsolationManager 销毁环境: %s", task_id)
        except Exception as e:
            cleanup_results["errors"].append(f"清理隔离环境失败: {str(e)}")
            logger.warning("[TaskService] 清理隔离环境失败: %s, 错误: %s", task_id, e)

        # 优先使用 lifecycle 进行工作空间清理
        lifecycle_cleaned = False
        try:
            from infrastructure.service_provider import get_service_provider
            provider = get_service_provider()
            lifecycle = provider.get("workspace_lifecycle_manager")
            if lifecycle:
                lifecycle.restore_ws_meta(task_id)
                cleanup_result = lifecycle.cleanup_workspace(task_id)
                if cleanup_result:
                    lifecycle_cleaned = True
                    cleanup_results["workspace_cleaned"] = True
                    logger.info("[TaskService] 已通过 lifecycle 清理工作空间: %s", task_id)
        except Exception as e:
            logger.debug("[TaskService] lifecycle 清理不可用，回退到原有逻辑: %s", e)

        if not lifecycle_cleaned and workspace:
            try:
                from isolation.workspace import get_workspace_config_root

                workspace_path = Path(workspace)
                ws_root = get_workspace_config_root()

                if not workspace_path.is_absolute():
                    workspace_path = Path(ws_root) / workspace

                ws_root_resolved = Path(ws_root).resolve()
                ws_path_resolved = workspace_path.resolve()

                if not ws_path_resolved.is_relative_to(ws_root_resolved):
                    logger.warning(
                        "[TaskService] 拒绝删除工作空间（不在配置根目录下）: %s (root=%s)",
                        ws_path_resolved, ws_root_resolved,
                    )
                    cleanup_results["errors"].append(
                        f"安全拦截：路径 {ws_path_resolved} 不在工作空间根目录 {ws_root_resolved} 下，已跳过删除"
                    )
                elif workspace_path.exists():
                    git_path = workspace_path / ".git"
                    if git_path.is_file():
                        self._remove_worktree(workspace_path, cleanup_results)
                    elif git_path.is_dir():
                        shutil.rmtree(str(workspace_path))
                        cleanup_results["workspace_cleaned"] = True
                        logger.info("[TaskService] 已清理工作空间: %s", workspace_path)
                    else:
                        shutil.rmtree(str(workspace_path))
                        cleanup_results["workspace_cleaned"] = True
                        logger.info("[TaskService] 已清理普通目录: %s", workspace_path)
                else:
                    logger.debug("[TaskService] 工作空间不存在: %s", workspace_path)
            except Exception as e:
                cleanup_results["errors"].append(f"清理工作空间失败: {str(e)}")
                logger.warning("[TaskService] 清理工作空间失败: %s, 错误: %s", workspace, e)

        return cleanup_results

    def _remove_worktree(
        self,
        workspace_path: Path,
        cleanup_results: dict[str, Any],
    ) -> None:
        """移除 git worktree 并清理对应分支。

        worktree 的 .git 是一个文件（指向主仓库 .git/worktrees/xxx），
        需要通过 git worktree remove 命令正确清理，而非直接 shutil.rmtree。

        Args:
            workspace_path: worktree 的工作空间路径
            cleanup_results: 清理结果字典，用于记录错误信息
        """
        try:
            # 读取 .git 文件内容，定位主仓库路径
            git_file_content = (workspace_path / ".git").read_text(encoding="utf-8").strip()
            # 格式为 "gitdir: /path/to/main-repo/.git/worktrees/xxx"
            if git_file_content.startswith("gitdir: "):
                worktree_gitdir = Path(git_file_content[len("gitdir: "):])
                # 主仓库根目录: .git/worktrees/xxx 的上上级
                main_repo = worktree_gitdir.parent.parent.parent
            else:
                main_repo = workspace_path.parent

            # 在主仓库中执行 git worktree remove --force
            subprocess.run(
                ["git", "worktree", "remove", str(workspace_path), "--force"],
                cwd=str(main_repo),
                capture_output=True,
                text=True,
                check=True,
            )
            logger.info("[TaskService] 已通过 git worktree remove 清理 worktree: %s", workspace_path)
            cleanup_results["workspace_cleaned"] = True
        except subprocess.CalledProcessError as e:
            cleanup_results["errors"].append(
                f"git worktree remove 失败: {e.stderr.strip() if e.stderr else str(e)}"
            )
            logger.warning(
                "[TaskService] git worktree remove 失败: %s, stderr: %s",
                workspace_path,
                e.stderr,
            )
        except Exception as e:
            cleanup_results["errors"].append(f"清理 worktree 失败: {str(e)}")
            logger.warning("[TaskService] 清理 worktree 失败: %s, 错误: %s", workspace_path, e)

    async def _cleanup_subtask_worktrees(
        self,
        container_task: Any,
        subtasks: list[Any],
    ) -> dict[str, Any]:
        """清理容器下所有子任务的 worktree。

        在容器标记完成之前调用，遍历每个子任务的 workspace_path，
        执行 git worktree remove 和分支清理。

        安全保护：
        - 跳过与容器自身 workspace 相同的路径（防止误删容器工作目录）
        - 每个子任务清理用 try-except 包裹，单个失败不阻塞后续清理
        - 整个清理过程不抛出异常到外层

        Args:
            container_task: 容器任务模型
            subtasks: 容器下的子任务列表

        Returns:
            清理结果统计字典，包含 total_subtasks / cleaned_count /
            skipped_count / error_count / errors
        """
        result: dict[str, Any] = {
            "total_subtasks": len(subtasks),
            "cleaned_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "errors": [],
        }

        if not subtasks:
            logger.info(
                "[TaskService] 容器 %s 无子任务，跳过 worktree 清理",
                container_task.id,
            )
            return result

        # 获取容器自身的 workspace 路径，用于保护
        container_workspace = (container_task.metadata or {}).get("workspace", "")
        # 解析为绝对路径用于比较
        container_ws_resolved = ""
        if container_workspace:
            try:
                container_ws_resolved = str(Path(container_workspace).resolve())
            except Exception:
                container_ws_resolved = container_workspace

        logger.info(
            "[TaskService] 开始清理容器 %s 的子任务 worktree，共 %d 个子任务",
            container_task.id,
            len(subtasks),
        )

        for subtask in subtasks:
            workspace = (subtask.metadata or {}).get("workspace", "")

            # 跳过无 workspace 的子任务
            if not workspace:
                logger.debug(
                    "[TaskService] 子任务 %s 无 workspace_path，跳过",
                    subtask.id,
                )
                result["skipped_count"] += 1
                continue

            # 安全校验：保护容器自身的 workspace
            try:
                sub_ws_resolved = str(Path(workspace).resolve())
            except Exception:
                sub_ws_resolved = workspace

            if container_ws_resolved and sub_ws_resolved == container_ws_resolved:
                logger.info(
                    "[TaskService] 子任务 %s 的 workspace 与容器相同 (%s)，跳过以保护容器工作目录",
                    subtask.id,
                    workspace,
                )
                result["skipped_count"] += 1
                continue

            # 执行清理
            try:
                # 优先使用 workspace_lifecycle 进行清理
                lifecycle_cleaned = False
                try:
                    from infrastructure.service_provider import get_service_provider

                    provider = get_service_provider()
                    lifecycle = provider.get("workspace_lifecycle_manager")
                    if lifecycle:
                        lifecycle.restore_ws_meta(subtask.id)
                        cleanup_result = lifecycle.cleanup_workspace(subtask.id)
                        if cleanup_result and (
                            cleanup_result.get("worktree_removed")
                            or cleanup_result.get("dir_removed")
                        ):
                            lifecycle_cleaned = True
                            result["cleaned_count"] += 1
                            logger.info(
                                "[TaskService] 已通过 lifecycle 清理子任务 %s 的 worktree: %s",
                                subtask.id,
                                workspace,
                            )
                except Exception as e:
                    logger.debug(
                        "[TaskService] lifecycle 清理子任务 %s 不可用: %s",
                        subtask.id,
                        e,
                    )

                # lifecycle 不可用时回退到 _cleanup_task_resources
                if not lifecycle_cleaned:
                    cleanup_result = await self._cleanup_task_resources(
                        task_id=subtask.id,
                        workspace=workspace,
                    )
                    if cleanup_result.get("workspace_cleaned"):
                        result["cleaned_count"] += 1
                        logger.info(
                            "[TaskService] 已清理子任务 %s 的 worktree: %s",
                            subtask.id,
                            workspace,
                        )
                    else:
                        # 清理了但 workspace 可能已不存在，不视为错误
                        errors = cleanup_result.get("errors", [])
                        if errors:
                            result["error_count"] += 1
                            result["errors"].extend(
                                [f"子任务 {subtask.id}: {e}" for e in errors]
                            )
                        else:
                            # workspace 已不存在，正常跳过
                            result["skipped_count"] += 1

            except Exception as e:
                result["error_count"] += 1
                error_msg = f"子任务 {subtask.id}: {str(e)}"
                result["errors"].append(error_msg)
                logger.warning(
                    "[TaskService] 清理子任务 %s 的 worktree 失败: %s, 错误: %s",
                    subtask.id,
                    workspace,
                    e,
                )

        logger.info(
            "[TaskService] 容器 %s 子任务 worktree 清理完成: "
            "总计=%d, 已清理=%d, 跳过=%d, 失败=%d",
            container_task.id,
            result["total_subtasks"],
            result["cleaned_count"],
            result["skipped_count"],
            result["error_count"],
        )

        return result

    def _collect_all_descendant_ids(self, task_id: str) -> list[str]:
        """递归收集任务的所有后代任务 ID（不含自身，深度优先）。

        Args:
            task_id: 起始任务 ID

        Returns:
            后代任务 ID 列表（叶子节点在前，根在后）
        """
        descendants: list[str] = []
        subtasks = self.list_subtasks(task_id)
        for subtask in subtasks:
            # 先递归收集更深层的后代
            descendants.extend(
                self._collect_all_descendant_ids(subtask.id)
            )
            # 再加入当前子任务
            descendants.append(subtask.id)
        return descendants

    def _cleanup_pipeline_file(self, pipeline_run_id: str) -> bool:
        """清理单个管道的执行记录文件（best-effort）。

        通过 ExecutionRecordStorage.delete_by_session 删除管道 YAML
        及其分片文件，并清理内存缓存。

        Args:
            pipeline_run_id: 管道运行 ID

        Returns:
            是否成功清理了记录
        """
        if not pipeline_run_id:
            return False
        try:
            storage = self._get_execution_record_storage()
            if storage is None:
                return False
            deleted = storage.delete_by_session(pipeline_run_id)
            if deleted > 0:
                logger.info(
                    "[TaskService] 已清理管道执行文件: %s (%d 条记录)",
                    pipeline_run_id, deleted,
                )
                return True
            return False
        except Exception as e:
            logger.warning(
                "[TaskService] 清理管道执行文件失败 (non-fatal): %s, 错误: %s",
                pipeline_run_id, e,
            )
            return False

    async def _cascade_cleanup_subtasks(
        self,
        task_id: str,
        *,
        skip_workspace: bool = False,
        container_workspace: str = "",
    ) -> dict[str, Any]:
        """级联清理任务的所有子任务资源并删除存储记录。

        执行以下操作（对每个后代任务）：
        1. 清理管道执行文件（ExecutionRecordStorage YAML）
        2. 清理工作空间 / worktree（跳过与容器 workspace 相同的路径）
        3. 从 TaskStorage 中删除存储记录

        处理顺序：叶子节点 → 根，确保父任务最后被删除。

        Args:
            task_id: 父任务 ID（其子树将被清理）
            skip_workspace: 是否完全跳过工作空间清理
            container_workspace: 容器自身的 workspace 路径（用于保护，防止误删）

        Returns:
            清理统计信息字典
        """
        stats: dict[str, Any] = {
            "subtasks_deleted": 0,
            "pipeline_files_cleaned": 0,
            "workspaces_cleaned": 0,
            "errors": [],
        }

        # 收集所有后代（深度优先，叶子在前）
        descendant_ids = self._collect_all_descendant_ids(task_id)

        if not descendant_ids:
            return stats

        logger.info(
            "[TaskService] 开始级联清理任务 %s 的 %d 个后代子任务",
            task_id, len(descendant_ids),
        )

        # 解析容器 workspace 用于保护
        container_ws_resolved = ""
        if container_workspace:
            try:
                container_ws_resolved = str(Path(container_workspace).resolve())
            except Exception:
                container_ws_resolved = container_workspace

        for descendant_id in descendant_ids:
            descendant_task = self.get_task(descendant_id)
            if descendant_task is None:
                continue

            # 1. 清理管道执行文件
            if descendant_task.pipeline_run_id:
                if self._cleanup_pipeline_file(descendant_task.pipeline_run_id):
                    stats["pipeline_files_cleaned"] += 1

            # 2. 清理工作空间（非跳过模式下）
            if not skip_workspace:
                workspace = (descendant_task.metadata or {}).get("workspace")
                if workspace:
                    # 保护容器自身的 workspace
                    try:
                        sub_ws_resolved = str(Path(workspace).resolve())
                    except Exception:
                        sub_ws_resolved = workspace

                    if container_ws_resolved and sub_ws_resolved == container_ws_resolved:
                        logger.debug(
                            "[TaskService] 子任务 %s 的 workspace 与容器相同，跳过",
                            descendant_id,
                        )
                    else:
                        try:
                            cleanup_result = await self._cleanup_task_resources(
                                task_id=descendant_id,
                                workspace=workspace,
                            )
                            if cleanup_result.get("workspace_cleaned"):
                                stats["workspaces_cleaned"] += 1
                        except Exception as e:
                            stats["errors"].append(
                                f"子任务 {descendant_id} 工作空间清理失败: {str(e)}"
                            )

            # 3. 删除存储记录
            try:
                await self.hard_delete(descendant_id)
                stats["subtasks_deleted"] += 1
            except Exception as e:
                stats["errors"].append(
                    f"子任务 {descendant_id} 记录删除失败: {str(e)}"
                )
                logger.warning(
                    "[TaskService] 删除子任务记录失败 (non-fatal): %s, 错误: %s",
                    descendant_id, e,
                )

        logger.info(
            "[TaskService] 级联清理完成: 子任务删除=%d, 管道文件清理=%d, 工作空间清理=%d, 错误=%d",
            stats["subtasks_deleted"],
            stats["pipeline_files_cleaned"],
            stats["workspaces_cleaned"],
            len(stats["errors"]),
        )

        return stats

    async def soft_delete_container(
        self, task_id: str, reason: str = "用户请求删除"
    ) -> dict[str, Any]:
        """软删除容器任务（标记取消 + 级联清理子任务）。

        Args:
            task_id: 任务 ID
            reason: 删除原因

        Returns:
            操作结果字典
        """
        from tasks.types import TaskStatus

        task = self.get_task(task_id)
        if task is None:
            return {"error": f"任务不存在: {task_id}"}

        old_status = task.status.value
        task_title = task.title

        task.status = TaskStatus.FAILED
        task.error = f"已取消: {reason}"
        if task.metadata is None:
            task.metadata = {}
        task.metadata["soft_deleted"] = True
        await self.save_task(task)

        self._cancel_pipeline_recursive(task_id)
        cascaded = await self.cancel_task_cascade(task_id, reason=reason)

        container_workspace = (task.metadata or {}).get("workspace", "")
        cascade_stats = await self._cascade_cleanup_subtasks(
            task_id,
            skip_workspace=False,
            container_workspace=container_workspace,
        )

        result: dict[str, Any] = {
            "task_id": task_id,
            "deleted": False,
            "soft_deleted": True,
            "old_status": old_status,
            "title": task_title,
            "reason": reason,
            "message": "容器任务已标记删除（软删除）",
            "pipeline_file_cleaned": False,
            "cascade_cleanup": cascade_stats,
        }
        if cascaded > 0:
            result["cascaded_subtasks"] = cascaded
        return result

    async def hard_delete_task(
        self, task_id: str, reason: str = "用户请求删除"
    ) -> dict[str, Any]:
        """硬删除非容器任务（级联清理 + 删除记录）。

        Args:
            task_id: 任务 ID
            reason: 删除原因

        Returns:
            操作结果字典
        """
        task = self.get_task(task_id)
        if task is None:
            return {"error": f"任务不存在: {task_id}"}

        old_status = task.status.value
        task_title = task.title

        is_child_of_container = self._is_child_of_container(task)
        skip_workspace = is_child_of_container

        self._cancel_pipeline_recursive(task_id)

        cascade_stats: dict[str, Any] = {
            "subtasks_deleted": 0,
            "pipeline_files_cleaned": 0,
            "workspaces_cleaned": 0,
            "errors": [],
        }
        subtasks = self.list_subtasks(task_id)
        if subtasks:
            cascade_stats = await self._cascade_cleanup_subtasks(
                task_id,
                skip_workspace=skip_workspace,
                container_workspace="",
            )

        pipeline_cleaned = False
        if task.pipeline_run_id:
            pipeline_cleaned = self._cleanup_pipeline_file(task.pipeline_run_id)

        if not skip_workspace:
            workspace = task.metadata.get("workspace")
            cleanup_results = await self._cleanup_task_resources(
                task_id=task_id,
                workspace=workspace,
            )
        else:
            cleanup_results = {"skipped": "容器子任务不清理工作空间"}

        await self.hard_delete(task_id)

        # WebSocket 通知
        try:
            from infrastructure.service_provider import get_service_provider
            _provider = get_service_provider()
            _ws_notifier = _provider.get("ws_interaction_notifier")
            if _ws_notifier:
                _ws_payload = {
                    "type": "task_deleted",
                    "data": {
                        "task_id": task_id,
                        "title": task_title,
                    },
                }
                _parent_pid = getattr(task, "parent_pipeline_id", "") or ""
                _ws_tid = ""
                if _parent_pid and hasattr(_ws_notifier, "get_thread_for_pipeline"):
                    _ws_tid = _ws_notifier.get_thread_for_pipeline(_parent_pid)
                if _ws_tid and hasattr(_ws_notifier, "send_to_thread"):
                    await _ws_notifier.send_to_thread(_ws_tid, _ws_payload)
                    logger.debug("[TaskService] task_deleted 已通过 send_to_thread 发送 | task_id=%s", task_id)
                elif hasattr(_ws_notifier, "send_to_user"):
                    _conns = getattr(_ws_notifier, "_active_connections", {})
                    _global_conns = getattr(_ws_notifier, "_global_connections", {})
                    if _conns or _global_conns:
                        for _tid, _ws_list in _conns.items():
                            for _ws in _ws_list:
                                try:
                                    await _ws.send_json(_ws_payload)
                                except Exception:
                                    pass
                        for _uid, _ws_list in _global_conns.items():
                            for _ws in _ws_list:
                                try:
                                    await _ws.send_json(_ws_payload)
                                except Exception:
                                    pass
                        logger.debug("[TaskService] task_deleted 已广播 | task_id=%s", task_id)
        except Exception as _ws_exc:
            logger.warning("[TaskService] task_deleted 广播失败: %s", _ws_exc)

        return {
            "task_id": task_id,
            "deleted": True,
            "old_status": old_status,
            "title": task_title,
            "reason": reason,
            "pipeline_file_cleaned": pipeline_cleaned,
            "cleanup": cleanup_results,
            "cascade_cleanup": cascade_stats,
        }

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

    def register_state_callback(self, callback: StateChangeCallback) -> None:
        """注册任务状态变更回调函数。

        Args:
            callback: 异步回调函数，签名为 async (task_id, old_status, new_status) -> None
        """
        self._state_callbacks.append(callback)

    def unregister_state_callback(self, callback: StateChangeCallback) -> None:
        """注销任务状态变更回调函数。

        Args:
            callback: 之前注册的回调函数
        """
        if callback in self._state_callbacks:
            self._state_callbacks.remove(callback)

    async def _emit_state_change(
        self,
        task_id: str,
        old_status: str,
        new_status: str,
    ) -> None:
        """通知所有注册的回调函数任务状态已变更。

        直接 await 调用回调，保证时序确定性。每个回调独立
        try-except，单个回调异常不影响其他回调。

        Args:
            task_id: 任务 ID
            old_status: 原状态
            new_status: 新状态
        """
        for cb in self._state_callbacks:
            try:
                await cb(task_id, old_status, new_status)
            except Exception as exc:
                logger.debug("state callback 执行失败: %s", exc)
