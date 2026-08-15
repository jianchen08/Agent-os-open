"""管道插件纯数据类型与接口（0.1 基础类型并入 SDK）。

本模块集中存放 sidecar 真正需要的**纯数据类型和接口**，来源于 0.1 的
``pipeline._base`` 三件套（types.py / plugin.py / plugin_types.py）。
**不含业务逻辑**：engine / registry / route 等仍留在原 pipeline 包内。

迁入内容：
- 枚举与常量：``TargetType`` / ``StateKeys`` / ``ErrorPolicy``
- 数据类：``RouteSignal`` / ``PluginContext`` / ``PluginResult`` / ``OutputResult``
- 接口：``IPlugin`` / ``IInputPlugin`` / ``ICorePlugin`` / ``IOutputPlugin``
- 工具函数：``create_initial_state`` / ``find_plugin_config``
- 类型插槽：``PluginTypeSlot``

所有引用改为 SDK 内部相对引用，不再依赖顶层 ``pipeline`` 包。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
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
    TASK_ID = "task_id"
    AGENT_LEVEL = "agent_level"
    RAW_RESULT = "raw_result"
    RAW_ERROR = "raw_error"
    RAW_TOOL_CALLS = "raw_tool_calls"
    RAW_THINKING = "raw_thinking"
    TOOL_RESULTS = "tool_results"
    EXECUTION_STATUS = "execution_status"
    ERROR_ANALYSIS = "error_analysis"
    LLM_ERROR_HISTORY = "llm_error_history"
    TASK_COMPLETE = "task_complete"
    SHOULD_STOP = "should_stop"
    APPROVAL_REQUIRED = "approval_required"
    ROUTED_TO = "routed_to"
    WAIT_FOR = "wait_for"
    DELEGATION_RESULT = "delegation_result"
    DELEGATION_SCORE = "delegation_score"
    DELEGATION_ERROR = "delegation_error"
    PIPELINE_ID = "pipeline_id"
    CONVERSATION_MODE = "conversation_mode"
    CONVERSATION_ROUND = "conversation_round"
    ATTACHMENTS = "attachments"


class ErrorPolicy(Enum):
    """插件错误处理策略。"""

    ABORT = "abort"
    SKIP = "skip"
    RETRY = "retry"
    FALLBACK = "fallback"


# ── 数据类 ─────────────────────────────────────────────


@dataclass
class RouteSignal:
    """路由信号数据类。

    由插件产生，经输出路由表仲裁后决定管道下一步走向。

    0.2 协议收敛（ROADMAP「路由方式收敛」）：仅支持 next_llm / next_tool /
    end / wait 四种。delegate / fork 已从引擎移除——跨管道路由统一经专门服务
    （任务系统 / 复盘系统）的工具调用显式发起，不产生路由信号；
    decision 下沉为组合插件的 YAML 条件分支（route_check.condition）。

    Attributes:
        route_type: 路由类型，next_llm / next_tool / end / wait
        target: 路由目标，可为字符串、字符串列表或 None
        reason: 路由原因描述
        payload: 附加数据
    """

    route_type: str
    target: str | list[str] | None = None
    reason: str = ""
    payload: dict[str, Any] | None = None


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
        StateKeys.EXECUTION_STATUS: "pending",
        StateKeys.ERROR_ANALYSIS: None,
        StateKeys.LLM_ERROR_HISTORY: [],
        StateKeys.TASK_COMPLETE: False,
        StateKeys.SHOULD_STOP: False,
        StateKeys.APPROVAL_REQUIRED: False,
        StateKeys.CONVERSATION_MODE: False,
        StateKeys.CONVERSATION_ROUND: 0,
    }
    state.update(overrides)
    return state


# ── 类型插槽（先定义，供 PluginContext / IPlugin 引用）──────────────


class PluginTypeSlot:
    """插件类型插槽，提供命名空间隔离的注册与读取 API。

    每个管道共享一个 PluginTypeSlot 实例，插件通过 register_types
    类方法在加载时注册自定义类型，其他插件通过 ctx.plugin_types 读取。

    Usage::

        slot = PluginTypeSlot()
        # 注册
        slot.register_enum("retry", "status", ["pending", "running", "done"])
        slot.register_constant("retry", "max_attempts", 3)
        slot.register_state_key("retry", "attempt_count", default=0)
        slot.register_handler("retry", "on_failure", my_handler)

        # 读取
        StatusEnum = slot.get_enum_class("retry", "status")
        max_val = slot.get_constant("retry", "max_attempts")
        key = slot.get_state_key("retry", "attempt_count")  # "retry.attempt_count"
        handler = slot.get_handler("retry", "on_failure")
    """

    def __init__(self) -> None:
        self._constants: dict[str, dict[str, Any]] = {}
        self._enums: dict[str, dict[str, list[str]]] = {}
        self._state_keys: dict[str, dict[str, Any]] = {}
        self._handlers: dict[str, dict[str, Callable[..., Any]]] = {}
        self._enum_cache: dict[str, type[Enum]] = {}

    # ── 注册 API ──────────────────────────────────────────

    def register_enum(self, namespace: str, name: str, values: list[str]) -> None:
        """注册一个动态枚举类型。

        Args:
            namespace: 命名空间标识
            name: 枚举名称（如 "status"）
            values: 枚举值列表

        Raises:
            ValueError: 同一 namespace + name 已注册时抛出
        """
        if namespace not in self._enums:
            self._enums[namespace] = {}
        if name in self._enums[namespace]:
            raise ValueError(f"Enum '{namespace}.{name}' already registered")
        self._enums[namespace][name] = list(values)

    def register_constant(self, namespace: str, key: str, value: Any) -> None:
        """注册一个常量值。

        Args:
            namespace: 命名空间标识
            key: 常量键名
            value: 常量值

        Raises:
            ValueError: 同一 namespace + key 已注册时抛出
        """
        if namespace not in self._constants:
            self._constants[namespace] = {}
        if key in self._constants[namespace]:
            raise ValueError(f"Constant '{namespace}.{key}' already registered")
        self._constants[namespace][key] = value

    def register_state_key(self, namespace: str, key: str, default: Any = None) -> None:
        """注册一个 state key 及其默认值。

        注册后的 key 格式为 "namespace.key"（如 "retry.attempt_count"）。

        Args:
            namespace: 命名空间标识
            key: 状态键名
            default: 默认值

        Raises:
            ValueError: 同一 namespace + key 已注册时抛出
        """
        if namespace not in self._state_keys:
            self._state_keys[namespace] = {}
        if key in self._state_keys[namespace]:
            raise ValueError(f"State key '{namespace}.{key}' already registered")
        self._state_keys[namespace][key] = default

    def register_handler(self, namespace: str, name: str, handler: Callable[..., Any]) -> None:
        """注册一个处理函数。

        Args:
            namespace: 命名空间标识
            name: 处理函数名称
            handler: 可调用对象

        Raises:
            ValueError: 同一 namespace + name 已注册时抛出
        """
        if namespace not in self._handlers:
            self._handlers[namespace] = {}
        if name in self._handlers[namespace]:
            raise ValueError(f"Handler '{namespace}.{name}' already registered")
        self._handlers[namespace][name] = handler

    # ── 读取 API ──────────────────────────────────────────

    def get_constant(self, namespace: str, key: str, default: Any = None) -> Any:
        """获取常量值。

        Args:
            namespace: 命名空间标识
            key: 常量键名
            default: 未找到时的默认返回值

        Returns:
            常量值，未找到返回 default
        """
        return self._constants.get(namespace, {}).get(key, default)

    def get_enum_class(self, namespace: str, name: str) -> type[Enum]:
        """获取动态生成的枚举类。

        根据 register_enum 注册的信息，动态创建 Enum 子类。
        类名格式：{NamespacePascalCase}{Name}（如 RetryStatus）。

        Args:
            namespace: 命名空间标识
            name: 枚举名称

        Returns:
            动态创建的 Enum 子类

        Raises:
            KeyError: 枚举未注册时抛出
        """
        cache_key = f"{namespace}.{name}"
        if cache_key in self._enum_cache:
            return self._enum_cache[cache_key]

        values = self._enums.get(namespace, {}).get(name)
        if values is None:
            raise KeyError(f"Enum '{cache_key}' not registered")

        # 动态创建 Enum 子类，类名如 RetryStatus
        class_name = f"{namespace.title().replace('_', '')}{name.title().replace('_', '')}"
        members = {v.upper(): v for v in values}
        enum_cls = Enum(class_name, members)  # type: ignore[misc]
        self._enum_cache[cache_key] = enum_cls
        return enum_cls

    def get_state_key(self, namespace: str, key: str) -> str:
        """获取 state key 名。

        Args:
            namespace: 命名空间标识
            key: 状态键名

        Returns:
            格式为 "namespace.key" 的完整键名
        """
        return f"{namespace}.{key}"

    def get_handler(self, namespace: str, name: str) -> Callable[..., Any] | None:
        """获取处理函数。

        Args:
            namespace: 命名空间标识
            name: 处理函数名称

        Returns:
            处理函数，未找到返回 None
        """
        return self._handlers.get(namespace, {}).get(name)

    def get_initial_state_defaults(self) -> dict[str, Any]:
        """获取所有已注册的 state key 默认值。

        Returns:
            字典，键为 "namespace.key" 格式，值为默认值
        """
        defaults: dict[str, Any] = {}
        for namespace, keys in self._state_keys.items():
            for key, default in keys.items():
                defaults[f"{namespace}.{key}"] = default
        return defaults

    # ── 元信息 API ────────────────────────────────────────

    def list_namespaces(self) -> list[str]:
        """列出所有已注册的命名空间。

        Returns:
            命名空间标识列表（去重排序）
        """
        all_ns: set[str] = set()
        all_ns.update(self._constants.keys())
        all_ns.update(self._enums.keys())
        all_ns.update(self._state_keys.keys())
        all_ns.update(self._handlers.keys())
        return sorted(all_ns)

    def list_all(self, namespace: str) -> dict[str, Any]:
        """列出某命名空间下的所有注册项。

        Args:
            namespace: 命名空间标识

        Returns:
            包含 constants、enums、state_keys、handlers 四个键的字典
        """
        return {
            "constants": dict(self._constants.get(namespace, {})),
            "enums": dict(self._enums.get(namespace, {})),
            "state_keys": dict(self._state_keys.get(namespace, {}).items()),
            "handlers": {k: repr(v) for k, v in self._handlers.get(namespace, {}).items()},
        }


# ── 插件接口 ───────────────────────────────────────────


class IPlugin(ABC):
    """插件抽象基类。

    所有管道插件的统一接口，提供名称、优先级和错误策略属性。
    子类必须实现 execute 方法。

    Class Attributes:
        error_policy: 插件错误处理策略，默认 ABORT
    """

    error_policy: ErrorPolicy = ErrorPolicy.ABORT

    @classmethod  # noqa: B027
    def register_types(cls, slots: PluginTypeSlot) -> None:
        """插件可覆盖此方法，在加载时注册自定义类型/变量。默认空实现。

        Args:
            slots: 类型插槽实例，通过它注册枚举、常量、状态键等
        """
        pass

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

    Class Attributes:
        fallback_state: 错误策略为 FALLBACK 时使用的默认状态更新
    """

    fallback_state: dict[str, Any] = {}

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
    plugin_types: PluginTypeSlot = field(default_factory=PluginTypeSlot)

    def __post_init__(self) -> None:
        if self.plugin_types is None:
            self.plugin_types = PluginTypeSlot()

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
        route_signal: 路由信号，仅输出插件有效
        skip_remaining: 是否跳过后续插件
        error: 执行过程中的异常
    """

    state_updates: dict[str, Any] = field(default_factory=dict)
    route_signal: RouteSignal | None = None
    skip_remaining: bool = False
    error: Exception | None = None


@dataclass
class OutputResult(PluginResult):
    """输出插件执行结果。

    继承 PluginResult，专门用于输出插件返回。
    route_signal 字段在输出插件中用于产生路由信号。
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
    "ErrorPolicy",
    "ICorePlugin",
    "IInputPlugin",
    "IOutputPlugin",
    "IPlugin",
    "OutputResult",
    "PluginContext",
    "PluginResult",
    "PluginTypeSlot",
    "RouteSignal",
    "StateKeys",
    "TargetType",
    "create_initial_state",
    "find_plugin_config",
]
