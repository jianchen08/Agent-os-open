"""停止检查 Output 插件 — 合并 stop_requested + stop_check + task_status。

负责在管道循环的输出阶段统一管理所有"停止判断"逻辑：
1. 用户请求停止（should_stop）
2. 迭代上限/超时检测（stop_check_strategy）
3. 任务被删除/取消（task_status）

三个关注点共享 should_stop / iteration_count 等状态字段，
统一在输出阶段一次判定并按优先级短路。

State 命名空间：
    - router.stop_reason : 本插件写入的停止原因
"""

from __future__ import annotations

import inspect
import logging
import time
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import RouteSignal, StateKeys

# state 聚合读取器（server.py on_load 经 pipeline-state capability 注入）：
# 任务实时状态的唯一可靠来源——外部终态写入（task_evaluate 经
# pipeline-state.update 写 task.status）对运行中循环的内存态不可见，
# 停止检测必须走聚合读面（用户裁定：任务终态当轮停止，不允许空转）。
_state_reader: Any = None


def set_state_reader(reader: Any) -> None:
    """注入 state 聚合读取器（约定 () -> list[dict] | None，sync/async 均可）。"""
    global _state_reader  # noqa: PLW0603
    _state_reader = reader


def _get_state_reader() -> Any:
    return _state_reader

logger = logging.getLogger(__name__)


