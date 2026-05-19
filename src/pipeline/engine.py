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
import logging
import traceback as _traceback
import uuid as _uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pipeline.chain import PluginChain
from pipeline.plugin import (
    IInputPlugin,
    IOutputPlugin,
    PluginContext,
)
from pipeline.registry import PipelineRegistry, PluginRegistry
from pipeline.route import InputRouteTable, OutputRouteTable
from pipeline.types import RouteSignal, StateKeys

# 从拆分模块重新导入，保持向后兼容
from pipeline.engine_state import (  # noqa: F401
    _PipelineLogFilter,
    _current_pipeline_id,
    _GLOBAL_SUSPENDED_ENGINES,
    _manual_copy_dict,
    _manual_copy_list,
    _safe_deepcopy,
    get_global_suspended_engine,
    register_suspended_engine,
    unregister_suspended_engine,
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
        self._watching_task_ids: list[str] = []
        self._consecutive_core_errors: int = 0
        self._max_consecutive_core_errors: int = 3
        # 外部通知队列（线程安全）：终态通知在此排队，_run_loop 每轮迭代检查
        self._pending_notifications: list[str] = []
        # BUG-FIX-fix_20260508_sub_pipeline_streaming:
        # 保存流式回调和 streaming 标志，resume 时重新注入 _suspended_state。
        # _safe_deepcopy 无法拷贝函数类型的 on_chunk，导致 resume 后流式输出丢失。
        self._saved_streaming: bool = False
        self._saved_on_chunk: Any = None

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
        self._saved_streaming = False
        self._saved_on_chunk = None
        self._pending_notifications.clear()

        # BUG-FIX-fix_20260513_pipeline_cross_talk:
        # 清除旧 pipeline_id 的引擎注册残留
        if self._pipeline_id:
            _old_running_key = f"__running_engine_{self._pipeline_id}"
            _old_suspended_key = f"__suspended_engine_{self._pipeline_id}"
            self._services.pop(_old_running_key, None)
            self._services.pop(_old_suspended_key, None)
            unregister_suspended_engine(self._pipeline_id)

        # pipeline_id 由引擎构造时确定，外部不可覆盖。
        extra_state["pipeline_id"] = self._pipeline_id

        if initial_state is not None and user_input is None:
            state: dict[str, Any] = {
                **initial_state,
                StateKeys.ITERATION: 0,
                StateKeys.ENDED: False,
            }
            if StateKeys.PIPELINE_ID not in state:
                state[StateKeys.PIPELINE_ID] = self._pipeline_id
            return await self._run_loop(state, resumed=False)

        if isinstance(user_input, dict) and initial_state is None:
            state = {
                **user_input,
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
        pipeline_run_id = state.get(StateKeys.PIPELINE_ID, self._pipeline_id)
        # BUG-FIX-fix_20260513_pipeline_cross_talk:
        self._pipeline_id = pipeline_run_id
        _pipeline_id_token = _current_pipeline_id.set(pipeline_run_id)
        # 重置连续错误计数器
        self._consecutive_core_errors = 0
        # BUG-FIX-fix_20260508_sub_pipeline_streaming:
        if not resumed:
            _on_chunk_val = state.get("on_chunk")
            if _on_chunk_val is not None:
                self._saved_on_chunk = _on_chunk_val
                self._saved_streaming = state.get("streaming", True)
        else:
            # resume 时重新注入流式回调
            if self._saved_on_chunk is not None and "on_chunk" not in state:
                state["on_chunk"] = self._saved_on_chunk
                state["streaming"] = self._saved_streaming
        # 注册运行中的引擎引用
        _engine_reg_key = f"__running_engine_{pipeline_run_id}"
        self._services[_engine_reg_key] = self
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

                # 安全阀
                if iteration > self.max_iterations:
                    logger.warning("Pipeline exceeded %d iterations, forcing end", self.max_iterations)
                    state[StateKeys.ENDED] = True
                    break

                if resumed:
                    logger.info("=== Pipeline iteration %d (resumed) ===", iteration)
                else:
                    logger.info("=== Pipeline iteration %d ===", iteration)

                # 显示当前使用的模型信息
                self._log_model_info()

                # 发射 iteration 事件
                self._emit_iteration_event(state, iteration)  # type: ignore[arg-type]

                # 自动保存检查点
                if self._checkpoint_manager is not None:
                    try:
                        _cp_pid = state.get(StateKeys.PIPELINE_ID, "default")
                        await self._checkpoint_manager.save(_cp_pid, state, phase="auto")
                    except Exception as exc:
                        logger.debug("Checkpoint auto-save failed: %s", exc)

                # BUG-FIX-fix_20260514_subtab_inject: 迭代开始时消费待处理通知
                if self._pending_notifications:
                    _iter_notifs = self._pending_notifications[:]
                    self._pending_notifications.clear()
                    _combined = "\n\n".join(_iter_notifs)
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

                # 5. 用更新后的 state 解析 target
                target, matched_entry = self.input_route_table.resolve_target(state)
                logger.info("Input route resolved target: %s (entry=%s)", target, matched_entry.name if matched_entry else "none")

                # 6. target == "end"
                if target == "end":
                    if matched_entry and matched_entry.result:
                        result_msg = matched_entry.format_result(state)
                        state[StateKeys.RAW_RESULT] = result_msg
                        logger.info("Input route end with result: %s", result_msg)
                    state[StateKeys.ENDED] = True
                    logger.info("Pipeline ended by input route (target=end)")
                    break

                # 7. target == "wait": 挂起
                if target == "wait":
                    self._suspended_state = _safe_deepcopy(state)
                    logger.info("Pipeline suspended by input route (target=wait), state saved")
                    if self._checkpoint_manager is not None:
                        try:
                            _s_pid = state.get(StateKeys.PIPELINE_ID, "default")
                            await self._checkpoint_manager.save(_s_pid, state, phase="suspended")
                        except Exception as exc:
                            logger.debug("Checkpoint suspended-save failed: %s", exc)
                    await self._suspend_and_wait(state)
                    if self._suspended_state is not None:
                        state["user_input"] = self._suspended_state.get("user_input", state.get("user_input", ""))
                        state["messages"] = self._suspended_state.get("messages", state.get("messages", []))
                        self._suspended_state = None
                    else:
                        break
                    logger.info("Pipeline woken up, resuming loop iteration")
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
                    # 无路由信号时的后续处理
                    action = await handle_no_route_signals(self, state, core_type, iteration)
                    if action == "continue":
                        continue

            # 管道结束后，再执行一次 Output 链
            if state.get(StateKeys.ENDED, False):
                state[StateKeys.ENDED] = True
                await run_post_end_output_chain(self, state)

        except asyncio.CancelledError:
            _task = asyncio.current_task()
            _must_cancel = getattr(_task, '_must_cancel', None) if _task else None
            logger.warning(
                "Pipeline cancelled | iteration=%d | _must_cancel=%s | stack:\n%s",
                state.get(StateKeys.ITERATION, 0),
                _must_cancel,
                ''.join(_traceback.format_stack()),
            )
            state[StateKeys.ENDED] = True
        except Exception as exc:
            logger.error(
                "Pipeline uncaught exception (iter=%d): %s",
                state.get(StateKeys.ITERATION, 0), exc,
            )
            state[StateKeys.ENDED] = True
            state[StateKeys.RAW_ERROR] = str(exc)
        finally:
            self._cleanup_run_loop(
                state, _pipeline_log_handler, _pipeline_loggers,
                _pipeline_id_token, _engine_reg_key,
            )

        return state

    # ------------------------------------------------------------------
    # _run_loop 辅助方法
    # ------------------------------------------------------------------

    def _setup_pipeline_logging(
        self,
        pipeline_run_id: str,
        resumed: bool,
        pipeline_loggers: list[logging.Logger],
    ) -> None:
        """为当前管道设置独立日志文件。"""
        try:
            _log_dir = Path.cwd() / "logs"
            _log_dir.mkdir(parents=True, exist_ok=True)
            log_mode = "a" if resumed else "w"
            _handler = logging.FileHandler(
                str(_log_dir / f"pipeline_{pipeline_run_id}.log"),
                encoding="utf-8", mode=log_mode,
            )
            _handler.setLevel(logging.DEBUG)
            _handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S",
            ))
            _handler.addFilter(_PipelineLogFilter(pipeline_run_id))
            for _ln in [
                "pipeline.engine", "pipeline.chain", "pipeline.event_bus",
                "pipeline.route", "pipeline.config", "pipeline.registry",
                "pipeline.stream_bridge",
                "plugins.core", "plugins.input", "plugins.output",
                "infrastructure.task_worker", "tasks",
                "tools.builtin", "evaluation",
                "llm.adapter", "llm.adapter._stream",
                "triggers.manager",
            ]:
                _lg = logging.getLogger(_ln)
                if _lg.level == logging.NOTSET:
                    _lg.setLevel(logging.DEBUG)
                _lg.addHandler(_handler)
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

    async def _cleanup_run_loop(
        self,
        state: dict[str, Any],
        log_handler: logging.FileHandler | None,
        loggers: list[logging.Logger],
        pipeline_id_token: contextvars.Token,
        engine_reg_key: str,
    ) -> None:
        """清理 _run_loop 的资源和注册。"""
        if log_handler:
            log_handler.close()
            for _lg in loggers:
                _lg.removeHandler(log_handler)
        _current_pipeline_id.reset(pipeline_id_token)
        # 清理运行中引擎注册
        self._services.pop(engine_reg_key, None)
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
        """只读：引擎自己管理的管道 ID，外部只能读取不能修改。"""
        return self._pipeline_id

    @property
    def is_suspended(self) -> bool:
        """管道是否处于暂停状态。"""
        return self._suspended_state is not None

    # ------------------------------------------------------------------
    # 挂起/恢复
    # ------------------------------------------------------------------

    async def _suspend_and_wait(self, state: dict[str, Any]) -> None:
        """挂起管道，等待外部通过 wake() 或 message_bus 唤醒。

        管道挂起后不退出 _run_loop，而是 await 内部 _wake_event。
        通过 pipeline.message_bus.send_pipeline_message() 注入消息并唤醒。

        超时（600s）时检查是否有新通知：
        - 有新通知：唤醒管道继续执行
        - 无新通知：重新挂起等待，避免无意义的 LLM 调用循环

        挂起时记录 submitted_task_ids，供外部判断该管道等待哪些任务。
        """
        pipeline_id = state.get(StateKeys.PIPELINE_ID, "")
        self._watching_task_ids = list(state.get("submitted_task_ids", []))
        pending_key = f"__pending_notifications_{pipeline_id}"

        # 消费子任务在父管道挂起前就已入队的通知（竞态修复）
        pending_notifications = self._services.pop(pending_key, [])
        if self._pending_notifications:
            pending_notifications.extend(self._pending_notifications[:])
            self._pending_notifications.clear()
        if pending_notifications:
            self._inject_notifications_to_suspended_state(pending_notifications)
            logger.info(
                "[Engine] 管道挂起时发现 %d 条待处理通知，立即唤醒: pipeline=%s",
                len(pending_notifications), pipeline_id,
            )
            logger.info("[Engine] 管道被唤醒: pipeline=%s", pipeline_id)
            return

        logger.info(
            "[Engine] 管道挂起，等待唤醒: pipeline=%s, watching_tasks=%s",
            pipeline_id, self._watching_task_ids,
        )

        max_wait_rounds = 50
        for wait_round in range(max_wait_rounds):
            self._wake_event = asyncio.Event()
            self._services[f"__suspended_engine_{pipeline_id}"] = self
            register_suspended_engine(pipeline_id, self)

            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=600)
                break  # 被外部唤醒
            except asyncio.TimeoutError:
                # BUG-FIX-fix_20260519_pipeline_retry:
                # 问题根因: 超时处理中先清除引擎注册(__suspended_engine_)再收集通知，
                #   两步之间若有 send_pipeline_message 调用，_find_engine 通过 services
                #   查不到引擎，通知可能走 _try_revive 路径而丢失。
                # 修复方案: 先收集通知再清除注册，确保不遗漏超时窗口内的通知。
                pending_notifications = self._services.pop(pending_key, [])
                if self._pending_notifications:
                    pending_notifications.extend(self._pending_notifications[:])
                    self._pending_notifications.clear()

                if pending_notifications:
                    self._inject_notifications_to_suspended_state(pending_notifications)
                    # 有通知时保留注册直到最终清理，避免唤醒前被其他路径回收
                    logger.info(
                        "[Engine] 管道超时后发现 %d 条通知，唤醒: pipeline=%s",
                        len(pending_notifications), pipeline_id,
                    )
                    break

                # 无通知，安全清除本轮注册（下一轮循环顶部会重新注册）
                self._services.pop(f"__suspended_engine_{pipeline_id}", None)
                unregister_suspended_engine(pipeline_id)
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

        # 最终清理
        self._services.pop(f"__suspended_engine_{pipeline_id}", None)
        unregister_suspended_engine(pipeline_id)
        self._wake_event = None
        self._watching_task_ids = []
        logger.info("[Engine] 管道被唤醒: pipeline=%s", pipeline_id)

    def _inject_notifications_to_suspended_state(self, notifications: list[str]) -> None:
        """将通知消息注入到挂起状态中。"""
        for notif in notifications:
            if self._suspended_state is not None:
                orig = self._suspended_state.get("user_input", "")
                self._suspended_state["user_input"] = f"{notif}\n\n{orig}".strip()
                self._suspended_state.setdefault("messages", []).append(
                    {"role": "user", "content": notif}
                )

    def wake(self) -> None:
        """唤醒挂起的管道（不注入消息）。

        仅用于特殊场景（如 CLI 空输入唤醒）。
        正常消息注入请使用 pipeline.message_bus.send_pipeline_message()。
        """
        if self._wake_event is not None:
            self._wake_event.set()

    def inject_and_wake(self, user_input: str) -> None:
        """向管道注入消息并唤醒。

        根据管道状态选择注入策略：
        - 挂起态（_suspended_state 不为 None）：注入到 _suspended_state 并唤醒
        - 运行态（_suspended_state 为 None）：入队到 _pending_notifications

        Args:
            user_input: 要注入的消息文本
        """
        if not user_input:
            return
        if self._suspended_state is not None:
            self._suspended_state["user_input"] = user_input
        else:
            self._pending_notifications.append(user_input)
        if self._wake_event is not None:
            self._wake_event.set()

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
