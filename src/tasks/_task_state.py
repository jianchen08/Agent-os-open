"""任务状态转换 Mixin — 状态变更、幽灵清理与评估完成。

从 service.py 拆分出的职责域，提供 TaskService 的所有状态操作方法。
依赖 _TaskCrudMixin 的 get_task / save_task / list_subtasks 等基础方法。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from src.tasks.state_machine import (
    _TASK_TRANSITIONS,
    InvalidTransitionError,
)
from utils.enum_utils import safe_enum_value

if TYPE_CHECKING:
    # 仅用于类型注解（运行时延迟求值，from __future__ import annotations）。
    # 放在 TYPE_CHECKING 下避免与 types.py 的潜在循环导入。
    from tasks.types import TaskModel

logger = logging.getLogger(__name__)


class _TaskStateMixin:
    """任务状态转换 Mixin。"""

    def can_transition(self, task_id: str, target_status: Any) -> bool:
        """检查任务是否可以转换到目标状态。

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

        current = safe_enum_value(task.status)
        target = safe_enum_value(target_status)
        allowed = _TASK_TRANSITIONS.get(current, [])

        return target in allowed

    def get_valid_transitions(self, task_id: str) -> list[str]:
        """获取任务当前状态可转换的目标状态列表。

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

        current = safe_enum_value(task.status)
        return _TASK_TRANSITIONS.get(current, [])

    async def force_transition(self, task_id: str, target_status: Any) -> None:
        """强制执行任务状态转换并持久化。

        与 start_task / complete_task 等具体方法不同，此方法接受任意 TaskStatus，
        通过 _TASK_TRANSITIONS 校验合法性后执行转换。

        例外：容器任务（task_scope == "container"）跳过状态机校验，允许 L1
        主 Agent / 任务管理工具在任意终态之间自由互转。容器本身只是子任务的
        "集合"，不承载执行语义，强加状态机会导致 UI 无法纠正错误的容器状态。

        Args:
            task_id: 任务 ID
            target_status: 目标状态（TaskStatus 枚举）

        Raises:
            KeyError: 任务不存在
            InvalidTransitionError: 当前状态不允许转换到目标状态（仅非容器任务）
        """
        if self._storage is None:
            raise KeyError(f"任务不存在: {task_id}")

        task = self._storage.get(task_id)
        if task is None:
            raise KeyError(f"任务不存在: {task_id}")

        from tasks.types import TaskStatus  # noqa: PLC0415

        current = safe_enum_value(task.status)
        target = safe_enum_value(target_status)

        # 容器任务：跳过状态机校验（仅作为子任务集合，状态由 L1 自由维护）
        is_container = (task.metadata or {}).get("task_scope") == "container"

        if not is_container:
            allowed = _TASK_TRANSITIONS.get(current, [])
            if target not in allowed:
                raise InvalidTransitionError(
                    current,
                    target,
                    f"不允许从 ''{current}'' 转换到 ''{target}''，合法目标: {allowed}",
                )

        task.status = TaskStatus(target)
        task.updated_at = datetime.now().isoformat()
        self._storage.save(task)

        await self._emit_state_change(task_id, current, target)

    async def pause_task(self, task_id: str, paused_by: str = "user") -> None:
        """暂停任务。

        Args:
            task_id: 任务 ID
            paused_by: 暂停来源，"user"或"system"

        Raises:
            KeyError: 任务不存在
            InvalidTransitionError: 当前状态不允许暂停
        """
        if self._storage is None:
            raise KeyError(f"任务不存在: {task_id}")

        task = self._storage.get(task_id)
        if task is None:
            raise KeyError(f"任务不存在: {task_id}")

        from tasks.types import TaskStatus  # noqa: PLC0415

        current = safe_enum_value(task.status)
        allowed = {"running", "pending"}
        if current not in allowed:
            raise InvalidTransitionError(
                current,
                "stopped",
                f"不允许从 ''{current}'' 停止任务",
            )

        old_status = current
        task.status = TaskStatus.STOPPED
        task.updated_at = datetime.now().isoformat()
        # 记录暂停来源，重启时区分用户暂停（应保持 STOPPED）和系统暂停（应恢复）
        if task.metadata is None:
            task.metadata = {}
        task.metadata["paused_by"] = paused_by
        self._storage.save(task)

        await self._emit_state_change(task_id, old_status, "stopped")

    async def resume_task(self, task_id: str) -> Any:
        """恢复暂停的任务。

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

        from tasks.types import TaskStatus  # noqa: PLC0415

        current = safe_enum_value(task.status)
        if current != "stopped":
            raise InvalidTransitionError(
                current,
                "running",
                f"只有 stopped 状态的任务可以恢复，当前: ''{current}''",
            )

        old_status = current
        task.status = TaskStatus.RUNNING
        task.updated_at = datetime.now().isoformat()
        # resume 仅补设缺失的 started_at，不覆盖已有值：
        # 暂停+恢复不应抹掉已运行时长（与 start_task 的幂等策略一致）。
        if not task.started_at:
            task.started_at = datetime.now().isoformat()
        if task.metadata:
            task.metadata.pop("paused_by", None)
        self._storage.save(task)

        await self._emit_state_change(task_id, old_status, "running")

        # 唤醒挂起的管道引擎
        try:
            from pipeline.registry import get_engine_registry  # noqa: PLC0415

            entries = get_engine_registry().find_by_tag("task_id", task_id)
            for entry in entries:
                if entry.engine is not None and entry.engine.is_suspended:
                    entry.engine.wake()
                    logger.info(
                        "TaskService: 唤醒挂起引擎 task_id=%s pipeline=%s",
                        task_id,
                        entry.pipeline_id[:12],
                    )
        except Exception as exc:
            logger.debug(
                "TaskService: 唤醒引擎失败（非致命）task_id=%s: %s",
                task_id,
                exc,
            )

        return task

    async def start_task(self, task_id: str) -> TaskModel:
        """将任务从 pending 状态推进到 running。

        Args:
            task_id: 任务 ID

        Returns:
            更新后的 TaskModel（与 resume_task 返回值契约统一）

        Raises:
            KeyError: 任务不存在
            InvalidTransitionError: 当前状态不允许启动
        """
        if self._storage is None:
            raise KeyError(f"任务不存在: {task_id}")

        task = self._storage.get(task_id)
        if task is None:
            raise KeyError(f"任务不存在: {task_id}")

        from tasks.types import TaskStatus  # noqa: PLC0415

        old_status = safe_enum_value(task.status)
        if old_status not in ("pending", "running"):
            raise InvalidTransitionError(
                old_status,
                "running",
                f"不允许从 '{old_status}' 启动任务",
            )

        task.status = TaskStatus.RUNNING
        task.updated_at = datetime.now().isoformat()
        # 首次启动时记录 started_at 起点（用于任务级耗时观测、僵尸任务判定）；
        # 幂等：已存在则不覆盖，避免 pending↔running 反复触发抹掉真实起点。
        if not task.started_at:
            task.started_at = datetime.now().isoformat()
        self._storage.save(task)

        await self._emit_state_change(task_id, old_status, "running")
        return task

    async def move_to_evaluating(self, task_id: str) -> None:
        """将任务从 running 状态推进到 evaluating。

        Args:
            task_id: 任务 ID

        Raises:
            KeyError: 任务不存在
            InvalidTransitionError: 当前状态不允许转换到 evaluating
        """
        if self._storage is None:
            raise KeyError(f"任务不存在: {task_id}")

        task = self._storage.get(task_id)
        if task is None:
            raise KeyError(f"任务不存在: {task_id}")

        from tasks.types import TaskStatus  # noqa: PLC0415

        old_status = safe_enum_value(task.status)
        if old_status not in ("running", "evaluating"):
            raise InvalidTransitionError(
                old_status,
                "evaluating",
                f"不允许从 ''{old_status}'' 转换到 evaluating",
            )

        task.status = TaskStatus.EVALUATING
        task.updated_at = datetime.now().isoformat()
        self._storage.save(task)

        await self._emit_state_change(task_id, old_status, "evaluating")

    async def fail_task(
        self,
        task_id: str,
        reason: str = "",
        extra_meta: dict | None = None,
    ) -> None:
        """将任务标记为失败。

        Args:
            task_id: 任务 ID
            reason: 失败原因
            extra_meta: 额外的结构化元数据（如错误类型统计），合并进 task.metadata。
                        供 watchdog/通知器/前端等任意消费方取用。
        """
        if self._storage is None:
            return

        task = self._storage.get(task_id)
        if task is None:
            return

        from tasks.types import TaskStatus  # noqa: PLC0415

        old_status = safe_enum_value(task.status)
        task.status = TaskStatus.FAILED
        task.updated_at = datetime.now().isoformat()
        if extra_meta:
            task.metadata.update(extra_meta)
        if reason:
            task.metadata["fail_reason"] = reason
            # 追加而非覆盖，保留完整错误链
            if task.error and task.error != reason:
                task.error = f"{task.error} → {reason}"
            else:
                task.error = reason
        self._storage.save(task)

        logger.info(
            "TaskService: fail_task 状态已落盘 | task=%s old=%s reason=%s",
            task_id,
            old_status,
            reason[:120] if reason else "",
        )

        # 以下三步各自隔离：单步失败不阻断后续步，避免某步异常导致
        # 通知/级联/清理整体丢失（这正是"子任务失败不通知父任务"的隐蔽根因）。
        # 1. 发射状态变更 → 触发 _on_task_state_changed → _notify_suspended_pipelines
        try:
            await self._emit_state_change(task_id, old_status, "failed")
        except Exception as exc:
            logger.error(
                "TaskService: fail_task 状态变更通知失败（父任务可能收不到失败通知）| task=%s error=%s",
                task_id,
                exc,
                exc_info=exc,
            )

        # 2. 父任务失败时级联取消所有非终态的子任务
        try:
            _cascade_count = await self.fail_task_cascade(task_id, reason=reason)
            if _cascade_count > 0:
                logger.info(
                    "TaskService: fail_task cascade 完成 | parent=%s, cancelled_subtasks=%d",
                    task_id,
                    _cascade_count,
                )
        except Exception as exc:
            logger.error(
                "TaskService: fail_task 级联取消子任务失败 | task=%s error=%s",
                task_id,
                exc,
                exc_info=exc,
            )

        # 3. 任务失败后尝试销毁容器（仅当 workspace 无其他活跃任务时）
        # 失败任务重试时 get_or_create_environment 会自动重建容器
        try:
            await self._try_destroy_container_if_idle(task_id)
        except Exception as exc:
            logger.error(
                "TaskService: fail_task 容器销毁检查失败 | task=%s error=%s",
                task_id,
                exc,
                exc_info=exc,
            )

    async def cancel_task(self, task_id: str, reason: str = "") -> None:
        """将任务标记为已取消。

        Args:
            task_id: 任务 ID
            reason: 取消原因
        """
        if self._storage is None:
            return

        task = self._storage.get(task_id)
        if task is None:
            return

        from tasks.types import TaskStatus  # noqa: PLC0415

        old_status = safe_enum_value(task.status)
        task.status = TaskStatus.STOPPED
        task.updated_at = datetime.now().isoformat()
        if reason:
            task.metadata["cancel_reason"] = reason
            if task.error and task.error != reason:
                task.error = f"{task.error} → {reason}"
            else:
                task.error = reason
        self._storage.save(task)

        await self._emit_state_change(task_id, old_status, "stopped")

    async def cancel_task_cascade(self, task_id: str, reason: str = "") -> int:
        """级联取消指定任务的所有子任务。

        Args:
            task_id: 父任务 ID
            reason: 取消原因

        Returns:
            被级联取消的子任务数量
        """
        if self._storage is None:
            return 0

        subtasks = self._storage.list_by_parent(task_id)
        cancelled_count = 0

        for subtask in subtasks:
            await self.cancel_task(
                subtask.id,
                reason=f"父任务取消，级联取消: {reason}" if reason else "父任务取消，级联取消",
            )
            cancelled_count += 1

            deeper_count = await self.cancel_task_cascade(subtask.id, reason=reason)
            cancelled_count += deeper_count

        return cancelled_count

    async def fail_task_cascade(self, task_id: str, reason: str = "") -> int:
        """级联取消父任务失败时的所有子任务。

        Args:
            task_id: 父任务 ID
            reason: 失败原因

        Returns:
            被级联取消的子任务数量
        """
        if self._storage is None:
            return 0

        from tasks.types import TaskStatus  # noqa: PLC0415

        _TERMINAL = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.STOPPED})  # noqa: N806

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

            deeper_count = await self.fail_task_cascade(subtask.id, reason=reason)
            cancelled_count += deeper_count

        return cancelled_count

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

        from tasks.types import TaskStatus  # noqa: PLC0415

        old_status = safe_enum_value(task.status)
        task.status = TaskStatus.COMPLETED
        task.updated_at = datetime.now().isoformat()
        self._storage.save(task)

        await self._emit_state_change(task_id, old_status, "completed")

        # 任务完成后尝试销毁容器（仅当 workspace 无其他活跃任务时）
        await self._try_destroy_container_if_idle(task_id)

    async def _try_destroy_container_if_idle(self, task_id: str) -> None:
        """任务终态后尝试销毁其 workspace 的容器。

        委托 IsolationManager.destroy_if_workspace_idle：仅当该 workspace
        已无其他活跃任务时才真正销毁。失败任务重试时容器会自动重建。
        异常不向上抛出，避免影响任务状态转换主流程。
        """
        try:
            from isolation.manager import get_isolation_manager  # noqa: PLC0415

            manager = await get_isolation_manager()
            await manager.destroy_if_workspace_idle(task_id)
        except Exception as e:
            logger.debug(
                "TaskService: 终态销毁容器检查失败（非致命）| task=%s, error=%s",
                task_id,
                e,
            )

    async def complete_evaluation(self, task_id: str, passed: bool, result: dict | None = None) -> None:
        """评估完成后更新任务状态。

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

        self._inject_context_usage(task)
        self._storage.save(task)

        if passed:
            await self.complete_task(task_id)
        else:
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

        Args:
            task: TaskModel 实例（会被原地修改 metadata）
        """
        pipeline_run_id = getattr(task, "pipeline_run_id", None)
        if not pipeline_run_id:
            return

        try:
            from pipeline.registry import get_engine_registry  # noqa: PLC0415

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
            pass

    async def recover_to_completed(
        self,
        task_id: str,
        result: dict | None = None,
    ) -> None:
        """将已 failed 的任务恢复为 completed。

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

        from tasks.types import TaskStatus  # noqa: PLC0415

        task.status = TaskStatus.COMPLETED
        task.completed_at = datetime.now(UTC)
        logger.info(
            "[TaskService] 任务已从 failed 恢复为 completed | task_id=%s",
            task_id,
        )

    async def reset_to_pending(self, task_id: str) -> TaskModel | None:
        """将任务重置为 pending 状态（用于恢复/重试）。

        Args:
            task_id: 任务 ID

        Returns:
            更新后的 TaskModel；storage 或任务不存在时返回 None（静默降级）
        """
        if self._storage is None:
            return None

        task = self._storage.get(task_id)
        if task is None:
            return None

        from tasks.types import TaskStatus  # noqa: PLC0415

        old_status = safe_enum_value(task.status)
        task.status = TaskStatus.PENDING
        task.updated_at = datetime.now().isoformat()
        # reset 回到未执行态：清空 started_at，使下次 start 能重记起点。
        task.started_at = None
        self._storage.save(task)

        await self._emit_state_change(task_id, old_status, "pending")
        return task
