"""REQ-43: 外部连接端到端集成测试。

用 mock 方式验证通道适配器与连接器的端到端集成场景，
不重复 test_strategy_health_config.py 中已有的单元级测试。

覆盖场景：
1. 通道适配器标准方法集成 — 自定义状态适配器的 health_check/is_connected/get_status 联动
2. 连接器健康检查与重连 — 完整的 连接→断开→ERROR→health_check→退避重连→恢复 流程
3. ConfigCenter 配置热加载集成 — 配置变更传播到连接器并改变行为
4. 策略模式端到端 — 运行时策略切换与降级
5. 通道-连接器联合场景 — 完整的 消息接收→处理→响应 流程
"""

from __future__ import annotations

from typing import Any

import pytest

from channels.input_adapter import IInputAdapter
from channels.output_adapter import IOutputAdapter
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


# ═══════════════════════════════════════════════════════════════════════
# 测试用 Mock 组件
# ═══════════════════════════════════════════════════════════════════════


class NetworkInputAdapter(IInputAdapter):
    """模拟网络输入适配器（如 WebSocket/钉钉），连接状态可控。"""

    def __init__(self) -> None:
        self._connected = False

    async def receive(self) -> dict[str, Any]:
        return {
            "user_input": "hello",
            "session_id": "test-session",
            "core_type": "llm_call",
            "should_stop": False,
            "iteration": 1,
        }

    async def health_check(self) -> bool:
        return self._connected

    @property
    def is_connected(self) -> bool:
        return self._connected

    def set_connected(self, value: bool) -> None:
        self._connected = value


class NetworkOutputAdapter(IOutputAdapter):
    """模拟网络输出适配器，记录发送的消息。"""

    def __init__(self) -> None:
        self._connected = False
        self.sent_states: list[dict[str, Any]] = []
        self.sent_chunks: list[dict[str, Any]] = []

    async def send(self, state: dict[str, Any]) -> None:
        self.sent_states.append(state)

    async def send_stream(self, chunk: dict[str, Any]) -> None:
        self.sent_chunks.append(chunk)

    async def health_check(self) -> bool:
        return self._connected

    @property
    def is_connected(self) -> bool:
        return self._connected

    def set_connected(self, value: bool) -> None:
        self._connected = value


class StubConnector(BaseConnector):
    """测试用连接器桩，连接/断开/上下文均可控。"""

    def __init__(
        self,
        name: str = "stub",
        capabilities: list[str] | None = None,
        priority: int = 5,
    ) -> None:
        super().__init__()
        self._name = name
        self._capabilities = capabilities or ["open_file", "show_diff"]
        self._priority = priority
        self._connect_count = 0
        self._disconnect_count = 0
        self._context = ConnectorContext(active_file="stub.py")
        self._connect_side_effect: Exception | None = None

    @property
    def connector_type(self) -> str:
        return self._name

    async def get_context(self) -> ConnectorContext:
        return self._context

    async def execute_action(self, action: ConnectorAction) -> ActionResult:
        return ActionResult(success=True, data={"action": action.action_type})

    async def connect(self) -> None:
        self._connect_count += 1
        if self._connect_side_effect:
            raise self._connect_side_effect
        self._set_state(ConnectorState.CONNECTED)

    async def disconnect(self) -> None:
        self._disconnect_count += 1
        self._set_state(ConnectorState.DISCONNECTED)

    def get_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            connector_type=self._name,
            display_name=f"Stub {self._name}",
            capabilities=self._capabilities,
            priority=self._priority,
        )


class StubConfigCenter:
    """测试用 ConfigCenter，手动触发配置变更。"""

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
        for cb in self._watches.get(path_prefix, []):
            cb(event_type, file_path, {"config_type": "test"})


