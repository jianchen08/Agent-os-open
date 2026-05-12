"""
路由引擎 - 基于策略模式的路由决策系统

使用策略模式重构 should_continue 逻辑，提高扩展性和可维护性。

核心概念：
- RoutingStrategy: 路由策略基类，每个策略负责特定的检查逻辑
- RoutingEngine: 路由引擎，管理所有策略并执行决策
- ContinueDecision/StopDecision: 决策结果封装

使用示例:
    engine = RoutingEngine()
    engine.register(StopRequestedStrategy())
    engine.register(ErrorStrategy())
    engine.register(PendingToolsStrategy())
    # ... 注册更多策略

    result = engine.evaluate(state)
"""

import logging
from abc import ABC, abstractmethod
from enum import Enum, auto
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from src.agents.state import AgentState

logger = logging.getLogger(__name__)


class ContinueReason(Enum):
    """继续执行的原因"""
    TOOLS = auto()              # 有工具要执行
    EVALUATE_REMINDER = auto()  # 需要评估提醒


class StopReason(Enum):
    """停止执行的原因"""
    TASK_COMPLETED = auto()     # 任务完成
    TASK_FAILED = auto()        # 任务失败
    MAX_ITERATIONS = auto()    # 达到最大迭代
    ERROR = auto()             # 执行错误
    STOP_REQUESTED = auto()    # 主动请求停止
    DUPLICATE_EXCEEDED = auto()  # 重复调用超限


class ContinueDecision:
    """继续执行决策"""

    def __init__(
        self,
        reason: ContinueReason,
        message: str = "",
        inject_message: str | None = None,
    ):
        self.reason = reason
        self.message = message
        self.inject_message = inject_message  # 需要注入到上下文的警告消息

    def __repr__(self) -> str:
        return f"ContinueDecision({self.reason.name}, message='{self.message}')"


class StopDecision:
    """停止执行决策"""

    def __init__(
        self,
        reason: StopReason,
        message: str = "",
        error: str | None = None,
    ):
        self.reason = reason
        self.message = message
        self.error = error  # 需要设置到 state 的 error

    def __repr__(self) -> str:
        return f"StopDecision({self.reason.name}, message='{self.message}')"


class RoutingStrategy(ABC):
    """路由策略基类"""

    @abstractmethod
    def evaluate(self, state: "AgentState") -> ContinueDecision | StopDecision | None:
        """
        评估状态，返回决策或 None（不适用）

        Returns:
            ContinueDecision: 继续执行
            StopDecision: 停止执行
            None: 此策略不适用，继续执行下一个策略
        """
        pass

    @property
    @abstractmethod
    def priority(self) -> int:
        """
        优先级，数字越小越先执行

        优先级顺序（从小到大）：
        1. 停止相关检查（should_stop, error, max_iterations）
        2. 重复调用检测（渐进式）
        3. 待执行工具检查
        4. 任务评估检查
        5. 评估提醒检查
        6. 默认结束
        """
        pass


class StopRequestedStrategy(RoutingStrategy):
    """策略1：检查是否主动请求停止"""

    @property
    def priority(self) -> int:
        return 10  # 最高优先级

    def evaluate(self, state: "AgentState") -> ContinueDecision | StopDecision | None:
        if state.get("should_stop"):
            return StopDecision(
                StopReason.STOP_REQUESTED,
                "主动请求停止",
                error="Execution stopped by request",
            )
        return None


class ErrorStrategy(RoutingStrategy):
    """策略2：检查是否有错误"""

    @property
    def priority(self) -> int:
        return 20

    def evaluate(self, state: "AgentState") -> ContinueDecision | StopDecision | None:
        error = state.get("error")
        if error:
            return StopDecision(
                StopReason.ERROR,
                f"执行出错: {error}",
                error=error,
            )
        return None


class MaxIterationsStrategy(RoutingStrategy):
    """策略3：检查是否达到最大迭代"""

    @property
    def priority(self) -> int:
        return 30

    def evaluate(self, state: "AgentState") -> ContinueDecision | StopDecision | None:
        iteration = state.get("iteration", 0)
        # 硬编码默认值，简单直接
        max_iterations = state.get("max_iterations", 200)

        if iteration >= max_iterations:
            return StopDecision(
                StopReason.MAX_ITERATIONS,
                f"已达到最大迭代次数 ({max_iterations})，任务结束",
            )
        return None


