"""PluginChain 逻辑单元测试。

测试插件链按优先级排序执行、状态更新、跳过后续、
以及四种错误策略（SKIP / ABORT / RETRY / FALLBACK）的正确行为。

所有插件使用 Mock，不依赖真实实现。
"""

from __future__ import annotations

from typing import Any


from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, RouteSignal, create_initial_state


# ---------------------------------------------------------------------------
# Mock 插件定义
# ---------------------------------------------------------------------------


class MockPlugin(IInputPlugin):
    """Mock 基础插件，用于测试 PluginChain 行为。

    通过构造参数控制 execute 的返回值和副作用，
    无需依赖真实插件实现。
    """

    def __init__(
        self,
        name: str = "mock_plugin",
        priority: int = 50,
        state_updates: dict[str, Any] | None = None,
        route_signal: RouteSignal | None = None,
        skip_remaining: bool = False,
        error_policy: ErrorPolicy = ErrorPolicy.ABORT,
        fallback_state: dict[str, Any] | None = None,
        max_retries: int = 3,
        should_raise: Exception | None = None,
        raise_until_count: int = 0,
    ) -> None:
        self._name = name
        self._priority = priority
        self._state_updates = state_updates or {}
        self._route_signal = route_signal
        self._skip_remaining = skip_remaining
        self.error_policy = error_policy
        self.fallback_state = fallback_state or {}
        self.max_retries = max_retries
        self._should_raise = should_raise
        self._raise_until_count = raise_until_count
        self._execute_count: int = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行插件逻辑。

        如果设置了 should_raise 且调用次数未超过阈值，抛出异常；
        否则返回预设结果。
        """
        self._execute_count += 1
        if self._should_raise and self._execute_count <= self._raise_until_count:
            raise self._should_raise
        return PluginResult(
            state_updates=self._state_updates,
            route_signal=self._route_signal,
            skip_remaining=self._skip_remaining,
        )

    @property
    def execute_count(self) -> int:
        return self._execute_count


def _make_ctx(state: dict[str, Any] | None = None) -> PluginContext:
    """创建 PluginContext 实例。"""
    if state is None:
        state = create_initial_state()
    return PluginContext(state=state, config={})


# ---------------------------------------------------------------------------
# 测试类
# ---------------------------------------------------------------------------


class TestPluginChain:
    """PluginChain 执行逻辑测试。"""

    async def test_chain_priority_order(self) -> None:
        """按优先级排序执行：priority 数字小的先执行。

        场景:
          插件 A priority=50, 插件 B priority=10
          执行顺序: B, A
        """
        from pipeline.chain import PluginChain

        execution_order: list[str] = []

        class OrderTracker(IInputPlugin):
            def __init__(self, name: str, priority: int):
                self._name = name
                self._priority = priority
                self.error_policy = ErrorPolicy.SKIP

            @property
            def name(self) -> str:
                return self._name

            @property
            def priority(self) -> int:
                return self._priority

            async def execute(self, ctx: PluginContext) -> PluginResult:
                execution_order.append(self._name)
                return PluginResult()

        plugin_a = OrderTracker("A", 50)
        plugin_b = OrderTracker("B", 10)

        chain = PluginChain([plugin_a, plugin_b])
        ctx = _make_ctx()
        await chain.execute(ctx)

        assert execution_order == ["B", "A"]

    async def test_chain_state_updates(self) -> None:
        """执行后 state 更新：多个插件的状态变更合并到 ctx.state。

        场景:
          插件 A 写入 state_updates={"key1": "val1"}
          插件 B 写入 state_updates={"key2": "val2"}
          最终 ctx.state 包含 key1 和 key2
        """
        from pipeline.chain import PluginChain

        plugin_a = MockPlugin(name="A", priority=10, state_updates={"key1": "val1"})
        plugin_b = MockPlugin(name="B", priority=20, state_updates={"key2": "val2"})

        chain = PluginChain([plugin_a, plugin_b])
        ctx = _make_ctx()
        await chain.execute(ctx)

        assert ctx.state["key1"] == "val1"
        assert ctx.state["key2"] == "val2"

    async def test_chain_skip_remaining(self) -> None:
        """skip_remaining=True 跳过后续插件。

        场景:
          插件 A 设置 skip_remaining=True
          插件 B 不应执行
        """
        from pipeline.chain import PluginChain

        plugin_a = MockPlugin(name="A", priority=10, skip_remaining=True)
        plugin_b = MockPlugin(name="B", priority=20, state_updates={"key2": "val2"})

        chain = PluginChain([plugin_a, plugin_b])
        ctx = _make_ctx()
        results = await chain.execute(ctx)

        # 插件 A 应执行
        assert len(results) >= 1
        # 插件 B 不应执行
        assert plugin_b.execute_count == 0
        # key2 不应存在于 state
        assert "key2" not in ctx.state

    async def test_chain_error_policy_skip(self) -> None:
        """ErrorPolicy.SKIP：插件异常时跳过，后续插件正常执行。

        场景:
          插件 A execute 抛异常, error_policy=SKIP
          插件 B 正常执行
        """
        from pipeline.chain import PluginChain

        plugin_a = MockPlugin(
            name="A",
            priority=10,
            error_policy=ErrorPolicy.SKIP,
            should_raise=RuntimeError("plugin A failed"),
            raise_until_count=1,
        )
        plugin_b = MockPlugin(name="B", priority=20, state_updates={"key2": "val2"})

        chain = PluginChain([plugin_a, plugin_b])
        ctx = _make_ctx()
        await chain.execute(ctx)

        # 插件 B 应正常执行
        assert plugin_b.execute_count == 1
        assert ctx.state["key2"] == "val2"

    async def test_chain_error_policy_abort(self) -> None:
        """ErrorPolicy.ABORT：插件异常时终止链执行。

        场景:
          插件 A execute 抛异常, error_policy=ABORT
          后续插件不执行
        """
        from pipeline.chain import PluginChain

        plugin_a = MockPlugin(
            name="A",
            priority=10,
            error_policy=ErrorPolicy.ABORT,
            should_raise=RuntimeError("plugin A failed critically"),
            raise_until_count=1,
        )
        plugin_b = MockPlugin(name="B", priority=20, state_updates={"key2": "val2"})

        chain = PluginChain([plugin_a, plugin_b])
        ctx = _make_ctx()
        await chain.execute(ctx)

        # ABORT 后续插件不应执行
        assert plugin_b.execute_count == 0

    async def test_chain_error_policy_fallback(self) -> None:
        """ErrorPolicy.FALLBACK：插件异常时使用 fallback_state 写入 state。

        场景:
          插件 A execute 抛异常, error_policy=FALLBACK,
          fallback_state={"fallback_key": "fallback_val"}
          fallback_state 写入 state
        """
        from pipeline.chain import PluginChain

        plugin_a = MockPlugin(
            name="A",
            priority=10,
            error_policy=ErrorPolicy.FALLBACK,
            fallback_state={"fallback_key": "fallback_val"},
            should_raise=RuntimeError("plugin A failed, use fallback"),
            raise_until_count=1,
        )

        chain = PluginChain([plugin_a])
        ctx = _make_ctx()
        await chain.execute(ctx)

        assert ctx.state.get("fallback_key") == "fallback_val"

    async def test_chain_error_policy_retry(self) -> None:
        """ErrorPolicy.RETRY：插件第一次失败，第二次成功。

        场景:
          插件 A 第一次 execute 抛异常，第二次成功
          验证重试后成功
        """
        from pipeline.chain import PluginChain

        plugin_a = MockPlugin(
            name="A",
            priority=10,
            error_policy=ErrorPolicy.RETRY,
            max_retries=3,
            state_updates={"retry_key": "retry_val"},
            should_raise=RuntimeError("transient failure"),
            raise_until_count=1,  # 只在第一次调用时抛异常
        )

        chain = PluginChain([plugin_a])
        ctx = _make_ctx()
        await chain.execute(ctx)

        # 重试后成功，state 应包含 retry_key
        assert ctx.state.get("retry_key") == "retry_val"
        # 首次失败 + 1 次重试成功 = 2 次调用
        assert plugin_a.execute_count == 2
