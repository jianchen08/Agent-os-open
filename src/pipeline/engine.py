"""管道引擎 — 核心循环和生命周期管理。

实现核心的 while 循环执行逻辑：
输入路由 → Input 插件链 → Core 插件 → Output 插件链 → 输出路由仲裁 → apply_route，
直到管道结束或挂起。

职责：
- 管道生命周期：run() / resume() / wake()
- 核心循环：_run_loop()
- 挂起/恢复：_suspend_and_wait()

已拆出的职责：
- 状态构建 → pipeline/state_builder.py
- 插件配置 → pipeline/plugin_resolver.py
- 检查点管理 → pipeline/checkpoint.py
- 消息注入/唤醒 → pipeline/message_bus.py
- 状态管理/深拷贝 → pipeline/engine_state.py
- 路由决策 → pipeline/engine_route.py
- 插件链执行 → pipeline/engine_chain.py
"""

from __future__ import annotations

import asyncio
import contextvars
import time as _time
import logging
import traceback as _traceback
import uuid as _uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pipeline.registry import EngineRegistry, PipelineRegistry, PluginRegistry, get_engine_registry
from pipeline.route import InputRouteTable, OutputRouteTable
from pipeline.types import StateKeys
from pipeline.plugin_resolver import apply_agent_model_override, _tier_cache

from pipeline.engine_state import (  # noqa: F401
    _PipelineLogFilter,
    _current_pipeline_id,
)
from pipeline.engine_route import (  # noqa: F401
    apply_route,
    resolve_output_plugins,
)
from pipeline.engine_chain import (  # noqa: F401
    execute_core_plugin,
    execute_input_chain,
    execute_output_chain,
    handle_no_route_signals,
    run_post_end_output_chain,
)

if TYPE_CHECKING:
    from infrastructure.checkpoint.pipeline_checkpoint import PipelineCheckpointManager

logger = logging.getLogger(__name__)


