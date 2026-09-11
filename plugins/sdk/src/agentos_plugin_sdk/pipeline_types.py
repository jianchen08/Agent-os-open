"""管道插件纯数据类型与接口（0.1 基础类型并入 SDK）。

本模块集中存放 sidecar 真正需要的**纯数据类型和接口**，来源于 0.1 的
``pipeline._base`` 三件套（types.py / plugin.py）。
**不含业务逻辑**：engine / registry / route 等仍留在原 pipeline 包内。

迁入内容：
- 枚举与常量：``TargetType`` / ``StateKeys``
- 数据类：``PluginContext`` / ``PluginResult`` / ``OutputResult``
- 接口：``IPlugin`` / ``IInputPlugin`` / ``ICorePlugin`` / ``IOutputPlugin``
- 工具函数：``create_initial_state`` / ``find_plugin_config``

所有引用改为 SDK 内部相对引用，不再依赖顶层 ``pipeline`` 包。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ── 枚举与常量 ─────────────────────────────────────────


class TargetType(Enum):
    """核心执行目标类型。"""

    LLM_CALL = "llm_call"
    TOOL_EXECUTE = "tool_execute"


class StateKeys:
    """状态字典字段名常量。

    用于统一引用 state 中的键名，避免硬编码字符串。
    """

    ITERATION = "iteration"
    CORE_TYPE = "core_type"
    ENDED = "ended"
    SESSION_ID = "session_id"
    # 任务身份权威键：内核 chat_send_handler 创建管道时注入（值 = pipeline_id）；
    # 任务域键统一点号命名空间（task.*），无下划线 task_id 键。
    TASK_ID = "task.id"
    AGENT_LEVEL = "agent_level"
    RAW_RESULT = "raw_result"
    RAW_ERROR = "raw_error"
    RAW_TOOL_CALLS = "raw_tool_calls"
    RAW_THINKING = "raw_thinking"
    TOOL_RESULTS = "tool_results"
    SHOULD_STOP = "should_stop"
    PIPELINE_ID = "pipeline_id"
    CONVERSATION_MODE = "conversation_mode"
    ATTACHMENTS = "attachments"


# ── 数据类 ─────────────────────────────────────────────


def create_initial_state(**overrides: Any) -> dict[str, Any]:
    """创建管道初始状态字典。

    Args:
        **overrides: 用于覆盖默认值的关键字参数。

    Returns:
        包含所有必要初始字段的管道状态字典。
    """
    state: dict[str, Any] = {
        StateKeys.ITERATION: 0,
        StateKeys.CORE_TYPE: TargetType.LLM_CALL.value,
        StateKeys.ENDED: False,
        StateKeys.SESSION_ID: "",
        StateKeys.TASK_ID: "",
        StateKeys.AGENT_LEVEL: "L1",
        StateKeys.RAW_RESULT: None,
        StateKeys.RAW_ERROR: None,
        StateKeys.RAW_TOOL_CALLS: [],
        StateKeys.RAW_THINKING: None,
        StateKeys.TOOL_RESULTS: [],
        StateKeys.SHOULD_STOP: False,
        StateKeys.CONVERSATION_MODE: False,
    }
    state.update(overrides)
    return state


# ── 插件接口 ───────────────────────────────────────────


class IPlugin(ABC):
    """插件抽象基类。

    所有管道插件的统一接口，提供名称和优先级属性。
    子类必须实现 execute 方法。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """插件唯一标识名称。"""

    @property
    @abstractmethod
    def priority(self) -> int:
        """插件执行优先级，数值越小越先执行。"""

    @abstractmethod
    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行插件逻辑。

        Args:
            ctx: 插件执行上下文，包含状态与配置。

        Returns:
            插件执行结果。
        """


class IInputPlugin(IPlugin):
    """输入插件基类。

    负责在管道循环的输入阶段对状态进行预处理，
    例如参数校验、上下文注入、权限检查等。
    """

    @abstractmethod
    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行输入插件逻辑。"""


class ICorePlugin(IPlugin):
    """核心插件基类。

    负责执行核心逻辑（LLM 调用或工具执行），
    返回包含核心执行结果的字典。
    """

    @abstractmethod
    async def execute(self, ctx: PluginContext) -> dict[str, Any]:  # type: ignore[override]
        # 设计意图：core 插件返回状态更新 dict（与 IPlugin.execute 的 PluginResult 不同，
        # 由引擎包装层转换）；类型系统无法表达该协变契约，豁免 override 检查。
        """执行核心插件逻辑。

        Args:
            ctx: 插件执行上下文。

        Returns:
            核心执行结果字典，将合并到管道状态中。
        """


class IOutputPlugin(IPlugin):
    """输出插件基类。

    负责在管道循环的输出阶段处理核心结果，
    例如结果格式化、后处理、路由信号生成等。
    """

    @property
    def route_signals(self) -> list[str]:
        """本插件可能产出的路由信号类型列表（仅声明用途，不影响执行过滤）。

        Returns:
            路由信号类型字符串列表，空列表表示不声明。
        """
        return []

    @abstractmethod
    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行输出插件逻辑。"""


# ── 插件上下文与结果 ───────────────────────────────────


@dataclass
class PluginContext:
    """插件执行上下文。

    封装管道状态、插件配置和服务访问能力，
    传递给每个插件的 execute 方法。

    Attributes:
        state: 管道当前状态字典
        config: 插件配置字典
        _services: 内部服务注册表，通过 get_service 访问
    """

    state: dict[str, Any]
    config: dict[str, Any] = field(default_factory=dict)
    _services: dict[str, Any] = field(default_factory=dict)

    def get_service(self, name: str) -> Any:
        """按名称获取已注册的服务实例。

        Args:
            name: 服务名称

        Returns:
            服务实例

        Raises:
            KeyError: 服务未注册时抛出
        """
        if name not in self._services:
            raise KeyError(f"Service '{name}' not registered")
        return self._services[name]


@dataclass
class PluginResult:
    """插件执行结果。

    Attributes:
        state_updates: 需要合并到管道状态的更新字典
        skip_remaining: 是否跳过后续插件
        error: 执行过程中的异常
    """

    state_updates: dict[str, Any] = field(default_factory=dict)
    skip_remaining: bool = False
    error: Exception | None = None


@dataclass
class OutputResult(PluginResult):
    """输出插件执行结果。

    继承 PluginResult，专门用于输出插件返回。
    """


def find_plugin_config(
    plugin_name: str,
    plugin_configs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """从 plugin_configs 中查找插件配置，支持前缀匹配。

    查找策略：
    1. 精确匹配：plugin_name == key
    2. 前缀匹配：plugin_name.startswith(key + "_")
    3. 键前缀匹配：key.startswith(plugin_name + "_")

    Args:
        plugin_name: 插件完整名称（如 isolation_guard）
        plugin_configs: plugin_configs 字典

    Returns:
        匹配到的配置字典，未匹配返回空字典
    """
    if not plugin_configs:
        return {}

    if plugin_name in plugin_configs:
        return plugin_configs[plugin_name]

    for key, config in plugin_configs.items():
        if plugin_name.startswith(key + "_"):
            return config
        if key.startswith(plugin_name + "_"):
            return config

    return {}


__all__ = [
    "ICorePlugin",
    "IInputPlugin",
    "IOutputPlugin",
    "IPlugin",
    "OutputResult",
    "PluginContext",
    "PluginResult",
    "StateKeys",
    "TargetType",
    "create_initial_state",
    "find_plugin_config",
]
