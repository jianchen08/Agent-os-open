"""插件接口与上下文定义。

定义统一的插件抽象基类 IPlugin 及其三个子接口
IInputPlugin、ICorePlugin、IOutputPlugin，
以及插件执行上下文 PluginContext 和执行结果 PluginResult / OutputResult。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from pipeline.types import ErrorPolicy, RouteSignal


class IPlugin(ABC):
    """插件抽象基类。

    所有管道插件的统一接口，提供名称、优先级和错误策略属性。
    子类必须实现 execute 方法。

    Class Attributes:
        error_policy: 插件错误处理策略，默认 ABORT
    """

    error_policy: ErrorPolicy = ErrorPolicy.ABORT

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

    fallback_state: dict[str, Any] = field(default_factory=dict)

    @abstractmethod
    async def execute(self, ctx: PluginContext) -> dict[str, Any]:
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

    每个 Output 插件声明自己关注的 route_signal 类型，
    仅当信号匹配时才执行。
    """

    @property
    def route_signals(self) -> list[str]:
        """本插件关注的路由信号类型列表。

        Returns:
            路由信号类型字符串列表，空列表表示关注所有信号。
        """
        return []

    @abstractmethod
    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行输出插件逻辑。"""


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

    route_signal: RouteSignal | None = None


def find_plugin_config(
    plugin_name: str,
    plugin_configs: dict[str, Any],
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
