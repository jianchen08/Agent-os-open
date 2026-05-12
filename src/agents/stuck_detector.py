"""
Agent 卡死检测器 (StuckDetector)

两层防卡死机制的第一层：通用层
- 对所有 Agent 执行过程都适用
- 检测周期：每次迭代
- 响应时间：秒级

检测模式：
1. 动作重复检测 - 连续多次相同动作
2. 无进展检测 - 多步无状态变化
3. 错误循环检测 - 连续失败
4. 状态停滞检测 - 长时间无更新
5. 迭代次数超限 - 超过最大迭代次数
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class StuckType(str, Enum):
    """卡死类型枚举"""

    REPEATED_ACTION = "repeated_action"
    NO_PROGRESS = "no_progress"
    ERROR_LOOP = "error_loop"
    STATE_STALLED = "state_stalled"
    MAX_ITERATIONS = "max_iterations"
    DUPLICATE_CALL = "duplicate_call"
    EXCESSIVE_FAILURES = "excessive_failures"
    CUSTOM = "custom"


class RecoveryAction(str, Enum):
    """恢复动作枚举"""

    TERMINATE = "terminate"
    RETRY_WITH_DIFFERENT_PARAMS = "retry_different"
    SKIP_AND_CONTINUE = "skip_continue"
    REQUEST_USER_INPUT = "request_input"
    ROLLBACK = "rollback"
    ESCALATE = "escalate"


@dataclass
class StuckResult:
    """卡死检测结果"""

    is_stuck: bool
    stuck_type: StuckType | None = None
    severity: str = "low"
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    recovery_suggestion: RecoveryAction = RecoveryAction.TERMINATE
    confidence: float = 0.0
    detected_at: datetime = field(default_factory=datetime.now)


@dataclass
class StateSnapshot:
    """状态快照"""

    iteration: int
    tool_calls_count: int
    last_tool_name: str | None
    last_tool_args: dict[str, Any] | None
    has_error: bool
    error_message: str | None
    timestamp: float = field(default_factory=time.time)
    state_hash: str = ""

    def __post_init__(self):
        if not self.state_hash:
            self.state_hash = self._compute_hash()

    def _compute_hash(self) -> str:
        """计算状态哈希"""
        data = {
            "iteration": self.iteration,
            "tool_calls_count": self.tool_calls_count,
            "last_tool_name": self.last_tool_name,
            "last_tool_args": self.last_tool_args,
        }
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]


class StuckDetector:
    """
    Agent 卡死检测器

    两层防卡死机制的第一层：通用层
    检测 Agent 执行过程中的各种异常状态，并提供恢复建议。

    使用示例：
        detector = StuckDetector()
        result = detector.detect(state_history)
        if result.is_stuck:
            print(f"检测到卡死: {result.message}")
            print(f"建议恢复动作: {result.recovery_suggestion}")
    """

    DEFAULT_CONFIG = {
        "max_consecutive_same_actions": 2,
        "max_no_progress_steps": 10,
        "max_consecutive_errors": 3,
        "max_stall_time_seconds": 300,
        "state_history_size": 20,
        "enable_all_detectors": True,
        "max_consecutive_failures": 2,
    }

    def __init__(self, config: dict[str, Any] | None = None):
        """
        初始化卡死检测器

        Args:
            config: 配置参数，可覆盖默认值
        """
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
        self._state_history: list[StateSnapshot] = []
        self._detection_stats: dict[str, int] = {
            "total_checks": 0,
            "stuck_detected": 0,
            "by_type": {t.value: 0 for t in StuckType},
        }

    def record_state(
        self,
        iteration: int,
        tool_calls: list[dict[str, Any]],
        error: str | None = None,
    ) -> None:
        """
        记录当前状态

        Args:
            iteration: 当前迭代次数
            tool_calls: 工具调用历史
            error: 错误信息
        """
        last_tool = tool_calls[-1] if tool_calls else None
        snapshot = StateSnapshot(
            iteration=iteration,
            tool_calls_count=len(tool_calls),
            last_tool_name=last_tool.get("tool_name") if last_tool else None,
            last_tool_args=last_tool.get("inputs") if last_tool else None,
            has_error=error is not None,
            error_message=error,
        )

        self._state_history.append(snapshot)

        max_size = self.config["state_history_size"]
        if len(self._state_history) > max_size:
            self._state_history = self._state_history[-max_size:]

    def detect(
        self,
        state: dict[str, Any] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> StuckResult:
        """
        检测是否卡死

        综合使用多种检测模式，返回最严重的卡死结果。

        Args:
            state: 当前 Agent 状态（可选）
            tool_calls: 工具调用历史（可选）

        Returns:
            StuckResult 检测结果
        """
        self._detection_stats["total_checks"] += 1

        if state and tool_calls is not None:
            self.record_state(
                iteration=state.get("iteration", 0),
                tool_calls=tool_calls,
                error=state.get("error"),
            )

        if not self._state_history:
            return StuckResult(is_stuck=False)

        results = []

        if self.config["enable_all_detectors"]:
            for detector in [
                self._detect_duplicate_call,
                self._detect_excessive_failures,
                self._detect_repeated_actions,
                self._detect_error_loop,
                self._detect_no_progress,
                self._detect_state_stalled,
            ]:
                result = detector()
                if result.is_stuck:
                    results.append(result)

        if results:
            results.sort(key=lambda r: self._severity_score(r.severity), reverse=True)
            best_result = results[0]
            self._detection_stats["stuck_detected"] += 1
            self._detection_stats["by_type"][best_result.stuck_type.value] += 1
            return best_result

        return StuckResult(is_stuck=False)

    def check_duplicate(
        self,
        tool_calls_history: list[dict[str, Any]],
        min_consecutive_calls: int | None = None,
    ) -> dict[str, Any] | None:
        """
        检查是否有重复的工具调用

        Args:
            tool_calls_history: 工具调用历史记录
            min_consecutive_calls: 最小连续调用次数

        Returns:
            如果检测到重复，返回包含 tool_name 和 inputs 的字典
        """
        if min_consecutive_calls is None:
            min_consecutive_calls = self.config["max_consecutive_same_actions"]

        if len(tool_calls_history) < min_consecutive_calls:
            return None

        recent_calls = tool_calls_history[-min_consecutive_calls:]

        first_call = recent_calls[0]
        first_tool_name = first_call.get("tool_name")
        first_inputs = first_call.get("inputs")

        for call in recent_calls[1:]:
            if (
                call.get("tool_name") != first_tool_name
                or call.get("inputs") != first_inputs
            ):
                return None

        return {
            "tool_name": first_tool_name,
            "inputs": first_inputs,
            "consecutive_count": min_consecutive_calls,
        }

    def get_consecutive_failures(
        self,
        tool_calls_history: list[dict[str, Any]],
    ) -> dict[str, int]:
        """
        获取每个工具的连续失败次数

        Args:
            tool_calls_history: 工具调用历史记录

        Returns:
            字典 {tool_name: consecutive_failure_count}
        """
        if not tool_calls_history:
            return {}

        consecutive_failures: dict[str, int] = {}
        last_tool_name: str | None = None

        for call in reversed(tool_calls_history):
            tool_name = call.get("tool_name")
            success = call.get("success", False)

            if not success:
                if tool_name == last_tool_name:
                    consecutive_failures[tool_name] = (
                        consecutive_failures.get(tool_name, 0) + 1
                    )
                else:
                    consecutive_failures[tool_name] = 1
                    last_tool_name = tool_name
            else:
                break

        return consecutive_failures

    def has_excessive_failures(
        self,
        tool_calls_history: list[dict[str, Any]],
        max_consecutive_failures: int | None = None,
    ) -> dict[str, Any] | None:
        """
        检查是否有工具连续失败超过限制

        Args:
            tool_calls_history: 工具调用历史记录
            max_consecutive_failures: 最大允许的连续失败次数

        Returns:
            如果有工具超过限制，返回包含 tool_name 和 failure_count 的字典
        """
        if max_consecutive_failures is None:
            max_consecutive_failures = self.config["max_consecutive_failures"]

        consecutive_failures = self.get_consecutive_failures(tool_calls_history)

        for tool_name, count in consecutive_failures.items():
            if count > max_consecutive_failures:
                return {
                    "tool_name": tool_name,
                    "failure_count": count,
                    "max_allowed": max_consecutive_failures,
                }

        return None

    def _detect_duplicate_call(self) -> StuckResult:
        """
        检测重复工具调用

        检查最近 N 次调用中是否有相同工具名称和参数的重复调用
        """
        tool_calls = self._get_tool_calls_from_history()
        if not tool_calls:
            return StuckResult(is_stuck=False)

        duplicate = self.check_duplicate(tool_calls)
        if duplicate:
            return StuckResult(
                is_stuck=True,
                stuck_type=StuckType.DUPLICATE_CALL,
                severity="high",
                message=f"检测到工具 '{duplicate['tool_name']}' 被重复调用相同参数",
                details={
                    "tool_name": duplicate["tool_name"],
                    "tool_args": duplicate["inputs"],
                    "consecutive_count": duplicate["consecutive_count"],
                },
                recovery_suggestion=RecoveryAction.TERMINATE,
                confidence=0.95,
            )

        return StuckResult(is_stuck=False)

    def _detect_excessive_failures(self) -> StuckResult:
        """
        检测过度失败

        检查是否有工具连续失败超过限制
        """
        tool_calls = self._get_tool_calls_from_history()
        if not tool_calls:
            return StuckResult(is_stuck=False)

        excessive = self.has_excessive_failures(tool_calls)
        if excessive:
            return StuckResult(
                is_stuck=True,
                stuck_type=StuckType.EXCESSIVE_FAILURES,
                severity="high",
                message=f"工具 '{excessive['tool_name']}' 连续失败 {excessive['failure_count']} 次",
                details={
                    "tool_name": excessive["tool_name"],
                    "failure_count": excessive["failure_count"],
                    "max_allowed": excessive["max_allowed"],
                },
                recovery_suggestion=RecoveryAction.SKIP_AND_CONTINUE,
                confidence=0.9,
            )

        return StuckResult(is_stuck=False)

    def _detect_repeated_actions(self) -> StuckResult:
        """
        检测重复动作

        检查最近 N 次动作是否完全相同
        """
        min_consecutive = self.config["max_consecutive_same_actions"]

        if len(self._state_history) < min_consecutive:
            return StuckResult(is_stuck=False)

        recent = self._state_history[-min_consecutive:]

        first = recent[0]
        all_same = all(
            s.last_tool_name == first.last_tool_name
            and s.last_tool_args == first.last_tool_args
            for s in recent[1:]
        )

        if all_same and first.last_tool_name:
            return StuckResult(
                is_stuck=True,
                stuck_type=StuckType.REPEATED_ACTION,
                severity="high",
                message=f"检测到工具 '{first.last_tool_name}' 连续调用 {min_consecutive} 次相同参数",
                details={
                    "tool_name": first.last_tool_name,
                    "tool_args": first.last_tool_args,
                    "consecutive_count": min_consecutive,
                },
                recovery_suggestion=RecoveryAction.TERMINATE,
                confidence=0.9,
            )

        return StuckResult(is_stuck=False)

    def _detect_no_progress(self) -> StuckResult:
        """
        检测无进展

        检查最近 N 步状态是否没有变化
        """
        max_steps = self.config["max_no_progress_steps"]

        if len(self._state_history) < max_steps:
            return StuckResult(is_stuck=False)

        recent = self._state_history[-max_steps:]

        first_hash = recent[0].state_hash
        all_same = all(s.state_hash == first_hash for s in recent[1:])

        if all_same:
            return StuckResult(
                is_stuck=True,
                stuck_type=StuckType.NO_PROGRESS,
                severity="medium",
                message=f"检测到最近 {max_steps} 步无状态变化",
                details={
                    "steps_without_progress": max_steps,
                    "state_hash": first_hash,
                },
                recovery_suggestion=RecoveryAction.REQUEST_USER_INPUT,
                confidence=0.7,
            )

        first = recent[0]
        last = recent[-1]
        if last.iteration > first.iteration and last.tool_calls_count == first.tool_calls_count:
            return StuckResult(
                is_stuck=True,
                stuck_type=StuckType.NO_PROGRESS,
                severity="medium",
                message="检测到迭代增加但工具调用数未变化",
                details={
                    "iteration_delta": last.iteration - first.iteration,
                    "tool_calls_count": last.tool_calls_count,
                },
                recovery_suggestion=RecoveryAction.RETRY_WITH_DIFFERENT_PARAMS,
                confidence=0.6,
            )

        return StuckResult(is_stuck=False)

    def _detect_error_loop(self) -> StuckResult:
        """
        检测错误循环

        检查是否有连续多次失败
        """
        max_errors = self.config["max_consecutive_errors"]

        if len(self._state_history) < max_errors:
            return StuckResult(is_stuck=False)

        recent = self._state_history[-max_errors:]

        if all(s.has_error for s in recent):
            error_messages = [s.error_message for s in recent if s.error_message]
            return StuckResult(
                is_stuck=True,
                stuck_type=StuckType.ERROR_LOOP,
                severity="high",
                message=f"检测到连续 {max_errors} 次错误",
                details={
                    "error_count": max_errors,
                    "error_messages": error_messages[-3:],
                },
                recovery_suggestion=RecoveryAction.TERMINATE,
                confidence=0.85,
            )

        tool_failures: dict[str, int] = {}
        for s in recent:
            if s.has_error and s.last_tool_name:
                tool_failures[s.last_tool_name] = tool_failures.get(s.last_tool_name, 0) + 1

        for tool_name, count in tool_failures.items():
            if count >= max_errors:
                return StuckResult(
                    is_stuck=True,
                    stuck_type=StuckType.ERROR_LOOP,
                    severity="high",
                    message=f"检测到工具 '{tool_name}' 连续失败 {count} 次",
                    details={
                        "tool_name": tool_name,
                        "failure_count": count,
                    },
                    recovery_suggestion=RecoveryAction.SKIP_AND_CONTINUE,
                    confidence=0.8,
                )

        return StuckResult(is_stuck=False)

    def _detect_state_stalled(self) -> StuckResult:
        """
        检测状态停滞

        检查状态是否长时间未更新
        """
        max_stall_time = self.config["max_stall_time_seconds"]

        if len(self._state_history) < 2:
            return StuckResult(is_stuck=False)

        last_state = self._state_history[-1]
        current_time = time.time()
        stall_time = current_time - last_state.timestamp

        if stall_time > max_stall_time:
            return StuckResult(
                is_stuck=True,
                stuck_type=StuckType.STATE_STALLED,
                severity="medium",
                message=f"检测到状态停滞 {int(stall_time)} 秒",
                details={
                    "stall_time_seconds": int(stall_time),
                    "last_update": datetime.fromtimestamp(last_state.timestamp).isoformat(),
                },
                recovery_suggestion=RecoveryAction.ESCALATE,
                confidence=0.75,
            )

        return StuckResult(is_stuck=False)

    def _get_tool_calls_from_history(self) -> list[dict[str, Any]]:
        """从状态历史中提取工具调用列表"""
        tool_calls = []
        for snapshot in self._state_history:
            if snapshot.last_tool_name:
                tool_calls.append({
                    "tool_name": snapshot.last_tool_name,
                    "inputs": snapshot.last_tool_args or {},
                    "success": not snapshot.has_error,
                })
        return tool_calls

    def _severity_score(self, severity: str) -> int:
        """将严重程度转换为分数"""
        scores = {"low": 1, "medium": 2, "high": 3, "critical": 4}
        return scores.get(severity, 0)

    def get_stats(self) -> dict[str, Any]:
        """
        获取检测统计信息

        Returns:
            统计信息字典
        """
        return {
            **self._detection_stats,
            "history_size": len(self._state_history),
            "config": self.config,
        }

    def reset(self) -> None:
        """重置检测器状态"""
        self._state_history.clear()
        self._detection_stats = {
            "total_checks": 0,
            "stuck_detected": 0,
            "by_type": {t.value: 0 for t in StuckType},
        }
        logger.info("[StuckDetector] 检测器已重置")

    def suggest_recovery(self, stuck_type: StuckType) -> RecoveryAction:
        """
        根据卡死类型建议恢复动作

        Args:
            stuck_type: 卡死类型

        Returns:
            建议的恢复动作
        """
        recovery_map = {
            StuckType.REPEATED_ACTION: RecoveryAction.TERMINATE,
            StuckType.NO_PROGRESS: RecoveryAction.REQUEST_USER_INPUT,
            StuckType.ERROR_LOOP: RecoveryAction.SKIP_AND_CONTINUE,
            StuckType.STATE_STALLED: RecoveryAction.ESCALATE,
            StuckType.MAX_ITERATIONS: RecoveryAction.TERMINATE,
            StuckType.DUPLICATE_CALL: RecoveryAction.TERMINATE,
            StuckType.EXCESSIVE_FAILURES: RecoveryAction.SKIP_AND_CONTINUE,
            StuckType.CUSTOM: RecoveryAction.REQUEST_USER_INPUT,
        }
        return recovery_map.get(stuck_type, RecoveryAction.TERMINATE)


_stuck_detector: StuckDetector | None = None


def get_stuck_detector(config: dict[str, Any] | None = None) -> StuckDetector:
    """
    获取全局卡死检测器实例

    Args:
        config: 配置参数（仅首次调用时生效）

    Returns:
        StuckDetector 实例
    """
    global _stuck_detector
    if _stuck_detector is None:
        _stuck_detector = StuckDetector(config)
    return _stuck_detector


def reset_stuck_detector() -> None:
    """重置全局卡死检测器"""
    global _stuck_detector
    if _stuck_detector:
        _stuck_detector.reset()