class ConfigurableStubConnector(StubConnector, ConfigSubscriberMixin):
    """带配置订阅的测试连接器，配置变更可改变其行为。"""

    def __init__(self) -> None:
        super().__init__(name="configurable")
        self.config_events: list[dict[str, Any]] = []
        self.active_file_from_config: str = "default.py"

    def _on_config_changed(
        self,
        event_type: str,
        file_path: str,
        context: dict[str, Any],
    ) -> None:
        self.config_events.append({
            "event_type": event_type,
            "file_path": file_path,
            "context": context,
        })
        # 模拟配置变更改变行为
        self.active_file_from_config = f"updated_{file_path.split('/')[-1]}"
        self._context = ConnectorContext(active_file=self.active_file_from_config)


# ═══════════════════════════════════════════════════════════════════════
# 场景 1: 通道适配器标准方法集成
# ═══════════════════════════════════════════════════════════════════════


class TestChannelAdapterIntegration:
    """通道适配器标准方法集成：health_check/is_connected/get_status 联动。"""

    @pytest.mark.asyncio
    async def test_input_adapter_health_reflects_connection(self) -> None:
        """health_check() 与 is_connected 同步反映连接状态变更。"""
        adapter = NetworkInputAdapter()

        # 未连接
        assert adapter.is_connected is False
        assert await adapter.health_check() is False

        # 连接后
        adapter.set_connected(True)
        assert adapter.is_connected is True
        assert await adapter.health_check() is True

        # 断开后
        adapter.set_connected(False)
        assert adapter.is_connected is False
        assert await adapter.health_check() is False

    @pytest.mark.asyncio
    async def test_output_adapter_health_reflects_connection(self) -> None:
        """输出适配器 health_check 与 is_connected 同步。"""
        adapter = NetworkOutputAdapter()

        adapter.set_connected(True)
        assert await adapter.health_check() is True

        adapter.set_connected(False)
        assert await adapter.health_check() is False

    @pytest.mark.asyncio
    async def test_get_status_reflects_current_connection(self) -> None:
        """get_status() 字典实时反映连接和健康状态。"""
        adapter = NetworkInputAdapter()

        # 未连接
        status = adapter.get_status()
        assert status["connected"] is False
        assert status["type"] == "NetworkInputAdapter"

        # 连接后
        adapter.set_connected(True)
        status = adapter.get_status()
        assert status["connected"] is True

    @pytest.mark.asyncio
    async def test_output_adapter_send_when_connected(self) -> None:
        """输出适配器在连接状态下可正常 send 和 send_stream。"""
        adapter = NetworkOutputAdapter()
        adapter.set_connected(True)

        await adapter.send({"raw_result": "test output"})
        await adapter.send_stream({"text": "chunk1", "type": "token"})
        await adapter.send_stream({"text": "chunk2", "type": "token"})

        assert len(adapter.sent_states) == 1
        assert adapter.sent_states[0]["raw_result"] == "test output"
        assert len(adapter.sent_chunks) == 2
        assert adapter.sent_chunks[0]["text"] == "chunk1"

    @pytest.mark.asyncio
    async def test_input_receive_returns_valid_state(self) -> None:
        """输入适配器 receive() 返回管道可用的 state 字典。"""
        adapter = NetworkInputAdapter()
        adapter.set_connected(True)

        state = await adapter.receive()
        assert "user_input" in state
        assert "session_id" in state
        assert state["iteration"] == 1
        assert state["should_stop"] is False

    @pytest.mark.asyncio
    async def test_input_output_adapter_status_schema_consistent(self) -> None:
        """输入和输出适配器 get_status() 返回的字典结构一致。"""
        in_adapter = NetworkInputAdapter()
        out_adapter = NetworkOutputAdapter()

        in_status = in_adapter.get_status()
        out_status = out_adapter.get_status()

        # 都必须包含这三个键
        for status in (in_status, out_status):
            assert "type" in status
            assert "connected" in status
            assert "healthy" in status


# ═══════════════════════════════════════════════════════════════════════
# 场景 2: 连接器健康检查与重连
# ═══════════════════════════════════════════════════════════════════════


