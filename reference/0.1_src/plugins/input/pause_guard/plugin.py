"""暂停守卫 Input 插件 — 在迭代间隙检测任务暂停状态。

当任务被外部（如 task_manage(pause)）设为 paused 时，
管道应在迭代间隙停下来，等待恢复。

本插件在每轮迭代开始时检查关联任务的状态：
- 如果任务状态为 paused → 产出 wait 路由信号，管道挂起
- 如果任务状态不是 paused → 正常继续

通过 ctx.get_service("task_service") 获取 TaskService 来查询任务状态。
如果服务不可用，则跳过检查（不影响管道正常运行）。

State 命名空间：
    - pause_guard.checked : 本插件写入的检查结果
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


class PauseGuardPlugin(IInputPlugin):
    """暂停守卫 Input 插件。

    在每轮迭代开始时检查关联任务是否被暂停，
    如果暂停则产出 wait 路由信号使管道挂起。

    优先级：5（最先执行，在 context_build 之前）
    错误策略：SKIP（检查失败不影响管道运行）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化暂停守卫插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用暂停守卫（默认 True）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "pause_guard"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 5)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行暂停检查。

        检查关联任务是否被暂停，如果是则设置管道挂起信号。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含检查结果的插件执行结果
        """
        result = await self._do_work(ctx)
        updates = result.copy()
        # 提取路由信号
        route_signal = updates.pop("__route_signal__", None)
        return PluginResult(state_updates=updates, route_signal=route_signal)

    async def _do_work(self, ctx: PluginContext) -> dict[str, Any]:  # noqa: PLR0911
        """执行暂停检查逻辑。

        Args:
            ctx: 插件执行上下文

        Returns:
            检查结果字典
        """
        if not self._enabled:
            return {"pause_guard.checked": {"paused": False, "reason": "disabled"}}

        # 获取关联任务 ID
        task_id = ctx.state.get(StateKeys.TASK_ID, "")
        if not task_id:
            return {"pause_guard.checked": {"paused": False, "reason": "no task_id"}}

        # 获取 TaskService
        try:
            task_service = ctx.get_service("task_service")
        except KeyError:
            return {"pause_guard.checked": {"paused": False, "reason": "task_service unavailable"}}

        # 查询任务状态
        try:
            task = task_service.get_task(task_id)
        except Exception as exc:
            logger.warning("[%s] Failed to get task %s: %s", self.name, task_id, exc)
            return {"pause_guard.checked": {"paused": False, "reason": f"query error: {exc}"}}

        if task is None:
            return {"pause_guard.checked": {"paused": False, "reason": "task not found"}}

        # 检查任务是否暂停
        from pipeline.types import RouteSignal  # noqa: PLC0415
        from tasks.types import TaskStatus  # noqa: PLC0415

        if task.status == TaskStatus.STOPPED:
            logger.info("[%s] Task %s is paused, suspending pipeline", self.name, task_id)
            return {
                "pause_guard.checked": {"paused": True, "reason": "task paused", "task_id": task_id},
                "__route_signal__": RouteSignal(
                    route_type="wait",
                    reason=f"Task {task_id} is paused",
                ),
            }

        return {"pause_guard.checked": {"paused": False, "reason": f"task status: {task.status.value}"}}