class DuplicateCallStrategy(RoutingStrategy):
    """
    策略4：检查重复调用（渐进式处理）

    渐进式处理机制：
    - 第1次检测到重复：注入警告消息，返回继续（让 Agent 意识到问题）
    - 第2次检测到重复：注入更强警告，返回继续
    - 第3次检测到重复：停止任务，标记失败
    """

    DEFAULT_MAX_REPEATS = 3  # 默认最多重复3次

    def __init__(self, max_repeats: int = DEFAULT_MAX_REPEATS):
        self.max_repeats = max_repeats

    @property
    def priority(self) -> int:
        return 40

    def evaluate(self, state: "AgentState") -> ContinueDecision | StopDecision | None:
        from src.agents.utils import DuplicateCallDetector

        tool_calls_history = state.get("tool_calls", [])
        pending_calls = state.get("pending_tool_calls", [])

        # 使用 DuplicateCallDetector 检查是否有重复调用
        duplicate = DuplicateCallDetector.check_duplicate(
            tool_calls_history, min_consecutive_calls=2
        )

        # 如果没有重复调用，或者没有待执行的工具，则不处理
        if not duplicate or not pending_calls:
            return None

        # 有待执行工具 + 检测到重复，进行渐进式处理
            # 获取当前重复计数
            repeat_count = state.get("tool_repeat_count", 0) + 1
            state["tool_repeat_count"] = repeat_count

            tool_name = duplicate.get("tool_name", "unknown")
            inputs = duplicate.get("inputs", {})

            logger.warning(
                f"[DuplicateCallStrategy] 检测到重复调用 | "
                f"tool={tool_name} | repeat_count={repeat_count}/{self.max_repeats}"
            )

            # 渐进式处理
            if repeat_count == 1:
                # 第1次：注入警告消息，返回继续
                warning_msg = (
                    f"⚠️ 检测到重复调用：工具 '{tool_name}' 使用相同参数已被调用多次。\n"
                    f"参数: {inputs}\n"
                    f"请检查参数是否正确，或尝试其他方法。"
                )
                return ContinueDecision(
                    ContinueReason.TOOLS,
                    message="第1次重复调用，注入警告",
                    inject_message=warning_msg,
                )

            elif repeat_count == 2:
                # 第2次：更强警告
                warning_msg = (
                    f"⚠️ 警告：'{tool_name}' 重复调用已达2次！\n"
                    f"参数: {inputs}\n"
                    f"这是最后一次机会，请分析现有结果并尝试不同方法，"
                    f"否则任务将失败。"
                )
                return ContinueDecision(
                    ContinueReason.TOOLS,
                    message="第2次重复调用，注入更强警告",
                    inject_message=warning_msg,
                )

            else:
                # 第3次及以上：结束任务
                error_msg = (
                    f"工具 '{tool_name}' 重复调用超过 {self.max_repeats} 次上限，"
                    f"任务失败。"
                )
                return StopDecision(
                    StopReason.DUPLICATE_EXCEEDED,
                    message=error_msg,
                    error=error_msg,
                )

        # 如果没有重复调用，重置计数
        if state.get("tool_repeat_count", 0) > 0:
            state["tool_repeat_count"] = 0
            logger.info("[DuplicateCallStrategy] 重复调用已解除，重置计数")

        return None


class PendingToolsStrategy(RoutingStrategy):
    """策略5：检查是否有待执行工具"""

    @property
    def priority(self) -> int:
        return 100

    def evaluate(self, state: "AgentState") -> ContinueDecision | StopDecision | None:
        pending_calls = state.get("pending_tool_calls", [])
        if pending_calls:
            tool_names = [tc.get("name", tc.get("function", {}).get("name", ""))
                         for tc in pending_calls]
            logger.debug(
                f"[PendingToolsStrategy] 有待执行工具 | count={len(pending_calls)} | tools={tool_names}"
            )
            return ContinueDecision(
                ContinueReason.TOOLS,
                message=f"有 {len(pending_calls)} 个工具待执行: {', '.join(tool_names)}",
            )
        return None


class TaskEvaluationStrategy(RoutingStrategy):
    """
    策略6：任务评估处理（合并版）

    职责：
    1. 检查 task_evaluate 是否已调用且通过 → 结束任务
    2. 检查是否有文本输出 + task_id + 未评估 → 触发评估提醒
    3. 其他情况 → 不干预，让后续策略处理
    """

    @property
    def priority(self) -> int:
        return 200

    def evaluate(self, state: "AgentState") -> ContinueDecision | StopDecision | None:
        tool_calls_history = state.get("tool_calls", [])

        # 1. 检查是否调用过 task_evaluate 且通过
        for call in tool_calls_history:
            if call.get("tool_name") == "task_evaluate":
                if call.get("success", False):
                    output = call.get("output", {})
                    if isinstance(output, dict):
                        task_status = output.get("task_status", "")
                        if task_status == "completed":
                            logger.info("[TaskEvaluationStrategy] 任务评估通过")
                            return StopDecision(
                                StopReason.TASK_COMPLETED,
                                message="任务评估通过，任务完成",
                            )

        # 2. 检查是否有文本输出（AI 返回文本但没有工具调用）
        final_output = state.get("final_output")

        layered_context_store = state.get("layered_context_store")
        messages = []
        if layered_context_store and hasattr(layered_context_store, "_messages"):
            messages = layered_context_store._messages
        else:
            messages = state.get("messages", [])

        has_text_output = bool(final_output)
        if not has_text_output and messages:
            last_msg = messages[-1]
            if hasattr(last_msg, "type") and last_msg.type == "ai":
                has_tool_calls = hasattr(last_msg, "tool_calls") and last_msg.tool_calls
                if not has_tool_calls:
                    has_text_output = True

        # 3. 如果有文本输出，检查是否需要触发评估提醒
        if has_text_output:
            # 获取 task_id
            context = state.get("context", {})
            task_id = None
            if isinstance(context, dict):
                task_id = context.get("task_id") or context.get("metadata", {}).get("task_id")
            elif hasattr(context, "metadata"):
                task_id = context.get("metadata", {}).get("task_id")

            # 如果有 task_id 且未调用过评估，触发提醒
            if task_id:
                has_called_evaluate = any(
                    c.get("tool_name") == "task_evaluate" for c in tool_calls_history
                )

                if not has_called_evaluate:
                    reminder_count = state.get("evaluate_reminder_count", 0)
                    if reminder_count < 1:
                        logger.info(
                            f"[TaskEvaluationStrategy] 触发评估提醒 | "
                            f"task_id={task_id} | reminder_count={reminder_count + 1}"
                        )
                        return ContinueDecision(
                            ContinueReason.EVALUATE_REMINDER,
                            message=f"需要为任务 {task_id} 提交评估",
                        )

        # 不干预，让后续策略处理
        return None