class TestConnectorHealthReconnectIntegration:
    """连接器健康检查与重连端到端流程。"""

    @pytest.mark.asyncio
    async def test_full_connect_disconnect_reconnect_cycle(self) -> None:
        """完整生命周期：连接→断开→ERROR→health_check→退避重连→恢复。"""
        conn = StubConnector(name="lifecycle")

        # 初始 DISCONNECTED
        assert conn.state == ConnectorState.DISCONNECTED
        assert conn.is_connected is False

        # 连接
        await conn.connect()
        assert conn.is_connected is True
        assert conn.state == ConnectorState.CONNECTED
        assert await conn.health_check() is True

        # 断开
        await conn.disconnect()
        assert conn.is_connected is False

        # 模拟异常
        conn._set_state(ConnectorState.ERROR)
        assert await conn.health_check() is True  # health_check 触发重连
        assert conn.is_connected is True
        assert conn._connect_count == 2  # 第一次 connect + 重连中 connect

    @pytest.mark.asyncio
    async def test_reconnect_backoff_timing(self) -> None:
        """指数退避重连：延迟随尝试次数指数增长。"""
        conn = StubConnector(name="backoff")
        conn._set_state(ConnectorState.DISCONNECTED)

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
    async def test_reconnect_calls_disconnect_before_each_attempt(self) -> None:
        """每次重连尝试前先调用 disconnect。"""
        conn = StubConnector(name="cleanup")
        conn._set_state(ConnectorState.DISCONNECTED)

        attempt = 0
        original_connect = conn.connect

        async def connect_then_fail() -> None:
            nonlocal attempt
            attempt += 1
            if attempt < 2:
                raise ConnectionError("fail")
            await original_connect()

        conn.connect = connect_then_fail  # type: ignore[assignment]

        await conn._reconnect_with_backoff(max_retries=3, base_delay=0.01)
        # disconnect 应在每次尝试前被调用
        assert conn._disconnect_count >= 2

    @pytest.mark.asyncio
    async def test_health_check_with_persistent_failure(self) -> None:
        """持续失败后 health_check 返回 False 而非抛异常。"""
        conn = StubConnector(name="persistent_fail")
        conn._connect_side_effect = ConnectionError("服务不可达")
        conn._set_state(ConnectorState.ERROR)

        result = await conn.health_check()
        assert result is False
        # reconnect_with_backoff 每次尝试前调用 disconnect() 将状态变为 DISCONNECTED
        assert conn.state == ConnectorState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_health_check_on_disconnected_returns_false(self) -> None:
        """DISCONNECTED 状态（非 ERROR）health_check 返回 False 且不尝试重连。"""
        conn = StubConnector(name="clean_disconnect")
        assert conn.state == ConnectorState.DISCONNECTED

        result = await conn.health_check()
        assert result is False
        assert conn._connect_count == 0  # 不应尝试重连

    @pytest.mark.asyncio
    async def test_connector_get_status_after_state_changes(self) -> None:
        """get_status() 在各种状态变更后都返回正确信息。"""
        conn = StubConnector(name="status_check")

        # 初始
        status = conn.get_status()
        assert status["type"] == "status_check"
        assert status["state"] == "disconnected"
        assert status["connected"] is False

        # 连接后
        await conn.connect()
        status = conn.get_status()
        assert status["state"] == "connected"
        assert status["connected"] is True
        assert status["info"]["display_name"] == "Stub status_check"

        # 异常后
        conn._set_state(ConnectorState.ERROR)
        status = conn.get_status()
        assert status["state"] == "error"
        assert status["connected"] is False


# ═══════════════════════════════════════════════════════════════════════
# 场景 3: ConfigCenter 配置热加载集成
# ═══════════════════════════════════════════════════════════════════════


