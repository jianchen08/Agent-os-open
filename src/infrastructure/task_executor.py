"""后台任务执行 Mixin。

负责后台任务的完整执行生命周期：PipelineEngine 创建、执行、
挂起/恢复、超时处理、管道取消等。

从 task_worker.py 拆分而出，降低原文件复杂度。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import json
import logging
import uuid as _uuid
from typing import Any

from infrastructure.task_context import TaskExecutionContext
from isolation.workspace_lifecycle import WorkspaceLifecycleManager
from pipeline.stream_bridge import PipelineStreamBridge, TargetedSink

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 管道引擎独立线程运行器 — 解耦工具执行与管道生命周期
# ---------------------------------------------------------------------------

def _run_engine_isolated(engine: Any, **run_kwargs: Any) -> Any:
    """在独立线程 + 独立事件循环中运行管道引擎。

    完全隔离管道引擎与调用方的事件循环：
    - 引擎跑在自己的线程和事件循环里
    - 调用方通过 Future 获取完成通知，不阻塞、不轮询
    - 引擎的 _cleanup_run_loop 自行管理注册/注销

    这消除了 asyncio.run() 的 _cancel_all_tasks() 级联取消问题，
    因为引擎的事件循环与工具/调用方的事件循环完全独立。

    Args:
        engine: PipelineEngine 实例
        **run_kwargs: 传递给 engine.run() 的参数

    Returns:
        engine.run() 的返回值（最终状态字典）
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(engine.run(**run_kwargs))
    finally:
        loop.close()


