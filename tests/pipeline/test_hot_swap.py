"""插件热替换管理器单元测试。

测试 HotSwapManager 的替换、预检查、健康检查、回滚等功能。
"""

from __future__ import annotations

import pytest

from pipeline.hot_swap import HotSwapManager, SwapResult
from pipeline.plugin import (
    ICorePlugin,
    IInputPlugin,
    IOutputPlugin,
    IPlugin,
    PluginContext,
    PluginResult,
)
from pipeline.registry import PluginRegistry


# --- 测试用插件实现 ---


class SimpleInputPlugin(IInputPlugin):
    """测试用输入插件。"""

    def __init__(self, name: str = "test_input", priority: int = 10) -> None:
        self._name = name
        self._priority = priority

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    async def execute(self, ctx: PluginContext) -> PluginResult:
        return PluginResult()


class SimpleOutputPlugin(IOutputPlugin):
    """测试用输出插件。"""

    def __init__(self, name: str = "test_output", priority: int = 20) -> None:
        self._name = name
        self._priority = priority

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    async def execute(self, ctx: PluginContext) -> PluginResult:
        return PluginResult()


class SimpleCorePlugin(ICorePlugin):
    """测试用核心插件。"""

    def __init__(self, name: str = "test_core", priority: int = 5) -> None:
        self._name = name
        self._priority = priority

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    async def execute(self, ctx: PluginContext) -> dict:
        return {}


class BrokenPlugin(IInputPlugin):
    """执行时会抛异常的坏插件，用于测试健康检查失败。"""

    def __init__(self) -> None:
        self._name = "broken_plugin"
        self._priority = 10

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    async def execute(self, ctx: PluginContext) -> PluginResult:
        raise RuntimeError("broken plugin")


# --- 测试用例 ---


class TestHotSwapBasic:
    """基础替换测试。"""

    @pytest.mark.asyncio
    async def test_swap_existing_plugin(self) -> None:
        """替换存在的插件 → SwapResult.success=True。"""
        registry = PluginRegistry()
        old_plugin = SimpleInputPlugin(name="my_plugin", priority=10)
        registry.register(old_plugin)

        manager = HotSwapManager(registry)
        new_plugin = SimpleInputPlugin(name="my_plugin_v2", priority=10)

        result = await manager.swap_plugin("my_plugin", new_plugin)

        assert result.success is True
        assert result.swap_id != ""
        assert result.error is None
        assert result.rolled_back is False

        # registry 中应该有新插件
        assert registry.get("my_plugin") is new_plugin

    @pytest.mark.asyncio
    async def test_swap_nonexistent_plugin_allowed(self) -> None:
        """替换不存在的插件 → 允许（视为新增）。"""
        registry = PluginRegistry()
        manager = HotSwapManager(registry)
        new_plugin = SimpleInputPlugin(name="new_plugin", priority=10)

        result = await manager.swap_plugin("nonexistent", new_plugin)

        assert result.success is True
        # registry 中应该有新插件
        assert registry.get("nonexistent") is new_plugin

    @pytest.mark.asyncio
    async def test_swap_snapshot_saves_old_plugin(self) -> None:
        """快照保存了旧插件实例。"""
        registry = PluginRegistry()
        old_plugin = SimpleInputPlugin(name="my_plugin", priority=10)
        registry.register(old_plugin)

        manager = HotSwapManager(registry)
        new_plugin = SimpleInputPlugin(name="my_plugin_v2", priority=10)

        result = await manager.swap_plugin("my_plugin", new_plugin)

        assert result.success is True
        snapshot = manager.get_snapshot(result.swap_id)
        assert snapshot is not None
        assert snapshot.old_plugin is old_plugin
        assert snapshot.plugin_name == "my_plugin"


class TestHotSwapPreCheck:
    """预检查测试。"""

    @pytest.mark.asyncio
    async def test_type_mismatch_warning(self) -> None:
        """新旧插件类型不同 → 预检查产生警告但不阻止替换。"""
        registry = PluginRegistry()
        old_plugin = SimpleInputPlugin(name="my_plugin", priority=10)
        registry.register(old_plugin)

        manager = HotSwapManager(registry)
        # 用 OutputPlugin 替换 InputPlugin
        new_plugin = SimpleOutputPlugin(name="my_plugin_v2", priority=10)

        result = await manager.swap_plugin("my_plugin", new_plugin)

        # 替换仍然成功（预检查只是警告）
        assert result.success is True

    @pytest.mark.asyncio
    async def test_same_type_no_warning(self) -> None:
        """新旧插件类型相同 → 无预检查警告。"""
        registry = PluginRegistry()
        old_plugin = SimpleInputPlugin(name="my_plugin", priority=10)
        registry.register(old_plugin)

        manager = HotSwapManager(registry)
        new_plugin = SimpleInputPlugin(name="my_plugin_v2", priority=5)

        # 手动调用 _pre_check 验证
        warnings = manager._pre_check(old_plugin, new_plugin)
        assert len(warnings) == 0