class DefaultEndStrategy(RoutingStrategy):
    """策略7：默认结束（兜底策略）"""

    @property
    def priority(self) -> int:
        return 1000  # 最低优先级

    def evaluate(self, state: "AgentState") -> ContinueDecision | StopDecision | None:
        # 没有其他策略返回决策，默认结束
        return StopDecision(
            StopReason.TASK_COMPLETED,
            message="默认结束",
        )


class RoutingEngine:
    """
    路由引擎 - 管理所有策略并执行决策

    使用示例:
        engine = RoutingEngine()
        engine.register(StopRequestedStrategy())
        engine.register(ErrorStrategy())
        engine.register(MaxIterationsStrategy())
        engine.register(DuplicateCallStrategy())
        engine.register(PendingToolsStrategy())
        engine.register(TaskEvaluationStrategy())
        engine.register(EvaluateReminderStrategy())
        engine.register(DefaultEndStrategy())

        decision = engine.evaluate(state)
    """

    def __init__(self):
        self._strategies: list[RoutingStrategy] = []
        self._initialized = False

    def register(self, strategy: RoutingStrategy) -> "RoutingEngine":
        """注册策略，支持链式调用"""
        self._strategies.append(strategy)
        self._initialized = False
        return self

    def _ensure_sorted(self):
        """确保策略按优先级排序"""
        if not self._initialized:
            self._strategies.sort(key=lambda s: s.priority)
            self._initialized = True
            logger.debug(
                f"[RoutingEngine] 策略优先级: "
                f"{[(s.__class__.__name__, s.priority) for s in self._strategies]}"
            )

    def evaluate(
        self, state: "AgentState"
    ) -> tuple[Literal["tools", "evaluate_reminder", "end"], ContinueDecision | StopDecision]:
        """
        执行路由决策

        Args:
            state: Agent 状态

        Returns:
            (路由指令, 决策详情)
            - "tools": 继续执行工具
            - "evaluate_reminder": 触发评估提醒
            - "end": 结束执行
        """
        self._ensure_sorted()

        for strategy in self._strategies:
            decision = strategy.evaluate(state)

            if decision is not None:
                logger.debug(
                    f"[RoutingEngine] 策略 {strategy.__class__.__name__} 返回: {decision}"
                )

                if isinstance(decision, ContinueDecision):
                    # 如果有注入消息，设置到 state
                    if decision.inject_message:
                        state["_routing_warning"] = decision.inject_message
                        logger.info(
                            f"[RoutingEngine] 注入警告消息: {decision.inject_message[:100]}..."
                        )

                    # 根据原因返回对应指令
                    if decision.reason == ContinueReason.TOOLS:
                        return "tools", decision
                    elif decision.reason == ContinueReason.EVALUATE_REMINDER:
                        return "evaluate_reminder", decision

                elif isinstance(decision, StopDecision):
                    # 如果有错误，设置到 state
                    if decision.error:
                        state["error"] = decision.error

                    logger.info(
                        f"[RoutingEngine] 任务停止 | reason={decision.reason.name} | "
                        f"message={decision.message}"
                    )
                    return "end", decision

        # 兜底：默认结束
        return "end", StopDecision(StopReason.TASK_COMPLETED, "默认结束")


# 全局默认引擎（延迟初始化）
_default_engine: RoutingEngine | None = None


def get_default_routing_engine() -> RoutingEngine:
    """获取默认路由引擎（单例）"""
    global _default_engine
    if _default_engine is None:
        _default_engine = (
            RoutingEngine()
            .register(StopRequestedStrategy())
            .register(ErrorStrategy())
            .register(MaxIterationsStrategy())
            .register(DuplicateCallStrategy())
            .register(PendingToolsStrategy())
            .register(TaskEvaluationStrategy())  # 合并了 EvaluateReminder 逻辑
            .register(DefaultEndStrategy())
        )
    return _default_engine
