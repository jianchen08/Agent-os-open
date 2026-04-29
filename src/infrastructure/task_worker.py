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
import uuid as _uuid
from typing import Any

from isolation.workspace_lifecycle import WorkspaceLifecycleManager

logger = logging.getLogger(__name__)

_TERMINAL_STATES = frozenset({"completed", "failed"})
_CONTAINER_CHECK_MIN_INTERVAL = 30.0


def _reconstruct_tool_calls(messages: list[dict[str, Any]]) -> None:
    """从 tool 记录反向重建 assistant 消息的 tool_calls 字段。

    旧版 ExecutionRecordData 不保存 tool_calls_json，导致恢复的对话历史中
    assistant 消息缺少 tool_calls，而 tool 消息也缺少 tool_call_id。
    Minimax API 校验时会拒绝这种不一致的消息结构。

    重建策略：
    1. 对于已有 tool_calls 的 assistant 消息 → 跳过（新格式已保存）
    2. 对于没有 tool_calls 的 assistant 消息 → 查看后续是否紧跟 tool 消息
    3. 如果是，从 tool 消息的 tool_input 重建 tool_calls
    4. 生成合成 tool_call_id 并同时赋值给 tool 消息

    Args:
        messages: 恢复的对话历史消息列表（原地修改）
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    i = 0
    while i < len(messages):
        msg = messages[i]
        # 只处理没有 tool_calls 的 assistant 消息
        if msg.get("role") != "assistant" or msg.get("tool_calls"):
            i += 1
            continue

        # 收集紧跟其后的 tool 消息
        tool_group_start = i + 1
        tool_indices: list[int] = []
        j = tool_group_start
        while j < len(messages) and messages[j].get("role") == "tool":
            tool_indices.append(j)
            j += 1

        if not tool_indices:
            i += 1
            continue

        # 从 tool 消息重建 tool_calls
        reconstructed: list[dict[str, Any]] = []
        for tidx in tool_indices:
            tool_msg = messages[tidx]
            # 如果 tool 消息已有 tool_call_id，复用
            tc_id = tool_msg.get("tool_call_id")
            if not tc_id:
                tc_id = f"call_{_uuid.uuid4().hex[:8]}"
                tool_msg["tool_call_id"] = tc_id

            # 从 tool_input 提取 name/args
            tool_input = tool_msg.get("tool_input")
            fn_name = ""
            fn_args = "{}"
            if isinstance(tool_input, dict):
                fn_name = tool_input.get("name", "")
                raw_args = tool_input.get("args", {})
                try:
                    fn_args = json.dumps(raw_args, ensure_ascii=False)
                except (TypeError, ValueError):
                    fn_args = str(raw_args)

            reconstructed.append({
                "id": tc_id,
                "type": "function",
                "function": {
                    "name": fn_name,
                    "arguments": fn_args,
                },
            })

        if reconstructed:
            msg["tool_calls"] = reconstructed
            _log.debug(
                "Reconstructed tool_calls for assistant msg[%d]: %d calls",
                i, len(reconstructed),
            )

        i = j


class TaskWorker:
    """后台任务执行器。

    监听 EventBus 上的 task.submitted 事件，当收到新任务时
    创建 PipelineEngine 实例执行子任务。

    子任务完成通知采用单一机制：_on_task_state_changed 收到终态事件后，
    通过 _notify_suspended_pipelines 直接定位挂起的父管道并调用
    inject_and_wake，同时 set _wake_events 唤醒 while 循环。
    无双重订阅，无竞态风险。

    Attributes:
        _task_service: 任务服务实例
        _plugin_registry: 插件注册表
        _input_route_table: 输入路由表
        _output_route_table: 输出路由表
        _services: 共享服务字典
        _event_bus: 事件总线
        _running: 是否正在运行
        _tasks: 后台协程集合
        _terminal_events: task_id → asyncio.Event 的映射
        _suspended_engines: task_id → 挂起的 PipelineEngine
        _wake_events: task_id → asyncio.Event，管道挂起等待唤醒信号
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
        # parent_task_id → Event，_notify_suspended_pipelines 成功后 set
        self._wake_events: dict[str, asyncio.Event] = {}
        self._last_container_check: float = 0.0
        self._active_tasks: set[str] = set()  # 管道正在执行中的任务（含运行工具/评估器）

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
        跳过容器任务（task_scope=container）。
        """
        if not self._task_service:
            return

        # 局部导入：避免模块级循环依赖
        from tasks.types import TaskStatus

        # 1. running 任务重置为 pending
        running_tasks = self._task_service.list_by_status(TaskStatus.RUNNING)
        for task in running_tasks:
            task_scope = task.metadata.get("task_scope", "non_container")
            if task_scope == "container":
                logger.debug(
                    "TaskWorker: 跳过容器任务恢复: task_id=%s", task.id,
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
            task_scope = task.metadata.get("task_scope", "non_container")
            if task_scope == "container":
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
        self._wake_events.clear()

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

            await self._notify_suspended_pipelines(task_id, new_status, data)

    async def _notify_suspended_pipelines(self, task_id: str, new_status: str, data: dict) -> None:
        """任务系统通知挂起的管道：子任务到达终态。

        通过子任务的 parent_pipeline_id 直接定位父管道，O(1) 查找。
        回退：若无 parent_pipeline_id，扫描所有 __suspended_engine_* 兜底。
        """
        task_info = data.get("task", {})
        if isinstance(task_info, dict):
            title = task_info.get("title", task_id)
            error = task_info.get("error", "")
        else:
            title = getattr(task_info, "title", task_id)
            error = getattr(task_info, "error", "") or ""

        if new_status == "completed":
            notification = (
                f"[系统通知] 子任务 '{title}' (ID: {task_id}) 已完成 ✅\n"
                "请继续执行后续流程，提交下一个子任务。"
            )
        else:
            err_hint = f": {error[:100]}" if error else ""
            notification = (
                f"[系统通知] 子任务 '{title}' (ID: {task_id}) {new_status} ❌{err_hint}\n"
                "请根据失败情况决定后续操作（重试/替代方案/标记失败）。"
            )

        # 主路径：通过 parent_pipeline_id 直接查找
        parent_pipeline_id = None
        task_obj = None
        task_service = self._task_service
        if task_service:
            try:
                task_obj = task_service.get_task(task_id)
                if task_obj:
                    parent_pipeline_id = getattr(task_obj, "parent_pipeline_id", None)
            except Exception:
                pass

        if parent_pipeline_id:
            engine_key = f"__suspended_engine_{parent_pipeline_id}"
            engine = self._services.get(engine_key)
            if engine and hasattr(engine, "inject_and_wake"):
                try:
                    engine.inject_and_wake(notification)
                    logger.info(
                        "TaskWorker: 通过 parent_pipeline_id 直接通知: "
                        "pipeline=%s, task=%s, status=%s",
                        parent_pipeline_id, task_id, new_status,
                    )
                    # 唤醒 while 循环中等待的 wake_event
                    parent_task_id = getattr(task_obj, "parent_task_id", None) if task_obj else None
                    if parent_task_id:
                        wake_evt = self._wake_events.get(parent_task_id)
                        if wake_evt is not None:
                            wake_evt.set()
                    return
                except Exception as exc:
                    logger.warning("TaskWorker: inject_and_wake 失败: %s", exc)
            else:
                # 父管道尚未挂起（竞态：子任务在父管道 _suspend_and_wait 之前失败）
                # 将通知入队，由 _suspend_and_wait 在挂起时消费
                pending_key = f"__pending_notifications_{parent_pipeline_id}"
                pending_list = self._services.get(pending_key, [])
                pending_list.append(notification)
                self._services[pending_key] = pending_list
                logger.info(
                    "TaskWorker: 父管道尚未挂起，通知已入队: "
                    "pipeline=%s, task=%s, status=%s, queue_size=%d",
                    parent_pipeline_id, task_id, new_status, len(pending_list),
                )
                return

        # 回退：扫描所有挂起管道（兼容旧任务无 parent_pipeline_id 的情况）
        for key, engine in list(self._services.items()):
            if not key.startswith("__suspended_engine_"):
                continue
            if not hasattr(engine, "inject_and_wake"):
                continue
            watching = getattr(engine, "_watching_task_ids", [])
            if watching and task_id not in watching:
                logger.debug(
                    "TaskWorker: 跳过无关管道: pipeline_key=%s, task=%s, watching=%s",
                    key, task_id, watching,
                )
                continue
            try:
                engine.inject_and_wake(notification)
                logger.info(
                    "TaskWorker: 回退扫描通知挂起管道: pipeline_key=%s, task=%s",
                    key, task_id,
                )
                # 回退路径：通过 _suspended_engines 反查 parent_task_id
                for pid, pengine in self._suspended_engines.items():
                    if pengine is engine:
                        wake_evt = self._wake_events.get(pid)
                        if wake_evt is not None:
                            wake_evt.set()
                        break
            except Exception as exc:
                logger.warning("TaskWorker: 通知挂起管道失败: %s", exc)

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
        _cleanup_done = False  # 防止多次清理

        # ── 0. 跳过容器任务 ──
        if task_service:
            task = task_service.get_task(task_id)
            if task is not None:
                task_scope = task.metadata.get("task_scope", "non_container")
                if task_scope == "container":
                    logger.info(
                        "TaskWorker: 跳过容器任务 %s (task_scope=container)", task_id,
                    )
                    # BUG-FIX-fix_20260425_container_workspace_init:
                    # 问题根因: 容器任务直接 return，未初始化工作空间（git init），
                    #          后续子任务 is_root=True 走 _start_root_task，
                    #          但 base_path 为空目录，创建空 project 而非基于容器空间做 worktree。
                    # 修复方案: 容器任务跳过前先调用 lifecycle 初始化容器空间，
                    #          增加 3 次重试，失败则 fail_task 报错。
                    # 影响范围: 所有容器任务的子任务工作空间隔离
                    lifecycle: WorkspaceLifecycleManager | None = self._services.get("workspace_lifecycle_manager")
                    if not lifecycle:
                        logger.error(
                            "TaskWorker: WorkspaceLifecycleManager 不可用，无法初始化容器空间: task_id=%s",
                            task_id,
                        )
                        task_service.fail_task(task_id, "容器空间初始化失败：WorkspaceLifecycleManager 不可用")
                        return

                    _CONTAINER_INIT_RETRIES = 3
                    _init_ok = False
                    _last_err: Exception | None = None
                    for _attempt in range(1, _CONTAINER_INIT_RETRIES + 1):
                        try:
                            container_ws = task.metadata.get("workspace") or None
                            lifecycle.init_container_workspace(task_id, container_ws, task_data)
                            container_workspace_path = lifecycle._ws_meta_store.get(task_id, {}).get("path", "")
                            if container_workspace_path:
                                task.metadata["container_workspace"] = container_workspace_path
                                self._task_service.save_task(task)
                                logger.info(
                                    "TaskWorker: 容器空间已初始化: task_id=%s, workspace=%s (attempt %d)",
                                    task_id, container_workspace_path, _attempt,
                                )
                                _init_ok = True
                                break
                            else:
                                _last_err = RuntimeError("init_container_workspace 成功但未返回有效 path")
                        except Exception as e:
                            _last_err = e
                            logger.warning(
                                "TaskWorker: 容器空间初始化失败 (attempt %d/%d): task_id=%s, error=%s",
                                _attempt, _CONTAINER_INIT_RETRIES, task_id, e,
                            )

                    if not _init_ok:
                        logger.error(
                            "TaskWorker: 容器空间初始化最终失败 (%d 次重试耗尽): task_id=%s, error=%s",
                            _CONTAINER_INIT_RETRIES, task_id, _last_err,
                        )
                        task_service.fail_task(
                            task_id,
                            f"容器空间初始化失败（{_CONTAINER_INIT_RETRIES} 次重试耗尽）：{_last_err}",
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

        # ── 3. 构建完整的 user_input ──
        user_input = task_data.get("user_input", "")
        description = task_data.get("description", "")
        acceptance_criteria = task_data.get("acceptance_criteria", {})
        explicit_workspace = task_data.get("workspace") or None
        workspace = self._resolve_task_workspace(task_id, explicit_workspace)
        task_data["_has_explicit_workspace"] = bool(explicit_workspace)

        # ── 3.x.0 等待父容器工作空间就绪（解决竞态条件） ──
        # BUG-FIX-fix_20260425_container_workspace_race:
        # 问题根因: 容器任务和子任务通过 asyncio.create_task 并行执行，
        #           子任务可能在容器工作空间初始化完成前就调用 on_task_start，
        #           导致 _find_container_workspace 返回 None → 失败。
        # 修复方案: 子任务启动前检查父容器是否为 container 类型，
        #           如果是则轮询等待其 container_workspace 元数据就绪（最多 30s）。
        if task_service:
            _t = task_service.get_task(task_id)
            if _t and _t.parent_task_id:
                _parent = task_service.get_task(_t.parent_task_id)
                if _parent and _parent.metadata.get("task_scope") == "container":
                    _WAIT_INTERVAL = 1.0
                    _WAIT_MAX = 30.0
                    _waited = 0.0
                    while _waited < _WAIT_MAX:
                        _parent_refreshed = task_service.get_task(_t.parent_task_id)
                        if _parent_refreshed and _parent_refreshed.metadata.get("container_workspace"):
                            logger.info(
                                "TaskWorker: 父容器工作空间已就绪: parent=%s, waited=%.1fs",
                                _t.parent_task_id, _waited,
                            )
                            break
                        await asyncio.sleep(_WAIT_INTERVAL)
                        _waited += _WAIT_INTERVAL
                    else:
                        logger.warning(
                            "TaskWorker: 等待父容器工作空间超时(%.1fs): parent=%s, 继续执行",
                            _waited, _t.parent_task_id,
                        )

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
                # BUG-FIX-fix_20260425_container_workspace_init:
                # 容器子任务找不到容器工作空间等致命错误应直接 fail_task
                logger.error(
                    "TaskWorker: lifecycle on_task_start failed: task_id=%s, error=%s",
                    task_id, e,
                )
                if task_service:
                    task_service.fail_task(task_id, f"工作空间初始化失败: {e}")
                return

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
                "plain": "你在一个临时工作目录中工作。使用相对路径。完成后直接调用 task_evaluate",
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

        # ── 4.5 检查任务是否已有 pipeline_run_id（重试时复用） ──
        existing_pipeline_id = None
        if task_service:
            _task_for_id = task_service.get_task(task_id)
            if _task_for_id and _task_for_id.pipeline_run_id:
                existing_pipeline_id = _task_for_id.pipeline_run_id

        # ── 5. 创建子 PipelineEngine 并执行 ──
        timer_manager = self._services.get("timer_manager")
        idle_timer_registered = False
        try:
            checkpoint_mgr = self._services.get("checkpoint_manager")
            engine = PipelineEngine(
                input_route_table=self._input_route_table,
                output_route_table=self._output_route_table,
                plugin_registry=self._plugin_registry,
                services=self._services,
                checkpoint_manager=checkpoint_mgr,
            )

            # 复用已有的 pipeline_run_id，确保任务重试时管道 ID 不变
            if existing_pipeline_id:
                engine._pipeline_id = existing_pipeline_id
                logger.info(
                    "TaskWorker: reusing existing pipeline_id %s for task %s (retry)",
                    existing_pipeline_id, task_id,
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
                    # 按根任务分组执行记录
                    exec_storage = self._services.get("execution_record_storage")
                    if exec_storage:
                        root_id = task_service.get_root_task_id(task_id)
                        if root_id:
                            exec_storage.register_pipeline(engine._pipeline_id, root_id)
                except Exception as exc:
                    logger.warning(
                        "TaskWorker: early bind_pipeline_run failed for %s: %s",
                        task_id, exc,
                    )

            # BUG-FIX-fix_20260422_idle_timer_timing:
            # 问题根因: idle 计时器在 start_task() 后立即注册，但 TaskWorker 的
            #           _execute_background_task 可能因事件循环调度延迟（如上级管道
            #           LLM 调用阻塞）导致实际管道执行推迟数分钟，这期间 idle 计时器
            #           持续倒计时，留给实际管道执行的时间被大幅压缩甚至直接超时。
            # 修复方案: 将 idle 计时器注册移到 engine.run() 之前，确保计时器只在
            #           任务真正开始执行管道时才启动，不受上级管道阻塞影响。
            # 影响范围: 所有通过 TaskWorker 执行的后台任务的 idle 超时行为
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
                    logger.error(
                        "TaskWorker: 注册 idle 计时器失败，任务拒绝执行: task_id=%s, error=%s",
                        task_id, e,
                    )
                    if task_service:
                        task_service.fail_task(task_id, f"idle计时器初始化失败，任务拒绝执行: {e}")
                    evt = self._terminal_events.pop(task_id, None)
                    if evt is not None:
                        evt.set()
                    return

            pipeline_timeout = self._config.get("pipeline_timeout", 1800)
            # Agent-level timeout override: respect agent's own timeout_seconds setting
            if agent_config and hasattr(agent_config, 'timeout_seconds') and agent_config.timeout_seconds > 0:
                pipeline_timeout = max(pipeline_timeout, agent_config.timeout_seconds)
            self._active_tasks.add(task_id)
            project_root = ws_meta.get("project_root", workspace) if ws_meta else workspace

            # ── 5.5 重试时从管道执行记录恢复对话历史 ──
            # 直接从 ExecutionRecordStorage 读取该 pipeline_run_id 的所有记录，
            # 转换为 messages 格式作为 conversation_history 传入，
            # 确保重试时管道 ID 不变、历史完整。
            conversation_history: list[dict[str, Any]] | None = None
            if existing_pipeline_id:
                exec_storage = self._services.get("execution_record_storage")
                if exec_storage:
                    try:
                        prev_records = exec_storage.list_by_pipeline(existing_pipeline_id)
                        if prev_records:
                            conversation_history = []
                            for r in prev_records:
                                msg: dict[str, Any] = {"role": r.role, "content": r.content}
                                if r.name:
                                    msg["name"] = r.name
                                if r.tool_call_id:
                                    msg["tool_call_id"] = r.tool_call_id
                                if r.tool_input:
                                    msg["tool_input"] = r.tool_input
                                # 从 tool_calls_json 恢复 tool_calls（新格式）
                                if r.tool_calls_json:
                                    try:
                                        msg["tool_calls"] = json.loads(r.tool_calls_json)
                                    except (json.JSONDecodeError, TypeError):
                                        pass
                                conversation_history.append(msg)

                            # 旧记录没有 tool_calls_json，需要从 tool 记录反向重建
                            _reconstruct_tool_calls(conversation_history)

                            logger.info(
                                "TaskWorker: restored %d messages from pipeline records "
                                "for task %s (retry, pipeline=%s)",
                                len(conversation_history), task_id, existing_pipeline_id,
                            )
                    except Exception as exc:
                        logger.warning(
                            "TaskWorker: failed to restore pipeline history for task %s: %s",
                            task_id, exc,
                        )

            try:
                pipeline_state = await asyncio.wait_for(
                    engine.run(
                        user_input=full_input,
                        agent_config=agent_config,
                        conversation_history=conversation_history,
                        task_id=task_id,
                        acceptance_criteria=acceptance_criteria,
                        workspace=workspace,
                        project_root=project_root,
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
                # BUG-FIX: pipeline hard timeout 必须清理 _active_tasks 和 idle 计时器
                # 防止 idle 计时器在任务已失败后仍不断重新创建（超时风暴）
                self._active_tasks.discard(task_id)
                self._idle_remind_counts.pop(task_id, None)
                if idle_timer_registered and timer_manager:
                    try:
                        await timer_manager.cancel_timer(task_id)
                    except Exception:
                        pass
                _cleanup_done = True
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

                # 注册 wake_event，由 _notify_suspended_pipelines 或 _try_resume_engine set
                wake_evt = asyncio.Event()
                self._wake_events[task_id] = wake_evt

                child_wait_timeout = self._config.get("child_wait_timeout", 600)
                try:
                    await asyncio.wait_for(wake_evt.wait(), timeout=child_wait_timeout)
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
                    self._wake_events.pop(task_id, None)

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
            self._active_tasks.discard(task_id)
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
                                lifecycle.on_before_evaluate(workspace, ws_meta)
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
                        # BUG-FIX: 进入 evaluating 后清理 idle 计时器
                        # 防止 evaluating 状态的任务仍触发 idle 超时
                        self._active_tasks.discard(task_id)
                        self._idle_remind_counts.pop(task_id, None)
                        if idle_timer_registered and timer_manager:
                            try:
                                await timer_manager.cancel_timer(task_id)
                            except Exception:
                                pass
                        _cleanup_done = True
                        return
                    else:
                        # 从 pipeline state 中提取完整诊断信息
                        iteration_count = pipeline_state.get("iteration", "?") if pipeline_state else "?"
                        max_iter = pipeline_state.get("max_iterations", "?") if pipeline_state else "?"
                        ended = pipeline_state.get("ended", "?") if pipeline_state else "?"
                        raw_error = pipeline_state.get("raw_error") if pipeline_state else None
                        llm_error_info = pipeline_state.get("llm_error_info") if pipeline_state else None
                        task_complete = pipeline_state.get("task_complete") if pipeline_state else None
                        error_analysis = pipeline_state.get("error_analysis") if pipeline_state else None

                        # 根据实际原因构建精确的错误信息
                        parts: list[str] = []
                        hit_max_iter = (
                            isinstance(iteration_count, int)
                            and isinstance(max_iter, int)
                            and iteration_count >= max_iter
                        )

                        if raw_error:
                            # 管道内有明确错误（LLM 调用失败、工具异常等）
                            parts.append(f"管道异常退出: {raw_error}")
                            if llm_error_info:
                                etype = llm_error_info.get("error_type", "")
                                if etype:
                                    parts.append(f"错误类型={etype}")
                        elif hit_max_iter:
                            # 确实是迭代耗尽
                            parts.append(
                                f"管道迭代耗尽"
                                f"({iteration_count}/{max_iter})"
                            )
                        else:
                            # 其他原因（无 route signal 强制退出等）
                            parts.append(
                                f"管道异常结束(iterations="
                                f"{iteration_count}/{max_iter})"
                            )

                        if error_analysis:
                            parts.append(f"错误分析: {error_analysis}")
                        if task_complete is False:
                            parts.append("Agent 标记任务未完成")

                        error_msg = (
                            "，".join(parts)
                            if parts
                            else "管道异常退出，Agent 未完成评估"
                        )

                        logger.warning(
                            "TaskWorker: task %s still RUNNING "
                            "after pipeline exit. "
                            "iterations=%s/%s, ended=%s, "
                            "raw_error=%s, "
                            "has_result=False → %s",
                            task_id, iteration_count,
                            max_iter, ended,
                            raw_error or "(none)",
                            error_msg,
                        )
                        evt = self._terminal_events.pop(
                            task_id, None,
                        )
                        task_service.fail_task(
                            task_id, error_msg,
                        )
                        if evt is not None:
                            evt.set()
                        # BUG-FIX: fail_task 后清理 idle 计时器
                        # 防止任务已标记 failed 但 idle 计时器
                        # 仍在 _active_tasks 检测中不断重建
                        self._active_tasks.discard(task_id)
                        self._idle_remind_counts.pop(
                            task_id, None,
                        )
                        if idle_timer_registered and timer_manager:
                            try:
                                await timer_manager.cancel_timer(
                                    task_id,
                                )
                            except Exception:
                                pass
                        _cleanup_done = True
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
                        "TaskWorker: lifecycle terminal hook failed: task_id=%s, error=%s, retrying once",
                        task_id, hook_exc,
                    )
                    try:
                        lifecycle.restore_ws_meta(task_id)
                        ws_meta_retry = lifecycle._ws_meta_store.get(task_id, ws_meta)
                        if _s == "completed":
                            lifecycle.on_eval_passed(task_id, workspace, ws_meta_retry)
                        elif _s == "failed":
                            lifecycle.on_eval_failed(task_id, workspace, ws_meta_retry)
                        logger.info("TaskWorker: lifecycle hook retry succeeded: task_id=%s", task_id)
                    except Exception as retry_exc:
                        logger.error(
                            "TaskWorker: lifecycle hook retry also failed: task_id=%s, error=%s",
                            task_id, retry_exc,
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

        idle 计时器仅在管道挂起（等待子任务）期间有意义：
        - 管道活跃时：暂停计时器（重建但不计数），不干预执行
        - 管道挂起时：唤醒并提醒（最多 idle_remind_limit 次），超限则标记 failed

        Args:
            task_id: 超时的任务ID
        """
        task_service = self._task_service
        if not task_service:
            logger.warning(
                "TaskWorker: idle 超时但无 task_service，"
                "无法处理: task_id=%s",
                task_id,
            )
            return

        task = task_service.get_task(task_id)
        if task is None:
            # 任务已不存在，取消残留计时器
            self._cancel_idle_timer_async(task_id)
            return

        status_str = (
            task.status
            if isinstance(task.status, str)
            else task.status.value
        )
        if status_str != "running":
            logger.debug(
                "TaskWorker: idle 超时但任务已不在 running"
                " 状态: task_id=%s, status=%s",
                task_id, status_str,
            )
            # BUG-FIX: 任务已非 running 状态，
            # 必须取消残留计时器防止超时风暴
            self._active_tasks.discard(task_id)
            self._idle_remind_counts.pop(task_id, None)
            self._cancel_idle_timer_async(task_id)
            return

        idle_remind_limit = 3
        remind_count = self._idle_remind_counts.get(task_id, 0)

        # 活跃管道期间暂停 idle 计时器：重建但不计数、不失败
        if task_id in self._active_tasks:
            logger.debug(
                "TaskWorker: idle 超时但管道活跃，"
                "暂停计时器: task_id=%s",
                task_id,
            )
            timer_manager = self._services.get("timer_manager")
            if timer_manager:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        self._recreate_idle_timer_async(
                            task_id, timer_manager,
                        ),
                    )
                except RuntimeError:
                    pass
            return

        if task_id in self._suspended_engines and remind_count < idle_remind_limit:
            self._idle_remind_counts[task_id] = remind_count + 1
            logger.info(
                "TaskWorker: idle 超时但有挂起管道，"
                "提醒 #%d: task_id=%s",
                remind_count + 1, task_id,
            )
            self._try_resume_engine(task_id)

            # BUG-FIX-fix_20260422_idle_timer_reset:
            # 提醒后重新创建计时器
            timer_manager = self._services.get("timer_manager")
            if timer_manager:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        self._recreate_idle_timer_async(
                            task_id, timer_manager,
                        ),
                    )
                except RuntimeError:
                    logger.warning(
                        "TaskWorker: no event loop to "
                        "recreate idle timer: task_id=%s",
                        task_id,
                    )
            return

        try:
            timer_mgr = self._services.get("timer_manager")
            threshold = (
                getattr(timer_mgr, "idle_threshold", "?")
                if timer_mgr else "?"
            )
            task_service.fail_task(
                task_id,
                f"idle 超时({threshold}s无活动)",
            )
            logger.warning(
                "TaskWorker: 任务 idle 超时，已标记 failed: "
                "task_id=%s", task_id,
            )
            self._active_tasks.discard(task_id)
            self._idle_remind_counts.pop(task_id, None)
            evt = self._terminal_events.pop(task_id, None)
            if evt is not None:
                evt.set()
        except Exception as e:
            logger.error(
                "TaskWorker: idle 超时处理失败: "
                "task_id=%s, error=%s", task_id, e,
            )

    def _cancel_idle_timer_async(self, task_id: str) -> None:
        """异步取消残留的 idle 计时器（从同步回调调用）。

        当 _on_idle_timeout 发现任务已不在 running 状态时，
        通过此方法调度异步计时器取消，防止计时器残留触发风暴。

        Args:
            task_id: 任务 ID
        """
        timer_manager = self._services.get("timer_manager")
        if not timer_manager:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._do_cancel_timer(task_id, timer_manager))
        except RuntimeError:
            pass

    async def _do_cancel_timer(
        self, task_id: str, timer_manager: Any,
    ) -> None:
        """实际执行计时器取消。

        Args:
            task_id: 任务 ID
            timer_manager: 计时器管理器实例
        """
        try:
            await timer_manager.cancel_timer(task_id)
            logger.debug(
                "TaskWorker: 残留 idle 计时器已取消: "
                "task_id=%s", task_id,
            )
        except Exception:
            pass

    async def _recreate_idle_timer_async(
        self, task_id: str, timer_manager: Any,
    ) -> None:
        """idle 超时提醒后异步重新创建计时器。

        在 _on_idle_timeout 发送提醒后调用，
        为下一个超时周期创建新计时器。
        重建前先检查任务状态，避免在任务已终态后
        无意义地重建计时器（防止超时风暴）。

        Args:
            task_id: 任务 ID
            timer_manager: 计时器管理器实例
        """
        try:
            # BUG-FIX: 重建前先检查任务是否仍在 running
            # 防止在任务已终态（failed/completed/evaluating）
            # 后无意义地重建计时器
            if self._task_service:
                task = self._task_service.get_task(task_id)
                if task is not None:
                    status = (
                        task.status
                        if isinstance(task.status, str)
                        else task.status.value
                    )
                    if status != "running":
                        logger.debug(
                            "TaskWorker: 跳过 idle 计时器重建，"
                            "任务已非 running: task_id=%s, "
                            "status=%s",
                            task_id, status,
                        )
                        self._active_tasks.discard(task_id)
                        self._idle_remind_counts.pop(
                            task_id, None,
                        )
                        return
            try:
                await timer_manager.cancel_timer(task_id)
            except Exception:
                pass
            await timer_manager.create_timer(
                task_id=task_id,
                timeout=float(timer_manager.idle_threshold),
                callback=lambda tid=task_id: self._on_idle_timeout(tid),
            )
            logger.info(
                "TaskWorker: idle timer recreated after "
                "remind for task %s", task_id,
            )
        except Exception as e:
            logger.warning(
                "TaskWorker: recreate idle timer failed: "
                "task_id=%s, error=%s",
                task_id, e,
            )

    def _try_resume_engine(self, task_id: str) -> None:
        """通过标记和 wake_event 请求主循环执行 resume。

        idle 超时回调是同步的，不能直接 await engine.resume()。
        旧方案通过 asyncio.create_task fire-and-forget 执行 resume，
        存在竞态和异常静默问题。新方案标记 _resume_requested 并
        直接 set wake_event，由主循环统一执行 resume，
        保证 engine 操作的串行性。

        Args:
            task_id: 挂起管道对应的任务 ID
        """
        if task_id not in self._suspended_engines:
            return

        self._resume_requested[task_id] = True
        logger.debug("TaskWorker: resume requested for task %s", task_id)

        # 直接 set wake_event 唤醒 while 循环，无需发虚假事件
        wake_evt = self._wake_events.get(task_id)
        if wake_evt is not None:
            wake_evt.set()

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
        """容器终态检查（已禁用超时自动判定）。

        容器的完成/失败由主 Agent 通过 complete_container / fail_container 决定，
        系统不做自动判定。
        """
        return