class TestHotSwapHealthCheck:
    """健康检查测试。"""

    @pytest.mark.asyncio
    async def test_health_check_failure_auto_rollback(self) -> None:
        """健康检查失败 → 自动回滚。"""
        registry = PluginRegistry()
        old_plugin = SimpleInputPlugin(name="my_plugin", priority=10)
        registry.register(old_plugin)

        manager = HotSwapManager(registry)
        broken_plugin = BrokenPlugin()

        result = await manager.swap_plugin("my_plugin", broken_plugin, health_check=True)

        assert result.success is False
        assert result.rolled_back is True
        assert "健康检查失败" in result.error

        # 旧插件应该被恢复
        current = registry.get("my_plugin")
        assert current is old_plugin

    @pytest.mark.asyncio
    async def test_skip_health_check(self) -> None:
        """跳过健康检查 → 即使插件有问题也替换成功。"""
        registry = PluginRegistry()
        old_plugin = SimpleInputPlugin(name="my_plugin", priority=10)
        registry.register(old_plugin)

        manager = HotSwapManager(registry)
        broken_plugin = BrokenPlugin()

        result = await manager.swap_plugin("my_plugin", broken_plugin, health_check=False)

        assert result.success is True
        assert result.rolled_back is False


class TestHotSwapRollback:
    """手动回滚测试。"""

    @pytest.mark.asyncio
    async def test_manual_rollback(self) -> None:
        """手动 rollback → 恢复旧插件。"""
        registry = PluginRegistry()
        old_plugin = SimpleInputPlugin(name="my_plugin", priority=10)
        registry.register(old_plugin)

        manager = HotSwapManager(registry)
        new_plugin = SimpleInputPlugin(name="my_plugin_v2", priority=5)

        result = await manager.swap_plugin(
            "my_plugin", new_plugin, health_check=False,
        )
        assert result.success is True

        # 手动回滚
        rolled_back = await manager.rollback(result.swap_id)
        assert rolled_back is True

        # 旧插件应该被恢复
        current = registry.get("my_plugin")
        assert current is old_plugin

    @pytest.mark.asyncio
    async def test_rollback_invalid_swap_id(self) -> None:
        """回滚无效的 swap_id → 返回 False。"""
        registry = PluginRegistry()
        manager = HotSwapManager(registry)

        rolled_back = await manager.rollback("nonexistent")
        assert rolled_back is False

    @pytest.mark.asyncio
    async def test_rollback_twice_same_swap_id(self) -> None:
        """对同一 swap_id 回滚两次 → 第二次返回 False。"""
        registry = PluginRegistry()
        old_plugin = SimpleInputPlugin(name="my_plugin", priority=10)
        registry.register(old_plugin)

        manager = HotSwapManager(registry)
        new_plugin = SimpleInputPlugin(name="my_plugin_v2", priority=5)

        result = await manager.swap_plugin(
            "my_plugin", new_plugin, health_check=False,
        )

        # 第一次回滚成功
        assert await manager.rollback(result.swap_id) is True
        # 第二次回滚失败（快照已清理）
        assert await manager.rollback(result.swap_id) is False

    @pytest.mark.asyncio
    async def test_rollback_new_plugin_removal(self) -> None:
        """回滚新增插件（原不存在）→ 移除新插件。"""
        registry = PluginRegistry()
        manager = HotSwapManager(registry)
        new_plugin = SimpleInputPlugin(name="new_plugin", priority=10)

        result = await manager.swap_plugin("new_plugin", new_plugin)
        assert result.success is True
        assert registry.get("new_plugin") is new_plugin

        # 回滚
        rolled_back = await manager.rollback(result.swap_id)
        assert rolled_back is True
        # 插件应被移除
        assert registry.get("new_plugin") is None


class TestHotSwapCorePlugin:
    """核心插件替换测试。"""

    @pytest.mark.asyncio
    async def test_swap_core_plugin(self) -> None:
        """替换核心插件 → _core_plugins 同步更新。"""
        registry = PluginRegistry()
        old_core = SimpleCorePlugin(name="llm_call", priority=5)
        registry.register_core("llm_call", old_core)

        manager = HotSwapManager(registry)
        new_core = SimpleCorePlugin(name="llm_call_v2", priority=5)

        result = await manager.swap_plugin("llm_call", new_core, health_check=False)
        assert result.success is True

        # registry 中应该有新插件
        assert registry.get("llm_call") is new_core