class TaskExecutorMixin:
    """后台任务执行混入类。

    提供 _execute_background_task、cancel_pipeline、_resolve_task_workspace
    及管道执行辅助方法，由 TaskWorker 通过多继承组合使用。
    idle 计时器相关方法由 TaskIdleTimerMixin 提供。
    """

    def _resolve_isolation_mode(self, task_data: dict[str, Any], task_obj: Any = None) -> str:
        """解析隔离级别，消除重复逻辑。"""
        if task_data.get("isolation_level"):
            return task_data["isolation_level"]
        if task_obj and task_obj.metadata and task_obj.metadata.get("isolation_level"):
            return task_obj.metadata["isolation_level"]
        try:
            import yaml as _yaml
            from pathlib import Path as _P
            _iso_cfg_path = _P("config/isolation/isolation_config.yaml")
            if _iso_cfg_path.exists():
                _iso_cfg = _yaml.safe_load(_iso_cfg_path.read_text(encoding="utf-8")) or {}
                return _iso_cfg.get("coordinator", {}).get("default_level", "")
        except Exception:
            pass
        return ""

    async def _execute_background_task(self, task_data: dict[str, Any], ctx: TaskExecutionContext) -> None:
        """执行后台任务的完整生命周期（start → run pipeline → wait terminal）。

        Args:
            task_data: 任务提交事件中的数据字典
            ctx: 任务执行上下文
        """

        task_id = task_data.get("task_id", "unknown")
        logger.info("TaskWorker: _execute_background_task 开始 | task=%s", task_id)

        # 从 services / 注入参数获取 WS 上下文
        _notifier = self._services.get("ws_interaction_notifier")
        if not _notifier:
            try:
                from ws_handler import ws_interaction_notifier as _global_notifier
                _notifier = _global_notifier
            except Exception:
                pass
        _ws_thread_id = ""
        _parent_pipeline_id = task_data.get("pipeline_id", "")

        # 尝试从注册表获取当前管道的 thread_id
        if _parent_pipeline_id:
            from pipeline.registry import get_engine_registry
            _entry = get_engine_registry().get(_parent_pipeline_id)
            if _entry:
                _ws_thread_id = _entry.thread_id or ""

        target_id = task_data.get("target_id", "")
        task_service = self._task_service

        # ── 0. 容器任务处理 ──
        if task_service:
            task = task_service.get_task(task_id)
            if task is not None and task.metadata.get("task_scope") == "container":
                logger.info("TaskWorker: 跳过容器任务 %s", task_id)
                await self._handle_container_task(task_id, task, task_data, task_service)
                return

        # ── 1. 加载 AgentConfig ──
        agent_config = await self._load_agent_config(task_id, target_id, task_service)
        if agent_config is None:
            return

        # ── 2. 启动任务 (pending → running) ──
        if task_service:
            try:
                current_task = task_service.get_task(task_id)
                if current_task and current_task.status.value == "running":
                    logger.info("TaskWorker: task %s already running, skip start", task_id)
                else:
                    # BUG-FIX-fix_20260512_async_compat: start_task 现在是 async
                    await task_service.start_task(task_id)
                    logger.info("TaskWorker: task %s started", task_id)
            except Exception as e:
                logger.error("TaskWorker: failed to start task %s: %s", task_id, e)
                # BUG-FIX-fix_20260512_async_compat: fail_task 现在是 async
                await task_service.fail_task(task_id, f"启动失败: {e}")
                return

        # ── 3. 构建完整的 user_input ──
        # BUG-FIX-fix_20260601_retry_lifecycle:
        # lifecycle 在 ctx.full_input 分支（retry _prepared_context）中未定义，
        # 但被 _cleanup_after_engine 闭包引用。移到 if/else 外保证始终可用。
        lifecycle: WorkspaceLifecycleManager | None = self._services.get("workspace_lifecycle_manager")
        ws_meta: dict[str, Any] = ctx.ws_meta or {}
        if ctx.full_input:
            workspace = ctx.workspace
            full_input = ctx.full_input
            # BUG-FIX-fix_20260530_description_lost: 诊断日志
            logger.info(
                "TaskWorker: full_input 来自 _prepared_context | task=%s | input_len=%d",
                task_id, len(full_input),
            )
        else:
            user_input = task_data.get("user_input", "")
            description = task_data.get("description", "")
            # BUG-FIX-fix_20260530_description_lost: 诊断日志
            logger.info(
                "TaskWorker: 构建 full_input | task=%s | user_input_len=%d | desc_len=%d | has_desc=%s",
                task_id, len(user_input), len(description), bool(description),
            )
            acceptance_criteria = task_data.get("acceptance_criteria", {})
            explicit_workspace = task_data.get("workspace") or None
            workspace = self._resolve_task_workspace(task_id, explicit_workspace)
            task_data["_has_explicit_workspace"] = bool(explicit_workspace)

            # HOST模式支持：将 isolation_level 传递给 lifecycle，用于工作空间创建决策
            task_obj = self._task_service.get_task(task_id) if self._task_service else None

            # ── 注入隔离模式配置 ──
            # BUG-FIX-fix_20260519_container_workspace_path:
            # 优先使用 LLM 传入的 isolation_level，没有才用配置文件
            task_data["isolation_mode"] = self._resolve_isolation_mode(task_data, task_obj)

            if task_obj and task_obj.metadata:
                iso_level = task_obj.metadata.get("isolation_level")
                if iso_level:
                    task_data["isolation_level"] = iso_level

            # ── 3.x.0 等待父容器工作空间就绪（解决竞态条件） ──
            await self._wait_for_parent_container(task_id, task_service)

            # ── 3.x 生命周期钩子：任务启动 + 工作空间状态注入 ──
            lifecycle: WorkspaceLifecycleManager | None = self._services.get("workspace_lifecycle_manager")
            ws_meta = {}
            if lifecycle:
                try:
                    # BUG-FIX-fix_20260529_on_task_start_blocks_eventloop:
                    # 问题根因: on_task_start 是同步方法，内部执行多个 subprocess.run（git 命令），
                    #           在 asyncio 事件循环中直接调用会冻结事件循环约 18 秒，
                    #           导致 drain_loop 无法推送流式事件到前端、心跳保活无法发送。
                    # 修复方案: 使用 loop.run_in_executor 将同步的 on_task_start 移到线程池执行，
                    #           不阻塞事件循环，前端的 drain_loop、心跳保活等异步任务正常运行。
                    loop = asyncio.get_running_loop()
                    ws_meta = await loop.run_in_executor(
                        None, lifecycle.on_task_start, task_id, workspace, task_data
                    )
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
                        # BUG-FIX-fix_20260512_async_compat: fail_task 现在是 async
                        await task_service.fail_task(task_id, f"工作空间初始化失败: {e}")
                    return

            # ── 3.x 构建完整输入 ──
            full_input = await self._build_full_task_input(
                task_id=task_id,
                task_data=task_data,
                workspace=workspace,
                ws_meta=ws_meta,
                acceptance_criteria=acceptance_criteria,
                explicit_workspace=explicit_workspace or "",
                task_service=task_service,
            )

        # ── 4.5 检查是否已有 pipeline_run_id（重试时复用） ──
        existing_pipeline_id = None
        if task_service:
            _task_for_id = task_service.get_task(task_id)
            if _task_for_id and _task_for_id.pipeline_run_id:
                existing_pipeline_id = _task_for_id.pipeline_run_id

        # ── 5. 注册管道 + 发送任务输入 ──
        timer_manager = self._services.get("timer_manager")
        try:
            from pipeline.registry import get_engine_registry

            _registry = get_engine_registry()

            _reg_result = _registry.register_pipeline(
                pipeline_id=existing_pipeline_id or "",
                thread_id=_ws_thread_id or "",
                tags={"mode": "interactive", "task_id": task_id, "parent_pipeline": _parent_pipeline_id or ""},
                input_route_table=self._input_route_table,
                output_route_table=self._output_route_table,
                plugin_registry=self._plugin_registry,
                services=self._services,
            )
            if not _reg_result:
                logger.error("TaskWorker: 管道注册失败 task=%s", task_id)
                return

            engine = _reg_result.engine
            pipeline_id = engine.pipeline_id

            await self._bind_pipeline_run(task_id, pipeline_id, task_service, _ws_thread_id)
            await self._send_sub_agent_created_event(task_id, target_id, pipeline_id, task_data)

            ctx.idle_timer_registered = await self._register_idle_timer(
                task_id, timer_manager, task_service, ctx,
            )
            if not ctx.idle_timer_registered and timer_manager:
                return

            conversation_history = await self._restore_conversation_history(existing_pipeline_id)

            from pipeline.message_bus import send_pipeline_message
            from pipeline.stream_bridge import TargetedSink

            _sink = TargetedSink(_notifier, _ws_thread_id) if _notifier else None

            # ── 启动管道引擎（独立线程，fire-and-forget）──
            # BUG-FIX-fix_20260601_isolated_engine:
            # 问题根因: asyncio.create_task(engine.run(...)) 将引擎绑在调用方事件循环上。
            #   _wait_for_engine_completion 轮询阻塞调用方协程，形成紧密耦合。
            #   父管道引擎被杀 → 子管道失去 thread_id → sink dead → 全部终止。
            # 修复方案: engine.run() 通过 run_in_executor 跑在独立线程+独立事件循环。
            #   调用方不等待、不阻塞，通过 Future 回调处理清理。
            #   PipelineEngine 自行管理注册/注销（_cleanup_run_loop），
            #   生命周期与 TaskWorker 完全解耦。
            _main_loop = asyncio.get_running_loop()

            def _on_engine_done(future: concurrent.futures.Future) -> None:
                """引擎完成后的清理回调（运行在 executor 线程）。"""
                try:
                    future.result()
                except Exception as exc:
                    logger.error(
                        "TaskWorker: 独立引擎异常 | task=%s | error=%s", task_id, exc,
                    )
                # 调度清理到主事件循环
                _main_loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(
                        _cleanup_after_engine(task_id, ctx, timer_manager, task_service, lifecycle, workspace, ws_meta, engine)
                    )
                )

            async def _cleanup_after_engine(
                _task_id: str,
                _ctx: TaskExecutionContext,
                _timer_mgr: Any,
                _task_svc: Any,
                _lifecycle: Any,
                _ws: str,
                _ws_meta: dict[str, Any],
                _engine_ref: Any,  # PipelineEngine
            ) -> None:
                """引擎结束后统一清理：检查终态 + 等待terminal + 清理上下文。"""
                try:
                    # 获取管道最终状态
                    _pipeline_state = getattr(_engine_ref, '_last_state', None)
                    # 检查管道退出后任务状态（转为 evaluating 或标记 failed）
                    await self._check_post_pipeline_state(
                        _task_id, _task_svc, _pipeline_state, _lifecycle, _ws, _ws_meta,
                        _ctx, _timer_mgr,
                    )
                    # 等待任务达到终态
                    terminal_wait_timeout = self._config.get("terminal_wait_timeout", 600)
                    try:
                        await asyncio.wait_for(_ctx.terminal_event.wait(), timeout=terminal_wait_timeout)
                        logger.info("TaskWorker: task %s reached terminal state", _task_id)
                    except asyncio.TimeoutError:
                        logger.warning("TaskWorker: task %s timed out waiting for terminal state", _task_id)
                except Exception as _cleanup_exc:
                    logger.error("TaskWorker: post-pipeline cleanup error | task=%s | error=%s", _task_id, _cleanup_exc)
                finally:
                    _ctx.suspended_engine = None
                    _ctx.active = False
                    _ctx.cleanup(_timer_mgr)
                    if _lifecycle and _ws_meta:
                        try:
                            _lifecycle.on_task_completed(_ws, _ws_meta)
                        except Exception as _hook_exc:
                            logger.debug("lifecycle on_task_completed: %s", _hook_exc)
                    # 从 TaskWorker 的 _contexts 中移除（fire-and-forget 不再由 _run_and_cleanup 负责）
                    self._contexts.pop(_task_id, None)
                    logger.info("TaskWorker: pipeline done | task=%s", _task_id)

            if conversation_history:
                logger.info(
                    "TaskWorker: 从历史恢复启动管道（独立线程）| task=%s | history_len=%d | pipeline=%s",
                    task_id, len(conversation_history), pipeline_id[:12],
                )
                engine_future = _main_loop.run_in_executor(
                    None,
                    functools.partial(
                        _run_engine_isolated, engine,
                        user_input="",
                        agent_config=agent_config,
                        conversation_history=conversation_history,
                        task_id=task_id,
                        workspace=workspace,
                        streaming=True,
                        on_chunk=_sink.on_chunk if _sink else lambda chunk: None,
                    ),
                )
                engine_future.add_done_callback(_on_engine_done)

                # 启动 drain_loop 消费流式输出（主事件循环）
                from pipeline.message_bus import _start_bg_drain
                _start_bg_drain(pipeline_id, _sink, engine, engine_task=engine_future)
                # engine_future (concurrent.futures.Future) 有 .done() 方法，
                # drain_loop 通过它判断引擎是否结束，避免死循环空转
                from pipeline.registry import get_engine_registry
                _entry = get_engine_registry().get(pipeline_id)
                if _entry:
                    _entry.engine_task = engine_future
            else:
                # 无历史记录: 正常发送消息启动管道
                if not full_input or not full_input.strip():
                    logger.error(
                        "TaskWorker: 拒绝发送空消息，任务终止 | task=%s",
                        task_id,
                    )
                    if task_service:
                        await task_service.fail_task(task_id, "消息内容为空，无法启动管道")
                    return

                _msg_result = await send_pipeline_message(
                    pipeline_id, full_input,
                    output_sink=_sink,
                    agent_config=agent_config,
                    conversation_history=conversation_history,
                    task_id=task_id,
                    workspace=workspace,
                    streaming=True,
                    thread_id=_ws_thread_id or "",
                )

                if not _msg_result.success:
                    logger.error("TaskWorker: 消息注入失败 task=%s error=%s", task_id, _msg_result.error)
                    return

                # send_pipeline_message 已在内部启动了引擎（asyncio.Task on main loop）
                # 从 registry 获取 engine_task 并注册完成回调
                _entry = get_engine_registry().get(pipeline_id)
                if _entry and _entry.engine_task is not None:
                    _entry.engine_task.add_done_callback(
                        lambda t: asyncio.ensure_future(
                            _cleanup_after_engine(task_id, ctx, timer_manager, task_service, lifecycle, workspace, ws_meta, engine)
                        )
                    )
                else:
                    await _cleanup_after_engine(task_id, ctx, timer_manager, task_service, lifecycle, workspace, ws_meta, engine)

            # fire-and-forget: 不阻塞等待引擎完成
            # ctx 和 _contexts 由 _on_engine_done 回调负责清理

        except asyncio.CancelledError:
            logger.info("TaskWorker: task %s cancelled", task_id)
            ctx.cleanup(timer_manager)
            raise

        except Exception as exc:
            logger.error("TaskWorker: pipeline failed for task %s: %s", task_id, exc)
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
                    await task_service.fail_task(task_id, str(exc))
                except Exception as fail_exc:
                    logger.error("TaskWorker: fail_task also failed: %s", fail_exc)
            ctx.set_terminal()
            ctx.cleanup(timer_manager)
            return

        # fire-and-forget: 引擎在独立线程中运行，不阻塞等待
        # 管道完成后的 _check_post_pipeline_state + terminal_event.wait()
        # 已移至 _cleanup_after_engine 回调，由 engine_future.add_done_callback 触发

    # ───────────────────────────────────────────────────────────────────
    # _execute_background_task 的辅助方法
    # ───────────────────────────────────────────────────────────────────

    async def _handle_container_task(
        self, task_id: str, task: Any, task_data: dict, task_service: Any,
    ) -> None:
        """处理容器任务：初始化容器工作空间后跳过管道执行。"""
        lifecycle: WorkspaceLifecycleManager | None = (
            self._services.get("workspace_lifecycle_manager")
        )
        if not lifecycle:
            logger.error(
                "TaskWorker: WorkspaceLifecycleManager 不可用，无法初始化容器空间: task_id=%s",
                task_id,
            )
            # BUG-FIX-fix_20260512_async_compat: fail_task 现在是 async
            await task_service.fail_task(task_id, "容器空间初始化失败：WorkspaceLifecycleManager 不可用")
            return

        _CONTAINER_INIT_RETRIES = 3
        _init_ok = False
        _last_err: Exception | None = None
        for _attempt in range(1, _CONTAINER_INIT_RETRIES + 1):
            try:
                # BUG-FIX-fix_20260518_container_ws_override:
                # 问题根因: 容器任务读取自身 metadata 中的 workspace 覆盖子任务指定的值，
                #          导致子任务在空目录执行。
                # 修复方案: 优先使用 task_data 中子任务显式指定的 workspace。
                explicit_ws = task_data.get("workspace") or None
                container_ws = explicit_ws or task.metadata.get("workspace") or None
                if "isolation_mode" not in task_data:
                    task_data["isolation_mode"] = self._resolve_isolation_mode(task_data, task)
                lifecycle.init_container_workspace(task_id, container_ws, task_data)
                container_workspace_path = lifecycle._ws_meta_store.get(task_id, {}).get("path", "")
                if container_workspace_path:
                    task.metadata["container_workspace"] = container_workspace_path
                    ws_meta = lifecycle._ws_meta_store.get(task_id)
                    if ws_meta:
                        task.metadata["ws_meta"] = ws_meta
                    await self._task_service.save_task(task)
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
            # BUG-FIX-fix_20260512_async_compat: fail_task 现在是 async
            await task_service.fail_task(
                task_id,
                f"容器空间初始化失败（{_CONTAINER_INIT_RETRIES} 次重试耗尽）：{_last_err}",
            )

    async def _load_agent_config(
        self, task_id: str, target_id: str, task_service: Any,
    ) -> Any | None:
        """加载 AgentConfig，失败时标记任务失败并返回 None。"""
        if not target_id:
            logger.error("TaskWorker: task %s has no target_id, failing", task_id)
            if task_service:
                # BUG-FIX-fix_20260512_async_compat: fail_task 现在是 async
                await task_service.fail_task(
                    task_id,
                    "任务缺少 target_id（目标 Agent），无法执行。"
                    "请检查 task_submit 是否正确指定了 target_id。",
                )
            return None

        agent_registry = self._services.get("agent_registry")
        logger.info(
            "TaskWorker: _load_agent_config | task=%s, target=%s, registry=%s, keys=%s",
            task_id, target_id,
            type(agent_registry).__name__ if agent_registry else "None",
            list(self._services.keys())[:10] if self._services else "empty",
        )
        if not agent_registry:
            logger.error("TaskWorker: agent_registry not found in services!")
            return None

        agent_config = agent_registry.get(target_id)
        if agent_config is None:
            logger.error(
                "TaskWorker: agent '%s' not found in registry, failing task %s",
                target_id, task_id,
            )
            if task_service:
                # BUG-FIX-fix_20260512_async_compat: fail_task 现在是 async
                await task_service.fail_task(
                    task_id,
                    f"目标 Agent '{target_id}' 未在系统中注册，无法执行任务。"
                    f"请检查 task_submit 的 target_id 是否正确。",
                )
            return None

        return agent_config

    async def _wait_for_parent_container(
        self, task_id: str, task_service: Any,
    ) -> None:
        """等待父容器工作空间就绪（最多 30s）。

        BUG-FIX-fix_20260425_container_workspace_race:
        解决容器任务和子任务并行执行时的竞态条件。
        """
        if not task_service:
            return
        _t = task_service.get_task(task_id)
        if not (_t and _t.parent_task_id):
            return
        _parent = task_service.get_task(_t.parent_task_id)
        if not (_parent and _parent.metadata.get("task_scope") == "container"):
            return

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
                return
            await asyncio.sleep(_WAIT_INTERVAL)
            _waited += _WAIT_INTERVAL
        logger.warning(
            "TaskWorker: 等待父容器工作空间超时(%.1fs): parent=%s, 继续执行",
            _waited, _t.parent_task_id,
        )

    async def _bind_pipeline_run(
        self, task_id: str, pipeline_id: str, task_service: Any,
        thread_id: str = "",
    ) -> None:
        """早期绑定 pipeline_run_id 到任务，并注册到 api_store 的会话映射。

        BUG-FIX-fix_20260417_task_manage_records:
        管道启动前立即绑定，确保运行中查询执行记录不为空。

        BUG-FIX-fix_20260603_api_store_pipeline_mapping:
        问题根因: 子任务的 pipeline_run_id 未写入 api_store 的 session.pipeline_ids，
                  导致删除会话时无法通过 pipeline_ids 直接找到所有子管道。
        修复方案: 在绑定管道时，将 pipeline_id 注册到 MemoryStore 对应会话的 pipeline_ids。
        """
        if not task_service:
            return
        try:
            # BUG-FIX-fix_20260512_async_compat: bind_pipeline_run 现在是 async
            await task_service.bind_pipeline_run(task_id, pipeline_id)
            logger.info(
                "TaskWorker: bound task %s to pipeline_run %s (early binding)",
                task_id, pipeline_id,
            )
            # 按根任务分组执行记录
            exec_storage = self._services.get("execution_record_storage")
            if exec_storage:
                root_id = task_service.get_root_task_id(task_id)
                if root_id:
                    exec_storage.register_pipeline(pipeline_id, root_id)

            # BUG-FIX-fix_20260603_api_store_pipeline_mapping:
            # 将子管道 ID 注册到 api_store 的 session.pipeline_ids，
            # 使 store.json 的 pipeline_ids 包含所有子管道，便于级联清理。
            if thread_id:
                try:
                    from channels.api.memory_store import store as api_store
                    session = api_store.get_session(thread_id)
                    if session:
                        session.register_pipeline(pipeline_id)
                        api_store.set_session(thread_id, session)
                        logger.info(
                            "TaskWorker: registered sub-pipeline %s to api_store session %s",
                            pipeline_id, thread_id,
                        )
                except Exception as reg_exc:
                    logger.warning(
                        "TaskWorker: failed to register sub-pipeline to api_store: %s", reg_exc,
                    )
        except Exception as exc:
            logger.warning(
                "TaskWorker: early bind_pipeline_run failed for %s: %s",
                task_id, exc,
            )

    async def _register_idle_timer(
        self,
        task_id: str,
        timer_manager: Any,
        task_service: Any,
        ctx: TaskExecutionContext,
    ) -> bool:
        """注册 idle 计时器（任务启动阶段调用）。

        底层调用 TaskIdleTimerMixin._arm_idle_timer 完成统一的
        cancel+create 流程；注册失败会直接 fail_task 并标记终态，
        避免管道在无 idle 监控的情况下"裸跑"。

        Args:
            task_id: 任务 ID
            timer_manager: 计时器管理器实例（可为 None）
            task_service: 任务服务实例，用于失败时回滚
            ctx: 当前任务执行上下文

        Returns:
            True=成功注册或无需注册；False=注册失败（任务已 fail）
        """
        if not timer_manager:
            return True
        try:
            await self._arm_idle_timer(task_id, timer_manager)
            logger.info(
                "TaskWorker: idle 计时器已注册: task_id=%s, timeout=%ds",
                task_id, timer_manager.idle_threshold,
            )
            return True
        except Exception as e:
            logger.error(
                "TaskWorker: 注册 idle 计时器失败，任务拒绝执行: task_id=%s, error=%s",
                task_id, e,
            )
            if task_service:
                await task_service.fail_task(
                    task_id,
                    f"idle计时器初始化失败，任务拒绝执行: {e}",
                )
            ctx.set_terminal()
            return False

    def _compute_pipeline_timeout(self, agent_config: Any) -> float:
        """计算管道执行超时时间（秒）。"""
        pipeline_timeout = float(self._config.get("pipeline_timeout", 1800))
        # Agent-level timeout override: respect agent's own timeout_seconds setting
        if (
            agent_config
            and hasattr(agent_config, "timeout_seconds")
            and agent_config.timeout_seconds > 0
        ):
            pipeline_timeout = max(pipeline_timeout, float(agent_config.timeout_seconds))
        return pipeline_timeout

    async def _restore_conversation_history(
        self, existing_pipeline_id: str | None,
    ) -> list[dict[str, Any]] | None:
        """重试时从执行记录恢复对话历史。"""
        if not existing_pipeline_id:
            return None
        exec_storage = self._services.get("execution_record_storage")
        if not exec_storage:
            return None
        try:
            prev_records = exec_storage.list_by_pipeline(existing_pipeline_id)[0]
            if not prev_records:
                return None
            conversation_history: list[dict[str, Any]] = []
            # BUG-FIX-fix_20260530_role_mapping: 基于 record.type 映射 role，
            # 避免 role 为空字符串时 assistant 消息被错误标记为 user
            _type_to_role = {"user": "user", "ai": "assistant", "tool": "tool", "system": "system"}
            for r in prev_records:
                role = r.role or _type_to_role.get(r.type, "user")
                msg: dict[str, Any] = {"role": role, "content": r.content}
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
                "for task (retry, pipeline=%s)",
                len(conversation_history), existing_pipeline_id,
            )
            return conversation_history
        except Exception as exc:
            logger.warning(
                "TaskWorker: failed to restore pipeline history: %s", exc,
            )
            return None

    # ───────────────────────────────────────────────────────────────────
    # 管道取消
    # ───────────────────────────────────────────────────────────────────

    def cancel_pipeline(self, task_id: str) -> bool:
        """取消任务关联的运行中管道。

        由 task_manage cancel 操作调用，强制停止 PipelineEngine。
        通过 EngineRegistry 查找引擎并唤醒，再取消 asyncio.Task。

        Args:
            task_id: 要取消的任务 ID

        Returns:
            是否成功发起取消（无运行中管道时返回 False）
        """
        # BUG-FIX-fix_20260524_cancel_container_pipeline:
        # 问题根因: 容器任务的 pipeline_run_id 是父管道的 ID，
        #           cancel_pipeline(container_task_id) 会错误地注销父管道引擎。
        # 修复方案: 容器任务没有自己的管道引擎，跳过 pipeline_id 查找和引擎注销，
        #           直接进入 context/bg_task 取消逻辑。
        # 影响范围: 容器任务取消流程。
        # 修复日期: 2026-05-24
        pipeline_id = None
        is_container = False
        if self._task_service:
            try:
                task = self._task_service.get_task(task_id)
                if task:
                    is_container = task.metadata.get("task_scope") == "container"
                    if not is_container:
                        pipeline_id = getattr(task, "pipeline_run_id", None)
            except Exception:
                logger.warning("TaskWorker: cancel_pipeline 获取 pipeline_id 失败: task_id=%s", task_id, exc_info=True)

        if pipeline_id:
            from pipeline.registry import get_engine_registry
            registry = get_engine_registry()
            entry = registry.get(pipeline_id)
            if entry is not None and entry.engine is not None:
                try:
                    entry.engine.wake()
                except Exception:
                    pass
            registry.unregister(pipeline_id)

        ctx = self._contexts.get(task_id)
        if ctx:
            ctx.suspended_engine = None
            ctx.wake_event.set()
            ctx.active = False
            ctx.set_terminal()
            bg_task = ctx.bg_task
        else:
            bg_task = None
        self._cancel_idle_timer_async(task_id)

        cancelled_any = False
        if bg_task is not None and not bg_task.done():
            bg_task.cancel()
            cancelled_any = True

        if not cancelled_any:
            from pipeline.registry import get_engine_registry as _get_reg
            entries = _get_reg().find_by_tag("task_id", task_id)
            for _e in entries:
                try:
                    _e.engine.wake()
                except Exception:
                    pass
                if not cancelled_any:
                    cancelled_any = True

        logger.info(
            "TaskWorker.cancel_pipeline: task=%s pipeline=%s cancelled=%s",
            task_id, pipeline_id[:12] if pipeline_id else "none", cancelled_any,
        )
        return cancelled_any

    # ───────────────────────────────────────────────────────────────────
    # 工作空间解析
    # ───────────────────────────────────────────────────────────────────

    def _resolve_task_workspace(
        self, task_id: str, task_workspace: str | None = None,
    ) -> str:
        """根据任务层级关系解析工作空间路径。

        子任务继承父任务的工作空间，形成嵌套目录结构：
        - 根任务: .ai_workspaces/{task_id}
        - 子任务: {parent_resolved_workspace}/{task_id}

        BUG-FIX-fix_20260419_workspace_inherit:
        问题根因: TaskWorker 为每个子任务创建平级工作空间 .ai_workspaces/{task_id}，
                 忽略了父任务的工作空间，导致子任务无法共享父任务的文件产出。
        修复方案: 沿 parent_task_id 链向上追溯，从根任务向下逐层解析工作空间路径，
                 确保子任务嵌套在父任务工作空间下。

        Args:
            task_id: 当前任务 ID
            task_workspace: 任务显式指定的 workspace

        Returns:
            解析后的工作空间路径字符串
        """
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
                # 根任务：无父空间，直接解析
                resolved = resolve_workspace(tid, tws, config_root=root)
            elif tid == task_id:
                # 当前任务：使用调用方传入的 task_workspace 参数
                resolved = resolve_workspace(tid, task_workspace, parent_resolved_workspace=resolved)
            else:
                # 中间祖先：使用其 metadata 中存储的 workspace 值
                resolved = resolve_workspace(tid, tws, parent_resolved_workspace=resolved)

        return resolved or f"{root}/{task_id}"

    # ───────────────────────────────────────────────────────────────────
    # 容器过期检查（已禁用）
    # ───────────────────────────────────────────────────────────────────

    async def _check_stale_containers(self) -> None:
        """容器终态检查（已禁用超时自动判定）。

        容器的完成/失败由主 Agent 通过 complete_container / fail_container 决定，
        系统不做自动判定。
        """
        return
