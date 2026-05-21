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
import uuid as _uuid
from typing import Any

from isolation.workspace_lifecycle import WorkspaceLifecycleManager

from infrastructure.task_evaluation_builder import TaskEvaluationBuilderMixin
from infrastructure.task_executor import TaskExecutorMixin
from infrastructure.task_idle_timer import TaskIdleTimerMixin
from infrastructure.task_notifier import TaskNotifierMixin
from infrastructure.task_post_pipeline import TaskPostPipelineMixin
from infrastructure.task_recovery import TaskRecoveryMixin

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


class TaskWorker(
    TaskExecutorMixin,
    TaskRecoveryMixin,
    TaskNotifierMixin,
    TaskPostPipelineMixin,
    TaskEvaluationBuilderMixin,
    TaskIdleTimerMixin,
):
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
        self._active_tasks: set[str] = set()
        self._task_id_to_bg_task: dict[str, asyncio.Task] = {}
        # BUG-FIX-fix_20260518_submitted_dedup: 事件级去重集合，防止重复 task.submitted 并发调度
        self._submitted_task_ids: set[str] = set()
        # BUG-FIX-fix_20260520_subscribe_args: 保存 subscribe_simple 返回的订阅 ID，用于 unsubscribe
        self._sub_ids: list[str] = []

    async def start(self) -> None:
        """启动后台任务监听，并恢复残留的 running 任务。"""
        if self._running:
            return
        self._running = True

        # 通过 ServiceProvider 注册全局引用，供 task_manage cancel 调用
        try:
            from infrastructure.service_provider import get_service_provider
            get_service_provider().register("task_worker", self)
        except Exception:
            logger.warning("TaskWorker: ServiceProvider 注册失败，不阻塞启动", exc_info=True)

        self._init_lifecycle()

        # BUG-FIX-fix_20260520_subscribe_args:
        # 问题根因: subscribe(handler, filter) 签名第一个参数是 handler，但传入了字符串事件类型，
        #          导致 handler 是 "task.submitted"（不可调用的 str），真正的 handler 被当作 filter。
        #          EventBus 内部 _notify_subscribers 调用 str() 时静默失败，事件无人处理，任务永远 pending。
        # 修复方案: 使用 subscribe_simple() 替代 subscribe()，自动解析事件类型并创建 EventFilter。
        if self._event_bus:
            sub1 = self._event_bus.subscribe_simple("task.submitted", self._on_task_submitted)
            sub2 = self._event_bus.subscribe_simple("task_state_changed", self._on_task_state_changed)
            self._sub_ids = [sub1, sub2]

        await self._recover_running_tasks()
        await self._recover_evaluating_tasks()

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

    async def stop(self) -> None:
        """停止后台任务监听，等待所有 pending 任务完成。"""
        self._running = False
        # BUG-FIX-fix_20260520_subscribe_args: 使用 subscribe_simple 返回的订阅 ID 取消订阅
        if self._event_bus:
            for _sid in self._sub_ids:
                try:
                    self._event_bus.unsubscribe(_sid)
                except Exception:
                    pass
            self._sub_ids.clear()

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
                            # BUG-FIX-fix_20260512_async_compat: fail_task 现在是 async
                            await self._task_service.fail_task(tid, "TaskWorker stopped, task forcibly terminated")
                            logger.info("TaskWorker.stop: task %s marked as failed", tid)
                    except Exception as e:
                        logger.warning("TaskWorker.stop: failed to update task %s: %s", tid, e)
            except Exception as e:
                logger.warning("TaskWorker.stop: failed to cleanup tasks: %s", e)
        self._terminal_events.clear()
        self._wake_events.clear()
        self._task_id_to_bg_task.clear()

        logger.info("TaskWorker stopped")

    async def _on_task_submitted(self, event: Any) -> None:
        """处理任务提交事件。

        BUG-FIX-fix_20260516_double_dispatch:
        问题根因: _on_task_submitted 没有去重，同一 task_id 收到两次 task.submitted 事件时
                 会创建两个并发协程，两个协程都尝试 create_timer 导致 ValueError。
        修复方案: 创建前检查 _task_id_to_bg_task，已有未完成的同 task_id 协程则跳过。

        Args:
            event: 任务提交事件
        """
        if not self._running:
            return
        task_data = event.data if hasattr(event, "data") else event
        task_id = task_data.get("task_id", "unknown") if isinstance(task_data, dict) else "unknown"

        # BUG-FIX-fix_20260518_submitted_dedup: 事件级 set[str] 去重
        if task_id != "unknown":
            if task_id in self._submitted_task_ids:
                logger.info(
                    "TaskWorker: 跳过重复调度 | task_id=%s (事件级去重命中)", task_id,
                )
                return
            self._submitted_task_ids.add(task_id)

        # 去重：已有同 task_id 的运行中协程则跳过
        if task_id != "unknown":
            existing = self._task_id_to_bg_task.get(task_id)
            if existing is not None and not existing.done():
                logger.info(
                    "TaskWorker: 跳过重复调度 | task_id=%s (已有运行中协程)", task_id,
                )
                self._submitted_task_ids.discard(task_id)
                return

        logger.info("TaskWorker received task: %s", task_id)
        bg_task = asyncio.create_task(self._execute_background_task(task_data))
        self._tasks.add(bg_task)
        bg_task.add_done_callback(self._tasks.discard)
        if task_id != "unknown":
            self._task_id_to_bg_task[task_id] = bg_task
            bg_task.add_done_callback(lambda t, tid=task_id: (
                self._task_id_to_bg_task.pop(tid, None),
                self._submitted_task_ids.discard(tid),
            ))