class PipelineEngine:
    """管道引擎。

    核心循环：输入路由 → Input 链 → Core → Output 链 → 输出路由仲裁 → apply_route。
    循环持续到 state["ended"] 为 True 或管道挂起。

    暂停/恢复机制：
    - 当路由信号为 wait 时，管道保存当前 state 到 _suspended_state 并挂起
    - 调用 resume() 从 _suspended_state 恢复执行，继续循环

    Agent 配置注入：
    - run(user_input, agent_config) 直接接受用户输入和 Agent 配置
    - Agent 配置通过 to_state() 转换为 state 注入字典
    - 插件从 ctx.state["plugin_configs"] 读取自己的配置

    Attributes:
        input_route_table: 输入路由表
        output_route_table: 输出路由表
        plugin_registry: 插件注册表
        pipeline_registry: 跨管道路由注册表（可选）
        _services: 服务实例字典，传递给 PluginContext
        _suspended_state: 暂停时保存的管道状态快照
        _checkpoint_manager: 管道检查点管理器（可选）
    """

    def __init__(
        self,
        input_route_table: InputRouteTable,
        output_route_table: OutputRouteTable,
        plugin_registry: PluginRegistry,
        pipeline_registry: PipelineRegistry | None = None,
        services: dict[str, Any] | None = None,
        max_iterations: int = 500,
        agent_registry: Any | None = None,
        checkpoint_manager: PipelineCheckpointManager | None = None,
    ) -> None:
        self.input_route_table = input_route_table
        self.output_route_table = output_route_table
        self.plugin_registry = plugin_registry.fork()
        self.pipeline_registry = pipeline_registry
        self._services = services or {}
        self.max_iterations = max_iterations
        self._agent_registry = agent_registry
        self._suspended_state: dict[str, Any] | None = None
        self._checkpoint_manager = checkpoint_manager
        self._pipeline_id: str = _uuid.uuid4().hex[:12]
        self._wake_event: asyncio.Event | None = None
        self._engine_loop: asyncio.AbstractEventLoop | None = None
        self._watching_task_ids: list[str] = []
        self._consecutive_core_errors: int = 0
        self._max_consecutive_core_errors: int = 3
        self._streaming_on_chunk: Any = None
        self._streaming_flag: bool = False
        self._last_state: dict[str, Any] | None = None
        self._agent_config: Any | None = None
        self._running: bool = False
        self._preserved_bridge: Any = None
        self._preserved_drain_task: Any = None
        # BUG-FIX-fix_20260602_send_message_thinking_forever:
        # 在 run() 的 unregister/register 循环中保留 engine_task 引用，
        # 避免 suspended 路径再次发消息时 entry.engine_task 为 None。
        self._preserved_engine_task: Any = None

    async def run(
        self,
        user_input: str | None = None,
        agent_config: Any | None = None,
        conversation_history: list[dict[str, Any]] | None = None,
        initial_state: dict[str, Any] | None = None,
        allow_default_fallback: bool = True,
        **extra_state: Any,
    ) -> dict[str, Any]:
        """执行管道。

        支持两种调用方式：
        1. 新方式：run(user_input="你好", agent_config=config)
        2. 兼容方式：run(initial_state={"user_input": "你好", ...})

        新方式中，Agent 配置通过 to_state() 自动注入到 state，
        插件从 ctx.state 读取 system_prompt、tool_ids、constraints 等。

        Args:
            user_input: 用户输入文本
            agent_config: Agent 配置实例，None 则使用系统默认
            conversation_history: 跨轮次对话历史（可选）
            initial_state: 管道初始状态字典（兼容旧接口）
            allow_default_fallback: 是否允许 agent_config=None 时回退到系统默认 Agent。
                CLI/WebSocket 主入口应为 True；TaskWorker/EvaluationEngine 等子任务
                入口应为 False，缺少 agent_config 时直接报错。
            **extra_state: 额外注入的 state 键值对

        Returns:
            管道最终状态字典
        """
        # BUG-FIX: 每次新 run() 调用重置挂起状态，防止引擎复用时旧状态泄漏。
        self._suspended_state = None
        self._wake_event = None
        self._streaming_flag = False
        self._streaming_on_chunk = None

        # 保留当前 entry 中的 bridge 和 drain_task 引用，
        # 避免下面 _run_loop register 时丢失 idle 阶段绑定的流式桥接。
        # BUG-FIX-fix_20260602_send_message_thinking_forever:
        # 同样需要保留 engine_task 引用，否则 suspended 路径下次
        # ensure_bridge 拿不到 engine_task，drain_loop 用 None 启动后
        # 在首个 chunk 到达前就退出，LLM 输出永远无人消费。
        _preserved_bridge = None
        _preserved_drain_task = None
        _preserved_engine_task: Any = None
        if self._pipeline_id:
            _old_entry = get_engine_registry().get(self._pipeline_id)
            if _old_entry is not None:
                _preserved_bridge = _old_entry.bridge
                _preserved_drain_task = _old_entry.drain_task
                _preserved_engine_task = _old_entry.engine_task
                # BUG-FIX-fix_20260513_pipeline_cross_talk:
                # 清除旧 pipeline_id 的引擎注册残留（保留 bridge）
                get_engine_registry().unregister(self._pipeline_id)
        self._preserved_bridge = _preserved_bridge
        self._preserved_drain_task = _preserved_drain_task
        self._preserved_engine_task = _preserved_engine_task

        # pipeline_id 由引擎构造时确定，外部不可覆盖。
        extra_state["pipeline_id"] = self._pipeline_id

        raw_state = None
        if initial_state is not None and user_input is None:
            raw_state = initial_state
        elif isinstance(user_input, dict) and initial_state is None:
            raw_state = user_input

        if raw_state is not None:
            state: dict[str, Any] = {
                **raw_state,
                StateKeys.ITERATION: 0,
                StateKeys.ENDED: False,
            }
            if StateKeys.PIPELINE_ID not in state:
                state[StateKeys.PIPELINE_ID] = self._pipeline_id
            return await self._run_loop(state, resumed=False)

        if agent_config is None:
            if not allow_default_fallback:
                raise ValueError(
                    "PipelineEngine.run() 收到 agent_config=None 且 "
                    "allow_default_fallback=False。子任务管道必须显式提供 Agent 配置，"
                    "禁止静默回退到系统默认 Agent（灵汐）。"
                )
            logger.warning(
                "[PipelineEngine] agent_config 为 None，回退到系统默认 Agent。"
                "调用方应显式传入 agent_config 以避免此警告。"
            )
            from pipeline.state_builder import load_default_agent
            agent_config = load_default_agent(self._agent_registry)

        from pipeline.state_builder import build_initial_state
        state = build_initial_state(
            user_input=user_input or "",
            agent_config=agent_config,
            conversation_history=conversation_history,
            pipeline_id=self._pipeline_id,
            services=self._services,
            extra_state=extra_state,
        )

        if agent_config and hasattr(agent_config, "max_iterations") and agent_config.max_iterations:
            self.max_iterations = agent_config.max_iterations

        self._agent_config = agent_config

        from pipeline.plugin_resolver import apply_agent_plugin_configs, apply_agent_model_override
        apply_agent_plugin_configs(self.plugin_registry, agent_config)
        apply_agent_model_override(self.plugin_registry, agent_config, self._services)

        return await self._run_loop(state, resumed=False)

    async def resume(self) -> dict[str, Any]:
        """从暂停状态恢复管道执行。

        使用 _suspended_state 中保存的状态快照恢复管道循环，
        继续从暂停点执行直到管道结束或再次挂起。

        Returns:
            管道最终状态字典

        Raises:
            RuntimeError: 没有暂停状态可恢复时抛出
        """
        if self._suspended_state is None:
            raise RuntimeError("No suspended state to resume from")

        saved_state = self._suspended_state
        self._suspended_state = None
        logger.info(
            "Pipeline resuming from suspended state (iteration=%d)",
            saved_state.get(StateKeys.ITERATION, 0),
        )

        return await self._run_loop(saved_state, resumed=True)

    async def _run_loop(self, state: dict[str, Any], *, resumed: bool = False) -> dict[str, Any]:
        """管道核心循环。

        循环流程：
        1. 递增迭代计数器
        2. 第一步：解析插件列表
        3. 获取 Input 插件 → PluginChain 执行
        4. 第二步：用更新后的 state 解析 target
        5. target == "end": 写入拦截原因到 RAW_RESULT，设 ended=True, break
        6. target == "wait": break（挂起）
        7. target == "core": 继续执行
        8. 获取 Core 插件 → 执行，更新 state
        9. 获取 Output 插件 → PluginChain 执行
        10. 收集 route_signals
        11. 输出路由表仲裁 → apply_route

        Args:
            state: 管道状态字典（原地修改）
            resumed: 是否从暂停状态恢复，控制日志前缀

        Returns:
            管道最终状态字典
        """
        _pipeline_log_handler = None
        _pipeline_loggers: list[logging.Logger] = []
        self._running = True
        self._run_start_time = _time.monotonic()
        logger.info(
            "[Engine] 引擎启动: pipeline=%s task_id=%s",
            self._pipeline_id[:12], state.get("task_id", "?"),
        )
        pipeline_run_id = state.get(StateKeys.PIPELINE_ID, self._pipeline_id)
        # BUG-FIX-fix_20260513_pipeline_cross_talk:
        self._pipeline_id = pipeline_run_id
        _pipeline_id_token = _current_pipeline_id.set(pipeline_run_id)
        # 重置连续错误计数器
        self._consecutive_core_errors = 0
        # BUG-FIX-fix_20260508_sub_pipeline_streaming:
        if not resumed:
            self.save_streaming_context(state)
        else:
            self.restore_streaming_context(state)
        _reg_tags: dict[str, str] = {}
        _reg_task_id = state.get("task_id")
        if _reg_task_id:
            _reg_tags["task_id"] = _reg_task_id
        _reg_entry = get_engine_registry().register(pipeline_run_id, self, tags=_reg_tags or None)
        if self._preserved_bridge is not None and _reg_entry.bridge is None:
            _reg_entry.bridge = self._preserved_bridge
        if self._preserved_drain_task is not None and _reg_entry.drain_task is None:
            _reg_entry.drain_task = self._preserved_drain_task
        # BUG-FIX-fix_20260602_send_message_thinking_forever:
        # 恢复 engine_task 引用，否则 suspended 路径下次 ensure_bridge
        # 拿到的 engine_task 为 None，drain_loop 提前退出。
        if self._preserved_engine_task is not None and _reg_entry.engine_task is None:
            _reg_entry.engine_task = self._preserved_engine_task
            logger.debug(
                "[Engine] 恢复 preserved engine_task: pipeline=%s has_task=%s",
                pipeline_run_id[:12], not self._preserved_engine_task.done(),
            )
        self._preserved_bridge = None
        self._preserved_drain_task = None
        self._preserved_engine_task = None
        self._run_started = True
        try:
            self._setup_pipeline_logging(pipeline_run_id, resumed, _pipeline_loggers)
            if _pipeline_loggers:
                _pipeline_log_handler = _pipeline_loggers[0].handlers[-1] if _pipeline_loggers[0].handlers else None

            # 重新获取 log_handler（_setup_pipeline_logging 可能修改列表）
            _pipeline_log_handler = self._get_last_file_handler(_pipeline_loggers)

            # BUG-FIX-fix_20260426_context_guard: context_window 需在首次迭代前注入 state
            if not state.get("context_window"):
                _llm_core = self.plugin_registry.get_core("llm_call")
                if _llm_core and hasattr(_llm_core, "_context_window") and _llm_core._context_window:
                    state["context_window"] = _llm_core._context_window

            while not state.get(StateKeys.ENDED, False):
                # 1. 递增迭代计数器
                state[StateKeys.ITERATION] = state.get(StateKeys.ITERATION, 0) + 1
                iteration = state[StateKeys.ITERATION]

                # 安全阀（-1 表示无限制）
                if self.max_iterations > 0 and iteration > self.max_iterations:
                    logger.warning("Pipeline exceeded %d iterations, forcing end", self.max_iterations)
                    state[StateKeys.ENDED] = True
                    break

                if resumed:
                    logger.info("=== Pipeline iteration %d (resumed) ===", iteration)
                else:
                    logger.info("=== Pipeline iteration %d ===", iteration)

                # 显示当前使用的模型信息
                self._log_model_info()

                # BUG-FIX-fix_20260530_config_not_take_effect:
                # 每次迭代刷新模型配置，确保运行中修改 YAML 后新配置生效。
                if self._agent_config is not None:
                    from pipeline.plugin_resolver import apply_agent_model_override, _tier_cache
                    _tier_cache.clear()
                    apply_agent_model_override(
                        self.plugin_registry, self._agent_config, self._services
                    )

                # 发射 iteration 事件
                self._emit_iteration_event(state, iteration)  # type: ignore[arg-type]

                # 自动保存检查点
                if self._checkpoint_manager is not None:
                    try:
                        _cp_pid = state.get(StateKeys.PIPELINE_ID, "default")
                        await self._checkpoint_manager.save(_cp_pid, state, phase="auto")
                    except Exception as exc:
                        logger.debug("Checkpoint auto-save failed: %s", exc)

                # 主动重置 idle timer：每轮迭代开始时重置，
                # 表示上一轮迭代已完成（含 Agent thinking），防止被误判为 idle
                _task_worker = self._services.get("task_worker")
                _task_id_for_reset = state.get("task_id")
                if _task_worker and _task_id_for_reset:
                    try:
                        await _task_worker.reset_idle_timer(_task_id_for_reset)
                    except Exception as _reset_exc:
                        logger.debug("idle timer reset failed (non-critical): %s", _reset_exc)

                # 不要在 tool_execute 迭代中消费待处理通知。
                # 通知应该等工具链完整执行完毕（tool_calls → tool results）
                # 之后再注入 LLM 上下文。在工具执行中插入 user 消息会打破
                # assistant(tool_calls) ↔ tool(result) 的配对，导致
                # Minimax API 返回 tool call id is invalid (2013)。
                _core_type = state.get(StateKeys.CORE_TYPE, state.get("core_type"))
                if _core_type == "tool_execute":
                    _iter_notifs: list[str] = []
                else:
                    _iter_notifs = self.consume_pending_notifications()
                if _iter_notifs:
                    # BUG-FIX-fix_20260601_empty_message:
                    # 过滤掉空字符串通知，避免空消息进入对话历史
                    _filtered_notifs = [n for n in _iter_notifs if n and n.strip()]
                    if _filtered_notifs:
                        _combined = "\n\n".join(_filtered_notifs)
                        _existing_input = state.get("user_input", "")
                        if _existing_input:
                            state["user_input"] = f"{_combined}\n\n{_existing_input}"
                        else:
                            state["user_input"] = _combined
                        state.setdefault("messages", []).append(
                            {"role": "user", "content": _combined}
                        )
                        state[StateKeys.CORE_TYPE] = "llm_call"
                        state.pop("raw_result", None)
                        state.pop("error_analysis", None)

                    logger.info(
                        "[Engine] 迭代 %d 开始时消费 %d 条待处理通知，注入 state",
                        iteration, len(_iter_notifs),
                    )

                # 2. 解析插件列表
                plugin_names = self.input_route_table.resolve_plugins(state)
                logger.info("Input route resolved plugins: %s", plugin_names)

                # 4. 获取 Input 插件 → PluginChain 执行
                await execute_input_chain(self, state, plugin_names)

                # 4.5 Input 插件可能设置 ENDED 终止管线
                if state.get(StateKeys.ENDED, False):
                    logger.info("Pipeline ended by input plugin (ENDED=True)")
                    break

                # 5. 用更新后的 state 解析 target
                target, matched_entry = self.input_route_table.resolve_target(state)
                logger.info("Input route resolved target: %s (entry=%s)", target, matched_entry.name if matched_entry else "none")

                # 6. target == "end"
                if target == "end":
                    _end_notifs = self.consume_pending_notifications()
                    if _end_notifs:
                        # BUG-FIX-fix_20260601_empty_message:
                        # 过滤掉空字符串通知，避免空消息进入对话历史
                        _filtered_end_notifs = [n for n in _end_notifs if n and n.strip()]
                        if _filtered_end_notifs:
                            _combined = "\n\n".join(_filtered_end_notifs)
                            state["user_input"] = _combined
                            state.setdefault("messages", []).append(
                                {"role": "user", "content": _combined}
                            )
                            state[StateKeys.CORE_TYPE] = "llm_call"
                            logger.info(
                                "[Engine] target=end 但有 %d 条待处理通知，继续循环",
                                len(_filtered_end_notifs),
                            )
                    else:
                        if matched_entry and matched_entry.result:
                            result_msg = matched_entry.format_result(state)
                            state[StateKeys.RAW_RESULT] = result_msg
                            logger.info("Input route end with result: %s", result_msg)
                        state[StateKeys.ENDED] = True
                        logger.info("Pipeline ended by input route (target=end)")
                        break

                # 7. target == "wait": 挂起
                if target == "wait":
                    _on_chunk_cb = state.get("on_chunk")
                    if _on_chunk_cb:
                        try:
                            _on_chunk_cb({
                                "type": "pipeline_suspended",
                                "pipeline_id": state.get(StateKeys.PIPELINE_ID, ""),
                            })
                        except Exception:
                            pass
                    self._suspended_state = self._suspend_copy_state(state)
                    # BUG-FIX-fix_20260603_wake_event_race:
                    # 在设置 _suspended_state 的同时创建 _wake_event，
                    # 避免 inject_message 在 _suspended_state 已设置但
                    # _wake_event 还是 None 的窗口内 set() 丢失。
                    self._wake_event = asyncio.Event()
                    logger.info("Pipeline suspended by input route (target=wait), state saved")
                    if self._checkpoint_manager is not None:
                        try:
                            _s_pid = state.get(StateKeys.PIPELINE_ID, "default")
                            await self._checkpoint_manager.save(_s_pid, state, phase="suspended")
                        except Exception as exc:
                            logger.debug("Checkpoint suspended-save failed: %s", exc)
                    # BUG-FIX-fix_20260521_on_chunk_missing:
                    # 恢复逻辑已内置到 _suspend_and_wait，无需手动恢复。
                    if not await self._suspend_and_wait(state):
                        break
                    logger.info("Pipeline woken up, resuming loop iteration")
                    # BUG-FIX-fix_20260603_pending_tool_calls_lost_on_resume:
                    # 从挂起恢复时，如果有待执行的工具调用（raw_tool_calls 非空），
                    # 必须先执行完工具再处理注入的消息。否则通知会插入
                    # assistant(tool_calls) 和 tool(result) 之间，破坏消息序列。
                    # 正确的时序：工具执行 → 结果入 messages → 通知入 messages → LLM。
                    if state.get(StateKeys.RAW_TOOL_CALLS):
                        state[StateKeys.CORE_TYPE] = "tool_execute"
                    else:
                        state[StateKeys.CORE_TYPE] = "llm_call"

                # 8. 执行 Core 插件
                core_type = state.get(StateKeys.CORE_TYPE, "llm_call")
                await execute_core_plugin(self, state, core_type)

                # 9. 执行 Output 插件链并收集路由信号
                route_signals = await execute_output_chain(self, state, core_type)

                # 10-11. 路由仲裁 → apply_route
                if route_signals:
                    resolved = self.output_route_table.arbitrate(route_signals, state)
                    logger.info(
                        "Route arbitrated: type=%s, target=%s, reason=%s",
                        resolved.route_type, resolved.target, resolved.reason,
                    )
                    should_break = await apply_route(self, resolved, state)
                    if should_break:
                        break
                else:
                    _no_route_action = await handle_no_route_signals(self, state, core_type, iteration)
                    if _no_route_action == "end":
                        break
                    continue

            # 管道结束后，再执行一次 Output 链
            if state.get(StateKeys.ENDED, False):
                state[StateKeys.ENDED] = True
                await run_post_end_output_chain(self, state)

        except asyncio.CancelledError:
            _task = asyncio.current_task()
            _must_cancel = getattr(_task, '_must_cancel', None) if _task else None
            # 尝试获取取消来源信息
            _cancel_source = "unknown"
            if _task and hasattr(_task, 'get_name'):
                _cancel_source = f"task_name={_task.get_name()}"
            if _must_cancel is True:
                _cancel_source = "explicit_cancel(_must_cancel=True)"
            logger.warning(
                "Pipeline cancelled | iteration=%d | _must_cancel=%s | cancel_source=%s",
                state.get(StateKeys.ITERATION, 0),
                _must_cancel,
                _cancel_source,
            )
            state[StateKeys.ENDED] = True
            state[StateKeys.RAW_ERROR] = f"Pipeline engine cancelled (source={_cancel_source})"
            await self._mark_task_failed_on_engine_exit(state, f"Pipeline engine cancelled (source={_cancel_source})")
        except Exception as exc:
            _iter = state.get(StateKeys.ITERATION, 0)
            _core_type = state.get(StateKeys.CORE_TYPE, "?")
            _elapsed = _time.monotonic() - getattr(self, '_run_start_time', _time.monotonic())
            _msg_count = len(state.get("messages", []))
            logger.error(
                "[Engine] 管道异常退出 | pipeline=%s iteration=%d core_type=%s "
                "messages=%d elapsed=%.0fs error=%s",
                self._pipeline_id[:12], _iter, _core_type,
                _msg_count, _elapsed, exc,
            )
            # 强制刷新日志以确保错误不被缓冲丢失
            for _h in logging.getLogger().handlers + logging.getLogger("pipeline").handlers:
                try:
                    _h.flush()
                except Exception:
                    pass
            state[StateKeys.ENDED] = True
            state[StateKeys.RAW_ERROR] = str(exc)
            # 构造含上下文的错误信息，写入任务 error 字段
            _err_detail = (
                f"管道异常退出: {exc}，错误分析: {{"
                f"'retryable': True, "
                f"'reason': '{exc}', "
                f"'category': 'core_error', "
                f"'iteration': {_iter}, "
                f"'core_type': '{_core_type}', "
                f"'messages': {_msg_count}, "
                f"'elapsed_seconds': {_elapsed:.0f}"
                f"}}"
            )
            await self._mark_task_failed_on_engine_exit(state, _err_detail)
        finally:
            self._running = False
            self._last_state = state
            logger.info(
                "[Engine] 引擎停止: pipeline=%s iteration=%d ended=%s "
                "raw_error=%s",
                self._pipeline_id[:12],
                state.get(StateKeys.ITERATION, 0),
                state.get(StateKeys.ENDED, False),
                (state.get(StateKeys.RAW_ERROR) or "(none)")[:100],
            )
            await self._cleanup_run_loop(
                state, _pipeline_log_handler, _pipeline_loggers,
                _pipeline_id_token,
            )

        return state

    # ------------------------------------------------------------------
    # _run_loop 辅助方法
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 管道日志过滤器 — 任务执行事件独立输出
    # ------------------------------------------------------------------
    _TASK_LOG_LOGGERS: tuple[str, ...] = (
        "tools.builtin.task_submit",
        "tools.builtin.task_manage",
        "tools.builtin.task_evaluate",
        "infrastructure.task_worker",
        "tasks",
    )

    class _TaskLogFilter(logging.Filter):
        """只放行任务执行相关的日志（task_submit/task_manage/task_evaluate/worker）。"""

        def __init__(self, pipeline_id: str) -> None:
            super().__init__()
            self.pipeline_id = pipeline_id

        def filter(self, record: logging.LogRecord) -> bool:
            if _current_pipeline_id.get() != self.pipeline_id:
                return False
            return any(
                record.name.startswith(prefix)
                for prefix in PipelineEngine._TASK_LOG_LOGGERS
            )

    # ------------------------------------------------------------------
    # 管道日志设置
    # ------------------------------------------------------------------

    def _setup_pipeline_logging(
        self,
        pipeline_run_id: str,
        resumed: bool,
        pipeline_loggers: list[logging.Logger],
    ) -> None:
        """为当前管道设置独立日志文件，按类型分文件夹存储。

        目录结构：
          logs/pipeline/  — 主日志（DEBUG~INFO，不含 WARNING+）
          logs/error/     — 错误日志（WARNING 及以上）
          logs/task/      — 任务执行日志（task_submit/manage/evaluate/worker）
        """
        try:
            # 防止 resume 时重复添加 Handler
            if hasattr(self, '_logging_pipeline_id') and self._logging_pipeline_id == pipeline_run_id:
                return
            self._logging_pipeline_id = pipeline_run_id

            _log_base = Path.cwd() / "logs"
            _pipeline_dir = _log_base / "pipeline"
            _error_dir = _log_base / "error"
            _task_dir = _log_base / "task"
            _pipeline_dir.mkdir(parents=True, exist_ok=True)
            _error_dir.mkdir(parents=True, exist_ok=True)
            _task_dir.mkdir(parents=True, exist_ok=True)

            log_mode = "a" if resumed else "w"
            _log_fmt = logging.Formatter(
                "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                datefmt="%H:%M:%S",
            )
            _pipeline_filter = _PipelineLogFilter(pipeline_run_id)

            # ---- 1. 主日志（DEBUG~INFO，排除 WARNING+，避免与 error 重复） ----
            _main_handler = logging.FileHandler(
                str(_pipeline_dir / f"pipeline_{pipeline_run_id}.log"),
                encoding="utf-8", mode=log_mode,
            )
            _main_handler.setLevel(logging.DEBUG)
            _main_handler.setFormatter(_log_fmt)
            _main_handler.addFilter(_pipeline_filter)
            # 排除 WARNING 及以上级别，这些内容只写 error 日志
            _main_handler.addFilter(
                lambda record: record.levelno < logging.WARNING
            )

            # ---- 2. 错误/中断/警告日志（WARNING+ 级别，独立文件夹） ----
            _error_handler = logging.FileHandler(
                str(_error_dir / f"pipeline_{pipeline_run_id}.log"),
                encoding="utf-8", mode=log_mode,
            )
            _error_handler.setLevel(logging.WARNING)
            _error_handler.setFormatter(_log_fmt)
            _error_handler.addFilter(_pipeline_filter)

            # ---- 3. 任务执行日志（独立文件夹） ----
            _task_handler = logging.FileHandler(
                str(_task_dir / f"pipeline_{pipeline_run_id}.log"),
                encoding="utf-8", mode=log_mode,
            )
            _task_handler.setLevel(logging.DEBUG)
            _task_handler.setFormatter(_log_fmt)
            _task_handler.addFilter(self._TaskLogFilter(pipeline_run_id))

            _all_loggers = [
                "pipeline.engine", "pipeline.chain", "pipeline.event_bus",
                "pipeline.route", "pipeline.config", "pipeline.registry",
                "pipeline.stream_bridge",
                "plugins.core", "plugins.input", "plugins.output",
                "infrastructure.task_worker", "tasks",
                "tools.builtin", "evaluation",
                "llm.adapter", "llm.adapter._stream",
                "triggers.manager",
                "pipeline.message_bus",
                "src.core.event_bus",
            ]
            for _ln in _all_loggers:
                _lg = logging.getLogger(_ln)
                if _lg.level == logging.NOTSET:
                    _lg.setLevel(logging.DEBUG)
                _lg.addHandler(_main_handler)
                _lg.addHandler(_error_handler)
                _lg.addHandler(_task_handler)
                pipeline_loggers.append(_lg)
        except Exception:
            pass

    @staticmethod
    def _get_last_file_handler(loggers: list[logging.Logger]) -> logging.FileHandler | None:
        """从日志器列表中获取最后一个 FileHandler（即当前管道的）。"""
        for _lg in reversed(loggers):
            for _h in reversed(_lg.handlers):
                if isinstance(_h, logging.FileHandler):
                    return _h
        return None

    def _log_model_info(self) -> None:
        """显示当前 LLM 模型信息。"""
        _llm_core_iter = self.plugin_registry.get_core("llm_call")
        if _llm_core_iter and hasattr(_llm_core_iter, "_model"):
            _model_info = f"{_llm_core_iter._model} (provider={_llm_core_iter._provider}"
            if getattr(_llm_core_iter, "_api_base", None):
                _model_info += f", api_base={_llm_core_iter._api_base}"
            _model_info += ")"
            logger.info("Model: %s", _model_info)

    def _emit_iteration_event(self, state: dict[str, Any], iteration: int) -> None:
        """发射 iteration 事件供 CLI 状态栏实时更新。"""
        on_chunk_cb = state.get("on_chunk")
        if on_chunk_cb:
            try:
                on_chunk_cb({
                    "type": "iteration",
                    "iteration": iteration,
                    "max_iterations": self.max_iterations,
                })
            except Exception as exc:
                logger.debug("on_chunk iteration emit failed: %s", exc)

    async def _mark_task_failed_on_engine_exit(
        self, state: dict[str, Any], reason: str,
    ) -> None:
        """引擎异常退出时，将关联的 running 任务标记为 failed。

        BUG-FIX-fix_20260522_task_stuck_running:
        问题根因: _run_loop 在 CancelledError/Exception 时只设了 state[ENDED]=True，
                  不更新关联任务的 YAML 状态，导致引擎死了但任务仍为 status: running。
        修复方案: 通过 pipeline_run_id 查找关联任务，调用 task_service.fail_task 标记失败。

        Args:
            state: 管道状态字典
            reason: 失败原因
        """
        pipeline_run_id = state.get(StateKeys.PIPELINE_ID, "")
        if not pipeline_run_id:
            return

        task_service = self._services.get("task_service")
        if task_service is None:
            logger.debug(
                "[Engine] 引擎异常退出但无 task_service，跳过任务状态清理: pipeline=%s",
                pipeline_run_id[:12],
            )
            return

        try:
            for task in task_service.list_by_status("running"):
                if getattr(task, "pipeline_run_id", None) == pipeline_run_id:
                    await task_service.fail_task(task.id, reason=reason)
                    logger.info(
                        "[Engine] 已将关联任务标记为 failed: task=%s pipeline=%s reason=%s",
                        task.id[:12], pipeline_run_id[:12], reason,
                    )
        except Exception as exc:
            logger.warning(
                "[Engine] 标记关联任务 failed 失败（非致命）: pipeline=%s err=%s",
                pipeline_run_id[:12], exc,
            )

    async def _cleanup_run_loop(
        self,
        state: dict[str, Any],
        log_handler: logging.FileHandler | None,
        loggers: list[logging.Logger],
        pipeline_id_token: contextvars.Token,
    ) -> None:
        """清理 _run_loop 的资源和注册。

        关闭所有为当前管道创建的 FileHandler（主日志/错误日志/任务日志），
        并从所有 logger 中移除，防止 handler 泄漏和文件句柄泄漏。
        """
        # 收集所有需要关闭的 FileHandler（可能有多个：主日志+错误日志+任务日志）
        _handlers_to_close: list[logging.FileHandler] = []
        _seen: set[int] = set()
        for _lg in loggers:
            for _h in _lg.handlers:
                if isinstance(_h, logging.FileHandler) and id(_h) not in _seen:
                    _seen.add(id(_h))
                    _handlers_to_close.append(_h)

        for _h in _handlers_to_close:
            try:
                _h.close()
            except Exception:
                pass
            for _lg in loggers:
                try:
                    _lg.removeHandler(_h)
                except Exception:
                    pass

        _current_pipeline_id.reset(pipeline_id_token)
        # 清理 EngineRegistry 注册
        _cp_pipeline_id = state.get(StateKeys.PIPELINE_ID, "")
        if _cp_pipeline_id:
            _cl_entry = get_engine_registry().get(_cp_pipeline_id)
            if _cl_entry:
                _cl_entry.engine_task = None
            get_engine_registry().unregister(_cp_pipeline_id)
            # 释放 chunk_service 内存缓存
            try:
                _cs = self._service_provider.get_service("chunk_service")
                if _cs:
                    await _cs.evict_pipeline(_cp_pipeline_id)
            except Exception:
                pass
        # 管道结束后自动清理旧检查点
        if self._checkpoint_manager is not None:
            try:
                _cp_pipeline_id = state.get(StateKeys.PIPELINE_ID, "default")
                await self._checkpoint_manager.cleanup_old(_cp_pipeline_id, keep_count=2)
            except Exception as _cp_exc:
                logger.debug("Checkpoint cleanup failed (non-critical): %s", _cp_exc)

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def pipeline_id(self) -> str:
        """管道唯一标识。"""
        return self._pipeline_id

    @pipeline_id.setter
    def pipeline_id(self, value: str) -> None:
        """设置管道 ID（供 registry 和会话恢复使用）。"""
        self._pipeline_id = value

    @property
    def services(self) -> dict[str, Any]:
        """服务实例字典，传递给 PluginContext。"""
        return self._services

    @property
    def consecutive_core_errors(self) -> int:
        """连续 Core 执行错误计数。"""
        return self._consecutive_core_errors

    @consecutive_core_errors.setter
    def consecutive_core_errors(self, value: int) -> None:
        """设置连续 Core 错误计数。"""
        self._consecutive_core_errors = value

    @property
    def max_consecutive_core_errors(self) -> int:
        """连续 Core 错误上限阈值（只读）。"""
        return self._max_consecutive_core_errors

    @property
    def is_running(self) -> bool:
        """管道是否正在运行（非挂起、非完成）。"""
        return self._running

    @property
    def is_suspended(self) -> bool:
        """管道是否处于暂停状态。"""
        return self._suspended_state is not None

    # ------------------------------------------------------------------
    # 挂起/恢复
    # ------------------------------------------------------------------

    async def _suspend_and_wait(self, state: dict[str, Any]) -> bool:
        """挂起管道，等待外部通过 wake() 或 message_bus 唤醒。

        管道挂起后不退出 _run_loop，而是 await 内部 _wake_event。
        通过 pipeline.message_bus.send_pipeline_message() 注入消息并唤醒。

        超时（600s）时检查是否有新通知：
        - 有新通知：唤醒管道继续执行
        - 无新通知：重新挂起等待，避免无意义的 LLM 调用循环

        挂起时记录 submitted_task_ids，供外部判断该管道等待哪些任务。

        BUG-FIX-fix_20260521_on_chunk_missing:
        唤醒后统一从 _suspended_state 恢复 state（含 on_chunk / streaming），
        避免调用方遗漏恢复字段。

        Returns:
            True 表示成功恢复（应继续循环），False 表示无恢复数据（应结束管道）。
        """
        pipeline_id = state.get(StateKeys.PIPELINE_ID, "")
        self._watching_task_ids = list(state.get("submitted_task_ids", []))
        self._running = False

        pending_notifications = self.consume_pending_notifications()
        if pending_notifications:
            self._inject_notifications_to_suspended_state(pending_notifications)
            logger.info(
                "[Engine] 管道挂起时发现 %d 条待处理通知，立即唤醒: pipeline=%s",
                len(pending_notifications), pipeline_id,
            )
        else:
            logger.info(
                "[Engine] 管道挂起，等待唤醒: pipeline=%s, watching_tasks=%s",
                pipeline_id, self._watching_task_ids,
            )
            max_wait_rounds = 50
            # BUG-FIX-fix_20260603_wake_event_threadsafe:
            # 捕获引擎线程的事件循环引用，供 inject_message 通过
            # call_soon_threadsafe 安全地跨线程 set() wake_event。
            self._engine_loop = asyncio.get_running_loop()
            for wait_round in range(max_wait_rounds):
                self._wake_event = asyncio.Event()
                _registry = get_engine_registry()
                _entry = _registry.get(pipeline_id)
                if _entry is not None:
                    _entry.engine = self
                else:
                    _registry.register(pipeline_id, self)
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=600)
                    break
                except asyncio.TimeoutError:
                    pending_notifications = self.consume_pending_notifications()
                    if pending_notifications:
                        self._inject_notifications_to_suspended_state(pending_notifications)
                        logger.info(
                            "[Engine] 管道超时后发现 %d 条通知，唤醒: pipeline=%s",
                            len(pending_notifications), pipeline_id,
                        )
                        break
                    if self._check_children_terminal(state):
                        logger.info(
                            "[Engine] 管道超时后发现子任务已终态，唤醒: pipeline=%s",
                            pipeline_id,
                        )
                        break
                    logger.info(
                        "[Engine] 管道等待超时(600s)无新通知，重新挂起 "
                        "(round=%d/%d): pipeline=%s",
                        wait_round + 1, max_wait_rounds, pipeline_id,
                    )
            else:
                logger.warning(
                    "[Engine] 管道等待超过 %d 轮，强制唤醒: pipeline=%s",
                    max_wait_rounds, pipeline_id,
                )

        self._wake_event = None
        self._engine_loop = None
        self._watching_task_ids = []
        self._running = True

        if self._suspended_state is not None:
            # BUG-FIX-fix_20260525_idle_wake_empty_llm:
            # 安全网: 唤醒后检查 suspended_state 中是否有实质性内容。
            # 如果 user_input 为空，说明唤醒原因不是真实用户输入或系统通知，
            # 而是 engine.resume() 或其他机制直接取走了旧 state。
            # 此时应丢弃唤醒，返回 False 让 _run_loop 结束。
            _pending_input = self._suspended_state.get("user_input", "").strip()
            if not _pending_input:
                logger.info(
                    "[Engine] 管道唤醒但 suspended_state 无新内容，"
                    "丢弃唤醒: pipeline=%s",
                    pipeline_id,
                )
                self._suspended_state = None
                return False

            state["user_input"] = self._suspended_state.get(
                "user_input", state.get("user_input", ""),
            )
            state["messages"] = self._suspended_state.get(
                "messages", state.get("messages", []),
            )
            for _key in ("on_chunk", "streaming"):
                if _key in self._suspended_state:
                    state[_key] = self._suspended_state[_key]
            self._suspended_state = None
            self.save_streaming_context(state)
            logger.info("[Engine] 管道被唤醒并恢复 state: pipeline=%s", pipeline_id)
            return True
        else:
            logger.info("[Engine] 管道被唤醒但无 suspended_state: pipeline=%s", pipeline_id)
            return False

    def _inject_notifications_to_suspended_state(self, notifications: list[str]) -> None:
        """将通知消息注入到挂起状态中。"""
        # BUG-FIX-fix_20260601_empty_message:
        # 过滤掉空字符串通知，避免空消息进入对话历史
        for notif in notifications:
            if not notif or not notif.strip():
                continue
            if self._suspended_state is not None:
                orig = self._suspended_state.get("user_input", "")
                self._suspended_state["user_input"] = f"{notif}\n\n{orig}".strip()
                self._suspended_state.setdefault("messages", []).append(
                    {"role": "user", "content": notif}
                )

    def _check_children_terminal(self, state: dict[str, Any]) -> bool:
        """检查 submitted_task_ids 中的子任务是否全部已到达终态。

        当管道因 child_task_guard 挂起时，submitted_task_ids 记录了活跃子任务。
        如果所有子任务都已完成/失败/取消，管道应自动唤醒继续执行，
        而不是无限等待。

        BUG-FIX-fix_20260531_pipeline_infinite_loop:
        确认所有子任务已终态后，清除 submitted_task_ids，
        防止下一轮 iteration 挂起时再次被 _check_children_terminal 误判唤醒。
        """
        task_ids = state.get("submitted_task_ids", [])
        if not task_ids:
            return False

        try:
            from infrastructure.service_provider import get_service_provider
            provider = get_service_provider()
            task_service = provider.get("task_service")
            if task_service is None:
                return False
        except Exception:
            return False

        terminal_statuses = {"completed", "failed", "cancelled"}

        for tid in task_ids:
            try:
                task = task_service.get_task(tid)
                if task is None:
                    continue
                status = task.status.value if hasattr(task.status, "value") else str(task.status)
                if status not in terminal_statuses:
                    return False
            except Exception:
                return False

        logger.info(
            "[Engine] 所有子任务已终态: pipeline=%s task_ids=%s",
            state.get(StateKeys.PIPELINE_ID, ""), task_ids,
        )
        state["submitted_task_ids"] = []
        return True

    def wake(self) -> None:
        """唤醒挂起的管道（不注入消息）。

        仅用于特殊场景（如 CLI 空输入唤醒）。
        正常消息注入请使用 pipeline.message_bus.send_pipeline_message()。
        """
        if self._wake_event is not None:
            # BUG-FIX-fix_20260603_wake_event_threadsafe
            if self._engine_loop is not None and self._engine_loop.is_running():
                self._engine_loop.call_soon_threadsafe(self._wake_event.set)
            else:
                self._wake_event.set()

    def _suspend_copy_state(self, state: dict) -> dict:
        """轻量级挂起状态拷贝，仅深拷贝 messages（唯一会被修改的嵌套结构）。"""
        import copy
        new_state = dict(state)
        new_state["messages"] = copy.deepcopy(state.get("messages", []))
        return new_state

    async def suspend_and_wait(self, state: dict[str, Any]) -> bool:
        """保存状态快照并挂起管道，等待外部唤醒（公开入口）。

        将 state 深拷贝保存到 _suspended_state，然后进入挂起等待。
        外部模块（engine_chain、engine_route）统一调此方法，
        代替直接操作 _suspended_state + _suspend_and_wait。

        Args:
            state: 当前管道状态字典

        Returns:
            True 表示成功恢复（应继续循环），False 表示无恢复数据（应结束管道）。
        """
        self._suspended_state = self._suspend_copy_state(state)
        return await self._suspend_and_wait(state)

    async def resume_from_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """从外部提供的状态快照恢复管道执行（检查点恢复专用）。

        将 state 注入为 _suspended_state，然后执行 resume()。

        Args:
            state: 从检查点加载的管道状态字典

        Returns:
            管道最终状态字典
        """
        self._suspended_state = state
        return await self.resume()

    def consume_pending_notifications(self) -> list[str]:
        """从 bridge 获取待处理通知。引擎不再维护自己的通知队列。

        通过 registry 找到 bridge，拉取通知。无 bridge 时返回空列表。
        """
        try:
            from pipeline.registry import get_engine_registry
            entry = get_engine_registry().get(self._pipeline_id)
            if entry and entry.bridge:
                bridge = entry.bridge
                if hasattr(bridge, '_pending_notifications'):
                    notifs = bridge._pending_notifications[:]
                    bridge._pending_notifications.clear()
                    # 提取通知文本
                    return [n if isinstance(n, str) else n.get("content", "") for n in notifs]
        except Exception:
            pass
        return []

    def save_streaming_context(self, state: dict[str, Any]) -> None:
        """从 state 保存流式上下文。"""
        on_chunk = state.get("on_chunk")
        if on_chunk is not None:
            self._streaming_on_chunk = on_chunk
            self._streaming_flag = state.get("streaming", True)

    def restore_streaming_context(self, state: dict[str, Any]) -> None:
        """恢复流式上下文到 state。"""
        if self._streaming_on_chunk is not None and "on_chunk" not in state:
            state["on_chunk"] = self._streaming_on_chunk
            state["streaming"] = self._streaming_flag

    def set_streaming_context(self, on_chunk: Any, streaming: bool = True) -> None:
        """外部设置流式上下文（替代直接写 _saved_on_chunk）。"""
        self._streaming_on_chunk = on_chunk
        self._streaming_flag = streaming

    def inject_message(self, message: str, *, source: str = "user") -> None:
        """消息注入入口。挂起态写入 suspended_state + 唤醒。

        运行态的系统通知走 bridge notification 路径，不经过此方法。
        运行态的 user 消息仅取消 pending 交互请求。

        Args:
            message: 要注入的消息文本
            source: 消息来源
        """
        if not message or not message.strip():
            logger.warning(
                "[Engine] inject_message: 拒绝空消息 | pipeline=%s source=%s",
                self._pipeline_id[:12], source,
            )
            return
        if self.is_suspended:
            if self._suspended_state is not None:
                # BUG-FIX-fix_20260603_inject_breaks_tool_sequence:
                # 如果挂起状态中有待执行的工具调用，不要直接追加到 messages
                # （会打破 assistant(tool_calls) → tool(result) 序列）。
                # 但要更新 user_input：恢复后工具先执行，然后 user_input
                # 由 handle_no_route_signals 追加到 messages 末尾，
                # 保证顺序：工具结果 → 用户消息 → LLM。
                pending_tool_calls = self._suspended_state.get(
                    StateKeys.RAW_TOOL_CALLS, [],
                )
                existing = self._suspended_state.get("user_input", "")
                self._suspended_state["user_input"] = f"{message}\n{existing}" if existing else message
                if not pending_tool_calls:
                    self._suspended_state.setdefault("messages", []).append(
                        {"role": "user", "content": message}
                    )
                else:
                    logger.info(
                        "[Engine] inject_message: 有 %d 个待执行工具调用，"
                        "消息已写入 user_input（暂不追加到 messages），"
                        "工具执行完成后自动处理 | pipeline=%s",
                        len(pending_tool_calls), self._pipeline_id[:12],
                    )
            if self._wake_event is not None:
                # BUG-FIX-fix_20260603_wake_event_threadsafe:
                # 跨线程 set() 在 Windows ProactorEventLoop 下不可靠，
                # 使用 call_soon_threadsafe 将 set() 调度到引擎线程的事件循环中执行。
                if self._engine_loop is not None and self._engine_loop.is_running():
                    self._engine_loop.call_soon_threadsafe(self._wake_event.set)
                else:
                    self._wake_event.set()
            logger.info(
                "[Engine] inject_message: 挂起态注入并唤醒: pipeline=%s source=%s preview=%.60s",
                self._pipeline_id[:12], source, message,
            )
        elif source == "user":
            # 运行态 user 消息：取消 pending 交互，唤醒引擎
            self._try_cancel_pending_interaction()
            if self._wake_event is not None:
                # BUG-FIX-fix_20260603_wake_event_threadsafe:
                # 跨线程 set() 在 Windows ProactorEventLoop 下不可靠，
                # 使用 call_soon_threadsafe 将 set() 调度到引擎线程的事件循环中执行。
                if self._engine_loop is not None and self._engine_loop.is_running():
                    self._engine_loop.call_soon_threadsafe(self._wake_event.set)
                else:
                    self._wake_event.set()

    def _try_cancel_pending_interaction(self) -> None:
        """尝试取消当前管道关联的 pending human_interaction 请求。

        当新消息通过 notification 路径注入时，引擎可能正卡在
        human_interaction 工具的 wait_for_choice() 上。此方法
        通过取消 pending 请求来解除阻塞，使 _run_loop 能进入
        下一轮迭代消费 notification。

        BUG-FIX-fix_20260524_notification_stuck_by_human_interaction:
        问题根因: human_interaction 工具通过 asyncio.Event.wait() 阻塞 _run_loop，
          导致新消息的 notification 无法被消费，前端无限等待。
        修复方案: inject_message 时自动取消 pending 交互请求，解除工具阻塞。
        """
        try:
            from human_interaction import get_human_interaction_service
            svc = get_human_interaction_service()
            if svc is not None:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        svc.cancel_pending_for_thread(self._pipeline_id)
                    )
                except RuntimeError:
                    pass
        except ImportError:
            pass

    async def save_checkpoint(self, phase: str = "manual") -> str | None:
        """保存管道检查点（委托到 pipeline.checkpoint）。"""
        from pipeline.checkpoint import save_checkpoint as _save
        return await _save(
            self._checkpoint_manager,
            self._suspended_state,
            self._pipeline_id,
            phase,
        )

    async def restore_from_checkpoint(self, checkpoint_id: str) -> bool:
        """从检查点恢复管道状态（委托到 pipeline.checkpoint）。"""
        from pipeline.checkpoint import restore_from_checkpoint as _restore
        success, state = await _restore(self._checkpoint_manager, checkpoint_id)
        if success and state is not None:
            self._suspended_state = state
        return success
