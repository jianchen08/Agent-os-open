"""后台任务执行器。

负责事件驱动的后台任务处理（如 task_submit 提交的子任务）。
TaskWorker 只负责启动子管道，子管道中的 Agent 通过 task_evaluate
工具自行评估并更新任务状态。TaskService 的 on_state_change 回调
负责终态事件通知，无需轮询。

与已删除的 Worker 的区别：
- Worker 是管道执行的中间层，CLI → Worker → Engine
- TaskWorker 是后台任务处理器，CLI → Engine（直接），TaskWorker（后台）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_TERMINAL_STATES = frozenset({"completed", "failed"})


class TaskWorker:
    """后台任务执行器。

    监听 EventBus 上的 task.submitted 事件，当收到新任务时
    创建 PipelineEngine 实例执行子任务。

    终态通知由 TaskService.on_state_change 回调自动触发，
    TaskWorker 通过 asyncio.Event 等待终态，无需轮询。

    Attributes:
        _task_service: 任务服务实例
        _plugin_registry: 插件注册表
        _input_route_table: 输入路由表
        _output_route_table: 输出路由表
        _services: 共享服务字典
        _event_bus: 事件总线
        _running: 是否正在运行
        _task: 后台监听协程
        _terminal_events: task_id → asyncio.Event 的映射
    """

    def __init__(
        self,
        task_service: Any,
        plugin_registry: Any,
        input_route_table: Any,
        output_route_table: Any,
        services: dict[str, Any] | None = None,
        event_bus: Any | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._task_service = task_service
        self._plugin_registry = plugin_registry
        self._input_route_table = input_route_table
        self._output_route_table = output_route_table
        self._services = services or {}
        self._event_bus = event_bus
        self._config = config or {}
        self._running: bool = False
        self._tasks: set[asyncio.Task] = set()
        self._terminal_events: dict[str, asyncio.Event] = {}

    async def start(self) -> None:
        """启动后台任务监听，并恢复残留的 running 任务。"""
        if self._running:
            return
        self._running = True
        if self._event_bus:
            self._event_bus.subscribe("task.submitted", self._on_task_submitted)
            self._event_bus.subscribe("task_state_changed", self._on_task_state_changed)

        await self._recover_running_tasks()

        logger.info("TaskWorker started (event-driven background task processor)")

    async def _recover_running_tasks(self) -> None:
        """恢复残留的 running 状态任务。

        Worker 启动时扫描所有 status=running 的任务，
        将其重置为 pending 以便重新拾取执行。
        跳过长期任务（task_scope=long_term）。
        """
        if not self._task_service:
            return

        from tasks.types import TaskStatus

        running_tasks = self._task_service.list_by_status(TaskStatus.RUNNING)
        if not running_tasks:
            return

        recovered = 0
        for task in running_tasks:
            task_scope = task.metadata.get("task_scope", "short_term")
            if task_scope == "long_term":
                logger.debug(
                    "TaskWorker: 跳过长期任务恢复: task_id=%s", task.id,
                )
                continue
            try:
                self._task_service.reset_to_pending(task.id)
                recovered += 1
                logger.info(
                    "TaskWorker: 恢复 running → pending: task_id=%s", task.id,
                )
            except Exception as e:
                logger.warning(
                    "TaskWorker: 恢复任务失败: task_id=%s, error=%s", task.id, e,
                )

        if recovered:
            logger.info("TaskWorker: 恢复了 %d 个 running 任务", recovered)

    async def stop(self) -> None:
        """停止后台任务监听，等待所有 pending 任务完成。"""
        self._running = False
        if self._event_bus:
            self._event_bus.unsubscribe("task.submitted", self._on_task_submitted)
            self._event_bus.unsubscribe("task_state_changed", self._on_task_state_changed)

        if self._tasks:
            await asyncio.sleep(0.1)

        pending = list(self._terminal_events.values())
        if pending:
            logger.info("TaskWorker: waiting for %d pending task(s) to finish...", len(pending))
            try:
                await asyncio.wait_for(
                    asyncio.gather(*[evt.wait() for evt in pending], return_exceptions=True),
                    timeout=600,
                )
            except asyncio.TimeoutError:
                logger.warning("TaskWorker: timed out waiting for pending tasks")

        for bg_task in list(self._tasks):
            if not bg_task.done():
                bg_task.cancel()
                try:
                    await bg_task
                except asyncio.CancelledError:
                    pass
        self._tasks.clear()
        logger.info("TaskWorker stopped")

    async def _on_task_submitted(self, event: Any) -> None:
        """处理任务提交事件。

        Args:
            event: 任务提交事件
        """
        if not self._running:
            return
        task_data = event.data if hasattr(event, "data") else event
        task_id = task_data.get("task_id", "unknown") if isinstance(task_data, dict) else "unknown"
        logger.info("TaskWorker received task: %s", task_id)
        bg_task = asyncio.create_task(self._execute_background_task(task_data))
        self._tasks.add(bg_task)
        bg_task.add_done_callback(self._tasks.discard)

    async def _on_task_state_changed(self, event: Any) -> None:
        """处理任务状态变更事件，触发对应的 asyncio.Event。

        当 TaskService 的 on_state_change 回调 emit 了
        task_state_changed 事件时，检查是否为终态，
        如果是则 set 对应的 asyncio.Event。

        Args:
            event: 状态变更事件
        """
        data = event.data if hasattr(event, "data") else event
        if not isinstance(data, dict):
            return

        task_id = data.get("task_id", "")
        new_status = data.get("new_status", "")

        if new_status in _TERMINAL_STATES:
            evt = self._terminal_events.get(task_id)
            if evt is not None:
                evt.set()
                logger.debug(
                    "TaskWorker: terminal event set for task %s (%s)",
                    task_id, new_status,
                )

            # 终态事件触发容器超时安全网检查
            await self._check_stale_containers()

    async def _execute_background_task(self, task_data: dict[str, Any]) -> None:
        """执行后台任务的完整生命周期。

        流程：start → run pipeline → wait terminal

        Args:
            task_data: 任务提交事件中的数据字典
        """
        import json

        from pipeline.engine import PipelineEngine

        task_id = task_data.get("task_id", "unknown")
        target_id = task_data.get("target_id", "")
        task_service = self._services.get("task_service")

        # ── 0. 跳过长期任务 ──
        if task_service:
            task = task_service.get_task(task_id)
            if task is not None:
                task_scope = task.metadata.get("task_scope", "short_term")
                if task_scope == "long_term":
                    logger.info(
                        "TaskWorker: 跳过长期任务 %s (task_scope=long_term)", task_id,
                    )
                    return

        # ── 1. 加载 AgentConfig ──
        agent_config = None
        agent_registry = self._services.get("agent_registry")
        if agent_registry and target_id:
            agent_config = agent_registry.get(target_id)
            if agent_config is None:
                logger.warning("TaskWorker: agent '%s' not found in registry", target_id)

        # ── 2. 启动任务 (pending → running) ──
        if task_service:
            try:
                task_service.start_task(task_id)
                logger.info("TaskWorker: task %s started", task_id)
            except Exception as e:
                logger.error("TaskWorker: failed to start task %s: %s", task_id, e)
                if task_service:
                    task_service.fail_task(task_id, f"启动失败: {e}")
                return

        # ── 2.5 注册 idle 计时器（心跳检测） ──
        timer_manager = self._services.get("timer_manager")
        idle_timer_registered = False
        if timer_manager:
            try:
                await timer_manager.create_timer(
                    task_id=task_id,
                    timeout=float(timer_manager.idle_threshold),
                    callback=lambda tid=task_id: self._on_idle_timeout(tid),
                )
                idle_timer_registered = True
                logger.info(
                    "TaskWorker: idle 计时器已注册: task_id=%s, timeout=%ds",
                    task_id, timer_manager.idle_threshold,
                )
            except Exception as e:
                logger.warning(
                    "TaskWorker: 注册 idle 计时器失败: task_id=%s, error=%s",
                    task_id, e,
                )

        # ── 3. 构建完整的 user_input ──
        user_input = task_data.get("user_input", "")
        description = task_data.get("description", "")
        acceptance_criteria = task_data.get("acceptance_criteria", {})
        workspace = task_data.get("workspace", "")

        full_input = user_input
        if description:
            full_input += f"\n\n详细描述：{description}"
        if acceptance_criteria:
            full_input += f"\n\n验收标准（必须满足）：{json.dumps(acceptance_criteria, ensure_ascii=False, indent=2)}"
        if workspace:
            full_input += f"\n\n工作目录：{workspace}"

        # ── 3.5 追加 Agent 特定的执行提示 ──
        if agent_config and hasattr(agent_config, "system_prompt"):
            execution_hint = self._build_execution_hint(agent_config)
            if execution_hint:
                full_input += f"\n\n{execution_hint}"

        # ── 4. 注册终态 Event ──
        terminal_evt = asyncio.Event()
        self._terminal_events[task_id] = terminal_evt

        # ── 5. 创建子 PipelineEngine 并执行 ──
        try:
            engine = PipelineEngine(
                input_route_table=self._input_route_table,
                output_route_table=self._output_route_table,
                plugin_registry=self._plugin_registry,
                services=self._services,
            )

            await engine.run(
                user_input=full_input,
                agent_config=agent_config,
                task_id=task_id,
                acceptance_criteria=acceptance_criteria,
                workspace=workspace,
            )

            logger.info("TaskWorker: pipeline completed for task %s", task_id)

        except Exception as exc:
            logger.error("TaskWorker: pipeline failed for task %s: %s", task_id, exc)
            if task_service:
                task_service.fail_task(task_id, str(exc))
            evt = self._terminal_events.pop(task_id, None)
            if evt is not None:
                evt.set()
            return

        # ── 5.5 检查任务是否已到达终态（BUG-FIX-P2） ──
        # 管道退出后若任务仍为 RUNNING：
        # - 有 raw_result 输出 → 标记 evaluating（交由评估流程处理）
        # - 无输出或有 raw_error → 标记 failed
        if task_service:
            task = task_service.get_task(task_id)
            if task is not None:
                status = task.status
                status_str = status if isinstance(status, str) else status.value
                if status_str == "running":
                    # 从 engine state 获取输出（engine.run 返回 state，此处无法直接拿到，
                    # 只能依赖 task.result 是否已被 Agent 写入）
                    task_result = getattr(task, "result", None)
                    if task_result:
                        logger.info(
                            "TaskWorker: task %s still RUNNING after pipeline exit, "
                            "has result output -> moving to evaluating",
                            task_id,
                        )
                        evt = self._terminal_events.pop(task_id, None)
                        try:
                            task_service.move_to_evaluating(task_id)
                        except Exception as e:
                            logger.warning(
                                "TaskWorker: move_to_evaluating failed for %s: %s, falling back to fail",
                                task_id, e,
                            )
                            task_service.fail_task(task_id, f"管道退出后状态转移失败: {e}")
                        if evt is not None:
                            evt.set()
                        return
                    else:
                        logger.warning(
                            "TaskWorker: task %s still RUNNING after pipeline exit "
                            "(iterations exhausted?), marking as failed",
                            task_id,
                        )
                        evt = self._terminal_events.pop(task_id, None)
                        task_service.fail_task(task_id, "管道迭代耗尽，Agent 未完成评估")
                        if evt is not None:
                            evt.set()
                        return

        # ── 6. 等待终态 Event ──
        try:
            await asyncio.wait_for(terminal_evt.wait(), timeout=600)
            logger.info("TaskWorker: task %s reached terminal state", task_id)
        except asyncio.TimeoutError:
            logger.warning("TaskWorker: task %s timed out waiting for terminal state", task_id)
            if task_service:
                try:
                    task_service.fail_task(task_id, "TaskWorker 等待终态超时(600s)")
                except Exception:
                    pass
        finally:
            self._terminal_events.pop(task_id, None)
            if idle_timer_registered and timer_manager:
                try:
                    await timer_manager.cancel_timer(task_id)
                    logger.debug("TaskWorker: idle 计时器已取消: task_id=%s", task_id)
                except Exception as e:
                    logger.warning(
                        "TaskWorker: 取消 idle 计时器失败: task_id=%s, error=%s",
                        task_id, e,
                    )

    def _on_idle_timeout(self, task_id: str) -> None:
        """idle 计时器超时回调。

        当任务长时间无活动时，TimerManager 触发此回调，
        将任务标记为 failed。

        Args:
            task_id: 超时的任务ID
        """
        task_service = self._services.get("task_service")
        if not task_service:
            logger.warning(
                "TaskWorker: idle 超时但无 task_service，无法处理: task_id=%s",
                task_id,
            )
            return

        task = task_service.get_task(task_id)
        if task is None:
            return

        status_str = task.status if isinstance(task.status, str) else task.status.value
        if status_str != "running":
            logger.debug(
                "TaskWorker: idle 超时但任务已不在 running 状态: task_id=%s, status=%s",
                task_id, status_str,
            )
            return

        try:
            timer_mgr = self._services.get("timer_manager")
            threshold = getattr(timer_mgr, "idle_threshold", "?") if timer_mgr else "?"
            task_service.fail_task(
                task_id,
                f"idle 超时({threshold}s无活动)",
            )
            logger.warning(
                "TaskWorker: 任务 idle 超时，已标记 failed: task_id=%s", task_id,
            )
            evt = self._terminal_events.pop(task_id, None)
            if evt is not None:
                evt.set()
        except Exception as e:
            logger.error(
                "TaskWorker: idle 超时处理失败: task_id=%s, error=%s", task_id, e,
            )

    def _build_execution_hint(self, agent_config: Any) -> str:
        """根据 Agent 配置构建执行提示。

        Agent 的工作流已在 system_prompt 中定义，此处不再硬编码覆盖。

        Args:
            agent_config: 目标 Agent 的配置对象

        Returns:
            执行提示文本（当前始终返回空字符串）
        """
        return ""

    async def _check_stale_containers(self) -> None:
        """检查并处理长时间无活动的容器任务。

        当容器任务（PENDING 状态且有子任务）超过指定时间无子任务状态变化时，
        自动将其标记为 failed，防止容器因主 Agent 异常而永远挂起。

        超时条件（同时满足）：
        1. 容器状态为 PENDING
        2. 容器有子任务
        3. 所有子任务都已到达终态（completed/failed）
        4. 距离最后一个子任务状态变更超过 timeout 秒

        触发时机：每次收到子任务终态事件时调用（事件驱动，非轮询）。
        """
        if not self._task_service:
            return

        try:
            from datetime import datetime, timedelta

            from tasks.types import TaskStatus

            timeout_seconds = self._config.get("container_timeout_seconds", 1800)  # 默认30分钟

            all_pending = self._task_service.list_by_status(TaskStatus.PENDING)
            for container in all_pending:
                subtasks = self._task_service.list_subtasks(container.id)
                if not subtasks:
                    continue

                # 所有子任务都必须已到达终态
                all_terminal = all(
                    s.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
                    for s in subtasks
                )
                if not all_terminal:
                    continue

                # 计算最后活动时间：取所有子任务中最新的时间戳
                last_activity = self._latest_subtask_timestamp(subtasks)

                # 如果没有子任务时间戳，回退到容器的 created_at
                if last_activity is None:
                    last_activity = self._parse_timestamp(container.created_at)

                if last_activity is None:
                    continue

                if datetime.now() - last_activity < timedelta(seconds=timeout_seconds):
                    continue

                logger.warning(
                    "TaskWorker: 容器超时无活动 | container_id=%s | subtasks=%d | timeout=%ds",
                    container.id, len(subtasks), timeout_seconds,
                )
                try:
                    self._task_service.fail_task(
                        container.id,
                        f"容器超时（{timeout_seconds}秒无活动），"
                        f"所有子任务已到达终态但容器未被主Agent处理",
                    )
                except Exception as e:
                    logger.error(
                        "TaskWorker: 容器超时标记失败 | container_id=%s | error=%s",
                        container.id, e,
                    )
        except Exception as e:
            logger.error("TaskWorker: 容器超时检查失败 | error=%s", e)

    @staticmethod
    def _latest_subtask_timestamp(subtasks: list[Any]) -> Any | None:
        """从子任务列表中提取最新的时间戳。

        依次检查每个子任务的 completed_at、updated_at、created_at，
        返回其中最新的 datetime 对象。

        Args:
            subtasks: 子任务 TaskModel 列表

        Returns:
            最新的 datetime 对象，无有效时间戳时返回 None
        """
        from datetime import datetime

        latest: datetime | None = None
        for s in subtasks:
            for ts in (s.completed_at, s.updated_at, s.created_at):
                parsed = TaskWorker._parse_timestamp(ts)
                if parsed is not None and (latest is None or parsed > latest):
                    latest = parsed
        return latest

    @staticmethod
    def _parse_timestamp(value: str | None) -> Any | None:
        """将 ISO 格式时间字符串解析为 datetime 对象。

        Args:
            value: ISO 格式的时间字符串，None 时返回 None

        Returns:
            datetime 对象，解析失败时返回 None
        """
        from datetime import datetime

        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None
