"""管道引擎。

实现核心的 while 循环执行逻辑：
输入路由 → Input 插件链 → Core 插件 → Output 插件链 → 输出路由仲裁 → apply_route，
直到管道结束或挂起。

支持暂停/恢复：
- 当 Output 插件产出 wait 路由信号时，管道保存当前 state 快照并挂起
- 调用 resume() 可从保存的 state 恢复继续执行

支持 Agent 配置注入：
- run(user_input, agent_config) 接受 Agent 配置，自动构建 state
- agent_config=None 时使用系统默认 Agent 配置
- 插件从 ctx.state["plugin_configs"] 读取自己的配置
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import logging
import traceback as _traceback
import uuid as _uuid
from pathlib import Path

_current_pipeline_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_pipeline_id", default=None,
)


def _safe_deepcopy(state: dict) -> dict:
    """Deep-copy state, skipping keys that hold non-deepcopyable values."""
    safe = {}
    for k, v in state.items():
        try:
            safe[k] = copy.deepcopy(v)
        except (TypeError, AttributeError):
            pass
    return safe
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
        max_iterations: int = 100,
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

    async def run(
        self,
        user_input: str | None = None,
        agent_config: Any | None = None,
        conversation_history: list[dict[str, Any]] | None = None,
        initial_state: dict[str, Any] | None = None,
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
            **extra_state: 额外注入的 state 键值对

        Returns:
            管道最终状态字典
        """
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
            agent_config = self._load_system_default_agent()

        state = self._build_initial_state(
            user_input=user_input or "",
            agent_config=agent_config,
            conversation_history=conversation_history,
            extra_state=extra_state,
        )

        if agent_config and hasattr(agent_config, "max_iterations") and agent_config.max_iterations:
            self.max_iterations = agent_config.max_iterations

        self._apply_agent_plugin_configs(agent_config)
        self._apply_agent_model_override(agent_config)

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

    def _build_initial_state(
        self,
        user_input: str,
        agent_config: Any | None,
        conversation_history: list[dict[str, Any]] | None,
        extra_state: dict[str, Any],
    ) -> dict[str, Any]:
        """构建管道初始状态字典。

        将用户输入、Agent 配置、对话历史和额外状态合并为管道 state。

        Args:
            user_input: 用户输入文本
            agent_config: Agent 配置实例
            conversation_history: 对话历史
            extra_state: 额外状态键值对

        Returns:
            管道初始状态字典
        """
        state: dict[str, Any] = {
            StateKeys.ITERATION: 0,
            StateKeys.CORE_TYPE: TargetType.LLM_CALL.value,
            StateKeys.ENDED: False,
            "user_input": user_input,
            "messages": list(conversation_history) if conversation_history else [],
        }

        # 将 user_input 追加为 user 消息（避免纯 system 消息被某些模型拒绝）
        if user_input:
            state["messages"].append({"role": "user", "content": user_input})

        if agent_config and hasattr(agent_config, "to_state"):
            agent_state = agent_config.to_state()
            state.update(agent_state)

        state.update(extra_state)

        return state

    def _load_system_default_agent(self) -> Any | None:
        """加载系统默认 Agent 配置。

        优先使用注入的 agent_registry；
        如果没有注入，则内部创建 AgentRegistry。

        优先级：
        1. config/agents/default.yaml
        2. config/agents/lingxi.yaml
        3. 返回空配置（Engine 内部用默认值兜底）

        Returns:
            AgentConfig 实例，未找到返回 None
        """
        if self._agent_registry is not None:
            registry = self._agent_registry
        else:
            from agents.registry import AgentRegistry
            from agents.types import AgentLevel

            project_root = Path(__file__).resolve().parent.parent.parent
            agent_config_dir = project_root / "config" / "agents"

            if not agent_config_dir.exists():
                return None

            registry = AgentRegistry()
            count = registry.load_directory(agent_config_dir)
            if count == 0:
                return None

        from agents.types import AgentLevel

        for candidate in ["default", "lingxi"]:
            config = registry.get(candidate)
            if config:
                return config

        l1_agents = registry.find_by_level(AgentLevel.L1_MAIN)
        if l1_agents:
            return l1_agents[0]

        return None

    def _build_plugin_list(self, agent_config: Any | None) -> list[IPlugin]:
        """根据 Agent 配置构建最终插件列表。

        配置合并逻辑：
        1. 从 PluginRegistry 获取所有已注册插件（Pipeline 默认）
        2. 移除 agent_config.plugins.disabled 中声明的插件
        3. 添加 agent_config.plugins.enabled 中声明的非默认插件

        Args:
            agent_config: Agent 配置实例

        Returns:
            最终生效的插件列表
        """
        result: list[IPlugin] = []

        for plugin in self.plugin_registry._plugins.values():
            result.append(plugin)

        if not agent_config or not hasattr(agent_config, "plugins"):
            return result

        plugins_config = agent_config.plugins

        if hasattr(plugins_config, "disabled") and plugins_config.disabled:
            result = [p for p in result if not self._matches_disabled(p.name, plugins_config.disabled)]

        if hasattr(plugins_config, "enabled") and plugins_config.enabled:
            for name, config in plugins_config.enabled.items():
                existing = self.plugin_registry.get(name)
                if existing is not None:
                    if isinstance(config, dict) and hasattr(existing, '_config'):
                        merged_config = {**existing._config, **config}
                        try:
                            new_plugin = type(existing)(config=merged_config)
                            # 在 result 列表中替换旧引用
                            for idx, p in enumerate(result):
                                if p is existing:
                                    result[idx] = new_plugin
                                    break
                        except Exception:
                            pass
                    continue
                logger.info(
                    "Agent enables non-default plugin: %s (config=%s)",
                    name, config,
                )

        return result

    def _apply_agent_plugin_configs(
        self, agent_config: Any | None,
    ) -> None:
        """将 Agent 配置的插件覆盖直接合并到 plugin_registry。

        遍历 agent_config.plugins.enabled，将配置合并到 registry 中
        已有的同名插件实例上（原地替换），使后续 _run_loop 通过
        registry.get() 拿到的就是合并后的插件。

        Args:
            agent_config: Agent 配置实例
        """
        if not agent_config or not hasattr(agent_config, "plugins"):
            return

        plugins_config = agent_config.plugins
        if not hasattr(plugins_config, "enabled") or not plugins_config.enabled:
            return

        for name, override in plugins_config.enabled.items():
            if not isinstance(override, dict):
                continue
            existing = self.plugin_registry.get(name)
            if existing is None:
                continue
            if not hasattr(existing, "_config"):
                continue
            merged_config = {**existing._config, **override}
            try:
                new_plugin = type(existing)(config=merged_config)
                self.plugin_registry._plugins[name] = new_plugin
                # 同步 core_plugins 映射
                for core_key, pname in list(
                    self.plugin_registry._core_plugins.items()
                ):
                    if pname == name:
                        self.plugin_registry._core_plugins[core_key] = name
                logger.debug(
                    "Agent plugin config merged: %s + %s -> %s",
                    name, list(override.keys()), list(merged_config.keys()),
                )
            except Exception:
                logger.debug(
                    "Agent plugin config merge failed for %s", name,
                )

    def _apply_agent_model_override(self, agent_config: Any | None) -> None:
        """将 Agent 配置中的 model_name/model_tier 覆盖到 llm_call 核心插件。

        优先级：model_name > model_tier > defaults.chat
        - model_name: 直接使用指定的模型标识
        - model_tier: 从 llm.yaml defaults.tiers 解析为 model_name
        - Router 模式：直接切换路由别名，不重建插件
        - 直连模式：从 llm.yaml 加载完整配置重建插件

        Args:
            agent_config: Agent 配置实例
        """
        if not agent_config or not hasattr(agent_config, "model_name"):
            return

        model_id = agent_config.model_name

        # model_tier 解析：当 model_name 未指定时，从 tiers 映射
        if not model_id and hasattr(agent_config, "model_tier") and agent_config.model_tier:
            model_id = self._resolve_tier(agent_config.model_tier)
            if model_id:
                logger.info(
                    "[_apply_agent_model_override] model_tier=%s → model_name=%s",
                    agent_config.model_tier, model_id,
                )

        if not model_id:
            return

        llm_call = self.plugin_registry.get_core("llm_call")
        if llm_call is None:
            return

        # Router 模式：只需切换路由别名，不重建插件
        if getattr(llm_call, "_use_router", False):
            llm_call._model = model_id
            # 更新 context_window（从 model_loader 读取）
            services = getattr(self, "_services", {})
            model_loader = services.get("model_loader") if services else None
            if model_loader:
                conf = model_loader.get_model_config(model_id)
                if conf:
                    llm_call._provider = conf.get("provider", llm_call._provider)
                    llm_call._context_window = conf.get("context_window")
            logger.info(
                "[_apply_agent_model_override] Router 模式切换模型: %s (provider=%s, context_window=%s)",
                model_id, llm_call._provider, llm_call._context_window,
            )
            return

        # 直连模式：重建插件（原有逻辑）
        model_loader = None
        services = getattr(self, "_services", {})
        if services:
            model_loader = services.get("model_loader")

        if model_loader is None:
            try:
                from config.models import get_model_config_loader
                model_loader = get_model_config_loader()
            except Exception:
                logger.warning(
                    "[_apply_agent_model_override] ModelConfigLoader 不可用，跳过模型覆盖"
                )
                return

        llm_conf = model_loader.get_llm_core_config(model_id)
        if not llm_conf:
            logger.warning(
                "[_apply_agent_model_override] 模型 %r 未在 llm.yaml 中找到配置，跳过覆盖",
                model_id,
            )
            return

        # 以现有 llm_call 配置为基础，用模型配置覆盖
        # 这样 provider/api_base/api_key 等字段跟随模型配置
        if hasattr(llm_call, "_config") and isinstance(llm_call._config, dict):
            merged_config = dict(llm_call._config)
            merged_config.update(llm_conf)
        else:
            merged_config = dict(llm_conf)
        merged_config["model_name"] = llm_conf.get("model_name", model_id)

        try:
            # 复用已有的 AdaptiveRouterAdapter（自适应并发）
            existing_adapter = getattr(llm_call, "_adapter", None)
            if existing_adapter is not None:
                new_plugin = type(llm_call)(config=merged_config, adapter=existing_adapter)
            else:
                new_plugin = type(llm_call)(config=merged_config)
            plugin_name = getattr(llm_call, "name", "LLMCorePlugin")
            self.plugin_registry._core_plugins["llm_call"] = new_plugin
            self.plugin_registry._plugins[plugin_name] = new_plugin
            logger.info(
                "[_apply_agent_model_override] Agent %s 使用模型: %s (provider=%s, context_window=%s)",
                getattr(agent_config, "config_id", "?"),
                merged_config.get("model_name"),
                merged_config.get("provider"),
                merged_config.get("context_window"),
            )
        except Exception as exc:
            logger.warning(
                "[_apply_agent_model_override] 重建 llm_call 插件失败: %s", exc,
            )

    def _resolve_tier(self, tier: str) -> str:
        """从 llm.yaml defaults.tiers 解析 tier 为 model_id。

        Args:
            tier: 分级标识（large/medium/small）

        Returns:
            对应的模型标识字符串，未找到返回空字符串
        """
        services = getattr(self, "_services", {})
        model_loader = services.get("model_loader") if services else None
        if model_loader is None:
            try:
                from config.models import get_model_config_loader
                model_loader = get_model_config_loader()
            except Exception:
                logger.warning("[_resolve_tier] ModelConfigLoader 不可用")
                return ""

        llm_data = model_loader._load_llm_data()
        tiers = llm_data.get("defaults", {}).get("tiers", {})
        model_id = tiers.get(tier, "")
        if not model_id:
            logger.warning("[_resolve_tier] tier=%r 未在 llm.yaml defaults.tiers 中定义", tier)
        return model_id

    @staticmethod
    def _matches_disabled(plugin_name: str, disabled_names: list[str]) -> bool:
        """检查插件名称是否匹配禁用列表。

        支持精确匹配和前缀匹配：
        - 'isolation_guard' 精确匹配 'isolation_guard'
        - 'isolation_guard' 前缀匹配 'isolation'（以 _ 分隔）

        Args:
            plugin_name: 插件完整名称
            disabled_names: 禁用名称列表

        Returns:
            是否匹配禁用列表
        """
        for disabled in disabled_names:
            if plugin_name == disabled:
                return True
            if plugin_name.startswith(disabled + "_"):
                return True
        return False

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
        _pipeline_id_token = _current_pipeline_id.set(pipeline_run_id)
        # 重置连续错误计数器
        self._consecutive_core_errors = 0
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
                    "plugins.core", "plugins.input", "plugins.output",
                    "infrastructure.task_worker", "tasks",
                    "tools.builtin", "evaluation",
                    "llm.adapter", "llm.adapter._stream",
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
                        # BUG-FIX: 无 route signals 时记录详细诊断信息
                        # 便于排查管道异常退出原因
                        _raw_tool_calls = state.get("raw_tool_calls", [])
                        _raw_result = state.get("raw_result")
                        _error_analysis = state.get("error_analysis")
                        _ended = state.get(StateKeys.ENDED, False)
                        logger.info(
                            "No route signals after LLM response "
                            "(iter=%d), ending pipeline. "
                            "output_plugins_count=%d, "
                            "raw_tool_calls=%d, ended=%s, "
                            "has_result=%s, "
                            "error_analysis=%s",
                            iteration,
                            len(output_plugins) if output_plugins else 0,
                            len(_raw_tool_calls),
                            _ended,
                            _raw_result is not None,
                            _error_analysis,
                        )
                        state[StateKeys.ENDED] = True

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
        """挂起管道，等待外部通过 wake() 或 inject_and_wake() 唤醒。

        管道挂起后不退出 _run_loop，而是 await 内部 _wake_event。
        任何外部系统都可以通过 inject_and_wake() 注入消息并唤醒管道。

        挂起时记录 submitted_task_ids，供外部判断该管道等待哪些任务。
        这是管道基础设施的通用机制，不依赖任何业务系统。
        """
        self._wake_event = asyncio.Event()
        pipeline_id = state.get(StateKeys.PIPELINE_ID, "")
        self._watching_task_ids = list(state.get("submitted_task_ids", []))
        self._services[f"__suspended_engine_{pipeline_id}"] = self

        # 同步注册到 ServiceProvider，供 TaskTool 等跨管道工具查找挂起引擎
        try:
            from infrastructure.service_provider import get_service_provider
            get_service_provider().register(
                f"__suspended_engine_{pipeline_id}", self,
            )
        except Exception:
            pass

        # 消费子任务在父管道挂起前就已入队的通知（竞态修复）
        pending_key = f"__pending_notifications_{pipeline_id}"
        pending_notifications = self._services.pop(pending_key, [])
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
            self._wake_event.set()

        logger.info(
            "[Engine] 管道挂起，等待唤醒: pipeline=%s, watching_tasks=%s",
            pipeline_id, self._watching_task_ids,
        )
        try:
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(), timeout=600,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[Engine] 管道等待唤醒超时(600s)，自动恢复"
                )
        finally:
            self._services.pop(f"__suspended_engine_{pipeline_id}", None)
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

        供任何外部系统在需要时主动恢复管道。
        """
        if self._wake_event is not None:
            self._wake_event.set()

    def inject_and_wake(self, user_input: str) -> None:
        """向挂起的管道注入消息并唤醒。

        将 user_input 前置注入到 _suspended_state，然后唤醒管道。
        下一轮迭代时 LLM 会看到注入的消息。

        这是管道基础设施提供的通用消息注入能力，
        任何外部系统都可以调用来通知管道。

        Args:
            user_input: 要注入的消息文本
        """
        if self._suspended_state is not None and user_input:
            orig = self._suspended_state.get("user_input", "")
            self._suspended_state["user_input"] = f"{user_input}\n\n{orig}".strip()
            # 注入到 messages，确保 _build_messages() 能让 LLM 看到
            self._suspended_state.setdefault("messages", []).append(
                {"role": "user", "content": user_input}
            )
            logger.info("[Engine] 消息已注入到挂起管道 state (%d 字符)", len(user_input))
        if self._wake_event is not None:
            self._wake_event.set()

    async def save_checkpoint(self, phase: str = "manual") -> str | None:
        """手动保存管道检查点。

        Args:
            phase: 检查点阶段标记

        Returns:
            检查点 ID，无检查点管理器时返回 None
        """
        if self._checkpoint_manager is None:
            logger.warning("No checkpoint manager configured")
            return None

        current_state = self._suspended_state or {}
        if not current_state:
            logger.warning("No state to checkpoint")
            return None

        pipeline_id = current_state.get(StateKeys.PIPELINE_ID, "default")
        try:
            checkpoint_id = await self._checkpoint_manager.save(pipeline_id, current_state, phase=phase)
            logger.info("Checkpoint saved: %s (phase=%s)", checkpoint_id, phase)
            return checkpoint_id
        except Exception as exc:
            logger.error("Failed to save checkpoint: %s", exc)
            return None

    async def restore_from_checkpoint(self, checkpoint_id: str) -> bool:
        """从检查点恢复管道状态。

        Args:
            checkpoint_id: 检查点 ID

        Returns:
            是否恢复成功
        """
        if self._checkpoint_manager is None:
            logger.warning("No checkpoint manager configured")
            return False

        try:
            data = await self._checkpoint_manager.load(checkpoint_id)
            if data is None:
                logger.error("Checkpoint not found: %s", checkpoint_id)
                return False

            state = data.get("state", {})
            self._suspended_state = state
            logger.info(
                "Restored from checkpoint %s (iteration=%d)",
                checkpoint_id, state.get(StateKeys.ITERATION, 0),
            )
            return True
        except Exception as exc:
            logger.error("Failed to restore from checkpoint: %s", exc)
            return False


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
                return False
            return True

        else:
            logger.warning("Unknown route type: %s, defaulting to end", route_type)
            state[StateKeys.ENDED] = True
            return False
