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

import logging
import uuid as _uuid
from pathlib import Path
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
from pipeline.types import RouteSignal, StateKeys

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
        max_iterations: int = 100,
        agent_registry: Any | None = None,
        checkpoint_manager: PipelineCheckpointManager | None = None,
    ) -> None:
        self.input_route_table = input_route_table
        self.output_route_table = output_route_table
        self.plugin_registry = plugin_registry
        self.pipeline_registry = pipeline_registry
        self._services = services or {}
        self.max_iterations = max_iterations
        self._agent_registry = agent_registry
        self._suspended_state: dict[str, Any] | None = None
        self._checkpoint_manager = checkpoint_manager

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
        # 生成管道运行 ID（如果调用方未通过 extra_state 传入）
        if "pipeline_id" not in extra_state:
            extra_state["pipeline_id"] = _uuid.uuid4().hex[:12]

        if initial_state is not None and user_input is None:
            state: dict[str, Any] = {
                **initial_state,
                StateKeys.ITERATION: 0,
                StateKeys.ENDED: False,
            }
            if StateKeys.PIPELINE_ID not in state:
                state[StateKeys.PIPELINE_ID] = _uuid.uuid4().hex[:12]
            return await self._run_loop(state, resumed=False)

        if isinstance(user_input, dict) and initial_state is None:
            state: dict[str, Any] = {
                **user_input,
                StateKeys.ITERATION: 0,
                StateKeys.ENDED: False,
            }
            if StateKeys.PIPELINE_ID not in state:
                state[StateKeys.PIPELINE_ID] = _uuid.uuid4().hex[:12]
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
                    continue
                logger.info(
                    "Agent enables non-default plugin: %s (config=%s)",
                    name, config,
                )

        return result

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

    async def _run_loop(self, state: dict[str, Any], *, resumed: bool = False) -> dict[str, Any]:
        """管道核心循环。

        循环流程：
        1. 递增迭代计数器
        2. 输入路由表解析 → 得到插件列表和 target
        3. target == "end": 设 ended=True, break
        4. target == "wait": break（挂起）
        5. target == "core": 继续执行
        6. 获取 Input 插件 → PluginChain 执行
        7. 获取 Core 插件 → 执行，更新 state
        8. 获取 Output 插件 → PluginChain 执行
        9. 收集 route_signals
        10. 输出路由表仲裁 → apply_route

        Args:
            state: 管道状态字典（原地修改）
            resumed: 是否从暂停状态恢复，控制日志前缀

        Returns:
            管道最终状态字典
        """
        while not state.get(StateKeys.ENDED, False):
            # 1. 递增迭代计数器
            state[StateKeys.ITERATION] = state.get(StateKeys.ITERATION, 0) + 1
            iteration = state[StateKeys.ITERATION]
            if resumed:
                logger.info("=== Pipeline iteration %d (resumed) ===", iteration)
            else:
                logger.info("=== Pipeline iteration %d ===", iteration)

            # 自动保存检查点（每次迭代开始）
            if self._checkpoint_manager is not None:
                try:
                    pipeline_id = state.get(StateKeys.PIPELINE_ID, "default")
                    await self._checkpoint_manager.save(pipeline_id, state, phase="auto")
                except Exception as exc:
                    logger.debug("Checkpoint auto-save failed: %s", exc)

            # 2. 输入路由表解析
            plugin_names, target = self.input_route_table.resolve(state)
            logger.info("Input route resolved: plugins=%s, target=%s", plugin_names, target)

            # 3. target == "end": 直接结束
            if target == "end":
                state[StateKeys.ENDED] = True
                logger.info("Pipeline ended by input route (target=end)")
                break

            # 4. target == "wait": 挂起并保存 state 快照
            if target == "wait":
                self._suspended_state = dict(state)
                logger.info("Pipeline suspended by input route (target=wait), state saved")

                if self._checkpoint_manager is not None:
                    try:
                        pipeline_id = state.get(StateKeys.PIPELINE_ID, "default")
                        await self._checkpoint_manager.save(pipeline_id, state, phase="suspended")
                    except Exception as exc:
                        logger.debug("Checkpoint suspended-save failed: %s", exc)
                break

            # 5. target == "core": 继续执行

            # 6. 获取 Input 插件 → PluginChain 执行
            input_plugins: list[IInputPlugin] = []
            for name in plugin_names:
                plugin = self.plugin_registry.get(name)
                if isinstance(plugin, IInputPlugin):
                    input_plugins.append(plugin)

            if input_plugins:
                input_ctx = PluginContext(state=dict(state), config={}, _services=self._services)
                input_chain = PluginChain(input_plugins)
                input_results = await input_chain.execute(input_ctx)
                for result in input_results:
                    if result.state_updates:
                        state.update(result.state_updates)
                logger.debug("Input chain completed: %d results", len(input_results))

            # 7. 获取 Core 插件 → 执行，更新 state
            core_type = state.get(StateKeys.CORE_TYPE, "llm_call")
            core_plugin = self.plugin_registry.get_core(core_type)
            if core_plugin is not None:
                core_ctx = PluginContext(state=dict(state), config={}, _services=self._services)
                try:
                    core_result = await core_plugin.execute(core_ctx)
                    if isinstance(core_result, dict):
                        state.update(core_result)
                    logger.debug("Core plugin executed: core_type=%s", core_type)
                except Exception as exc:
                    logger.error("Core plugin error: %s", exc)
                    state[StateKeys.RAW_ERROR] = str(exc)
            else:
                logger.warning("No core plugin registered for type: %s", core_type)

            # 8. 获取 Output 插件 → PluginChain 执行
            output_plugins = self.plugin_registry.get_output_plugins(
                core_type=core_type
            )
            route_signals: list[RouteSignal] = []

            if output_plugins:
                output_ctx = PluginContext(state=dict(state), config={}, _services=self._services)
                output_chain = PluginChain(output_plugins)
                output_results = await output_chain.execute(output_ctx)
                for result in output_results:
                    if result.state_updates:
                        state.update(result.state_updates)
                    if result.route_signal is not None:
                        route_signals.append(result.route_signal)
                logger.debug("Output chain completed: %d results, %d signals", len(output_results), len(route_signals))

            # 9-10. 输出路由表仲裁 → apply_route
            if route_signals:
                resolved = self.output_route_table.arbitrate(route_signals, state)
                logger.info(
                    "Route arbitrated: type=%s, target=%s, reason=%s",
                    resolved.route_type, resolved.target, resolved.reason,
                )
                await self._apply_route(resolved, state)
            else:
                if core_type == "tool_execute":
                    logger.debug("No route signals after tool execution, defaulting to next_llm")
                    state[StateKeys.CORE_TYPE] = "llm_call"
                else:
                    logger.debug("No route signals after LLM response, ending turn")
                    state[StateKeys.ENDED] = True

            # 安全阀：迭代次数过多时终止
            if state[StateKeys.ITERATION] >= self.max_iterations:
                logger.warning("Pipeline exceeded %d iterations, forcing end", self.max_iterations)
                state[StateKeys.ENDED] = True

        # 管道结束后，再执行一次 Output 插件链以保存 PipelineRunSummary 等终态数据
        if state.get(StateKeys.ENDED, False):
            state[StateKeys.ENDED] = True
            output_plugins = self.plugin_registry.get_output_plugins(state)
            if output_plugins:
                try:
                    output_ctx = PluginContext(state=dict(state), config={}, _services=self._services)
                    output_chain = PluginChain(output_plugins)
                    await output_chain.execute(output_ctx)
                    for result in output_chain.results:
                        if result.state_updates:
                            state.update(result.state_updates)
                except Exception as exc:
                    logger.debug("Post-end output chain failed (non-critical): %s", exc)

        return state

    @property
    def is_suspended(self) -> bool:
        """管道是否处于暂停状态。"""
        return self._suspended_state is not None

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

    async def _apply_route(self, route: RouteSignal, state: dict[str, Any]) -> None:
        """应用路由信号到管道状态。

        根据路由类型更新状态字典：
        - next_llm → state["core_type"] = "llm_call"
        - next_tool → state["core_type"] = "tool_execute"
        - end → state["ended"] = True
        - delegate → 通过 pipeline_registry.route() 路由，不设 ended=True
        - wait → 不做变更（管道挂起）

        Args:
            route: 仲裁后的路由信号
            state: 管道状态字典（原地修改）
        """
        route_type = route.route_type

        if route_type == "next_llm":
            state[StateKeys.CORE_TYPE] = "llm_call"
            logger.info("Route applied: next_llm")

        elif route_type == "next_tool":
            state[StateKeys.CORE_TYPE] = "tool_execute"
            if route.target:
                state["tool_name"] = route.target
            logger.info("Route applied: next_tool, target=%s", route.target)

        elif route_type == "end":
            state[StateKeys.ENDED] = True
            logger.info("Route applied: end, reason=%s", route.reason)

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
                logger.warning("Route delegate but no PipelineRegistry configured")

        elif route_type == "wait":
            logger.info("Route applied: wait")

        else:
            logger.warning("Unknown route type: %s, defaulting to end", route_type)
            state[StateKeys.ENDED] = True
