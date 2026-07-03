"""子任务守护 Output 插件。

当 LLM 只输出纯文本（没有工具调用）且当前任务有 pending/running 子任务时，
挂起管道（route_signal=wait），避免无意义地调用 LLM 浪费 token。

管道挂起后由 TaskWorker 在子任务终态或 idle 超时时调 engine.resume() 唤醒。

State 命名空间：
    - child_task_guard_remind_count : idle 超时提醒次数计数器
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ErrorPolicy, RouteSignal
from utils.enum_utils import safe_enum_value

logger = logging.getLogger(__name__)


class ChildTaskGuard(IOutputPlugin):
    """子任务守护插件。

    在 LLM 输出纯文本且存在未完成子任务时：
    1. 返回 route_signal=wait 挂起管道（零 token 消耗）

    idle 计时器由 TaskWorker 在任务开始时启动一次，本插件不负责重置。
    优先级应高于 TaskReminder（30 < 35），确保有子任务时先被拦截。

    检测机制：
    - 通过当前管道的 pipeline_id 查询 task_service 中
      parent_pipeline_id 匹配且状态为 active 的子任务。
    - 统一了有 task_id（子任务管道）和无 task_id（CLI 主管道）两种场景。

    Attributes:
        _idle_remind_limit: idle 超时后最多提醒次数（默认 3）
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._idle_remind_limit: int = self._config.get("idle_remind_limit", 3)

    @property
    def name(self) -> str:
        return "child_task_guard"

    @property
    def priority(self) -> int:
        return self._config.get("priority", 28)

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """检测子任务状态，决定是否挂起管道。

        触发条件：
        1. core_type 为 llm_call
        2. LLM 只输出了纯文本（raw_tool_calls 为空）
        3. 当前管道有 pending/running 子任务

        满足条件时返回 wait 信号挂起管道。
        """
        state = ctx.state
        iteration = state.get("iteration", -1)

        core_type = state.get("core_type", "")

        if state.get("task_evaluation_completed"):
            logger.debug(
                "ChildTaskGuard[iter=%s]: task evaluation passed, emitting end signal to terminate pipeline",
                iteration,
            )
            return OutputResult(
                state_updates={},
                route_signal=RouteSignal(
                    route_type="end",
                    reason="child_task_guard: task_evaluate passed, pipeline completed",
                ),
                skip_remaining=True,
            )

        task_id = state.get("task_id")
        pipeline_id = state.get("pipeline_id", "")
        has_active, active_ids = self._get_active_children(pipeline_id, task_id, ctx)

        if not has_active:
            logger.debug(
                "ChildTaskGuard[iter=%s][pipeline=%s]: no active children (%s)",
                iteration,
                pipeline_id[:8] if pipeline_id else "none",
                core_type,
            )
            return OutputResult()

        if core_type != "llm_call":
            logger.debug(
                "ChildTaskGuard[iter=%s][pipeline=%s]: active children found but "
                "core_type=%s, deferring suspension to next LLM call",
                iteration,
                pipeline_id[:8] if pipeline_id else "none",
                core_type,
            )
            return OutputResult()

        if state.get("raw_tool_calls"):
            logger.debug(
                "ChildTaskGuard[iter=%s][pipeline=%s]: active children found but "
                "LLM has pending tool calls, continuing",
                iteration,
                pipeline_id[:8] if pipeline_id else "none",
            )
            return OutputResult()

        logger.debug(
            "ChildTaskGuard[iter=%s][pipeline=%s]: ACTIVE children found (%s), "
            "suspending pipeline (wait signal), child_ids=%s",
            iteration,
            pipeline_id[:8] if pipeline_id else "none",
            core_type,
            active_ids,
        )
        return OutputResult(
            state_updates={"submitted_task_ids": active_ids},
            route_signal=RouteSignal(
                route_type="wait",
                reason=f"child_task_guard: active children during {core_type}",
            ),
            skip_remaining=True,
        )

    def _get_active_children(
        self,
        pipeline_id: str,
        task_id: str | None,
        ctx: PluginContext,
    ) -> tuple[bool, list[str]]:
        """通过 parent_pipeline_id 或 parent_task_id 检查是否有活跃子任务。

        主路径：用当前 pipeline_id 查找 parent_pipeline_id 匹配的活跃子任务，
        统一 CLI 主管道和子任务管道两种场景。
        回退：用 task_id 查找子任务（兼容旧数据）。

        Returns:
            (has_active, active_child_ids) 元组
        """
        task_service = self._get_task_service(ctx)
        if task_service is None:
            return False, []

        active_statuses = {"pending", "running", "evaluating", "scheduled"}
        seen_ids: set[str] = set()

        if pipeline_id:
            try:
                from tasks.types import TaskStatus as TS  # noqa: N817,PLC0415

                for status_val in (TS.RUNNING, TS.PENDING, TS.EVALUATING):
                    for t in task_service.list_by_status(status_val):
                        if getattr(t, "parent_pipeline_id", None) == pipeline_id:
                            seen_ids.add(t.id)
            except Exception as exc:
                logger.warning("ChildTaskGuard: list_by_status query failed: %s", exc)

        if task_id:
            try:
                subtasks = task_service.list_subtasks(task_id)
                for st in subtasks:
                    status = safe_enum_value(st.status)
                    if status in active_statuses:
                        seen_ids.add(st.id)
            except Exception as exc:
                logger.warning("ChildTaskGuard: list_subtasks failed: %s", exc)

        if seen_ids:
            return True, list(seen_ids)
        return False, []

    def _get_task_service(self, ctx: PluginContext) -> Any:
        """获取 TaskService 实例。

        优先从插件上下文获取，fallback 到公共 service_access 接口。

        Args:
            ctx: 插件执行上下文

        Returns:
            TaskService 实例，不可用时返回 None
        """
        try:
            return ctx.get_service("task_service")
        except KeyError:
            pass

        from tasks.service_access import get_task_service  # noqa: PLC0415

        return get_task_service()
