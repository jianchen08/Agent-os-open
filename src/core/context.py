"""
执行上下文模块

提供执行过程中的状态管理和上下文传递。
"""

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.core.states import ExecutionStatus


@dataclass
class StepRecord:
    """步骤记录"""

    step_id: str
    name: str
    status: str  # success / failed / skipped
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] | None = None
    error: str | None = None
    started_at: datetime = field(default_factory=datetime.now)
    completed_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典"""
        return {
            "step_id": self.step_id,
            "name": self.name,
            "status": self.status,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "error": self.error,
            "started_at": self.started_at.isoformat(),
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
        }


@dataclass
class AttemptRecord:
    """尝试记录"""

    attempt_number: int
    step_id: str
    strategy: str  # retry_same / retry_modified / rollback_one / replan
    result: str  # success / failed
    error_message: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典"""
        return {
            "attempt_number": self.attempt_number,
            "step_id": self.step_id,
            "strategy": self.strategy,
            "result": self.result,
            "error_message": self.error_message,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class ErrorContext:
    """错误上下文"""

    error_type: str
    error_message: str
    failed_step: str
    step_inputs: dict[str, Any]
    step_outputs: dict[str, Any] | None = None
    stack_trace: str | None = None
    previous_attempts: list[AttemptRecord] = field(default_factory=list)
    root_cause: str | None = None
    suggested_fix: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典"""
        return {
            "error_type": self.error_type,
            "error_message": self.error_message,
            "failed_step": self.failed_step,
            "step_inputs": self.step_inputs,
            "step_outputs": self.step_outputs,
            "stack_trace": self.stack_trace,
            "previous_attempts": [a.to_dict() for a in self.previous_attempts],
            "root_cause": self.root_cause,
            "suggested_fix": self.suggested_fix,
        }


class ExecutionContext:
    """
    执行上下文

    管理执行过程中的状态、变量和历史记录。
    """

    def __init__(
        self,
        session_id: str,
        task: str,
        initial_variables: dict[str, Any] | None = None,
    ):
        """
        初始化执行上下文

        Args:
            session_id: 会话 ID
            task: 任务描述
            initial_variables: 初始变量
        """
        self.session_id = session_id
        self.task = task
        self._state = ExecutionStatus.PENDING
        self._current_step: str | None = None
        self._current_step_name: str | None = None
        self._variables: dict[str, Any] = initial_variables or {}
        self._completed_steps: list[StepRecord] = []
        self._skipped_steps: list[str] = []
        self._attempt_counts: dict[str, int] = {}
        self._attempt_records: dict[str, list[AttemptRecord]] = {}
        self._last_error: ErrorContext | None = None
        self._human_guidance: list[str] = []
        self._current_plan: dict[str, Any] | None = None
        self._result: dict[str, Any] | None = None
        self._start_time: float | None = None
        self._end_time: float | None = None

    @property
    def state(self) -> ExecutionStatus:
        """获取当前状态"""
        return self._state

    @property
    def current_step(self) -> str | None:
        """获取当前步骤 ID"""
        return self._current_step

    @property
    def current_step_name(self) -> str | None:
        """获取当前步骤名称"""
        return self._current_step_name

    @property
    def completed_steps(self) -> list[StepRecord]:
        """获取已完成步骤列表"""
        return self._completed_steps.copy()

    @property
    def skipped_steps(self) -> list[str]:
        """获取已跳过步骤列表"""
        return self._skipped_steps.copy()

    @property
    def last_error(self) -> ErrorContext | None:
        """获取最后一个错误上下文"""
        return self._last_error

    @property
    def human_guidance(self) -> list[str]:
        """获取人类指导列表"""
        return self._human_guidance.copy()

    @property
    def current_plan(self) -> dict[str, Any] | None:
        """获取当前执行计划"""
        return self._current_plan

    @property
    def result(self) -> dict[str, Any] | None:
        """获取执行结果"""
        return self._result

    def set_state(self, state: ExecutionStatus) -> None:
        """设置状态"""
        self._state = state

    def set_current_step(self, step_id: str, step_name: str) -> None:
        """设置当前步骤"""
        self._current_step = step_id
        self._current_step_name = step_name

    def record_step_result(
        self,
        status: str,
        inputs: dict[str, Any] | None = None,
        outputs: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """记录步骤结果"""
        if not self._current_step:
            return

        record = StepRecord(
            step_id=self._current_step,
            name=self._current_step_name or self._current_step,
            status=status,
            inputs=inputs or {},
            outputs=outputs,
            error=error,
            completed_at=datetime.now(),
        )
        self._completed_steps.append(record)

    def get_attempt_count(self, step_id: str) -> int:
        """获取步骤尝试次数"""
        return self._attempt_counts.get(step_id, 0)

    def increment_attempt(self, step_id: str) -> None:
        """增加步骤尝试次数"""
        self._attempt_counts[step_id] = self._attempt_counts.get(step_id, 0) + 1

    def add_attempt_record(
        self,
        step_id: str,
        strategy: str,
        result: str,
        error_message: str | None = None,
    ) -> None:
        """添加尝试记录"""
        if step_id not in self._attempt_records:
            self._attempt_records[step_id] = []

        attempt_number = len(self._attempt_records[step_id]) + 1
        record = AttemptRecord(
            attempt_number=attempt_number,
            step_id=step_id,
            strategy=strategy,
            result=result,
            error_message=error_message,
        )
        self._attempt_records[step_id].append(record)

    def get_attempts(self, step_id: str) -> list[AttemptRecord]:
        """获取步骤的尝试记录"""
        return self._attempt_records.get(step_id, []).copy()

    def add_error_context(self, error_context: ErrorContext) -> None:
        """添加错误上下文"""
        self._last_error = error_context

    def add_human_guidance(self, guidance: str) -> None:
        """添加人类指导"""
        self._human_guidance.append(guidance)

    def skip_current_step(self) -> None:
        """跳过当前步骤"""
        if self._current_step:
            self._skipped_steps.append(self._current_step)

    def set_variable(self, key: str, value: Any) -> None:
        """设置变量"""
        self._variables[key] = value

    def get_variable(self, key: str, default: Any = None) -> Any:
        """获取变量"""
        return self._variables.get(key, default)

    def update_plan(self, plan: dict[str, Any]) -> None:
        """更新执行计划"""
        self._current_plan = plan

    def set_result(self, result: dict[str, Any]) -> None:
        """设置执行结果"""
        self._result = result

    def start_execution(self) -> None:
        """开始执行"""
        self._start_time = time.time()
        self._state = ExecutionStatus.RUNNING

    def end_execution(self) -> None:
        """结束执行"""
        self._end_time = time.time()

    def get_execution_duration(self) -> float:
        """获取执行时长（秒）"""
        if self._start_time is None:
            return 0.0
        end = self._end_time or time.time()
        return end - self._start_time

    def get_state_snapshot(self) -> dict[str, Any]:
        """获取状态快照"""
        return {
            "state": self._state.value,
            "current_step": self._current_step,
            "current_step_name": self._current_step_name,
            "variables": self._variables.copy(),
            "completed_steps": [s.to_dict() for s in self._completed_steps],
            "skipped_steps": self._skipped_steps.copy(),
            "attempt_counts": self._attempt_counts.copy(),
        }

    def restore_from_snapshot(self, snapshot: dict[str, Any]) -> None:
        """从快照恢复"""
        self._state = ExecutionStatus(snapshot.get("state", "pending"))
        self._current_step = snapshot.get("current_step")
        self._current_step_name = snapshot.get("current_step_name")
        self._variables = snapshot.get("variables", {}).copy()
        self._skipped_steps = snapshot.get("skipped_steps", []).copy()
        self._attempt_counts = snapshot.get("attempt_counts", {}).copy()
        # completed_steps 需要特殊处理，这里简化
        self._completed_steps = []

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典"""
        return {
            "session_id": self.session_id,
            "task": self.task,
            "state": self._state.value,
            "current_step": self._current_step,
            "current_step_name": self._current_step_name,
            "variables": self._variables.copy(),
            "completed_steps": [s.to_dict() for s in self._completed_steps],
            "skipped_steps": self._skipped_steps.copy(),
            "human_guidance": self._human_guidance.copy(),
            "result": self._result,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionContext":
        """从字典反序列化"""
        context = cls(
            session_id=data["session_id"],
            task=data.get("task", ""),
            initial_variables=data.get("variables", {}),
        )
        context._state = ExecutionStatus(data.get("state", "pending"))
        context._current_step = data.get("current_step")
        context._current_step_name = data.get("current_step_name")
        context._skipped_steps = data.get("skipped_steps", [])
        context._human_guidance = data.get("human_guidance", [])
        context._result = data.get("result")
        return context