class TestConfigHotReloadIntegration:
    """ConfigCenter 配置热加载端到端：订阅→变更→行为更新。"""

    def test_config_change_propagates_to_connector(self) -> None:
        """配置变更通过回调传播到连接器。"""
        center = StubConfigCenter()
        conn = ConfigurableStubConnector()
        conn.subscribe_config(center, "adapters.yaml")

        center.fire_change("adapters.yaml", "modified", "/config/adapters.yaml")

        assert len(conn.config_events) == 1
        event = conn.config_events[0]
        assert event["event_type"] == "modified"
        assert event["file_path"] == "/config/adapters.yaml"

    @pytest.mark.asyncio
    async def test_config_change_updates_connector_behavior(self) -> None:
        """配置变更实际改变了连接器的 get_context 行为。"""
        center = StubConfigCenter()
        conn = ConfigurableStubConnector()
        await conn.connect()
        conn.subscribe_config(center, "capability_adapters.yaml")

        # 变更前：_context 由 StubConnector.__init__ 初始化为 active_file="stub.py"
        ctx_before = await conn.get_context()
        assert ctx_before.active_file == "stub.py"

        # 触发配置变更
        center.fire_change("capability_adapters.yaml", "modified", "/config/capability_adapters.yaml")

        # 变更后：_on_config_changed 更新了 _context
        ctx_after = await conn.get_context()
        assert ctx_after.active_file == "updated_capability_adapters.yaml"

    def test_multiple_config_changes_accumulate(self) -> None:
        """多次配置变更全部被记录。"""
        center = StubConfigCenter()
        conn = ConfigurableStubConnector()
        conn.subscribe_config(center, "rules.yaml")

        center.fire_change("rules.yaml", "modified", "/config/rules.yaml")
        center.fire_change("rules.yaml", "modified", "/config/rules.yaml")
        center.fire_change("rules.yaml", "deleted", "/config/rules.yaml")

        assert len(conn.config_events) == 3
        assert conn.config_events[0]["event_type"] == "modified"
        assert conn.config_events[2]["event_type"] == "deleted"

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_config_updates(self) -> None:
        """取消订阅后配置变更不再影响连接器。"""
        center = StubConfigCenter()
        conn = ConfigurableStubConnector()
        conn.subscribe_config(center, "test.yaml")

        center.fire_change("test.yaml", "modified", "/config/test.yaml")
        assert len(conn.config_events) == 1

        conn.unsubscribe_config()
        center.fire_change("test.yaml", "modified", "/config/test.yaml")
        assert len(conn.config_events) == 1  # 未增加

    @pytest.mark.asyncio
    async def test_config_change_with_disconnected_connector(self) -> None:
        """断开状态的连接器仍能接收配置变更通知。"""
        center = StubConfigCenter()
        conn = ConfigurableStubConnector()
        conn.subscribe_config(center, "setup.yaml")

        # 未连接也能收到配置变更
        center.fire_change("setup.yaml", "created", "/config/setup.yaml")
        assert len(conn.config_events) == 1


# ═══════════════════════════════════════════════════════════════════════
# 场景 4: 策略模式端到端
# ═══════════════════════════════════════════════════════════════════════


