"""熔断器 Input 插件。

基于三态模型实现管道级别的熔断保护：
CLOSED（正常）→ OPEN（熔断）→ HALF_OPEN（探测）。

当连续失败次数达到阈值时自动熔断，阻止请求继续通过管道；
经过恢复超时后进入半开状态，允许有限次数的探测请求；
探测成功则恢复为 CLOSED，失败则回到 OPEN。

State 命名空间：
    - consecutive_failures : 外部写入的连续失败计数
    - circuit_open : 本插件写入的熔断标记
    - circuit_state : 本插件写入的当前熔断器状态
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)


class CircuitBreaker(IInputPlugin):
    """熔断器 Input 插件。

    三态模型：CLOSED（正常）→ OPEN（熔断）→ HALF_OPEN（探测）

    Attributes:
        failure_threshold: 连续失败 N 次后熔断（默认 5）
        recovery_timeout: OPEN → HALF_OPEN 的等待秒数（默认 60）
        half_open_max_calls: 半开状态允许的探测次数（默认 1）
    """

    error_policy = ErrorPolicy.SKIP

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化熔断器插件。

        Args:
            config: 插件配置字典，支持以下键：
                - failure_threshold: 连续失败熔断阈值（默认 5）
                - recovery_timeout: 恢复等待秒数（默认 60）
                - half_open_max_calls: 半开探测次数（默认 1）
                - priority: 插件优先级（默认 10）
        """
        self._config = config or {}
        self._failure_threshold = self._config.get("failure_threshold", 5)
        self._recovery_timeout = self._config.get("recovery_timeout", 60)
        self._half_open_max_calls = self._config.get("half_open_max_calls", 1)
        self._state = self.CLOSED
        self._last_failure_time: float = 0.0
        self._half_open_calls = 0
        self._enabled_by_agent: bool = True

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "circuit_breaker"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 10)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行熔断器检查。

        根据当前熔断器状态决定是否放行请求：
        1. CLOSED：检查连续失败次数，达到阈值则熔断
        2. OPEN：检查是否超过恢复超时，是则进入半开状态
        3. HALF_OPEN：限制探测次数，根据结果决定恢复或重新熔断

        从 ctx.state["plugin_configs"] 读取 Agent 覆盖的配置，
        未配置时使用构造函数的默认值。

        Args:
            ctx: 插件执行上下文

        Returns:
            插件执行结果。熔断时通过 skip_remaining 短路管道。
        """
        self._apply_runtime_config(ctx)

        if self._state == self.CLOSED:
            return self._handle_closed(ctx)
        if self._state == self.OPEN:
            return self._handle_open(ctx)
        return self._handle_half_open(ctx)

    def _apply_runtime_config(self, ctx: PluginContext) -> None:
        """从 ctx.state 读取 Agent 覆盖的运行时配置。

        Agent 可通过 plugins.enabled.circuit_breaker 覆盖默认参数。

        Args:
            ctx: 插件执行上下文
        """
        from pipeline.plugin import find_plugin_config  # noqa: PLC0415

        plugin_configs = ctx.state.get("plugin_configs", {})
        config = find_plugin_config("circuit_breaker", plugin_configs)

        if not config.get("enabled", True):
            self._enabled_by_agent = False
            return

        self._enabled_by_agent = True
        if "failure_threshold" in config:
            self._failure_threshold = config["failure_threshold"]
        if "recovery_timeout" in config:
            self._recovery_timeout = config["recovery_timeout"]
        if "half_open_max_calls" in config:
            self._half_open_max_calls = config["half_open_max_calls"]

    def _handle_closed(self, ctx: PluginContext) -> PluginResult:
        """处理 CLOSED 状态逻辑。

        检查连续失败次数，达到阈值则转为 OPEN 并短路管道。

        Args:
            ctx: 插件执行上下文

        Returns:
            插件执行结果
        """
        consecutive_failures = ctx.state.get("consecutive_failures", 0)
        if consecutive_failures >= self._failure_threshold:
            self._transition_to_open()
            logger.warning(
                "[%s] 熔断触发 | failures=%d | threshold=%d",
                self.name,
                consecutive_failures,
                self._failure_threshold,
            )
            return PluginResult(
                state_updates={"circuit_open": True, "circuit_state": self.OPEN},
                skip_remaining=True,
            )

        return PluginResult(
            state_updates={"circuit_open": False, "circuit_state": self.CLOSED},
        )

    def _handle_open(self, ctx: PluginContext) -> PluginResult:
        """处理 OPEN 状态逻辑。

        检查是否超过恢复超时，是则转为 HALF_OPEN 放行探测请求，
        否则继续短路管道。

        Args:
            ctx: 插件执行上下文

        Returns:
            插件执行结果
        """
        elapsed = time.monotonic() - self._last_failure_time
        if elapsed >= self._recovery_timeout:
            self._transition_to_half_open()
            logger.info(
                "[%s] 进入半开状态 | elapsed=%.1fs | timeout=%ds",
                self.name,
                elapsed,
                self._recovery_timeout,
            )
            return PluginResult(
                state_updates={"circuit_open": False, "circuit_state": self.HALF_OPEN},
            )

        return PluginResult(
            state_updates={"circuit_open": True, "circuit_state": self.OPEN},
            skip_remaining=True,
        )

    def _handle_half_open(self, ctx: PluginContext) -> PluginResult:
        """处理 HALF_OPEN 状态逻辑。

        限制探测次数，超过限制则转回 OPEN。
        检查 state 中的成功标记决定是否恢复为 CLOSED。

        Args:
            ctx: 插件执行上下文

        Returns:
            插件执行结果
        """
        self._half_open_calls += 1

        if self._half_open_calls > self._half_open_max_calls:
            self._transition_to_open()
            logger.warning(
                "[%s] 半开探测超限，重新熔断 | calls=%d | max=%d",
                self.name,
                self._half_open_calls,
                self._half_open_max_calls,
            )
            return PluginResult(
                state_updates={"circuit_open": True, "circuit_state": self.OPEN},
                skip_remaining=True,
            )

        if ctx.state.get("last_call_success", False):
            self._transition_to_closed()
            logger.info("[%s] 探测成功，恢复正常", self.name)
            return PluginResult(
                state_updates={"circuit_open": False, "circuit_state": self.CLOSED},
            )

        return PluginResult(
            state_updates={"circuit_open": False, "circuit_state": self.HALF_OPEN},
        )

    def _transition_to_open(self) -> None:
        """将熔断器状态转为 OPEN。"""
        self._state = self.OPEN
        self._last_failure_time = time.monotonic()
        self._half_open_calls = 0

    def _transition_to_half_open(self) -> None:
        """将熔断器状态转为 HALF_OPEN。"""
        self._state = self.HALF_OPEN
        self._half_open_calls = 0

    def _transition_to_closed(self) -> None:
        """将熔断器状态转为 CLOSED。"""
        self._state = self.CLOSED
        self._half_open_calls = 0
