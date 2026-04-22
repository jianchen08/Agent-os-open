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
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any

from isolation.workspace_lifecycle import WorkspaceLifecycleManager

logger = logging.getLogger(__name__)

_TERMINAL_STATES = frozenset({"completed", "failed"})
_CONTAINER_CHECK_MIN_INTERVAL = 30.0


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
        self._services = services or {}
        self._task_service = task_service or self._services.get("task_service")
        self._plugin_registry = plugin_registry
        self._input_route_table = input_route_table
        self._output_route_table = output_route_table
        self._event_bus = event_bus
        self._config = config or {}
        self._running: bool = False
        self._tasks: set[asyncio.Task] = set()
        self._terminal_events: dict[str, asyncio.Event] = {}
        self._suspended_engines: dict[str, Any] = {}
        self._idle_remind_counts: dict[str, int] = {}
        self._resume_requested: dict[str, bool] = {}
        self._last_container_check: float = 0.0

    async def start(self) -> None:
        """启动后台任务监听，并恢复残留的 running 任务。"""
        if self._running:
            return
        self._running = True

        self._init_lifecycle()

        if self._event_bus:
            self._event_bus.subscribe("task.submitted", self._on_task_submitted)
            self._event_bus.subscribe("task_state_changed", self._on_task_state_changed)

        await self._recover_running_tasks()

        logger.info("TaskWorker started (event-driven background task processor)")

    def _init_lifecycle(self) -> None:
        """初始化 WorkspaceLifecycleManager 实例并注册到 services

        BUG-FIX-fix_20260422_lifecycle_not_registered:
        问题根因: WorkspaceLifecycleManager 从未被实例化，task_worker 中
                  lifecycle 始终为 None，所有生命周期钩子（worktree 创建、
                  合并、清理）被静默跳过，Agent 在空目录中无法读取项目文件。
        修复方案: 在 TaskWorker.start() 中自行创建 lifecycle 实例，
                  不依赖外部 services 注入，lifecycle 是 TaskWorker 自身的职责。
        """
        try:
            from pathlib import Path as _Path
            from tools.builtin.resource_merge import ResourceMergeTool

            project_root = str(_Path.cwd())
            resource_merge = ResourceMergeTool(base_path=project_root)

            iso_config: dict[str, Any] = {}
            config_path = _Path("config/isolation/isolation_config.yaml")
            if config_path.exists():
                import yaml
                iso_config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

            ws_meta_store: dict[str, Any] = {}

            lifecycle = WorkspaceLifecycleManager(
                resource_merge=resource_merge,
                config=iso_config,
                task_tree=self._task_service,
                ws_meta_store=ws_meta_store,
                base_path=project_root,
            )
            self._services["workspace_lifecycle_manager"] = lifecycle
            logger.info(
                "TaskWorker: WorkspaceLifecycleManager initialized, base_path=%s",
                project_root,
            )
        except Exception as exc:
            logger.warning(
                "TaskWorker: WorkspaceLifecycleManager init failed, "
                "lifecycle hooks will be skipped: %s", exc,
            )

    async def _recover_running_tasks(self) -> None:
        """恢复残留的 running 和 pending 任务。

        Worker 启动时扫描所有 status=running 和 status=pending 的任务，
        running 任务先重置为 pending，然后统一通过 task.submitted 事件触发执行。
        跳过长期任务（task_scope=long_term）。
        """
        if not self._task_service:
            return

        # 局部导入：避免模块级循环依赖
        from tasks.types import TaskStatus

        # 1. running 任务重置为 pending
        running_tasks = self._task_service.list_by_status(TaskStatus.RUNNING)
        for task in running_tasks:
            task_scope = task.metadata.get("task_scope", "short_term")
            if task_scope == "long_term":
                logger.debug(
                    "TaskWorker: 跳过长期任务恢复: task_id=%s", task.id,
                )
                continue
            try:
                self._task_service.reset_to_pending(task.id)
                logger.info(
                    "TaskWorker: 恢复 running → pending: task_id=%s", task.id,
                )
            except Exception as e:
                logger.warning(
                    "TaskWorker: 恢复任务失败: task_id=%s, error=%s", task.id, e,
                )

        # 2. 所有 pending 任务统一通过 task.submitted 事件触发
        if not self._event_bus:
            return

        recovered = 0
        pending_tasks = self._task_service.list_by_status(TaskStatus.PENDING)
        for task in pending_tasks:
            task_scope = task.metadata.get("task_scope", "short_term")
            if task_scope == "long_term":
                continue
            if not task.metadata.get("target_id"):
                continue
            try:
                await self._event_bus.emit("task.submitted", {
                    "task_id": task.id,
                    "target_type": task.target_type or "agent",
                    "target_id": task.metadata.get("target_id", ""),
                    "user_input": task.title,
                    "description": task.description,
                    "acceptance_criteria": task.metadata.get("acceptance_criteria", {}),
                    "workspace": task.metadata.get("workspace", ""),
                })
                recovered += 1
                logger.info(
                    "TaskWorker: 恢复 pending 任务: task_id=%s", task.id,
                )
            except Exception as e:
                logger.warning(
                    "TaskWorker: 恢复 pending 任务失败: task_id=%s, error=%s", task.id, e,
                )

        if recovered:
            logger.info("TaskWorker: 恢复了 %d 个任务", recovered)

    async def stop(self) -> None:
        """停止后台任务监听，等待所有 pending 任务完成。"""
        self._running = False
        if self._event_bus:
            self._event_bus.unsubscribe("task.submitted", self._on_task_submitted)
            self._event_bus.unsubscribe("task_state_changed", self._on_task_state_changed)

        if self._tasks:
            # 等待已提交的 asyncio.Task 开始执行，确保 _terminal_events 已注册
            await asyncio.sleep(0.1)

        pending = list(self._terminal_events.values())
        if pending:
            logger.info("TaskWorker: waiting for %d pending task(s) to finish...", len(pending))
            stop_wait_timeout = self._config.get("stop_wait_timeout", 600)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*[evt.wait() for evt in pending], return_exceptions=True),
                    timeout=stop_wait_timeout,
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

        # 将仍在 running/pending 的任务标记为 failed
        if self._task_service:
            try:
                # 局部导入：避免模块级循环依赖
                from tasks.types import TaskStatus
                remaining_ids = list(self._terminal_events.keys())
                for tid in remaining_ids:
                    try:
                        task = self._task_service.get_task(tid)
                        if task and (task.status == TaskStatus.RUNNING or task.status == TaskStatus.PENDING):
                            self._task_service.fail_task(tid, "TaskWorker stopped, task forcibly terminated")
                            logger.info("TaskWorker.stop: task %s marked as failed", tid)
                    except Exception as e:
                        logger.warning("TaskWorker.stop: failed to update task %s: %s", tid, e)
            except Exception as e:
                logger.warning("TaskWorker.stop: failed to cleanup tasks: %s", e)
        self._terminal_events.clear()

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

            await self._check_stale_containers()

    async def _execute_background_task(self, task_data: dict[str, Any]) -> None:
        """执行后台任务的完整生命周期。

        流程：start → run pipeline → wait terminal

        Args:
            task_data: 任务提交事件中的数据字典
        """
        # 局部导入：避免启动时加载完整管道模块
        from pipeline.engine import PipelineEngine

        task_id = task_data.get("task_id", "unknown")
        target_id = task_data.get("target_id", "")
        task_service = self._task_service

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
                current_task = task_service.get_task(task_id)
                if current_task and current_task.status.value == "running":
                    logger.info("TaskWorker: task %s already running, skip start", task_id)
                else:
                    task_service.start_task(task_id)
                    logger.info("TaskWorker: task %s started", task_id)
            except Exception as e:
                logger.error("TaskWorker: failed to start task %s: %s", task_id, e)
                task_service.fail_task(task_id, f"启动失败: {e}")
                return

        # ── 2.5 注册 idle 计时器（心跳检测） ──
        timer_manager = self._services.get("timer_manager")
        idle_timer_registered = False
        if timer_manager:
            try:
                try:
                    await timer_manager.cancel_timer(task_id)
                except Exception:
                    pass
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
                # BUG-FIX: idle 计时器注册失败时直接拒绝任务（保守策略），
                # 避免任务在无超时保护下无限运行
                logger.error(
                    "TaskWorker: 注册 idle 计时器失败，任务拒绝执行: task_id=%s, error=%s",
                    task_id, e,
                )
                if task_service:
                    task_service.fail_task(task_id, f"idle计时器初始化失败，任务拒绝执行: {e}")
                return

        # ── 3. 构建完整的 user_input ──
        user_input = task_data.get("user_input", "")
        description = task_data.get("description", "")
        acceptance_criteria = task_data.get("acceptance_criteria", {})
        explicit_workspace = task_data.get("workspace") or None
        workspace = self._resolve_task_workspace(task_id, explicit_workspace)

        # ── 3.x 生命周期钩子：任务启动 + 工作空间状态注入 ──
        lifecycle: WorkspaceLifecycleManager | None = self._services.get("workspace_lifecycle_manager")
        ws_meta: dict[str, Any] = {}
        if lifecycle:
            try:
                ws_meta = lifecycle.on_task_start(task_id, workspace, task_data)
                workspace = ws_meta.get("path", workspace)
                logger.info(
                    "TaskWorker: lifecycle on_task_start, task_id=%s, mode=%s",
                    task_id, ws_meta.get("mode"),
                )
            except Exception as e:
                logger.warning(
                    "TaskWorker: lifecycle on_task_start failed: task_id=%s, error=%s",
                    task_id, e,
                )

        # BUG-FIX-fix_20260421_goal_context_injection:
        # 问题根因: task_submit 将 goal.context 存入 metadata["goal_context"]，
        #          但 TaskWorker 构建 full_input 时未提取该字段，导致包含原始用户需求的
        #          结构化上下文在传递中丢失，下游 Agent 只能看到简化的标题。
        # 修复方案: 从 task.metadata 中提取 goal_context 并拼入 full_input。
        # 影响范围: 所有通过 task_submit 提交且携带 goal.context 的任务
        goal_context = None
        if task_service:
            _task_obj = task_service.get_task(task_id)
            if _task_obj and _task_obj.metadata:
                goal_context = _task_obj.metadata.get("goal_context")

        is_default_workspace = not explicit_workspace

        full_input = user_input
        if description:
            full_input += f"\n\n详细描述：{description}"
        if goal_context:
            full_input += f"\n\n上下文信息：{json.dumps(goal_context, ensure_ascii=False, indent=2) if isinstance(goal_context, dict) else str(goal_context)}"
        if acceptance_criteria:
            acceptance_criteria = self._normalize_acceptance_criteria_paths(
                acceptance_criteria, workspace
            )
            full_input += f"\n\n验收标准（必须满足）：{json.dumps(acceptance_criteria, ensure_ascii=False, indent=2)}"

        if not is_default_workspace:
            full_input += "\n\n工作目录已设置（系统自动管理，无需关注具体路径）"
            full_input += (
                "\n\n路径使用规则（重要）："
                "\n- 所有文件操作使用相对路径即可，系统会自动拼接到工作目录"
                "\n- 示例：file_write(path=\"docs/report.md\")"
            )

        # ── 3.x 注入场景化工作空间提示 ──
        if ws_meta:
            _SCENE_PROMPTS = {
                "project_root": "你正在创建一个全新的项目。当前目录就是项目根目录，先规划目录结构再逐步实现。可运行测试。完成后调用 task_evaluate",
                "branch": "你在项目的功能分支中工作。使用相对路径。可运行测试。评估通过后系统自动合并到项目主线",
                "worktree": "你在一个隔离的项目完整副本中工作。使用相对路径。修改不影响主项目。可运行 pytest/mypy/lint。评估通过后系统自动合并",
                "shared": "你在父任务的工作空间中工作。使用相对路径。完成后直接调用 task_evaluate",
            }
            _scene_hint = _SCENE_PROMPTS.get(ws_meta.get("mode", ""))
            if _scene_hint:
                full_input += f"\n\n工作空间模式提示：{_scene_hint}"

        # ── 3.5 追加 Agent 特定的执行提示 ──
        # Agent 的工作流已在 system_prompt 中定义，无需额外注入执行提示

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

            # BUG-FIX-fix_20260417_task_manage_records: 管道启动前立即绑定 pipeline_run_id
            # 之前只在 cli_main.py 管道完成后才回填，导致运行中查询执行记录必然为空
            if task_service:
                try:
                    task_service.bind_pipeline_run(task_id, engine._pipeline_id)
                    logger.info(
                        "TaskWorker: bound task %s to pipeline_run %s (early binding)",
                        task_id, engine._pipeline_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "TaskWorker: early bind_pipeline_run failed for %s: %s",
                        task_id, exc,
                    )

            pipeline_timeout = self._config.get("pipeline_timeout", 600)
            try:
                pipeline_state = await asyncio.wait_for(
                    engine.run(
                        user_input=full_input,
                        agent_config=agent_config,
                        task_id=task_id,
                        acceptance_criteria=acceptance_criteria,
                        workspace=workspace,
                    ),
                    timeout=pipeline_timeout,
                )
            except asyncio.TimeoutError:
                logger.error("TaskWorker: pipeline hard timeout for task %s (%ds)", task_id, pipeline_timeout)
                if task_service:
                    try:
                        task_service.fail_task(task_id, f"Pipeline execution hard timeout ({pipeline_timeout}s)")
                    except Exception:
                        pass
                evt = self._terminal_events.pop(task_id, None)
                if evt is not None:
                    evt.set()
                if idle_timer_registered and timer_manager:
                    try:
                        await timer_manager.cancel_timer(task_id)
                    except Exception:
                        pass
                return

            # ── 5.1 管道挂起处理：有子任务时 child_task_guard 会触发 wait 挂起 ──
            # 等待子任务完成后再 resume，不要立即 resume 导致空转
            while engine.is_suspended:
                logger.info(
                    "TaskWorker: pipeline suspended for task %s (waiting for children), "
                    "saving engine reference",
                    task_id,
                )
                self._suspended_engines[task_id] = engine

                child_evt = asyncio.Event()

                def _on_child_done(event_data: Any) -> None:
                    data = event_data.data if hasattr(event_data, "data") else event_data
                    if not isinstance(data, dict):
                        return
                    child_parent = self._find_parent_task_id(data)
                    if child_parent == task_id:
                        new_status = data.get("new_status", "")
                        if new_status in _TERMINAL_STATES:
                            child_evt.set()
                    if self._resume_requested.pop(task_id, False):
                        child_evt.set()

                if self._event_bus:
                    self._event_bus.subscribe("task_state_changed", _on_child_done)

                child_wait_timeout = self._config.get("child_wait_timeout", 600)
                try:
                    await asyncio.wait_for(child_evt.wait(), timeout=child_wait_timeout)
                    if self._resume_requested.pop(task_id, False):
                        logger.info(
                            "TaskWorker: idle timeout requested resume for task %s",
                            task_id,
                        )
                    else:
                        logger.info(
                            "TaskWorker: child task completed, resuming pipeline for task %s",
                            task_id,
                        )
                except asyncio.TimeoutError:
                    logger.warning(
                        "TaskWorker: timed out waiting for child task of %s",
                        task_id,
                    )
                finally:
                    if self._event_bus:
                        try:
                            self._event_bus.unsubscribe("task_state_changed", _on_child_done)
                        except Exception:
                            pass

                if not engine.is_suspended:
                    logger.info(
                        "TaskWorker: engine already resumed for task %s, skipping",
                        task_id,
                    )
                    continue

                child_notifications = self._build_child_notifications(task_id, task_service)
                if child_notifications and hasattr(engine, "_suspended_state") and engine._suspended_state:
                    orig_input = engine._suspended_state.get("user_input", "")
                    engine._suspended_state["user_input"] = f"{child_notifications}\n\n{orig_input}".strip()
                    logger.info(
                        "TaskWorker: injected child task notifications for task %s: %s",
                        task_id, child_notifications[:200],
                    )

                try:
                    pipeline_state = await engine.resume()
                except Exception as resume_exc:
                    logger.error(
                        "TaskWorker: engine.resume failed for task %s: %s",
                        task_id, resume_exc,
                    )
                    self._suspended_engines.pop(task_id, None)
                    break

                self._idle_remind_counts.pop(task_id, None)

            self._suspended_engines.pop(task_id, None)
            logger.info("TaskWorker: pipeline completed for task %s", task_id)

        except Exception as exc:
            logger.error("TaskWorker: pipeline failed for task %s: %s", task_id, exc)
            # ── 生命周期钩子：执行异常回滚 ──
            if lifecycle and ws_meta:
                try:
                    lifecycle.on_task_failed(workspace, ws_meta)
                except Exception as hook_exc:
                    logger.warning(
                        "TaskWorker: lifecycle on_task_failed failed: task_id=%s, error=%s",
                        task_id, hook_exc,
                    )
            if task_service:
                try:
                    task_service.fail_task(task_id, str(exc))
                except Exception as fail_exc:
                    logger.error("TaskWorker: fail_task also failed: %s", fail_exc)
            evt = self._terminal_events.pop(task_id, None)
            if evt is not None:
                evt.set()
            # BUG-FIX: engine.run 异常后清理 idle_timer，防止计时器泄漏
            if idle_timer_registered and timer_manager:
                try:
                    await timer_manager.cancel_timer(task_id)
                except Exception:
                    pass
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
                        # ── 生命周期钩子：评估前保存 ──
                        if lifecycle:
                            try:
                                lifecycle.on_before_evaluate(workspace)
                            except Exception as e:
                                logger.warning(
                                    "TaskWorker: lifecycle on_before_evaluate failed: task_id=%s, error=%s",
                                    task_id, e,
                                )
                        evt = self._terminal_events.pop(task_id, None)
                        try:
                            task_service.move_to_evaluating(task_id)
                        except Exception as e:
                            logger.warning(
                                "TaskWorker: move_to_evaluating failed for %s: %s, falling back to fail",
                                task_id, e,
                            )
                            # BUG-FIX: fallback fail_task 也需要异常保护，防止二次异常导致任务卡住
                            try:
                                task_service.fail_task(task_id, f"管道退出后状态转移失败: {e}")
                            except Exception as fail_exc:
                                logger.error(
                                    "TaskWorker: fallback fail_task also failed for %s: %s",
                                    task_id, fail_exc,
                                )
                        if evt is not None:
                            evt.set()
                        return
                    else:
                        iteration_count = pipeline_state.get("iteration", "?") if pipeline_state else "?"
                        max_iter = pipeline_state.get("max_iterations", "?") if pipeline_state else "?"
                        ended = pipeline_state.get("ended", "?") if pipeline_state else "?"
                        has_task_id = bool(pipeline_state.get("task_id")) if pipeline_state else False
                        logger.warning(
                            "TaskWorker: task %s still RUNNING after pipeline exit. "
                            "iterations=%s/%s, ended=%s, has_task_id=%s, "
                            "has_result=False → marking as failed",
                            task_id, iteration_count, max_iter, ended, has_task_id,
                        )
                        evt = self._terminal_events.pop(task_id, None)
                        task_service.fail_task(task_id, "管道迭代耗尽，Agent 未完成评估")
                        if evt is not None:
                            evt.set()
                        return

        # ── 6. 等待终态 Event ──
        terminal_wait_timeout = self._config.get("terminal_wait_timeout", 600)
        try:
            await asyncio.wait_for(terminal_evt.wait(), timeout=terminal_wait_timeout)
            logger.info("TaskWorker: task %s reached terminal state", task_id)
            # ── 生命周期钩子：终态处理 ──
            if lifecycle and ws_meta and task_service:
                try:
                    _t = task_service.get_task(task_id)
                    if _t:
                        _s = _t.status if isinstance(_t.status, str) else _t.status.value
                        if _s == "completed":
                            lifecycle.on_eval_passed(task_id, workspace, ws_meta)
                            logger.info("TaskWorker: lifecycle on_eval_passed, task_id=%s", task_id)
                        elif _s == "failed":
                            lifecycle.on_eval_failed(task_id, workspace, ws_meta)
                            logger.info("TaskWorker: lifecycle on_eval_failed, task_id=%s", task_id)
                except Exception as hook_exc:
                    logger.warning(
                        "TaskWorker: lifecycle terminal hook failed: task_id=%s, error=%s",
                        task_id, hook_exc,
                    )
        except asyncio.TimeoutError:
            logger.warning("TaskWorker: task %s timed out waiting for terminal state", task_id)
            if task_service:
                try:
                    task_service.fail_task(task_id, f"TaskWorker 等待终态超时({terminal_wait_timeout}s)")
                except Exception as e:
                    logger.error(
                        "TaskWorker: fail_task after timeout failed for %s: %s",
                        task_id, e,
                    )
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

        当任务长时间无活动时，TimerManager 触发此回调。
        如果任务有活跃子任务，则唤醒管道并注入提醒消息（最多 3 次）；
        如果没有活跃子任务或提醒次数已达上限，则标记为 failed。

        Args:
            task_id: 超时的任务ID
        """
        task_service = self._task_service
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

        idle_remind_limit = 3
        remind_count = self._idle_remind_counts.get(task_id, 0)

        if task_id in self._suspended_engines and remind_count < idle_remind_limit:
            self._idle_remind_counts[task_id] = remind_count + 1
            logger.info(
                "TaskWorker: idle 超时但有挂起管道，提醒 #%d: task_id=%s",
                remind_count + 1, task_id,
            )
            self._try_resume_engine(task_id)
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

    def _find_parent_task_id(self, data: dict[str, Any]) -> str | None:
        """从状态变更事件数据中提取父任务 ID。

        Args:
            data: 事件数据

        Returns:
            父任务 ID，不存在时返回 None
        """
        task = data.get("task")
        if task is None:
            return None

        if isinstance(task, dict):
            return task.get("parent_task_id")
        return getattr(task, "parent_task_id", None)

    def _try_resume_engine(self, task_id: str) -> None:
        """通过标记请求主循环执行 resume，而非直接操作 engine。

        idle 超时回调是同步的，不能直接 await engine.resume()。
        旧方案通过 asyncio.create_task fire-and-forget 执行 resume，
        存在竞态和异常静默问题。新方案仅标记 _resume_requested，
        由主循环的 _on_child_done 回调检测标记并唤醒 child_evt，
        统一由主循环执行 resume，保证 engine 操作的串行性。

        Args:
            task_id: 挂起管道对应的任务 ID
        """
        if task_id not in self._suspended_engines:
            return

        self._resume_requested[task_id] = True
        logger.debug("TaskWorker: resume requested for task %s", task_id)

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._notify_resume(task_id))
        except RuntimeError:
            logger.warning("TaskWorker: no event loop for resume notification: task_id=%s", task_id)

    async def _notify_resume(self, task_id: str) -> None:
        """发送虚拟事件唤醒主循环中等待的 child_evt。"""
        if self._event_bus:
            try:
                await self._event_bus.emit("task_state_changed", {
                    "task_id": task_id,
                    "new_status": "running",
                    "task": {"parent_task_id": task_id},
                })
            except Exception as exc:
                logger.warning("TaskWorker: _notify_resume failed for %s: %s", task_id, exc)

    def _build_child_notifications(self, parent_task_id: str, task_service: Any) -> str:
        """构建子任务完成通知文本，供 resume 后注入到管道 user_input。

        查找 parent_task_id 下的所有子任务，将最近到达终态的任务
        构建为系统通知文本。

        Args:
            parent_task_id: 父任务 ID
            task_service: 任务服务实例

        Returns:
            通知文本，无子任务时返回空字符串
        """
        if not task_service:
            return ""

        try:
            children = task_service.list_subtasks(parent_task_id)
            if not children:
                return ""
        except Exception:
            return ""

        notifications = []
        for child in children:
            cid = child.id if hasattr(child, "id") else ""
            status_val = child.status.value if hasattr(child.status, "value") else str(child.status)
            title = child.title if hasattr(child, "title") else "未知任务"
            error = getattr(child, "error", "") or ""

            if status_val == "completed":
                notifications.append(f"[系统通知] 子任务 '{title}' (ID: {cid}) 已完成 ✅")
            elif status_val == "failed":
                err_hint = f": {error[:100]}" if error else ""
                notifications.append(f"[系统通知] 子任务 '{title}' (ID: {cid}) 失败 ❌{err_hint}")

        return "\n".join(notifications)

    def _resolve_task_workspace(self, task_id: str, task_workspace: str | None = None) -> str:
        """根据任务层级关系解析工作空间路径。

        子任务继承父任务的工作空间，形成嵌套目录结构：
        - 根任务: .ai_workspaces/{task_id}
        - 子任务: {parent_resolved_workspace}/{task_id}

        BUG-FIX-fix_20260419_workspace_inherit:
        问题根因: TaskWorker 为每个子任务创建平级工作空间 .ai_workspaces/{task_id}，
                 忽略了父任务的工作空间，导致子任务无法共享父任务的文件产出。
        修复方案: 沿 parent_task_id 链向上追溯，从根任务向下逐层解析工作空间路径，
                 确保子任务嵌套在父任务工作空间下。
        影响范围: 工作空间路径解析、任务文件产出共享

        Args:
            task_id: 当前任务 ID
            task_workspace: 任务显式指定的 workspace（来自 task_submit 参数）

        Returns:
            解析后的工作空间路径字符串
        """
        # 局部导入：避免模块级循环依赖
        from isolation.workspace import get_workspace_config_root, resolve_workspace

        root = get_workspace_config_root()
        task_service = self._task_service

        if not task_service:
            return task_workspace or f"{root}/{task_id}"

        ancestor_chain: list[tuple[str, str | None]] = []
        current_id = task_id
        visited: set[str] = set()

        while current_id and current_id not in visited:
            task = task_service.get_task(current_id)
            if task is None:
                break
            visited.add(current_id)
            stored_ws = task.metadata.get("workspace") or None
            ancestor_chain.append((current_id, stored_ws))
            current_id = task.parent_task_id

        if not ancestor_chain:
            return task_workspace or f"{root}/{task_id}"

        resolved: str | None = None
        for tid, tws in reversed(ancestor_chain):
            if resolved is None:
                # 根任务：无父空间，直接解析（task_workspace 与 metadata 中的值等价）
                resolved = resolve_workspace(tid, tws, config_root=root)
            elif tid == task_id:
                # 当前任务：使用调用方传入的 task_workspace 参数
                resolved = resolve_workspace(tid, task_workspace, parent_resolved_workspace=resolved)
            else:
                # 中间祖先：使用其 metadata 中存储的 workspace 值
                resolved = resolve_workspace(tid, tws, parent_resolved_workspace=resolved)

        return resolved or f"{root}/{task_id}"

    def _normalize_acceptance_criteria_paths(
        self, criteria: dict | list, workspace: str
    ) -> dict | list:
        """递归规范化验收标准中的路径，转为相对于 workspace 的相对路径。

        将验收标准中的绝对/半绝对路径转换为相对于当前 workspace 的路径：
        - 以 workspace 开头的路径：去掉 workspace 前缀
        - workspace 的祖先路径：计算相对路径（如 ../task_plan.md）
        - 已经是相对路径：保持不变

        Args:
            criteria: 验收标准字典或列表
            workspace: 当前工作目录路径

        Returns:
            规范化后的验收标准字典或列表
        """
        workspace_normalized = workspace.replace("\\", "/").rstrip("/")

        def _to_relative(value_normalized: str) -> str:
            if value_normalized.startswith(workspace_normalized + "/"):
                return value_normalized[len(workspace_normalized) + 1:]
            if value_normalized == workspace_normalized:
                return "."
            if value_normalized.startswith(".ai_workspaces/") or (
                "/" in value_normalized and not value_normalized.startswith("/")
            ):
                try:
                    ws_parts = workspace_normalized.split("/")
                    val_parts = value_normalized.split("/")
                    common_len = 0
                    for i in range(min(len(ws_parts), len(val_parts))):
                        if ws_parts[i] == val_parts[i]:
                            common_len += 1
                        else:
                            break
                    up_count = len(ws_parts) - common_len
                    down_parts = val_parts[common_len:]
                    rel = "/".join([".."] * up_count + down_parts)
                    return rel or "."
                except Exception:
                    return value_normalized
            return value_normalized

        def _normalize_value(value: Any) -> Any:
            if isinstance(value, dict):
                return {k: _normalize_value(v) for k, v in value.items()}
            elif isinstance(value, list):
                return [_normalize_value(item) for item in value]
            elif isinstance(value, str):
                value_normalized = value.replace("\\", "/")
                if os.path.isabs(value_normalized):
                    return value_normalized
                return _to_relative(value_normalized)
            return value

        return _normalize_value(criteria)

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
        内置 30 秒节流，避免高频终态事件导致全量扫描。
        """
        if not self._task_service:
            return

        now = time.monotonic()
        if now - self._last_container_check < _CONTAINER_CHECK_MIN_INTERVAL:
            return
        self._last_container_check = now

        try:
            # 局部导入：避免模块级循环依赖
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
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None
