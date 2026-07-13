"""停止检查 Output 插件 — 合并 stop_requested + stop_check + task_status。

负责在管道循环的输出阶段统一管理所有"停止判断"逻辑：
1. 用户请求停止（should_stop）
2. 迭代上限/超时检测（stop_check_strategy）
3. 任务被删除/取消（task_status）

合并收益：高内聚（共享 should_stop/iteration_count 状态字段）+ 低维护成本。

M6d 阶段：从旧代码 agents/decision/strategies/iteration/ 中的
stop_requested、stop_check_strategy、task_status 合并迁移。

State 命名空间：
    - router.stop_reason : 本插件写入的停止原因
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ErrorPolicy, RouteSignal, StateKeys

logger = logging.getLogger(__name__)


class StopCheckPlugin(IOutputPlugin):
    """停止检查 Output 插件。

    合并了旧代码中 stop_requested、stop_check_strategy、task_status
    三个策略的停止判断逻辑。三者共享 should_stop/iteration_count 状态，
    合并后统一管理"停止"关注点的状态读取和判断。

    检查维度（按优先级）：
    1. 用户请求停止 → should_stop == True
    2. 迭代上限检测 → iteration > max_iterations
    4. 执行超时检测 → elapsed > max_duration
    5. task_evaluate 工具结果检测 → completed/failed
    6. 任务状态检测 → task 被删除/取消/完成/失败

    优先级：1（系统级，最高优先级检查）
    错误策略：ABORT（停止判断异常必须终止管道）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.ABORT

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化停止检查插件。

        Args:
            config: 插件配置字典，支持以下键：
                - max_iterations: 最大迭代次数（默认 20）
                - max_duration_seconds: 最大执行时间秒数（默认 600）
                - check_task_status: 是否检查任务状态（默认 True）
        """
        self._config = config or {}
        self._max_iterations = self._config.get("max_iterations", 20)
        self._max_duration = self._config.get("max_duration_seconds", 600)
        self._check_task_status = self._config.get("check_task_status", True)
        self._start_time = time.monotonic()

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "stop_check"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 1)

    @property
    def route_signals(self) -> list[str]:
        """本插件可能产出的路由信号类型。"""
        return ["end"]

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行停止检查。

        依次检查所有停止条件，任一条件满足即返回 end 路由信号。
        优先使用 Agent 配置通过 state 注入的参数覆盖构造时默认值。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含路由信号的输出结果（有停止条件时）
        """
        self._apply_runtime_config(ctx)
        result = await self._do_work(ctx)

        if result.get("__route_signal__"):
            signal = result.pop("__route_signal__")
            return OutputResult(state_updates=result, route_signal=signal)
        return OutputResult(state_updates=result)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:
        """执行停止检查逻辑。

        Args:
            ctx: 插件执行上下文

        Returns:
            停止检查结果字典
        """
        iteration = ctx.state.get(StateKeys.ITERATION, 0)
        pipeline_id = ctx.state.get("pipeline_id", "?")
        elapsed = time.monotonic() - self._start_time
        raw_tc_count = len(ctx.state.get(StateKeys.RAW_TOOL_CALLS, []))
        logger.debug(
            "[%s] pipeline=%s iter=%d max_iter=%d elapsed=%.1f/%d raw_tool_calls=%d start_time=%.2f",
            self.name,
            pipeline_id,
            iteration,
            self._max_iterations,
            elapsed,
            self._max_duration,
            raw_tc_count,
            self._start_time,
        )

        # 1. 用户请求停止
        if ctx.state.get(StateKeys.SHOULD_STOP, False):
            logger.info("[%s] Stop requested by user", self.name)
            return {
                "router.stop_reason": "user_requested",
                "__route_signal__": RouteSignal(
                    route_type="end",
                    reason="User requested stop",
                ),
            }

        # 2. 迭代上限检测（-1 表示无限制）
        if self._max_iterations != -1 and iteration > self._max_iterations:
            logger.warning(
                "[%s] Max iterations reached: %d > %d",
                self.name,
                iteration,
                self._max_iterations,
            )
            return {
                "router.stop_reason": "max_iterations",
                "__route_signal__": RouteSignal(
                    route_type="end",
                    reason=f"Max iterations reached: {iteration}",
                ),
            }

        # 4. 执行超时检测（-1 表示无限制）
        if self._max_duration != -1 and elapsed > self._max_duration:
            logger.warning(
                "[%s] Execution timeout: %.1f > %d seconds",
                self.name,
                elapsed,
                self._max_duration,
            )
            return {
                "router.stop_reason": "timeout",
                "__route_signal__": RouteSignal(
                    route_type="end",
                    reason=f"Execution timeout: {elapsed:.1f}s",
                ),
            }

        # 5. task_evaluate 工具结果检测
        eval_stop = self._check_task_evaluate_result(ctx)
        if eval_stop:
            return eval_stop

        # 6. 任务状态检测（state 缓存 + TaskService 实际查询）
        if self._check_task_status:
            task_status = self._check_task_terminal_status(ctx)
            if task_status:
                logger.info("[%s] Task terminal status detected: %s", self.name, task_status)
                return {
                    "router.stop_reason": f"task_{task_status}",
                    "__route_signal__": RouteSignal(
                        route_type="end",
                        reason=f"Task {task_status}",
                    ),
                }

        return {"router.stop_reason": ""}

    def _check_task_evaluate_result(self, ctx: PluginContext) -> dict[str, Any] | None:
        """检查 task_evaluate 工具执行结果是否表明任务已完成或失败。

        当 task_evaluate 返回 metadata.result 为 completed 或 failed 时，
        任务状态已被 TaskEvaluateTool 变更为终态，管道应立即停止。

        Args:
            ctx: 插件执行上下文

        Returns:
            停止结果字典，无匹配返回 None
        """
        tool_results = ctx.state.get(StateKeys.TOOL_RESULTS, [])
        if not tool_results:
            return None

        for tr in tool_results:
            if not isinstance(tr, dict):
                continue
            tool_name = tr.get("tool_name", "")
            if tool_name != "task_evaluate":
                continue
            data = tr.get("data")
            if not isinstance(data, dict):
                continue
            metadata = data.get("metadata", {})
            if not isinstance(metadata, dict):
                continue
            result = metadata.get("result", "")
            if result in ("completed", "failed"):
                message = metadata.get("message", f"task_evaluate: {result}")
                logger.info(
                    "[%s] task_evaluate result: %s",
                    self.name,
                    result,
                )
                return {
                    "router.stop_reason": f"task_evaluate_{result}",
                    "__route_signal__": RouteSignal(
                        route_type="end",
                        reason=message,
                    ),
                }
        return None

    # TaskStatus 枚举仅有 stopped/completed/failed（无 cancelled/canceled/deleted）。
    # pause_task/cancel_task 都 emit "stopped"，必须纳入终态，否则暂停后引擎仍空转。
    # 与 task_notifier._TERMINAL_STATES、engine._check_children_terminal 保持一致。
    _TERMINAL_STATUSES = frozenset({"stopped", "completed", "failed"})

    def _check_task_terminal_status(self, ctx: PluginContext) -> str:
        """检查任务是否已到达终态（停止/完成/失败）。

        两个检测路径：
        1. 从 state["task_status"] 读取（由外部插件注入的缓存值）
        2. 从 TaskService 查询任务的实际状态（兜底，防止 state 未被更新）

        终态检测范围包含 stopped/completed/failed，并从 TaskService
        查询任务实际状态兜底，确保无论 state 是否被更新都能检测到终态，避免管道在任务
        完成或暂停/取消后仍持续循环执行（state["task_status"] 可能从未被任何插件更新，
        task_event_receiver 只修改 user_input）。

        Args:
            ctx: 插件执行上下文

        Returns:
            任务终态字符串，空字符串表示正常运行
        """
        cached_status = ctx.state.get("task_status", "")
        if cached_status in self._TERMINAL_STATUSES:
            return cached_status

        actual_status = self._check_task_actual_status(ctx)
        if actual_status:
            return actual_status

        return ""

    def _check_task_actual_status(self, ctx: PluginContext) -> str:
        """从 TaskService 查询任务的实际状态。

        当 state["task_status"] 未被更新时，通过查询 task_service 获取
        任务的真实状态。为避免频繁数据库访问，每 3 次迭代查询一次。

        Args:
            ctx: 插件执行上下文

        Returns:
            任务终态字符串，空字符串表示正常运行或查询失败
        """
        iteration = ctx.state.get(StateKeys.ITERATION, 0)
        if iteration % 3 != 0:
            return ""

        task_id = ctx.state.get("task_id", "")
        if not task_id:
            return ""

        try:
            task_service = ctx._services.get("task_service")
            if task_service is None:
                return ""

            task = task_service.get_task(task_id)
            if task is None:
                return ""

            status = task.status
            if hasattr(status, "value"):
                status = status.value
            status = str(status)

            if status in self._TERMINAL_STATUSES:
                logger.info(
                    "[%s] Task actual status is terminal: %s (task=%s, detected via task_service query, iter=%d)",
                    self.name,
                    status,
                    task_id,
                    iteration,
                )
                return status
        except Exception as exc:
            logger.debug(
                "[%s] Failed to query task actual status: %s",
                self.name,
                exc,
            )

        return ""

    def _apply_runtime_config(self, ctx: PluginContext) -> None:
        """从 Agent 配置覆盖运行时参数。

        优先使用 Agent YAML 中配置的 max_iterations / timeout_seconds
        覆盖构造时的默认值。特殊值 -1 表示无限制。

        重置 _start_time，防止共享插件实例在子管道（如评估管道）中
        因 elapsed 时间已超过 timeout_seconds 而误触发超时终止。

        Args:
            ctx: 插件执行上下文
        """
        agent_max_iter = ctx.state.get("max_iterations")
        if agent_max_iter is not None:
            self._max_iterations = agent_max_iter

        agent_timeout = ctx.state.get("timeout_seconds")
        if agent_timeout is not None:
            self._max_duration = agent_timeout

        pipeline_id = ctx.state.get("pipeline_id", "")
        if pipeline_id and pipeline_id != getattr(self, "_last_pipeline_id", None):
            self._start_time = time.monotonic()
            self._last_pipeline_id = pipeline_id
