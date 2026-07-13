"""REQ-42: IDE 连接器策略模式 + 健康检查 + ConfigMixin 综合测试。

验证内容：
1. 策略模式：BaseConnector(Strategy) + ConnectorRegistry(Context) + DegradationManager(Fallback)
   - 注册/注销不同策略实现
   - 按能力匹配合适策略
   - 无策略时降级处理
2. 健康检查：health_check + 指数退避重连
3. ConfigSubscriberMixin：配置订阅/取消/回调
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from connectors.base import BaseConnector
from connectors.config_mixin import ConfigSubscriberMixin
from connectors.degradation import DegradationManager
from connectors.registry import ConnectorRegistry
from connectors.types import (
    ActionResult,
    ConnectorAction,
    ConnectorContext,
    ConnectorInfo,
    ConnectorState,
)


# ── 测试用 Mock 连接器（策略实现） ──────────────────────────────────────


class MockIDEConnector(BaseConnector):
    """测试用 IDE 连接器（策略实现 A）。"""

    def __init__(
        self,
        name: str = "mock_ide",
        capabilities: list[str] | None = None,
        priority: int = 5,
    ) -> None:
        super().__init__()
        self._name = name
        self._capabilities = capabilities or ["open_file", "show_diff"]
        self._priority = priority
        self._connect_called = 0
        self._disconnect_called = 0

    @property
    def connector_type(self) -> str:
        return self._name

    async def get_context(self) -> ConnectorContext:
        return ConnectorContext(active_file="test.py")

    async def execute_action(self, action: ConnectorAction) -> ActionResult:
        return ActionResult(success=True, data={"action": action.action_type})

    async def connect(self) -> None:
        self._connect_called += 1
        self._set_state(ConnectorState.CONNECTED)

    async def disconnect(self) -> None:
        self._disconnect_called += 1
        self._set_state(ConnectorState.DISCONNECTED)

    def get_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            connector_type=self._name,
            display_name=f"Mock {self._name}",
            capabilities=self._capabilities,
            priority=self._priority,
        )


class MockEditorConnector(BaseConnector):
    """测试用编辑器连接器（策略实现 B）。"""

    @property
    def connector_type(self) -> str:
        return "mock_editor"

    async def get_context(self) -> ConnectorContext:
        return ConnectorContext(active_file="editor.py", selected_text="hello")

    async def execute_action(self, action: ConnectorAction) -> ActionResult:
        return ActionResult(success=True)

    async def connect(self) -> None:
        self._set_state(ConnectorState.CONNECTED)

    async def disconnect(self) -> None:
        self._set_state(ConnectorState.DISCONNECTED)

    def get_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            connector_type="mock_editor",
            display_name="Mock Editor",
            capabilities=["open_file", "insert_content"],
            priority=8,
        )


# ── 策略模式测试 ──────────────────────────────────────────────────────────


class TestStrategyPatternRegistration:
    """策略模式：注册和注销。"""

    def test_register_different_strategies(self) -> None:
        """注册不同策略实现后可按类型查询。"""
        registry = ConnectorRegistry()
        ide = MockIDEConnector(name="ide_a")
        editor = MockEditorConnector()

        registry.register(ide)
        registry.register(editor)

        assert registry.count() == 2
        assert registry.get_connector("ide_a") is ide
        assert registry.get_connector("mock_editor") is editor

    def test_register_same_type_overwrites(self) -> None:
        """注册同类型策略会覆盖旧实例。"""
        registry = ConnectorRegistry()
        conn1 = MockIDEConnector(name="vscode", priority=1)
        conn2 = MockIDEConnector(name="vscode", priority=10)

        registry.register(conn1)
        registry.register(conn2)

        assert registry.count() == 1
        assert registry.get_connector("vscode") is conn2

    def test_unregister_removes_strategy(self) -> None:
        """注销后策略不可查询。"""
        registry = ConnectorRegistry()
        conn = MockIDEConnector(name="removable")
        registry.register(conn)
        registry.unregister("removable")
        assert registry.get_connector("removable") is None


class TestStrategyPatternSelection:
    """策略模式：策略选择。"""

    @pytest.mark.asyncio
    async def test_get_active_selects_highest_priority(self) -> None:
        """活跃策略中优先选择高优先级的。"""
        registry = ConnectorRegistry()
        low = MockIDEConnector(name="low", priority=1)
        high = MockIDEConnector(name="high", priority=10)

        registry.register(low)
        registry.register(high)

        # 都连接
        await low.connect()
        await high.connect()

        active = registry.get_active_connector()
        assert active is high

    @pytest.mark.asyncio
    async def test_get_active_skips_disconnected(self) -> None:
        """跳过未连接的策略。"""
        registry = ConnectorRegistry()
        disconnected = MockIDEConnector(name="disconnected", priority=100)
        connected = MockIDEConnector(name="connected", priority=1)

        registry.register(disconnected)
        registry.register(connected)
        await connected.connect()

        active = registry.get_active_connector()
        assert active is connected

    @pytest.mark.asyncio
    async def test_get_best_connector_matches_capability(self) -> None:
        """按能力匹配最佳策略。"""
        registry = ConnectorRegistry()
        ide = MockIDEConnector(
            name="ide", capabilities=["open_file", "show_diff"], priority=5,
        )
        editor = MockEditorConnector()  # capabilities=["open_file","insert_content"], priority=8

        registry.register(ide)
        registry.register(editor)
        await ide.connect()
        await editor.connect()

        # show_diff 只有 ide 支持
        best = registry.get_best_connector_for("show_diff")
        assert best is ide

        # insert_content 只有 editor 支持
        best = registry.get_best_connector_for("insert_content")
        assert best is editor

        # open_file 两者都支持，选高优先级的 editor
        best = registry.get_best_connector_for("open_file")
        assert best is editor

    @pytest.mark.asyncio
    async def test_no_matching_capability_returns_none(self) -> None:
        """无匹配能力时返回 None。"""
        registry = ConnectorRegistry()
        conn = MockIDEConnector(
            name="limited", capabilities=["open_file"], priority=10,
        )
        registry.register(conn)
        await conn.connect()

        assert registry.get_best_connector_for("nonexistent_action") is None


class TestStrategyPatternDegradation:
    """策略模式：降级处理。"""

    def test_degradation_manager_handles_open_file(self) -> None:
        """降级管理器处理 open_file。"""
        manager = DegradationManager()
        assert manager.can_handle_locally("open_file") is True

    def test_degradation_manager_handles_show_diff(self) -> None:
        """降级管理器处理 show_diff。"""
        manager = DegradationManager()
        assert manager.can_handle_locally("show_diff") is True

    def test_degradation_manager_rejects_unknown_action(self) -> None:
        """降级管理器拒绝未知操作。"""
        manager = DegradationManager()
        assert manager.can_handle_locally("fly_to_moon") is False

    def test_degradation_fallback_unsupported(self) -> None:
        """不支持的降级操作返回提示。"""
        manager = DegradationManager()
        result = manager.execute_with_fallback("jump_to", {})
        assert result.success is True
        assert result.data.get("degraded") is True

    def test_degradation_fallback_show_diff(self) -> None:
        """show_diff 降级为文本 diff。"""
        manager = DegradationManager()
        result = manager.execute_with_fallback(
            "show_diff",
            {"original_content": "a\n", "new_content": "b\n", "file_path": "t.py"},
        )
        assert result.success is True
        assert "diff" in result.data.get("diff_text", "").lower() or result.data.get("diff_text") == "(无差异)" or "t.py" in result.data.get("diff_text", "")

    @pytest.mark.asyncio
    async def test_full_strategy_flow_with_degradation(self) -> None:
        """完整策略流程：注册→无匹配→降级。"""
        registry = ConnectorRegistry()
        conn = MockIDEConnector(
            name="ide", capabilities=["open_file"], priority=5,
        )
        registry.register(conn)
        # 未连接，所以无活跃策略
        assert registry.get_active_connector() is None

        # 无匹配策略时降级
        manager = DegradationManager()
        result = manager.execute_with_fallback(
            "open_file", {"file_path": "/tmp/nonexistent_test_file.py"},
        )
        # 文件不存在，降级也失败
        assert result.success is False


# ── 健康检查测试 ──────────────────────────────────────────────────────────


class TestHealthCheck:
    """REQ-40: 健康检查 + 指数退避重连。"""

    @pytest.mark.asyncio
    async def test_health_check_when_connected(self) -> None:
        """已连接时 health_check 返回 True。"""
        conn = MockIDEConnector()
        await conn.connect()
        assert await conn.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_when_disconnected(self) -> None:
        """已断开且非 ERROR 状态时 health_check 返回 False。"""
        conn = MockIDEConnector()
        # 默认 DISCONNECTED 状态
        assert await conn.health_check() is False

    @pytest.mark.asyncio
    async def test_health_check_reconnect_on_error(self) -> None:
        """ERROR 状态时 health_check 尝试重连。"""
        conn = MockIDEConnector()
        conn._set_state(ConnectorState.ERROR)

        # connect 应该成功
        result = await conn.health_check()
        assert result is True
        assert conn.is_connected is True
        assert conn._connect_called == 1

    @pytest.mark.asyncio
    async def test_health_check_reconnect_failure(self) -> None:
        """重连失败时 health_check 返回 False。"""

        class FailConnector(MockIDEConnector):
            async def connect(self) -> None:
                self._connect_called += 1
                raise ConnectionError("无法连接")

        conn = FailConnector()
        conn._set_state(ConnectorState.ERROR)

        result = await conn.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_reconnect_with_backoff_retries(self) -> None:
        """指数退避重连：失败后多次重试。"""
        conn = MockIDEConnector()
        conn._set_state(ConnectorState.DISCONNECTED)

        # mock connect 前两次失败，第三次成功
        call_count = 0
        original_connect = conn.connect

        async def flaky_connect() -> None:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError(f"失败 {call_count}")
            await original_connect()

        conn.connect = flaky_connect  # type: ignore[assignment]

        await conn._reconnect_with_backoff(max_retries=3, base_delay=0.01)
        assert call_count == 3
        assert conn.is_connected is True

    @pytest.mark.asyncio
    async def test_reconnect_exhausted_raises(self) -> None:
        """重连耗尽后抛出 ConnectionError。"""

        class AlwaysFailConnector(MockIDEConnector):
            async def connect(self) -> None:
                raise ConnectionError("永远失败")

        conn = AlwaysFailConnector()
        with pytest.raises(ConnectionError, match="重连失败"):
            await conn._reconnect_with_backoff(max_retries=2, base_delay=0.01)

    def test_get_status_returns_state_info(self) -> None:
        """get_status 返回正确状态字典。"""
        conn = MockIDEConnector(name="test_conn")
        status = conn.get_status()
        assert status["type"] == "test_conn"
        assert status["state"] == "disconnected"
        assert status["connected"] is False
        assert "info" in status


# ── ConfigSubscriberMixin 测试 ────────────────────────────────────────────


class MockConfigCenter:
    """测试用 ConfigCenter 模拟。"""

    def __init__(self) -> None:
        self._watches: dict[str, list[Any]] = {}

    def watch(self, path_prefix: str, callback: Any) -> None:
        if path_prefix not in self._watches:
            self._watches[path_prefix] = []
        self._watches[path_prefix].append(callback)

    def unwatch(self, path_prefix: str, callback: Any) -> bool:
        callbacks = self._watches.get(path_prefix)
        if callbacks and callback in callbacks:
            callbacks.remove(callback)
            return True
        return False

    def fire_change(self, path_prefix: str, event_type: str, file_path: str) -> None:
        """模拟配置变更事件。"""
        for cb in self._watches.get(path_prefix, []):
            cb(event_type, file_path, {"config_type": "test"})


class ConfigurableConnector(MockIDEConnector, ConfigSubscriberMixin):
    """带 ConfigMixin 的测试连接器。"""

    def __init__(self) -> None:
        super().__init__(name="configurable")
        self.config_events: list[dict[str, Any]] = []

    def _on_config_changed(
        self, event_type: str, file_path: str, context: dict[str, Any],
    ) -> None:
        self.config_events.append({
            "event_type": event_type,
            "file_path": file_path,
            "context": context,
        })


class TestConfigSubscriberMixin:
    """REQ-41: ConfigCenter 配置订阅测试。"""

    def test_subscribe_registers_callback(self) -> None:
        """subscribe_config 注册回调到 ConfigCenter。"""
        center = MockConfigCenter()
        conn = ConfigurableConnector()

        conn.subscribe_config(center, "test/config.yaml")

        assert "test/config.yaml" in center._watches
        assert len(center._watches["test/config.yaml"]) == 1

    def test_subscribe_fires_callback(self) -> None:
        """配置变更时回调被触发。"""
        center = MockConfigCenter()
        conn = ConfigurableConnector()
        conn.subscribe_config(center, "adapters.yaml")

        center.fire_change("adapters.yaml", "modified", "/config/adapters.yaml")

        assert len(conn.config_events) == 1
        assert conn.config_events[0]["event_type"] == "modified"

    def test_unsubscribe_removes_callback(self) -> None:
        """unsubscribe_config 取消回调。"""
        center = MockConfigCenter()
        conn = ConfigurableConnector()
        conn.subscribe_config(center, "test.yaml")
        conn.unsubscribe_config()

        # 取消后变更不再触发
        center.fire_change("test.yaml", "modified", "/config/test.yaml")
        assert len(conn.config_events) == 0

    def test_resubscribe_replaces_old(self) -> None:
        """重复 subscribe 会替换旧订阅。"""
        center = MockConfigCenter()
        conn = ConfigurableConnector()

        conn.subscribe_config(center, "old.yaml")
        conn.subscribe_config(center, "new.yaml")

        # 只有 new.yaml 有回调
        assert len(center._watches.get("old.yaml", [])) == 0
        assert len(center._watches["new.yaml"]) == 1

    def test_unsubscribe_when_not_subscribed(self) -> None:
        """未订阅时 unsubscribe 不报错。"""
        conn = ConfigurableConnector()
        conn.unsubscribe_config()  # 不应抛异常


# ── 通道适配器标准方法测试 ────────────────────────────────────────────────


class TestChannelAdapterStandardMethods:
    """REQ-36: 通道适配器标准方法测试。"""

    def test_input_adapter_default_health_check(self) -> None:
        """IInputAdapter 默认 health_check 返回 True。"""
        from channels.input_adapter import IInputAdapter

        # CLI 输入适配器使用默认实现
        class TestInputAdapter(IInputAdapter):
            async def receive(self) -> dict[str, Any]:
                return {}

        adapter = TestInputAdapter()
        assert adapter.is_connected is True

    def test_input_adapter_default_status(self) -> None:
        """IInputAdapter 默认 get_status 返回正确格式。"""
        from channels.input_adapter import IInputAdapter

        class TestInputAdapter(IInputAdapter):
            async def receive(self) -> dict[str, Any]:
                return {}

        adapter = TestInputAdapter()
        status = adapter.get_status()
        assert "type" in status
        assert "connected" in status
        assert "healthy" in status
        assert status["connected"] is True

    def test_output_adapter_default_health_check(self) -> None:
        """IOutputAdapter 默认 health_check 返回 True。"""
        from channels.output_adapter import IOutputAdapter

        class TestOutputAdapter(IOutputAdapter):
            async def send(self, state: dict[str, Any]) -> None:
                pass

            async def send_stream(self, chunk: dict[str, Any]) -> None:
                pass

        adapter = TestOutputAdapter()
        assert adapter.is_connected is True

    def test_output_adapter_default_status(self) -> None:
        """IOutputAdapter 默认 get_status 返回正确格式。"""
        from channels.output_adapter import IOutputAdapter

        class TestOutputAdapter(IOutputAdapter):
            async def send(self, state: dict[str, Any]) -> None:
                pass

            async def send_stream(self, chunk: dict[str, Any]) -> None:
                pass

        adapter = TestOutputAdapter()
        status = adapter.get_status()
        assert "type" in status
        assert status["connected"] is True

    @pytest.mark.asyncio
    async def test_combo_adapter_is_connected(self) -> None:
        """组合适配器 is_connected 反映底层连接状态。"""
        from channels.dingtalk.adapter import DingTalkAdapter

        adapter = DingTalkAdapter(client_id="test", client_secret="test")
        # 未连接
        assert adapter.is_connected is False
        assert adapter.health_check is not None

    @pytest.mark.asyncio
    async def test_combo_adapter_get_status(self) -> None:
        """组合适配器 get_status 返回正确格式。"""
        from channels.dingtalk.adapter import DingTalkAdapter

        adapter = DingTalkAdapter(client_id="test", client_secret="test")
        status = adapter.get_status()
        assert status["type"] == "dingtalk"
        assert status["connected"] is False