class TestStrategyPatternEndToEnd:
    """策略模式端到端：运行时策略切换与降级。"""

    @pytest.mark.asyncio
    async def test_strategy_switch_on_failure(self) -> None:
        """高优先级策略失败后自动切换到低优先级策略。"""
        registry = ConnectorRegistry()
        high_priority = StubConnector(name="vscode", priority=10)
        low_priority = StubConnector(name="fallback_editor", priority=3)

        registry.register(high_priority)
        registry.register(low_priority)

        await high_priority.connect()
        await low_priority.connect()

        # 高优先级活跃
        active = registry.get_active_connector()
        assert active is high_priority

        # 模拟高优先级断开
        await high_priority.disconnect()

        # 自动切换到低优先级
        active = registry.get_active_connector()
        assert active is low_priority

    @pytest.mark.asyncio
    async def test_strategy_action_routing(self) -> None:
        """不同操作类型路由到支持该操作的策略。"""
        registry = ConnectorRegistry()
        code_editor = StubConnector(
            name="code_editor",
            capabilities=["open_file", "show_diff", "jump_to"],
            priority=5,
        )
        rich_editor = StubConnector(
            name="rich_editor",
            capabilities=["open_file", "insert_content"],
            priority=8,
        )

        registry.register(code_editor)
        registry.register(rich_editor)
        await code_editor.connect()
        await rich_editor.connect()

        # show_diff 只有 code_editor 支持
        best = registry.get_best_connector_for("show_diff")
        assert best is code_editor

        # insert_content 只有 rich_editor 支持
        best = registry.get_best_connector_for("insert_content")
        assert best is rich_editor

        # open_file 两者都支持，选高优先级
        best = registry.get_best_connector_for("open_file")
        assert best is rich_editor

    @pytest.mark.asyncio
    async def test_degradation_when_no_connector_available(self) -> None:
        """所有策略不可用时的降级处理。"""
        registry = ConnectorRegistry()
        degradation = DegradationManager()

        # 无连接器
        assert registry.get_active_connector() is None
        assert degradation.can_handle_locally("show_diff") is True

        result = degradation.execute_with_fallback(
            "show_diff",
            {
                "original_content": "old\n",
                "new_content": "new\n",
                "file_path": "test.py",
            },
        )
        assert result.success is True
        assert result.data.get("degraded") is True

    @pytest.mark.asyncio
    async def test_connector_execute_action_through_strategy(self) -> None:
        """通过策略选择的连接器执行操作。"""
        registry = ConnectorRegistry()
        conn = StubConnector(
            name="executor",
            capabilities=["open_file"],
            priority=5,
        )
        registry.register(conn)
        await conn.connect()

        best = registry.get_best_connector_for("open_file")
        assert best is not None

        action = ConnectorAction(
            action_type="open_file",
            parameters={"file_path": "/tmp/test.py"},
        )
        result = await best.execute_action(action)
        assert result.success is True
        assert result.data["action"] == "open_file"

    @pytest.mark.asyncio
    async def test_strategy_priority_ordering(self) -> None:
        """同状态下严格按优先级排序选择策略。"""
        registry = ConnectorRegistry()
        conn_p1 = StubConnector(name="p1", priority=1)
        conn_p5 = StubConnector(name="p5", priority=5)
        conn_p10 = StubConnector(name="p10", priority=10)

        for c in (conn_p1, conn_p5, conn_p10):
            registry.register(c)
            await c.connect()

        active = registry.get_active_connector()
        assert active is conn_p10

        # 列表也应按优先级排序
        infos = registry.list_connectors()
        assert infos[0].connector_type == "p10"
        assert infos[-1].connector_type == "p1"


# ═══════════════════════════════════════════════════════════════════════
# 场景 5: 通道-连接器联合场景
# ═══════════════════════════════════════════════════════════════════════


