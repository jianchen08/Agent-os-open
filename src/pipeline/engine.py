"""管道引擎 — 核心循环和生命周期管理。

实现核心的 while 循环执行逻辑：
输入路由 → Input 插件链 → Core 插件 → Output 插件链 → 输出路由仲裁 → apply_route，
直到管道结束或挂起。

职责：
- 管道生命周期：run() / resume() / wake()
- 核心循环：_run_loop()
- 挂起/恢复：_suspend_and_wait()
- 路由信号：_apply_route()

已拆出的职责：
- 状态构建 → pipeline/state_builder.py
- 插件配置 → pipeline/plugin_resolver.py
- 检查点管理 → pipeline/checkpoint.py
- 消息注入/唤醒 → pipeline/message_bus.py
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import logging
import traceback as _traceback
import uuid as _uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

_current_pipeline_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_pipeline_id", default=None,
)

_GLOBAL_SUSPENDED_ENGINES: dict[str, PipelineEngine] = {}


def register_suspended_engine(pipeline_id: str, engine: PipelineEngine) -> None:
    _GLOBAL_SUSPENDED_ENGINES[pipeline_id] = engine
    logger.info("[Engine] 全局注册挂起引擎: pipeline=%s, engine_pid=%s, total=%d",
                pipeline_id, id(engine), len(_GLOBAL_SUSPENDED_ENGINES))


def unregister_suspended_engine(pipeline_id: str) -> None:
    _GLOBAL_SUSPENDED_ENGINES.pop(pipeline_id, None)


def get_global_suspended_engine(pipeline_id: str) -> PipelineEngine | None:
    return _GLOBAL_SUSPENDED_ENGINES.get(pipeline_id)


_SAFE_JSON_TYPES = (str, int, float, bool, type(None))

_SKIP_COPY_KEYS = frozenset({
    "on_chunk",
})

def _safe_deepcopy(state: dict) -> dict:
    """安全复制 state，避免 RecursionError。

    BUG-FIX-fix_20260510_pipeline_recursion:
    问题根因: copy.deepcopy(state) 在 state 包含复杂对象时触发 RecursionError，
    导致 _apply_route(wait) 崩溃，管道异常退出。
    修复方案: 放弃 copy.deepcopy，改用逐键手动复制：
    - JSON 安全类型 (str/int/float/bool/None) → 直接引用
    - list/dict → 递归手动复制（受深度限制保护）
    - 其他类型 → 浅拷贝或直接引用
    影响范围: 所有管道的挂起/恢复机制
    """
    import json as _json

    safe = {}
    for k, v in state.items():
        if k in _SKIP_COPY_KEYS:
            continue
        if isinstance(v, _SAFE_JSON_TYPES):
            safe[k] = v
        elif isinstance(v, list):
            safe[k] = _manual_copy_list(v, depth=0)
        elif isinstance(v, dict):
            safe[k] = _manual_copy_dict(v, depth=0)
        elif isinstance(v, (set, tuple)):
            try:
                safe[k] = type(v)(v)
            except (TypeError, ValueError):
                safe[k] = v
        else:
            try:
                safe[k] = _json.loads(_json.dumps(v, default=str))
            except (TypeError, ValueError, _json.JSONDecodeError):
                safe[k] = v
    return safe


_MAX_MANUAL_COPY_DEPTH = 20


def _manual_copy_dict(d: dict, depth: int) -> dict:
    """手动深拷贝 dict，受深度限制保护。"""
    if depth > _MAX_MANUAL_COPY_DEPTH:
        return dict(d)
    result = {}
    for k, v in d.items():
        if isinstance(v, _SAFE_JSON_TYPES):
            result[k] = v
        elif isinstance(v, list):
            result[k] = _manual_copy_list(v, depth + 1)
        elif isinstance(v, dict):
            result[k] = _manual_copy_dict(v, depth + 1)
        else:
            result[k] = v
    return result


def _manual_copy_list(lst: list, depth: int) -> list:
    """手动深拷贝 list，受深度限制保护。"""
    if depth > _MAX_MANUAL_COPY_DEPTH:
        return list(lst)
    result = []
    for v in lst:
        if isinstance(v, _SAFE_JSON_TYPES):
            result.append(v)
        elif isinstance(v, list):
            result.append(_manual_copy_list(v, depth + 1))
        elif isinstance(v, dict):
            result.append(_manual_copy_dict(v, depth + 1))
        else:
            result.append(v)
    return result
from typing import TYPE_CHECKING, Any

from pipeline.chain import PluginChain
from pipeline.plugin import (
    ICorePlugin,
    IInputPlugin,
    IOutputPlugin,
    IPlugin,
    PluginContext,
)
from pipeline.registry import PipelineRegistry, PluginRegistry
from pipeline.route import InputRouteTable, OutputRouteTable
from pipeline.types import RouteSignal, StateKeys, TargetType

if TYPE_CHECKING:
    from infrastructure.checkpoint.pipeline_checkpoint import PipelineCheckpointManager

logger = logging.getLogger(__name__)


class _PipelineLogFilter(logging.Filter):
    """只放行当前 context 中匹配 pipeline_id 的日志记录。"""

    def __init__(self, pipeline_id: str):
        super().__init__()
        self.pipeline_id = pipeline_id

    def filter(self, record: logging.LogRecord) -> bool:
        return _current_pipeline_id.get() == self.pipeline_id


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
        # 同一 PipelineEngine 实例在 WebSocket 长连接中被多次调用 run()，
        # 上一轮的 _suspended_state 会残留，导致 is_suspended 在新一轮中误报 True。
        self._suspended_state = None
        self._wake_event = None
        self._saved_streaming = False
        self._saved_on_chunk = None

        # BUG-FIX-fix_20260513_pipeline_cross_talk:
        # 清除旧 pipeline_id 的引擎注册残留，防止 _find_engine 通过旧 key
        # 找到当前引擎实例，导致通知路由到错误的管道（消息串线）。
        # 场景：WebSocket 长连接中同一引擎实例被不同 thread_id 复用，
        # 上一轮 run() 注册了 __running_engine_{old_pid}，如果正常结束
        # 会在 finally 中清理，但如果引擎被挂起后通过新 run() 恢复，
        # 旧的 __running_engine_ 和 __suspended_engine_ 可能仍残留。
        if self._pipeline_id:
            _old_running_key = f"__running_engine_{self._pipeline_id}"
            _old_suspended_key = f"__suspended_engine_{self._pipeline_id}"
            self._services.pop(_old_running_key, None)
            self._services.pop(_old_suspended_key, None)
            unregister_suspended_engine(self._pipeline_id)
            try:
                from infrastructure.service_provider import get_service_provider
                get_service_provider()._services.pop(_old_running_key, None)
                get_service_provider()._services.pop(_old_suspended_key, None)
            except Exception:
                pass

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
            state: dict[str, Any] = {
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


    def _resolve_output_plugins(
        self, state: dict[str, Any], core_type: str,
    ) -> list[IOutputPlugin]:
        """解析当前迭代需要执行的 Output 插件列表。

        优先使用 output_route_table 的插件路由（与 input_routes 对称），
        当路由表中没有声明 plugins 字段时，回退到 registry 获取全部输出插件。
        兼容测试中使用的 Mock 路由表（无 has_plugin_routing 方法）。

        Args:
            state: 管道当前状态字典
            core_type: 当前核心类型标识

        Returns:
            匹配的输出插件实例列表
        """
        ort = self.output_route_table
        if hasattr(ort, "has_plugin_routing") and ort.has_plugin_routing():
            plugin_names = ort.resolve_plugins(state)
            if plugin_names:
                plugins: list[IOutputPlugin] = []
                for name in plugin_names:
                    plugin = self.plugin_registry.get(name)
                    if isinstance(plugin, IOutputPlugin):
                        plugins.append(plugin)
                    else:
                        logger.debug(
                            "Output route plugin '%s' not found or not IOutputPlugin, skipping",
                            name,
                        )
                return sorted(plugins, key=lambda p: p.priority)

        return self.plugin_registry.get_output_plugins(core_type=core_type)

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
        # 同步引擎实例 ID 与当前管道 ID，防止同一引擎被注册到多个 pipeline_id 下
        # 导致通知路由到错误管道（消息串线）。
        # 场景：引擎复用时 self._pipeline_id 为旧值，state[PIPELINE_ID] 为新值，
        # _suspend_and_wait 的双重注册会将同一引擎挂到两个 pipeline_id 下。
        self._pipeline_id = pipeline_run_id
        _pipeline_id_token = _current_pipeline_id.set(pipeline_run_id)
        # 重置连续错误计数器
        self._consecutive_core_errors = 0
        # BUG-FIX-fix_20260508_sub_pipeline_streaming:
        # 保存流式回调到引擎实例。_safe_deepcopy 无法拷贝函数类型的 on_chunk，
        # 导致 resume() 使用 _suspended_state 时丢失流式回调。在 _run_loop 首次
        # 进入时保存，resume 时恢复。
        if not resumed:
            _on_chunk_val = state.get("on_chunk")
            if _on_chunk_val is not None:
                self._saved_on_chunk = _on_chunk_val
                self._saved_streaming = state.get("streaming", True)
        else:
            # resume 时重新注入流式回调（_safe_deepcopy 会跳过函数类型）
            if self._saved_on_chunk is not None and "on_chunk" not in state:
                state["on_chunk"] = self._saved_on_chunk
                state["streaming"] = self._saved_streaming
        # 注册运行中的引擎引用，供外部通知直接注入
        _engine_reg_key = f"__running_engine_{pipeline_run_id}"
        self._services[_engine_reg_key] = self
        # BUG-FIX-fix_20260509_wake_pipeline:
        # 同时注册到 ServiceProvider，使 TriggerManager 的 _wake_pipeline()
        # 能通过 provider.get() 找到运行中的引擎实例。
        try:
            from infrastructure.service_provider import get_service_provider
            get_service_provider().register(_engine_reg_key, self)
        except Exception:
            pass
        try:
            try:
                _log_dir = Path.cwd() / "logs"
                _log_dir.mkdir(parents=True, exist_ok=True)
                log_mode = "a" if resumed else "w"
                _pipeline_log_handler = logging.FileHandler(
                    str(_log_dir / f"pipeline_{pipeline_run_id}.log"),
                    encoding="utf-8", mode=log_mode,
                )
                _pipeline_log_handler.setLevel(logging.DEBUG)
                _pipeline_log_handler.setFormatter(logging.Formatter(
                    "%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S",
                ))
                _pipeline_log_handler.addFilter(_PipelineLogFilter(pipeline_run_id))
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
                    _lg.addHandler(_pipeline_log_handler)
                    _pipeline_loggers.append(_lg)
            except Exception:
                _pipeline_log_handler = None

            # BUG-FIX-fix_20260426_context_guard: context_window 需在首次迭代前注入 state，
            # 否则 context_window_guard 在第一次 LLM 调用前无法获取到 context_window，
            # 导致守卫完全失效。llm_core 只在调用成功后才写入 context_window，
            # 但 guard 是 Input 插件，在 llm_call 之前执行。
            if not state.get("context_window"):
                _llm_core = self.plugin_registry.get_core("llm_call")
                if _llm_core and hasattr(_llm_core, "_context_window") and _llm_core._context_window:
                    state["context_window"] = _llm_core._context_window

            while not state.get(StateKeys.ENDED, False):
                # 1. 递增迭代计数器
                state[StateKeys.ITERATION] = state.get(StateKeys.ITERATION, 0) + 1
                iteration = state[StateKeys.ITERATION]

                # 安全阀：迭代次数过多时终止（在 Core 执行前检查，避免浪费最后一轮调用）
                if iteration > self.max_iterations:
                    logger.warning("Pipeline exceeded %d iterations, forcing end", self.max_iterations)
                    state[StateKeys.ENDED] = True
                    break

                if resumed:
                    logger.info("=== Pipeline iteration %d (resumed) ===", iteration)
                else:
                    logger.info("=== Pipeline iteration %d ===", iteration)

                # 显示当前使用的模型信息
                _llm_core_iter = self.plugin_registry.get_core("llm_call")
                if _llm_core_iter and hasattr(_llm_core_iter, "_model"):
                    _model_info = (
                        f"{_llm_core_iter._model}"
                        f" (provider={_llm_core_iter._provider}"
                    )
                    if getattr(_llm_core_iter, "_api_base", None):
                        _model_info += f", api_base={_llm_core_iter._api_base}"
                    _model_info += ")"
                    logger.info("Model: %s", _model_info)

                # 发射 iteration 事件供 CLI 状态栏实时更新
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

                # 自动保存检查点（每次迭代开始）
                if self._checkpoint_manager is not None:
                    try:
                        pipeline_id = state.get(StateKeys.PIPELINE_ID, "default")
                        await self._checkpoint_manager.save(pipeline_id, state, phase="auto")
                    except Exception as exc:
                        logger.debug("Checkpoint auto-save failed: %s", exc)

                # 2. 第一步：解析插件列表
                plugin_names = self.input_route_table.resolve_plugins(state)
                logger.info("Input route resolved plugins: %s", plugin_names)

                # 3. target == "core": 继续执行（解析插件后不判断 target，因为 state 还没被 input 插件更新）

                # 4. 获取 Input 插件 → PluginChain 执行
                input_plugins: list[IInputPlugin] = []
                for name in plugin_names:
                    plugin = self.plugin_registry.get(name)
                    if isinstance(plugin, IInputPlugin):
                        input_plugins.append(plugin)

                if input_plugins:
                    input_ctx = PluginContext(state=state, config={}, _services=self._services)
                    input_chain = PluginChain(input_plugins)
                    input_results = await input_chain.execute(input_ctx)
                    for result in input_results:
                        if result.state_updates:
                            state.update(result.state_updates)
                    logger.debug("Input chain completed: %d results", len(input_results))

                # 5. 第二步：用更新后的 state 解析 target
                target, matched_entry = self.input_route_table.resolve_target(state)
                logger.info("Input route resolved target: %s (entry=%s)", target, matched_entry.name if matched_entry else "none")

                # 6. target == "end": 将拦截原因写入 RAW_RESULT，然后结束
                if target == "end":
                    if matched_entry and matched_entry.result:
                        result_msg = matched_entry.format_result(state)
                        state[StateKeys.RAW_RESULT] = result_msg
                        logger.info("Input route end with result: %s", result_msg)
                    state[StateKeys.ENDED] = True
                    logger.info("Pipeline ended by input route (target=end)")
                    break

                # 7. target == "wait": 挂起并等待唤醒信号
                if target == "wait":
                    self._suspended_state = _safe_deepcopy(state)
                    logger.info("Pipeline suspended by input route (target=wait), state saved")
                    if self._checkpoint_manager is not None:
                        try:
                            pipeline_id = state.get(StateKeys.PIPELINE_ID, "default")
                            await self._checkpoint_manager.save(pipeline_id, state, phase="suspended")
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

                # 8. 获取 Core 插件 → 执行，更新 state
                core_type = state.get(StateKeys.CORE_TYPE, "llm_call")
                core_plugin = self.plugin_registry.get_core(core_type)
                if core_plugin is not None:
                    core_ctx = PluginContext(state=state, config={}, _services=self._services)
                    # Core plugin retry with exponential backoff
                    max_core_retries = getattr(core_plugin, "max_retries", 3)
                    core_retry_delay = getattr(core_plugin, "retry_delay", 1.0)
                    core_error_policy = getattr(core_plugin, "error_policy", None)
                    core_attempts = 0
                    while True:
                        core_attempts += 1
                        try:
                            core_result = await core_plugin.execute(core_ctx)
                            if isinstance(core_result, dict):
                                state.update(core_result)
                            logger.debug("Core plugin executed: core_type=%s", core_type)
                            self._consecutive_core_errors = 0
                            break  # success, exit retry loop
                        except Exception as exc:
                            # Check if retryable (RETRY policy + attempts left)
                            from pipeline.types import ErrorPolicy as _EP
                            import random as _rand
                            is_retryable = (
                                core_error_policy == _EP.RETRY
                                and core_attempts < max_core_retries + 1
                            )
                            if is_retryable:
                                # API overload (529/overloaded) uses
                                # a much longer delay
                                exc_lower = str(exc).lower()
                                is_overload = (
                                    "overloaded" in exc_lower
                                    or "529" in exc_lower
                                )
                                if is_overload:
                                    delay = getattr(
                                        core_plugin,
                                        "overload_retry_delay", 180.0,
                                    )
                                else:
                                    delay = core_retry_delay * (
                                        2 ** (core_attempts - 1)
                                    ) * (
                                        0.5 + _rand.random() * 0.5
                                    )
                                logger.warning(
                                    "[%s] Core retry %d/%d"
                                    " (delay=%.1fs%s): %s",
                                    core_type, core_attempts,
                                    max_core_retries, delay,
                                    " [OVERLOAD]" if is_overload else "",
                                    exc,
                                )
                                await asyncio.sleep(delay)
                                continue
                            # Non-retryable or exhausted retries
                            logger.error("Core plugin error: %s", exc)
                            state[StateKeys.RAW_ERROR] = str(exc)
                            state[StateKeys.RAW_RESULT] = None

                            # 追踪连续核心错误，超过阈值强制结束管道
                            error_msg_lower = str(exc).lower()

                            # 判断是否为可恢复错误（不计入连续错误）
                            is_transient = any(
                                kw in error_msg_lower
                                for kw in (
                                    "overloaded", "529", "rate_limit",
                                    "rate limit", "timeout", "timed out",
                                    "503", "502", "connection",
                                )
                            )
                            is_fixable = any(
                                kw in error_msg_lower
                                for kw in (
                                    "context window exceeds",
                                    "context_length_exceeded",
                                    "context length",
                                    "invalid function arguments",
                                    "invalid params",
                                )
                            ) or (
                                "max_tokens" in error_msg_lower
                                and "exceed" in error_msg_lower
                            ) or (
                                "token" in error_msg_lower
                                and "limit" in error_msg_lower
                            )

                            should_count = (
                                core_type == "llm_call"
                                and not is_transient
                                and not is_fixable
                            )

                            if should_count:
                                self._consecutive_core_errors += 1
                                if (
                                    self._consecutive_core_errors
                                    >= self._max_consecutive_core_errors
                                ):
                                    logger.error(
                                        "Pipeline force-ending:"
                                        " %d consecutive core errors",
                                        self._consecutive_core_errors,
                                    )
                                    state[StateKeys.ENDED] = True
                            else:
                                logger.info(
                                    "[%s] error not counting as"
                                    " consecutive (transient=%s,"
                                    " fixable=%s): %s",
                                    core_type, is_transient, is_fixable,
                                    exc,
                                )

                            if core_type == "llm_call":
                                # 将错误信息存入 state，由 llm_error_recovery 插件处理
                                error_msg = str(exc)
                                error_lower = error_msg.lower()

                                is_context_overflow = (
                                    "context window exceeds" in error_lower
                                    or "context_length_exceeded" in error_lower
                                    or "context length" in error_lower
                                    or ("max_tokens" in error_lower and "exceed" in error_lower)
                                    or ("token" in error_lower and "limit" in error_lower)
                                )

                                is_llm_fixable = (
                                    "invalid function arguments" in error_lower
                                    or "invalid params" in error_lower
                                )

                                error_type = (
                                    "context_overflow" if is_context_overflow
                                    else ("llm_fixable" if is_llm_fixable else "unknown")
                                )

                                state["llm_error_info"] = {
                                    "error_msg": error_msg,
                                    "error_type": error_type,
                                    "core_type": core_type,
                                }
                            break  # exit retry loop after error handling
                else:
                    logger.warning("No core plugin registered for type: %s", core_type)

                # 9. 获取 Output 插件 → PluginChain 执行
                output_plugins = self._resolve_output_plugins(state, core_type)
                route_signals: list[RouteSignal] = []

                if output_plugins:
                    plugin_names = [getattr(p, "name", type(p).__name__) for p in output_plugins]
                    logger.debug(
                        "Output plugins for core_type=%s: %s",
                        core_type, plugin_names,
                    )
                    output_ctx = PluginContext(state=state, config={}, _services=self._services)
                    output_chain = PluginChain(output_plugins)
                    output_results = await output_chain.execute(output_ctx)
                    for result in output_results:
                        if result.state_updates:
                            state.update(result.state_updates)
                        if result.route_signal is not None:
                            route_signals.append(result.route_signal)
                    signal_summary = ", ".join(
                        f"{s.route_type}({s.reason[:60]})" for s in route_signals
                    ) if route_signals else "none"
                    logger.info(
                        "Output chain: %d plugins, %d signals [%s], ended=%s",
                        len(output_results), len(route_signals), signal_summary,
                        state.get(StateKeys.ENDED, False),
                    )

                # 10-11. 收集 route_signals → 输出路由表仲裁 → apply_route
                if route_signals:
                    resolved = self.output_route_table.arbitrate(route_signals, state)
                    logger.info(
                        "Route arbitrated: type=%s, target=%s, reason=%s",
                        resolved.route_type, resolved.target, resolved.reason,
                    )
                    should_break = await self._apply_route(resolved, state)
                    if should_break:
                        break
                else:
                    if core_type == "tool_execute":
                        logger.debug("No route signals after tool execution, defaulting to next_llm")
                        state[StateKeys.CORE_TYPE] = "llm_call"
                    elif state.get("thinking_retry_needed"):
                        # 思考截断重试：丢弃本次输出，重新触发 LLM 调用
                        state.pop("thinking_retry_needed", None)
                        state[StateKeys.CORE_TYPE] = "llm_call"
                        retry_count = state.get("thinking_retry_count", 0)
                        logger.info(
                            "Thinking truncated, retrying LLM call "
                            "(retry=%d)", retry_count,
                        )
                    else:
                        # ── 外部通知消费 ──
                        # 子任务终态通知可能在管道结束前就已入队，
                        # 检查队列，有通知则注入 state 继续循环，不急着结束。
                        notif_sources: list[str] = []

                        # 来源1: message_bus 注入的通知消息
                        if self._pending_notifications:
                            notif_sources.extend(self._pending_notifications[:])
                            self._pending_notifications.clear()

                        # 来源2: MessageQueue 兜底消息（inject 时引擎可能刚好在迭代间）
                        try:
                            _mq = self._services.get("message_queue")
                            if _mq is not None:
                                _pid = state.get(StateKeys.PIPELINE_ID, "")
                                if _pid:
                                    while True:
                                        _mq_msg = await _mq.pop(_pid)
                                        if _mq_msg is None:
                                            break
                                        notif_sources.append(_mq_msg.content)
                        except Exception as _mq_err:
                            logger.debug("[Engine] MessageQueue 兜底检查跳过: %s", _mq_err)

                        if notif_sources:
                            combined = "\n\n".join(notif_sources)
                            state["user_input"] = combined
                            state.setdefault("messages", []).append(
                                {"role": "user", "content": combined}
                            )
                            state[StateKeys.CORE_TYPE] = "llm_call"
                            state.pop("raw_result", None)
                            state.pop("error_analysis", None)
                            logger.info(
                                "[Engine] 管道即将结束但发现 %d 条待处理消息，"
                                "注入 state 继续循环",
                                len(notif_sources),
                            )
                            continue

                        # BUG-FIX-fix_20260509_trigger_pipeline_end:
                        # 管道即将结束前检查是否有活跃触发器，
                        # 如果有则挂起等待触发器唤醒，而不是直接结束。
                        _has_active_triggers = False
                        try:
                            from triggers.manager import get_trigger_manager
                            _tm = get_trigger_manager()
                            _pipeline_id = state.get(StateKeys.PIPELINE_ID, self._pipeline_id)
                            _has_active_triggers = any(
                                t.pipeline_id == _pipeline_id
                                and t.status.value == "active"
                                for t in _tm._triggers.values()
                            )
                        except Exception:
                            pass

                        if _has_active_triggers:
                            logger.info(
                                "[Engine] 管道即将结束但存在活跃触发器，"
                                "挂起等待触发器唤醒 (iter=%d)",
                                iteration,
                            )
                            state[StateKeys.CORE_TYPE] = "llm_call"
                            state["user_input"] = (
                                "[系统提示] 管道已挂起，等待触发器唤醒。"
                                "当触发器触发时会自动收到通知并继续执行。"
                            )
                            state.setdefault("messages", []).append(
                                {"role": "user", "content": state["user_input"]}
                            )
                            self._suspended_state = _safe_deepcopy(state)
                            await self._suspend_and_wait(state)
                            if self._suspended_state is not None:
                                state["user_input"] = self._suspended_state.get(
                                    "user_input", state.get("user_input", ""),
                                )
                                state["messages"] = self._suspended_state.get(
                                    "messages", state.get("messages", []),
                                )
                                if "on_chunk" in self._suspended_state:
                                    state["on_chunk"] = self._suspended_state["on_chunk"]
                                    self._saved_on_chunk = self._suspended_state["on_chunk"]
                                if "streaming" in self._suspended_state:
                                    state["streaming"] = self._suspended_state["streaming"]
                                    self._saved_streaming = self._suspended_state["streaming"]
                                self._suspended_state = None
                            continue

                        # 无 route signals → 挂起等待下一条用户消息，而非直接结束。
                        # 管道只在收到显式 end 路由信号时才真正结束。
                        logger.info(
                            "No route signals after LLM response "
                            "(iter=%d), suspending pipeline to wait for next message.",
                            iteration,
                        )
                        # BUG-FIX-fix_20260511_suspended_system_msg:
                        # 问题根因: 挂起前将 "[系统提示] 管道已挂起..." 作为 user 消息
                        # 注入到 messages 中，LLM 会看到这条无意义的消息，且
                        # _inject_and_wake_engine 会把它拼接到用户实际消息后面。
                        # 修复方案: 只作为 user_input 的内部占位符，不注入 messages。
                        state["user_input"] = ""
                        self._suspended_state = _safe_deepcopy(state)
                        await self._suspend_and_wait(state)
                        if self._suspended_state is not None:
                            state["user_input"] = self._suspended_state.get(
                                "user_input", state.get("user_input", ""),
                            )
                            state["messages"] = self._suspended_state.get(
                                "messages", state.get("messages", []),
                            )
                            if "on_chunk" in self._suspended_state:
                                state["on_chunk"] = self._suspended_state["on_chunk"]
                                self._saved_on_chunk = self._suspended_state["on_chunk"]
                            if "streaming" in self._suspended_state:
                                state["streaming"] = self._suspended_state["streaming"]
                                self._saved_streaming = self._suspended_state["streaming"]
                            self._suspended_state = None
                        continue

            # 管道结束后，再执行一次 Output 插件链以保存 PipelineRunSummary 等终态数据
            if state.get(StateKeys.ENDED, False):
                state[StateKeys.ENDED] = True
                core_type = state.get(StateKeys.CORE_TYPE, "llm_call")
                output_plugins = self._resolve_output_plugins(state, core_type)
                if output_plugins:
                    try:
                        output_ctx = PluginContext(state=state, config={}, _services=self._services)
                        output_chain = PluginChain(output_plugins)
                        post_end_results = await output_chain.execute(output_ctx)
                        for result in post_end_results:
                            if result.state_updates:
                                state.update(result.state_updates)
                    except Exception as exc:
                        logger.debug("Post-end output chain failed (non-critical): %s", exc)

        except asyncio.CancelledError:
            _task = asyncio.current_task()
            _must_cancel = getattr(_task, '_must_cancel', None) if _task else None
            logger.warning(
                "Pipeline cancelled | "
                "iteration=%d | _must_cancel=%s | "
                "stack:\n%s",
                state.get(StateKeys.ITERATION, 0),
                _must_cancel,
                ''.join(_traceback.format_stack()),
            )
            state[StateKeys.ENDED] = True
        except Exception as exc:
            # BUG-FIX: 管道未捕获异常保护
            # 确保异常不会导致管道永远不结束，
            # 造成任务卡在 running 状态
            logger.error(
                "Pipeline uncaught exception (iter=%d): %s",
                state.get(StateKeys.ITERATION, 0), exc,
            )
            state[StateKeys.ENDED] = True
            state[StateKeys.RAW_ERROR] = str(exc)
        finally:
            if _pipeline_log_handler:
                _pipeline_log_handler.close()
                for _lg in _pipeline_loggers:
                    _lg.removeHandler(_pipeline_log_handler)
            _current_pipeline_id.reset(_pipeline_id_token)
            # 清理运行中引擎注册
            self._services.pop(_engine_reg_key, None)
            # BUG-FIX-fix_20260509_wake_pipeline:
            # 同步清理 ServiceProvider 中的运行态引擎注册，避免内存泄漏。
            try:
                from infrastructure.service_provider import get_service_provider
                get_service_provider()._services.pop(_engine_reg_key, None)
            except Exception:
                pass
            # 管道结束后自动清理旧检查点，只保留最近 2 个
            if self._checkpoint_manager is not None:
                try:
                    _cp_pipeline_id = state.get(StateKeys.PIPELINE_ID, "default")
                    await self._checkpoint_manager.cleanup_old(_cp_pipeline_id, keep_count=2)
                except Exception as _cp_exc:
                    logger.debug("Checkpoint cleanup failed (non-critical): %s", _cp_exc)
            # NOTE: 不在此处关闭 litellm HTTP 客户端。
            # 客户端生命周期由应用层（cli_main / auto_confirm_runner）
            # 统一管理，每次 run 完就关闭会导致后续请求报
            # "Cannot send a request, as the client has been closed"。

        return state

    @property
    def pipeline_id(self) -> str:
        """只读：引擎自己管理的管道 ID，外部只能读取不能修改。"""
        return self._pipeline_id

    @property
    def is_suspended(self) -> bool:
        """管道是否处于暂停状态。"""
        return self._suspended_state is not None

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
        # 合并两个来源：services 字典（_direct_notify_parent 兜底）和
        # 实例变量（message_bus 注入，管道运行时注入但未在 _run_loop 消费）
        pending_notifications = self._services.pop(pending_key, [])
        if self._pending_notifications:
            pending_notifications.extend(self._pending_notifications[:])
            self._pending_notifications.clear()
        if pending_notifications:
            for notif in pending_notifications:
                if self._suspended_state is not None:
                    orig = self._suspended_state.get("user_input", "")
                    self._suspended_state["user_input"] = (
                        f"{notif}\n\n{orig}".strip()
                    )
                    self._suspended_state.setdefault("messages", []).append(
                        {"role": "user", "content": notif}
                    )
            logger.info(
                "[Engine] 管道挂起时发现 %d 条待处理通知，立即唤醒: "
                "pipeline=%s",
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
                from infrastructure.service_provider import get_service_provider
                get_service_provider().register(
                    f"__suspended_engine_{pipeline_id}", self,
                )
            except Exception:
                pass

            try:
                await asyncio.wait_for(
                    self._wake_event.wait(), timeout=600,
                )
                # 被外部唤醒（message_bus / wake / resume）
                break
            except asyncio.TimeoutError:
                # 清理本轮注册
                self._services.pop(f"__suspended_engine_{pipeline_id}", None)
                try:
                    from infrastructure.service_provider import get_service_provider
                    get_service_provider()._services.pop(
                        f"__suspended_engine_{pipeline_id}", None,
                    )
                except Exception:
                    pass

                # 检查超时期间是否有新通知入队（两个来源合并）
                pending_notifications = self._services.pop(pending_key, [])
                if self._pending_notifications:
                    pending_notifications.extend(self._pending_notifications[:])
                    self._pending_notifications.clear()
                if pending_notifications:
                    for notif in pending_notifications:
                        if self._suspended_state is not None:
                            orig = self._suspended_state.get("user_input", "")
                            self._suspended_state["user_input"] = (
                                f"{notif}\n\n{orig}".strip()
                            )
                            self._suspended_state.setdefault("messages", []).append(
                                {"role": "user", "content": notif}
                            )
                    logger.info(
                        "[Engine] 管道超时后发现 %d 条通知，唤醒: pipeline=%s",
                        len(pending_notifications), pipeline_id,
                    )
                    break

                # 无新数据：重新挂起，不返回给调用方触发无意义 LLM 调用
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
        try:
            from infrastructure.service_provider import get_service_provider
            get_service_provider()._services.pop(
                f"__suspended_engine_{pipeline_id}", None,
            )
        except Exception:
            pass
        self._wake_event = None
        self._watching_task_ids = []
        logger.info("[Engine] 管道被唤醒: pipeline=%s", pipeline_id)

    def wake(self) -> None:
        """唤醒挂起的管道（不注入消息）。

        仅用于特殊场景（如 CLI 空输入唤醒）。
        正常消息注入请使用 pipeline.message_bus.send_pipeline_message()。
        """
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


    async def _apply_route(self, route: RouteSignal, state: dict[str, Any]) -> bool:
        """应用路由信号到管道状态。

        根据路由类型更新状态字典：
        - next_llm → state["core_type"] = "llm_call"
        - next_tool → state["core_type"] = "tool_execute"
        - end → state["ended"] = True
        - delegate → 通过 pipeline_registry.route() 路由，不设 ended=True
        - wait → 保存挂起状态快照

        Args:
            route: 仲裁后的路由信号
            state: 管道状态字典（原地修改）

        Returns:
            是否应中断管道循环（wait 时为 True）
        """
        route_type = route.route_type

        if route_type == "next_llm":
            state[StateKeys.CORE_TYPE] = "llm_call"
            logger.info("Route applied: next_llm")
            return False

        elif route_type == "next_tool":
            state[StateKeys.CORE_TYPE] = "tool_execute"
            if route.target:
                state["tool_name"] = route.target
            logger.info("Route applied: next_tool, target=%s", route.target)
            return False

        elif route_type == "end":
            state[StateKeys.ENDED] = True
            logger.info("Route applied: end, reason=%s", route.reason)
            return False

        elif route_type == "delegate":
            if self.pipeline_registry is not None:
                target = route.target
                if target is not None:
                    target_str = target if isinstance(target, str) else target[0]
                    child_id = await self.pipeline_registry.route(
                        source_id=state.get(StateKeys.PIPELINE_ID, "unknown"),
                        target=target_str,
                        state=state,
                    )
                    state[StateKeys.ROUTED_TO] = child_id
                    logger.info(
                        "Route applied: delegate to %s (pipeline_id=%s)",
                        target_str, child_id,
                    )
            else:
                logger.error(
                    "Route delegate but pipeline_registry is None, "
                    "ending pipeline to prevent dead loop"
                )
                state[StateKeys.ENDED] = True
                state["raw_error"] = "delegate route failed: no pipeline_registry configured"
            return False

        elif route_type == "wait":
            self._suspended_state = _safe_deepcopy(state)
            state[StateKeys.ENDED] = False
            logger.info("Route applied: wait, pipeline suspended")
            await self._suspend_and_wait(state)
            if self._suspended_state is not None:
                state["user_input"] = self._suspended_state.get("user_input", state.get("user_input", ""))
                state["messages"] = self._suspended_state.get("messages", state.get("messages", []))
                self._suspended_state = None
                logger.info("Pipeline woken up from output wait, resetting CORE_TYPE to llm_call")
                state[StateKeys.CORE_TYPE] = "llm_call"
                return False
            return True

        else:
            logger.warning("Unknown route type: %s, defaulting to end", route_type)
            state[StateKeys.ENDED] = True
            return False