class StopCheckPlugin(IOutputPlugin):
    """停止检查 Output 插件——用户停止 / 迭代上限 / 超时 / 评估终态 / 任务终态。

    检查维度（按优先级）：
    1. 用户请求停止 → should_stop == True
    2. 迭代上限检测 → iteration > max_iterations
    4. 执行超时检测 → elapsed > max_duration
    5. task_evaluate 工具结果检测 → completed/failed
    6. 任务状态检测 → task 被删除/取消/完成/失败

    优先级：1（系统级，最高优先级检查）
    停止判断异常必须终止管道。

    Attributes:
        _config: 插件配置字典
    """

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
            return self._task_failure_stop(
                ctx,
                stop_reason="max_iterations",
                reason=f"Max iterations reached: {iteration}",
            )

        # 4. 执行超时检测（-1 表示无限制）
        if self._max_duration != -1 and elapsed > self._max_duration:
            logger.warning(
                "[%s] Execution timeout: %.1f > %d seconds",
                self.name,
                elapsed,
                self._max_duration,
            )
            return self._task_failure_stop(
                ctx,
                stop_reason="timeout",
                reason=f"Execution timeout: {elapsed:.1f}s",
            )

        # 5. task_evaluate 工具结果检测
        eval_stop = self._check_task_evaluate_result(ctx)
        if eval_stop:
            return eval_stop

        # 6. 任务状态检测（state 缓存 + TaskService 实际查询）
        if self._check_task_status:
            task_status = await self._check_task_terminal_status(ctx)
            if task_status:
                logger.info("[%s] Task terminal status detected: %s", self.name, task_status)
                # 终态收束三键一次写全（state_updates 带内平铺键，SDK 契约）：
                # - ended=true：引擎循环在本轮末立即 break（任务终态当轮停止，
                #   不允许空转——用户裁定）
                # - task.status：任务域终态落循环态（与外部写入对账）
                # - router.stop_reason：收束原因（task_failed/task_completed）
                return {
                    "ended": True,
                    "task.status": task_status,
                    "router.stop_reason": f"task_{task_status}",
                    "__route_signal__": RouteSignal(
                        route_type="end",
                        reason=f"Task {task_status}",
                    ),
                }

        return {"router.stop_reason": ""}

    def _task_failure_stop(self, ctx: PluginContext, stop_reason: str, reason: str) -> dict[str, Any]:
        """超线（迭代上限/超时）终止：任务管道同时落 task.status=failed。

        任务完成唯一判据是 task_evaluate 评估通过；跑到检测线仍未通过评估
        = 失败（用户裁定）。仅任务管道（state 带 task.id）写任务状态——
        聊天主管道无 task 上下文，写 task.* 会制造幽灵任务标记。
        """
        updates: dict[str, Any] = {
            "router.stop_reason": stop_reason,
            "__route_signal__": RouteSignal(route_type="end", reason=reason),
        }
        if ctx.state.get("task.id"):
            updates["task.status"] = "failed"
        return updates

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

    # cancelled/canceled 双拼写并存：任务域写 cancelled，历史记录存在 canceled
    _TERMINAL_STATUSES = frozenset({"canceled", "cancelled", "deleted", "completed", "failed"})

    def set_state_reader(self, reader: Any) -> None:
        """注入 state 聚合读取器（server.py on_load 经单例调用）。"""
        set_state_reader(reader)

    async def _check_task_terminal_status(self, ctx: PluginContext) -> str:
        """检查任务是否已到达终态（取消/删除/完成/失败）。

        两个检测路径：
        1. ctx.state 缓存键 task_status / task.status（进程内可见的写入）
        2. state 聚合行 task.status 实时读（0.2 单一真值——外部终态写入
           （task_evaluate 经 pipeline-state.update）对运行中循环的内存态
           不可见，聚合是唯一实时来源；用户裁定：任务到达终态当轮停止，
           不允许空转）

        Args:
            ctx: 插件执行上下文

        Returns:
            任务终态字符串，空字符串表示正常运行
        """
        for key in ("task_status", "task.status"):
            cached_status = ctx.state.get(key, "")
            if cached_status in self._TERMINAL_STATUSES:
                return cached_status

        actual_status = await self._check_task_actual_status(ctx)
        if actual_status:
            return actual_status

        return ""

    async def _check_task_actual_status(self, ctx: PluginContext) -> str:
        """从 state 聚合读任务实时状态（0.2 单一真值；每轮查，无节流）。

        外部终态写入（task_evaluate 经 pipeline-state.update 写 task.status）
        对运行中管道的循环内存态不可见，聚合读面是唯一可靠的实时来源——
        任务到达终态必须当轮停止（用户裁定：成功/失败都立即结束，不允许
        空转）。桥未就绪/读取失败按未终态继续（与既有降级语义一致）。

        Args:
            ctx: 插件执行上下文

        Returns:
            任务终态字符串，空字符串表示正常运行或查询失败
        """
        task_id = ctx.state.get(StateKeys.TASK_ID, "")
        if not task_id:
            return ""

        reader = _get_state_reader()
        if reader is None:
            return ""

        try:
            rows = reader()
            if inspect.isawaitable(rows):
                rows = await rows
        except Exception as exc:
            logger.warning(
                "[%s] state 聚合读取失败，本轮按未终态继续 | task=%s | %s: %s",
                self.name,
                task_id,
                type(exc).__name__,
                exc,
            )
            return ""

        if not isinstance(rows, list):
            return ""

        row = next(
            (
                r
                for r in rows
                if isinstance(r, dict)
                and (
                    str(r.get("task.id") or "") == task_id
                    or str(r.get("pipeline_id") or "") == task_id
                )
            ),
            None,
        )
        if row is None:
            return ""

        status = str(row.get("task.status") or "")

        if status in self._TERMINAL_STATUSES:
            logger.info(
                "[%s] Task actual status is terminal: %s (task=%s, detected via state aggregation)",
                self.name,
                status,
                task_id,
            )
            return status

        return ""

    def _apply_runtime_config(self, ctx: PluginContext) -> None:
        """从 Agent 配置覆盖运行时参数（每次 execute 复位后应用）。

        同 sidecar 实例被多个 agent 管道连续复用：先回构造默认值再应用本管道
        state 注入的 max_iterations / timeout_seconds 覆盖——前一 agent 注入的
        阈值不得残留到下一 agent。特殊值 -1 表示无限制。

        重置 _start_time（新 pipeline_id 首次出现时），防止共享实例在子管道
        （如评估管道）中因 elapsed 时间已超过 timeout_seconds 而误触发超时终止。

        Args:
            ctx: 插件执行上下文
        """
        self._max_iterations = self._config.get("max_iterations", 20)
        self._max_duration = self._config.get("max_duration_seconds", 600)

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