class TestChannelConnectorJoint:
    """通道-连接器联合：消息接收→处理→响应全流程。"""

    @pytest.mark.asyncio
    async def test_full_message_flow(self) -> None:
        """完整的消息接收→连接器获取上下文→处理→输出适配器响应。"""
        input_adapter = NetworkInputAdapter()
        output_adapter = NetworkOutputAdapter()
        connector = StubConnector(name="vscode", capabilities=["open_file"])
        registry = ConnectorRegistry()

        # 初始化
        input_adapter.set_connected(True)
        output_adapter.set_connected(True)
        registry.register(connector)
        await connector.connect()

        # 1. 从输入适配器接收消息
        state = await input_adapter.receive()
        assert state["user_input"] == "hello"

        # 2. 通过连接器获取 IDE 上下文
        ctx = await connector.get_context()
        assert ctx.active_file is not None

        # 3. 模拟处理（将上下文加入 state）
        state["context"] = {
            "active_file": ctx.active_file,
        }
        state["raw_result"] = f"处理完成: {state['user_input']}"

        # 4. 通过输出适配器发送结果
        await output_adapter.send(state)
        assert len(output_adapter.sent_states) == 1
        assert output_adapter.sent_states[0]["raw_result"] == "处理完成: hello"

    @pytest.mark.asyncio
    async def test_streaming_flow(self) -> None:
        """流式消息场景：接收→逐 chunk 输出。"""
        input_adapter = NetworkInputAdapter()
        output_adapter = NetworkOutputAdapter()

        input_adapter.set_connected(True)
        output_adapter.set_connected(True)

        _ = await input_adapter.receive()

        # 模拟流式输出多个 chunk
        chunks = [
            {"text": "你", "type": "token"},
            {"text": "好", "type": "token"},
            {"text": "！", "type": "token"},
        ]
        for chunk in chunks:
            await output_adapter.send_stream(chunk)

        assert len(output_adapter.sent_chunks) == 3
        assert output_adapter.sent_chunks[0]["text"] == "你"

    @pytest.mark.asyncio
    async def test_flow_with_connector_action(self) -> None:
        """消息处理中通过连接器执行操作（如打开文件）。"""
        input_adapter = NetworkInputAdapter()
        output_adapter = NetworkOutputAdapter()
        connector = StubConnector(
            name="vscode",
            capabilities=["open_file", "show_diff"],
            priority=10,
        )
        registry = ConnectorRegistry()

        input_adapter.set_connected(True)
        output_adapter.set_connected(True)
        registry.register(connector)
        await connector.connect()

        # 接收消息
        state = await input_adapter.receive()

        # 通过注册表找到支持 open_file 的连接器
        best = registry.get_best_connector_for("open_file")
        assert best is not None

        # 执行打开文件操作
        action = ConnectorAction(
            action_type="open_file",
            parameters={"file_path": "/src/main.py"},
        )
        result = await best.execute_action(action)
        assert result.success is True

        # 输出结果
        state["action_result"] = {"success": result.success}
        await output_adapter.send(state)
        assert output_adapter.sent_states[0]["action_result"]["success"] is True

    @pytest.mark.asyncio
    async def test_flow_with_degraded_fallback(self) -> None:
        """连接器不可用时降级处理仍能完成流程。"""
        input_adapter = NetworkInputAdapter()
        output_adapter = NetworkOutputAdapter()
        registry = ConnectorRegistry()
        degradation = DegradationManager()

        input_adapter.set_connected(True)
        output_adapter.set_connected(True)

        state = await input_adapter.receive()

        # 无活跃连接器 → 降级
        assert registry.get_active_connector() is None
        assert registry.get_best_connector_for("show_diff") is None

        result = degradation.execute_with_fallback(
            "show_diff",
            {
                "original_content": "line1\n",
                "new_content": "line2\n",
                "file_path": "changed.py",
            },
        )
        assert result.success is True

        # 将降级结果作为响应
        state["diff_result"] = result.data
        await output_adapter.send(state)
        assert output_adapter.sent_states[0]["diff_result"]["degraded"] is True

    @pytest.mark.asyncio
    async def test_flow_health_check_before_processing(self) -> None:
        """处理前检查所有组件健康状态，确保链路可用。"""
        input_adapter = NetworkInputAdapter()
        output_adapter = NetworkOutputAdapter()
        connector = StubConnector(name="health_checked")

        input_adapter.set_connected(True)
        output_adapter.set_connected(True)
        await connector.connect()

        # 处理前全链路健康检查
        assert await input_adapter.health_check() is True
        assert await output_adapter.health_check() is True
        assert await connector.health_check() is True
        assert input_adapter.is_connected is True
        assert output_adapter.is_connected is True
        assert connector.is_connected is True

        # 链路正常，执行流程
        state = await input_adapter.receive()
        await output_adapter.send(state)
        assert len(output_adapter.sent_states) == 1

    @pytest.mark.asyncio
    async def test_flow_recovers_from_error_with_reconnect(self) -> None:
        """流程中连接器异常后自动重连恢复。"""
        input_adapter = NetworkInputAdapter()
        output_adapter = NetworkOutputAdapter()
        connector = StubConnector(name="recovery")

        input_adapter.set_connected(True)
        output_adapter.set_connected(True)
        await connector.connect()

        # 正常流程
        assert await connector.health_check() is True

        # 模拟连接器异常
        connector._set_state(ConnectorState.ERROR)
        assert connector.is_connected is False

        # health_check 触发自动重连
        assert await connector.health_check() is True
        assert connector.is_connected is True
        assert connector._connect_count == 2  # 初始 + 重连

        # 重连后继续流程
        state = await input_adapter.receive()
        ctx = await connector.get_context()
        state["context_file"] = ctx.active_file
        await output_adapter.send(state)
        assert output_adapter.sent_states[0]["context_file"] == "stub.py"
