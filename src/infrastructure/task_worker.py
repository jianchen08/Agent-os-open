"""后台任务执行器。

负责事件驱动的后台任务处理（如 task_submit 提交的子任务），
不再是管道执行的中间层。管道执行由 PipelineEngine 直接承担。

与已删除的 Worker 的区别：
- Worker 是管道执行的中间层，CLI → Worker → Engine
- TaskWorker 是后台任务处理器，CLI → Engine（直接），TaskWorker（后台）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class TaskWorker:
    """后台任务执行器。

    监听 EventBus 上的任务事件，当收到新任务时
    创建 PipelineEngine 实例执行子任务。

    Attributes:
        _task_service: 任务服务实例
        _plugin_registry: 插件注册表
        _input_route_table: 输入路由表
        _output_route_table: 输出路由表
        _services: 共享服务字典
        _event_bus: 事件总线
        _running: 是否正在运行
        _task: 后台监听协程
    """

    def __init__(
        self,
        task_service: Any,
        plugin_registry: Any,
        input_route_table: Any,
        output_route_table: Any,
        services: dict[str, Any] | None = None,
        event_bus: Any | None = None,
    ) -> None:
        self._task_service = task_service
        self._plugin_registry = plugin_registry
        self._input_route_table = input_route_table
        self._output_route_table = output_route_table
        self._services = services or {}
        self._event_bus = event_bus
        self._running: bool = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """启动后台任务监听。"""
        if self._running:
            return
        self._running = True
        if self._event_bus:
            self._event_bus.subscribe("task.submitted", self._on_task_submitted)
        logger.info("TaskWorker started (event-driven background task processor)")

    async def stop(self) -> None:
        """停止后台任务监听。"""
        self._running = False
        if self._event_bus:
            self._event_bus.unsubscribe("task.submitted", self._on_task_submitted)
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
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
        self._task = asyncio.create_task(self._execute_background_task(task_data))

    async def _execute_background_task(self, task_data: dict[str, Any]) -> None:
        """执行后台任务的完整生命周期。

        流程：start → run pipeline → evaluate → complete/fail → notify

        Args:
            task_data: 任务提交事件中的数据字典
        """
        import json

        from pipeline.engine import PipelineEngine

        task_id = task_data.get("task_id", "unknown")
        target_id = task_data.get("target_id", "")
        task_service = self._services.get("task_service")
        event_bus = self._services.get("event_bus")

        # ── 1. 加载真正的 AgentConfig ──
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

        # ── 3. 构建完整的 user_input（包含目标和验收标准）──
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

        # ── 4. 创建子 PipelineEngine 并执行 ──
        try:
            engine = PipelineEngine(
                input_route_table=self._input_route_table,
                output_route_table=self._output_route_table,
                plugin_registry=self._plugin_registry,
                services=self._services,
            )

            result = await engine.run(
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
            self._emit_task_done(event_bus, task_id, "failed", str(exc))
            return

        # ── 5. 评估 (running → evaluating → completed/failed) ──
        if task_service:
            try:
                task_service.move_to_evaluating(task_id)
                logger.info("TaskWorker: task %s moved to evaluating", task_id)

                from evaluation.executor import EvaluationExecutor
                evaluator = EvaluationExecutor(task_service=task_service)

                task = task_service.get_task(task_id)
                criteria = {}
                if task and task.metadata:
                    criteria = task.metadata.get("acceptance_criteria", {})
                if not criteria:
                    criteria = acceptance_criteria

                if criteria:
                    metric_ids = list(criteria.keys())
                    input_params = {}
                    for k, v in criteria.items():
                        if isinstance(v, dict):
                            input_params[k] = v.get("input_params", {})
                        else:
                            input_params[k] = {}

                    eval_result = evaluator.run_evaluation(
                        task_id=task_id,
                        metric_ids=metric_ids,
                        input_params=input_params,
                    )
                    overall_passed = getattr(eval_result, "overall_passed", True)
                else:
                    overall_passed = True

                final_status = "completed" if overall_passed else "failed"
                logger.info(
                    "TaskWorker: task %s evaluation %s (passed=%s)",
                    task_id, final_status, overall_passed,
                )

            except Exception as e:
                logger.error("TaskWorker: evaluation failed for task %s: %s", task_id, e)
                try:
                    task_service.fail_task(task_id, f"评估失败: {e}")
                except Exception:
                    pass
                final_status = "failed"

            # ── 6. 通知提交者 ──
            self._emit_task_done(event_bus, task_id, final_status, result)

    def _emit_task_done(
        self, event_bus: Any, task_id: str, status: str, result: Any = None
    ) -> None:
        """发射任务完成事件通知提交者。

        Args:
            event_bus: 事件总线实例
            task_id: 任务 ID
            status: 最终状态 (completed/failed)
            result: 管道执行结果
        """
        if event_bus is None:
            return
        try:
            import asyncio
            asyncio.create_task(event_bus.emit("task_state_changed", {
                "task_id": task_id,
                "new_status": status,
                "source": "task_worker",
                "result": result,
            }))
        except Exception as e:
            logger.warning("TaskWorker: failed to emit task_state_changed: %s", e)
