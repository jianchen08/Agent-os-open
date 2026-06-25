"""后台任务执行 Mixin。



负责后台任务的完整执行生命周期：PipelineEngine 创建、执行、

挂起/恢复、超时处理、管道取消等。



从 task_worker.py 拆分而出，降低原文件复杂度。

"""



from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import functools  # noqa: F401
import json
import logging
import os
import uuid as _uuid  # noqa: F401
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from infrastructure.protocols import MemoryStoreProtocol



from infrastructure.task_context import TaskExecutionContext
from isolation.workspace_lifecycle import WorkspaceLifecycleManager
from pipeline.stream_bridge import PipelineStreamBridge, TargetedSink  # noqa: F401

logger = logging.getLogger(__name__)





# _run_engine_isolated 已删除：engine.run() 作为 asyncio.Task 在主事件循环中运行





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

            from config.config_center import get_config_center  # noqa: PLC0415

            _iso_cfg = get_config_center().get("isolation/isolation_config.yaml") or {}

            return _iso_cfg.get("coordinator", {}).get("default_level", "")

        except Exception:

            logger.debug("读取 isolation_config 失败（非致命）")

        return ""



    async def _execute_background_task(self, task_data: dict[str, Any], ctx: TaskExecutionContext) -> None:  # noqa: PLR0911,PLR0912,PLR0915

        """执行后台任务的完整生命周期（start → run pipeline → wait terminal）。



        Args:

            task_data: 任务提交事件中的数据字典

            ctx: 任务执行上下文

        """



        task_id = task_data.get("task_id", "unknown")

        logger.debug("TaskWorker: _execute_background_task 开始 | task=%s", task_id)



        # 从 services / 注入参数获取 WS 上下文

        _notifier = self._services.get("ws_interaction_notifier")

        if not _notifier:

            try:

                from channels.websocket.ws_handler import ws_interaction_notifier as _global_notifier  # noqa: PLC0415

                _notifier = _global_notifier

            except Exception:

                logger.debug("ws_interaction_notifier 全局单例不可用（非致命）")

        _ws_thread_id = ""

        _parent_pipeline_id = task_data.get("pipeline_id", "")



        # 尝试从注册表获取当前管道的 thread_id

        if _parent_pipeline_id:

            from pipeline.registry import get_engine_registry  # noqa: PLC0415

            _entry = get_engine_registry().get(_parent_pipeline_id)

            if _entry:

                _ws_thread_id = _entry.thread_id or ""



        target_id = task_data.get("target_id", "")

        task_service = self._task_service



        # 从 task.metadata 提取上下文身份（user_id / session_id），播种到管道 state 与

        # registry tags。确保子任务 task_submit 能继承并写入自身 metadata，

        # task_status_update / task_status_changed 推送链路不再因身份缺失而静默。

        _ctx_user_id = ""

        _ctx_session_id = ""

        if task_service:

            _ctx_task = task_service.get_task(task_id)

            if _ctx_task and _ctx_task.metadata:

                _ctx_user_id = _ctx_task.metadata.get("user_id", "") or ""

                _ctx_session_id = _ctx_task.metadata.get("session_id", "") or ""

        # session_id 回退到当前管道注册时记录的 WS thread_id（权威投递目标）

        if not _ctx_session_id:

            _ctx_session_id = _ws_thread_id

        if not _ctx_user_id:

            logger.error(

                "TaskWorker: task metadata 缺 user_id，管道 state 无法播种用户身份 | task=%s",

                task_id,

            )



        # ── 0. 容器任务处理 ──

        if task_service:

            task = task_service.get_task(task_id)

            if task is not None and task.metadata.get("task_scope") == "container":

                logger.debug("TaskWorker: 跳过容器任务 %s", task_id)

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

                    logger.debug("TaskWorker: task %s already running, skip start", task_id)

                else:

                    await task_service.start_task(task_id)

                    logger.debug("TaskWorker: task %s started", task_id)

            except Exception as e:

                logger.error("TaskWorker: failed to start task %s: %s", task_id, e)

                await task_service.fail_task(task_id, f"启动失败: {e}")

                return



        # ── 3. 构建完整的 user_input ──

        lifecycle: WorkspaceLifecycleManager | None = self._services.get("workspace_lifecycle_manager")

        ws_meta: dict[str, Any] = ctx.ws_meta or {}

        if ctx.full_input:

            workspace = ctx.workspace

            full_input = ctx.full_input

            logger.debug(

                "TaskWorker: full_input 来自 _prepared_context | task=%s | input_len=%d",

                task_id, len(full_input),

            )

        else:

            user_input = task_data.get("user_input", "")

            description = task_data.get("description", "")

            logger.debug(

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

            # 优先使用 LLM 传入的 isolation_level

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

            # 工作空间已在 task_submit 阶段完成初始化（on_task_start/init_container_workspace），

            # ws_meta 已写入 task.metadata。此处优先复用，仅在异常情况下兜底重建。

            _task_for_ws = task_service.get_task(task_id) if task_service else None

            if _task_for_ws and _task_for_ws.metadata:

                _existing_ws_meta = _task_for_ws.metadata.get("ws_meta")

                if isinstance(_existing_ws_meta, dict) and _existing_ws_meta.get("path"):

                    ws_meta = _existing_ws_meta

                    workspace = ws_meta.get("path", workspace)

                    logger.debug(

                        "TaskWorker: 复用 task_submit 阶段初始化的工作空间: task_id=%s, mode=%s, path=%s",

                        task_id, ws_meta.get("mode"), ws_meta.get("path"),

                    )

            if not ws_meta and lifecycle:

                try:

                    # on_task_start 通过 run_in_executor 在线程池执行，避免阻塞事件循环

                    loop = asyncio.get_running_loop()

                    ws_meta = await loop.run_in_executor(

                        None, lifecycle.on_task_start, task_id, workspace, task_data

                    )

                    workspace = ws_meta.get("path", workspace)

                    logger.warning(

                        "TaskWorker: ws_meta 缺失，兜底重建工作空间: task_id=%s, mode=%s",

                        task_id, ws_meta.get("mode"),

                    )

                except Exception as e:

                    # 容器子任务找不到容器工作空间等致命错误应直接 fail_task

                    logger.error(

                        "TaskWorker: lifecycle on_task_start failed: task_id=%s, error=%s",

                        task_id, e,

                    )

                    if task_service:

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



        # pipe 继承：从源任务的 pipeline 恢复对话历史

        _inherit_pipe_pipeline_id = task_data.get("_inherit_pipe_pipeline_id")

        # task_submit 预生成的 pipeline_id（pipe 继承时已同步 clone 好历史到该管道）。
        # 引擎复用它，避免重复 clone；None 表示非继承或 task_submit 未预生成。
        _pre_pipeline_id = task_data.get("_pre_pipeline_id")



        # ── 5. 注册管道 + 发送任务输入 ──

        timer_manager = self._services.get("timer_manager")

        try:

            from pipeline.registry import get_engine_registry  # noqa: PLC0415



            _registry = get_engine_registry()



            _reg_result = _registry.register_pipeline(

                pipeline_id=existing_pipeline_id or _pre_pipeline_id or "",

                thread_id=_ws_thread_id or "",

                tags={

                    "mode": "interactive",

                    "task_id": task_id,

                    "workspace": workspace,

                    "parent_pipeline": _parent_pipeline_id or "",

                    # agent_id 从任务数据（target_id）绑定，供引擎重建时直接读取

                    "agent_id": target_id or "",

                    # user_id / session_id：上下文身份，供 _start_idle_engine 恢复并播种 state

                    "user_id": _ctx_user_id,

                    "session_id": _ctx_session_id,

                },

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



            # 记录引擎实例 id，供 cleanup 回调判断是否仍是同一引擎

            ctx._active_engine_id = id(engine)



            await self._bind_pipeline_run(task_id, pipeline_id, task_service, _ws_thread_id)

            await self._send_sub_agent_created_event(task_id, target_id, pipeline_id, task_data)



            ctx.idle_timer_registered = await self._register_idle_timer(

                task_id, timer_manager, task_service, ctx,

            )

            if not ctx.idle_timer_registered and timer_manager:

                return



            # pipe 继承：物理拷贝源管道 records → 引擎自加载（和重试同路）

            if _inherit_pipe_pipeline_id and not existing_pipeline_id:

                # 历史已由 task_submit 同步 clone 到 _pre_pipeline_id（=pipeline_id），
                # 引擎经 resolve_conversation_history 从存储自加载，无需再 clone。

                conversation_history = None

            else:

                conversation_history = await self._restore_conversation_history(existing_pipeline_id)



            from pipeline.message_bus import send_pipeline_message  # noqa: PLC0415
            from pipeline.stream_bridge import create_targeted_sink  # noqa: PLC0415



            _sink = create_targeted_sink(_notifier, _ws_thread_id)



            # ── 启动管道引擎（fire-and-forget）──

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

                    _pipeline_state = getattr(_engine_ref, 'last_state', None)



                    # 比较引擎实例 id，避免旧引擎 cleanup 误标记新管道任务

                    _skip_state_check = False

                    _active_engine_id = getattr(_ctx, '_active_engine_id', None)

                    if _active_engine_id is not None and _active_engine_id != id(_engine_ref):

                        logger.warning(

                            "TaskWorker: 跳过旧引擎回调（引擎已被替换）"

                            "| task=%s | old_engine_id=%d | active_engine_id=%d",

                            _task_id, id(_engine_ref), _active_engine_id,

                        )

                        _skip_state_check = True



                    if not _skip_state_check:

                        # 检查管道退出后任务状态（转为 evaluating 或标记 failed）

                        await self._check_post_pipeline_state(

                            _task_id, _task_svc, _pipeline_state, _lifecycle, _ws, _ws_meta,

                            _ctx, _timer_mgr,

                        )

                    # 等待任务达到终态

                    terminal_wait_timeout = self._config.get("terminal_wait_timeout", 600)

                    try:

                        await asyncio.wait_for(_ctx.terminal_event.wait(), timeout=terminal_wait_timeout)

                        logger.debug("TaskWorker: task %s reached terminal state", _task_id)

                    except asyncio.TimeoutError:

                        logger.warning("TaskWorker: task %s timed out waiting for terminal state", _task_id)

                except Exception as _cleanup_exc:

                    logger.error("TaskWorker: post-pipeline cleanup error | task=%s | error=%s", _task_id, _cleanup_exc)

                finally:

                    _ctx.suspended_engine = None

                    _ctx.active = False

                    _ctx.cleanup(_timer_mgr)

                    # BUG-FIX-fix_20260619_worktree_destroyed_on_retry:

                    # 不在引擎结束时调用 lifecycle.on_task_cleanup。

                    # 该方法会无条件 git worktree remove + 删分支 + 扫删所有 __wt_ 孤儿目录，

                    # 导致任务失败后重试时 worktree 已被销毁、无法复用（重试必然失败）。

                    # worktree 的唯一销毁点应在「评估通过 + 合并完成」后，

                    # 由 on_eval_passed → _cleanup_worktree 负责。

                    # 从 TaskWorker 的 _contexts 中移除（fire-and-forget 不再由 _run_and_cleanup 负责）

                    self._contexts.pop(_task_id, None)

                    logger.debug("TaskWorker: pipeline done | task=%s", _task_id)



            if conversation_history:

                # FILE DIAG branch

                _data_dir = os.environ.get("DATA_DIR", "") or str(Path(__file__).resolve().parents[2] / "data")

                os.makedirs(_data_dir, exist_ok=True)  # noqa: PTH103

                with open(str(Path(_data_dir) / "diag_inherit.log"), "a", encoding="utf-8") as _f:

                    from datetime import datetime  # noqa: PLC0415

                    _f.write(f"{datetime.now().isoformat()} | BRANCH=HISTORY task={task_id} | "

                             f"history_len={len(conversation_history)}\n")

                logger.debug(

                    "TaskWorker: 从历史恢复启动管道（主循环）| task=%s | history_len=%d | pipeline=%s",

                    task_id, len(conversation_history), pipeline_id[:12],

                )

                # engine.run() 作为协程在主事件循环中运行

                engine_future = asyncio.ensure_future(engine.run(

                    user_input="",

                    agent_config=agent_config,

                    conversation_history=conversation_history,

                    task_id=task_id,

                    workspace=workspace,

                    streaming=True,

                    on_chunk=None,

                    user_id=_ctx_user_id,

                    session_id=_ctx_session_id,

                ))

                engine_future.add_done_callback(_on_engine_done)



                # Phase 1: drain_loop 已删除，engine 主动 emit 推送事件。

                # _start_bg_drain 为兼容空实现，保留调用仅为不破坏导入链。

                from pipeline.message_bus import _start_bg_drain  # noqa: PLC0415

                _start_bg_drain(pipeline_id, _sink, engine, engine_task=engine_future)

                # engine_future (concurrent.futures.Future) 有 .done() 方法，

                # drain_loop 通过它判断引擎是否结束，避免死循环空转

                from pipeline.registry import get_engine_registry  # noqa: PLC0415

                _entry = get_engine_registry().get(pipeline_id)

                if _entry:

                    _entry.engine_task = engine_future

            else:

                # FILE DIAG branch

                _data_dir2 = os.environ.get("DATA_DIR", "") or str(Path(__file__).resolve().parents[2] / "data")

                os.makedirs(_data_dir2, exist_ok=True)  # noqa: PTH103

                with open(str(Path(_data_dir2) / "diag_inherit.log"), "a", encoding="utf-8") as _f:

                    from datetime import datetime  # noqa: PLC0415

                    _f.write(f"{datetime.now().isoformat()} | BRANCH=NO_HISTORY task={task_id} | "

                             f"full_input_len={len(full_input) if full_input else 0}\n")

                # 无历史记录: 正常发送消息启动管道

                if not full_input or not full_input.strip():

                    logger.error(

                        "TaskWorker: 拒绝发送空消息，任务终止 | task=%s",

                        task_id,

                    )

                    if task_service:

                        await task_service.fail_task(task_id, "消息内容为空，无法启动管道")

                    return



                from pipeline.message_types import MessageType, PipelineMessage  # noqa: PLC0415

                _pipe_msg = PipelineMessage(

                    type=MessageType.CHAT,

                    content=full_input,

                    pipeline_id=pipeline_id,

                    thread_id=_ws_thread_id or "",

                )

                _msg_result = await send_pipeline_message(

                    _pipe_msg,

                    output_sink=_sink,

                    agent_config=agent_config,

                    conversation_history=conversation_history,

                    task_id=task_id,

                    workspace=workspace,

                )



                if not _msg_result.success:

                    logger.error("TaskWorker: 消息注入失败 task=%s error=%s", task_id, _msg_result.error)

                    return



                # send_pipeline_message 已启动引擎（run_in_executor），引擎自身有完整

                # 的生命周期管理：stop_check、idle timer、_cleanup_run_loop、

                # _mark_task_failed_on_engine_exit。不需要外部注册清理回调。



            # fire-and-forget: 不阻塞等待引擎完成

            # ctx 和 _contexts 由引擎内部机制 + task_worker._run_and_cleanup 兜底清理



        except asyncio.CancelledError:

            logger.debug("TaskWorker: task %s cancelled", task_id)

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

        """处理容器任务：容器工作空间已在 task_submit 阶段初始化，此处仅复用校验。

        若 ws_meta 已存在（正常流程），跳过初始化；仅在异常缺失时兜底重建。
        """

        lifecycle: WorkspaceLifecycleManager | None = (

            self._services.get("workspace_lifecycle_manager")

        )

        # ── 优先复用 task_submit 阶段已写入的 ws_meta ──

        _existing_ws_meta = (task.metadata or {}).get("ws_meta") if task and task.metadata else None

        if isinstance(_existing_ws_meta, dict) and _existing_ws_meta.get("path"):

            logger.debug(

                "TaskWorker: 容器复用 task_submit 阶段初始化的工作空间: task_id=%s, path=%s",

                task_id, _existing_ws_meta.get("path"),

            )

            return

        # ── 防御性兜底：ws_meta 缺失时重建（仅异常情况触发） ──

        if not lifecycle:

            logger.error(

                "TaskWorker: WorkspaceLifecycleManager 不可用，无法初始化容器空间: task_id=%s",

                task_id,

            )

            await task_service.fail_task(task_id, "容器空间初始化失败：WorkspaceLifecycleManager 不可用")

            return



        _CONTAINER_INIT_RETRIES = 3  # noqa: N806

        _init_ok = False

        _last_err: Exception | None = None

        for _attempt in range(1, _CONTAINER_INIT_RETRIES + 1):

            try:

                # 优先使用 task_data 中子任务显式指定的 workspace

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

                    logger.warning(

                        "TaskWorker: 容器 ws_meta 缺失，兜底重建: task_id=%s, workspace=%s (attempt %d)",

                        task_id, container_workspace_path, _attempt,

                    )

                    _init_ok = True

                    break

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

                await task_service.fail_task(

                    task_id,

                    "任务缺少 target_id（目标 Agent），无法执行。"

                    "请检查 task_submit 是否正确指定了 target_id。",

                )

            return None



        agent_registry = self._services.get("agent_registry")

        logger.debug(

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



        _WAIT_INTERVAL = 1.0  # noqa: N806

        _WAIT_MAX = 30.0  # noqa: N806

        _waited = 0.0

        while _waited < _WAIT_MAX:

            _parent_refreshed = task_service.get_task(_t.parent_task_id)

            if _parent_refreshed and _parent_refreshed.metadata.get("container_workspace"):

                logger.debug(

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

            logger.debug(

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

                    _api_store: MemoryStoreProtocol | None = self._services.get("api_store")

                    session = _api_store.get_session(thread_id) if _api_store else None

                    if session:

                        # BUG-FIX-fix_20260622_subpipeline_overwrites_active:
                        # 子管道（L2）注册时绝不能覆盖主管道的 active 指针，
                        # 否则一旦子管道未落盘执行记录，会话历史将整体丢失。
                        session.register_pipeline(pipeline_id, set_active=False)

                        _api_store.set_session(thread_id, session)

                        logger.debug(

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

            logger.debug(

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

                    with contextlib.suppress(json.JSONDecodeError, TypeError):

                        msg["tool_calls"] = json.loads(r.tool_calls_json)

                conversation_history.append(msg)



            # 旧记录没有 tool_calls_json，需要从 tool 记录反向重建

            from infrastructure.task_worker import _reconstruct_tool_calls  # noqa: PLC0415

            _reconstruct_tool_calls(conversation_history)



            logger.debug(

                "TaskWorker: restored %d messages from pipeline records "

                "for task (retry, pipeline=%s)",

                len(conversation_history), existing_pipeline_id,

            )

            return conversation_history

        except Exception as exc:

            _data_dir3 = os.environ.get("DATA_DIR", "") or str(Path(__file__).resolve().parents[2] / "data")

            os.makedirs(_data_dir3, exist_ok=True)  # noqa: PTH103

            with open(str(Path(_data_dir3) / "diag_inherit.log"), "a", encoding="utf-8") as _f:

                from datetime import datetime as _dt  # noqa: PLC0415

                _f.write(f"{_dt.now().isoformat()} | RESTORE_EXCEPTION: {exc} | pid={existing_pipeline_id}\n")

            logger.warning(

                "TaskWorker: failed to restore pipeline history: %s", exc,

            )

            return None



    # ───────────────────────────────────────────────────────────────────

    # 管道取消

    # ───────────────────────────────────────────────────────────────────



    def cancel_pipeline(self, task_id: str) -> bool:  # noqa: PLR0912

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

            from pipeline.registry import get_engine_registry  # noqa: PLC0415

            registry = get_engine_registry()

            entry = registry.get(pipeline_id)

            if entry is not None and entry.engine is not None:

                try:

                    entry.engine.wake()

                except Exception:

                    logger.debug("cancel_pipeline: engine.wake() 失败（非致命）: pipeline=%s", pipeline_id[:12])

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

            from pipeline.registry import get_engine_registry as _get_reg  # noqa: PLC0415

            entries = _get_reg().find_by_tag("task_id", task_id)

            for _e in entries:

                try:

                    _e.engine.wake()

                except Exception:

                    logger.debug("cancel_pipeline: engine.wake() 失败（非致命）: task=%s", task_id[:12])

                if not cancelled_any:

                    cancelled_any = True



        logger.debug(

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

        """根据任务数据解析工作空间路径。



        路径来源优先级：

        1. 任务的 ws_meta（已有工作空间的工作任务）

        2. 调用方显式传入的 task_workspace

        3. 配置的 workspace.root + task_id（新任务）



        所有路径统一使用 isolation_config.yaml 的 workspace.root 配置，

        不存在硬编码的 .ai_workspaces fallback。

        """

        from tasks.workspace import resolve_task_workspace  # noqa: PLC0415



        task = self._task_service.get_task(task_id) if self._task_service else None

        if task is not None:

            ws = resolve_task_workspace(task)

            if ws:

                return ws



        if task_workspace:

            return task_workspace



        # 新任务：从配置读取 workspace.root

        from isolation.workspace import get_workspace_config_root  # noqa: PLC0415

        root = get_workspace_config_root()

        return f"{root}/{task_id}"



    # ───────────────────────────────────────────────────────────────────

    # 容器过期检查（已禁用）

    # ───────────────────────────────────────────────────────────────────



    async def _check_stale_containers(self) -> None:

        """容器终态检查（已禁用超时自动判定）。



        容器的完成/失败由主 Agent 通过 task_manage(action="change", status=...) 决定，

        系统不做自动判定。

        """

        return

