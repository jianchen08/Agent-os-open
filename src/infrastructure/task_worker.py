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
import contextlib
import json
import logging
import uuid as _uuid
from typing import Any

from infrastructure.task_context import TaskExecutionContext
from infrastructure.task_evaluation_builder import TaskEvaluationBuilderMixin
from infrastructure.task_executor import TaskExecutorMixin
from infrastructure.task_idle_timer import TaskIdleTimerMixin
from infrastructure.task_notifier import TaskNotifierMixin
from infrastructure.task_post_pipeline import TaskPostPipelineMixin
from infrastructure.task_recovery import TaskRecoveryMixin
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
    import logging as _logging  # noqa: PLC0415

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

            reconstructed.append(
                {
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": fn_name,
                        "arguments": fn_args,
                    },
                }
            )

        if reconstructed:
            msg["tool_calls"] = reconstructed
            _log.debug(
                "Reconstructed tool_calls for assistant msg[%d]: %d calls",
                i,
                len(reconstructed),
            )

        i = j


class TaskWorker(
    TaskExecutorMixin,
    TaskRecoveryMixin,
    TaskNotifierMixin,
    TaskPostPipelineMixin,
    TaskEvaluationBuilderMixin,
    TaskIdleTimerMixin,
):
    """后台任务执行器。

    通过 submit_task() 接收任务提交，创建 PipelineEngine 实例执行子任务。
    每个任务有独立的 TaskExecutionContext，支持异步并行执行。

    子任务完成通知：_on_task_state_changed 收到终态事件后，
    通过 _notify_suspended_pipelines 直接定位挂起的父管道并调用
    inject_message，同时 set ctx.wake_event 唤醒 while 循环。

    Attributes:
        _task_service: 任务服务实例
        _plugin_registry: 插件注册表
        _input_route_table: 输入路由表
        _output_route_table: 输出路由表
        _services: 共享服务字典
        _event_bus: 事件总线（仅用于 task_state_changed）
        _running: 是否正在运行
        _tasks: 后台协程集合
        _contexts: task_id → TaskExecutionContext 映射
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
        self._contexts: dict[str, TaskExecutionContext] = {}
        self._last_container_check: float = 0.0
        self._main_loop: asyncio.AbstractEventLoop | None = None
        # 注册自身到 services，供 PipelineEngine 通过 services 访问 idle timer reset
        self._services["task_worker"] = self

    async def start(self) -> None:
        """启动后台任务监听，并恢复残留的 running 任务。"""
        if self._running:
            return
        self._running = True
        self._main_loop = asyncio.get_running_loop()

        # 通过 ServiceProvider 注册全局引用，供 task_manage cancel 调用
        try:
            from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

            get_service_provider().register("task_worker", self)
        except Exception:
            logger.warning("TaskWorker: ServiceProvider 注册失败，不阻塞启动", exc_info=True)

        self._init_lifecycle()

        if self._task_service:
            self._task_service.register_state_callback(self._on_task_state_changed)
            logger.info("TaskWorker: 已注册任务状态变更回调")

        await self._recover_running_tasks()
        await self._recover_evaluating_tasks()

        logger.info("TaskWorker started (callback-driven background task processor)")

    def _init_lifecycle(self) -> None:
        """初始化 WorkspaceLifecycleManager 实例并注册到 services

        在 TaskWorker.start() 中自行创建 lifecycle 实例，不依赖外部 services 注入
        （lifecycle 是 TaskWorker 自身的职责），确保所有生命周期钩子（worktree 创建、
        合并、清理）能被正常执行。
        """
        try:
            from pathlib import Path as _Path  # noqa: PLC0415

            from tools.builtin.resource_merge import ResourceMergeTool  # noqa: PLC0415

            project_root = str(_Path.cwd())
            resource_merge = ResourceMergeTool(base_path=project_root)

            from config.config_center import get_config_center  # noqa: PLC0415

            iso_config: dict[str, Any] = get_config_center().get("isolation/isolation_config.yaml") or {}

            ws_meta_store: dict[str, Any] = {}

            lifecycle = WorkspaceLifecycleManager(
                resource_merge=resource_merge,
                config=iso_config,
                task_tree=self._task_service,
                ws_meta_store=ws_meta_store,
                base_path=project_root,
            )
            self._services["workspace_lifecycle_manager"] = lifecycle
            # 注册到 ServiceProvider，供 task_evaluate / _task_cleanup 等跨模块
            # 通过 provider.get("workspace_lifecycle_manager") 获取同一实例。
            try:
                from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

                get_service_provider().register("workspace_lifecycle_manager", lifecycle)
            except Exception:
                logger.warning(
                    "TaskWorker: workspace_lifecycle_manager 注册到 ServiceProvider 失败，不阻塞",
                    exc_info=True,
                )
            logger.info(
                "TaskWorker: WorkspaceLifecycleManager initialized, base_path=%s",
                project_root,
            )
        except Exception as exc:
            logger.warning(
                "TaskWorker: WorkspaceLifecycleManager init failed, lifecycle hooks will be skipped: %s",
                exc,
            )

    async def stop(self) -> None:
        """停止后台任务监听，等待所有 pending 任务完成。"""
        self._running = False
        if self._task_service:
            self._task_service.unregister_state_callback(self._on_task_state_changed)

        if self._tasks:
            # 等待已提交的 asyncio.Task 开始执行，确保 _contexts 已注册
            await asyncio.sleep(0.1)

        pending = [ctx.terminal_event for ctx in self._contexts.values()]
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
                with contextlib.suppress(asyncio.CancelledError):
                    await bg_task
        self._tasks.clear()

        if self._task_service:
            try:
                from tasks.types import TaskStatus  # noqa: PLC0415

                remaining_ids = list(self._contexts.keys())
                for tid in remaining_ids:
                    try:
                        task = self._task_service.get_task(tid)
                        if task and (task.status in (TaskStatus.RUNNING, TaskStatus.PENDING)):
                            await self._task_service.pause_task(tid, paused_by="system")
                            logger.info("TaskWorker.stop: task %s marked as paused (system)", tid)
                    except Exception as e:
                        logger.warning("TaskWorker.stop: failed to pause task %s: %s", tid, e)
            except Exception as e:
                logger.warning("TaskWorker.stop: failed to cleanup tasks: %s", e)
        self._contexts.clear()

        logger.info("TaskWorker stopped")

    def submit_task(self, task_data: dict[str, Any]) -> bool:
        """提交任务到后台执行（替代 EventBus 的直接调用接口）。

        多个任务可并行调用，每个任务有独立的 TaskExecutionContext。

        Args:
            task_data: 任务数据字典，可包含 _prepared_context

        Returns:
            True=成功创建后台协程, False=重复提交或未启动
        """
        if not self._running:
            logger.warning("TaskWorker: 未启动，拒绝提交 | task=%s", task_data.get("task_id"))
            return False

        task_id = task_data.get("task_id", "")
        if not task_id or task_id == "unknown":
            return False

        # 去重
        ctx = self._contexts.get(task_id)
        if ctx and ctx.bg_task and not ctx.bg_task.done():
            logger.info("TaskWorker: 跳过重复提交 | task=%s", task_id)
            return True

        # 创建独立上下文
        context = TaskExecutionContext(task_id)

        # 如果携带了准备上下文，填充
        prepared = task_data.get("_prepared_context")
        if prepared:
            context.workspace = prepared.get("workspace", "")
            context.ws_meta = prepared.get("ws_meta", {})
            context.full_input = prepared.get("full_input", "")
            context.isolation_mode = prepared.get("isolation_mode", "")
            context.has_explicit_workspace = prepared.get("has_explicit_workspace", False)
            context.agent_config_validated = prepared.get("agent_config_validated", False)

        self._contexts[task_id] = context

        async def _run_and_cleanup(td, ctx):
            try:
                await self._execute_background_task(td, ctx)
            except Exception as e:
                logger.error("TaskWorker: 执行失败 | task=%s | error=%s", td.get("task_id"), e)
                self._contexts.pop(td.get("task_id"), None)  # 启动失败时清理

        loop = self._main_loop
        if loop is None or loop.is_closed():
            logger.error("TaskWorker: 主事件循环不可用 | task=%s", task_id)
            self._contexts.pop(task_id, None)
            return False

        def _create_task_on_main_loop():
            bg_task = loop.create_task(_run_and_cleanup(task_data, context))
            self._tasks.add(bg_task)
            context.bg_task = bg_task
            bg_task.add_done_callback(self._tasks.discard)

        try:
            running_loop = asyncio.get_running_loop()
            if running_loop is loop:
                _create_task_on_main_loop()
            else:
                loop.call_soon_threadsafe(_create_task_on_main_loop)
        except RuntimeError:
            loop.call_soon_threadsafe(_create_task_on_main_loop)

        logger.info("TaskWorker: 后台任务已创建 | task=%s | 并行任务数=%d", task_id, len(self._contexts))
        return True
