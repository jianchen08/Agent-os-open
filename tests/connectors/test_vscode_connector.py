"""VSCode 连接器的单元测试。

测试 VSCodeConnector 的生命周期（connect/disconnect）、上下文获取、操作执行和连接失败场景。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from connectors.types import ConnectorAction, ConnectorState
from connectors.vscode.connector import VSCodeConnector


@pytest.fixture
def connector() -> VSCodeConnector:
    """创建未连接的 VSCode 连接器实例。"""
    return VSCodeConnector(host="localhost", port=9999, timeout=1.0)


@pytest.fixture
def connected_connector(connector: VSCodeConnector) -> VSCodeConnector:
    """创建已连接的 VSCode 连接器实例。"""
    connector._set_state(ConnectorState.CONNECTED)
    return connector


class TestConnect:
    """连接测试。"""

    @pytest.mark.asyncio
    async def test_connect_success(self, connector: VSCodeConnector) -> None:
        """测试连接成功。"""
        connector.channel.is_available = MagicMock(return_value=True)
        await connector.connect()
        assert connector.state == ConnectorState.CONNECTED

    @pytest.mark.asyncio
    async def test_connect_failure_sets_error_state(self, connector: VSCodeConnector) -> None:
        """测试连接失败后状态为 ERROR。"""
        connector.channel.is_available = MagicMock(return_value=False)
        with pytest.raises(ConnectionError, match="连接失败"):
            await connector.connect()
        assert connector.state == ConnectorState.ERROR

    @pytest.mark.asyncio
    async def test_connect_when_already_connected(self, connected_connector: VSCodeConnector) -> None:
        """测试已连接时重复调用 connect 不抛异常。"""
        await connected_connector.connect()
        assert connected_connector.state == ConnectorState.CONNECTED


class TestDisconnect:
    """断开连接测试。"""

    @pytest.mark.asyncio
    async def test_disconnect_from_connected(self, connected_connector: VSCodeConnector) -> None:
        """测试从已连接状态断开。"""
        await connected_connector.disconnect()
        assert connected_connector.state == ConnectorState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_disconnect_when_already_disconnected(self, connector: VSCodeConnector) -> None:
        """测试从已断开状态调用 disconnect 不抛异常。"""
        await connector.disconnect()
        assert connector.state == ConnectorState.DISCONNECTED


class TestGetContext:
    """获取上下文测试。"""

    @pytest.mark.asyncio
    async def test_get_context_when_connected(self, connected_connector: VSCodeConnector) -> None:
        """测试已连接时获取上下文。"""
        from connectors.types import ConnectorContext, CursorPosition

        expected = ConnectorContext(
            active_file="test.py",
            selected_text="hello",
            cursor_position=CursorPosition(line=1, column=0),
        )
        connected_connector.channel.listen_for_context = AsyncMock(return_value=expected)

        ctx = await connected_connector.get_context()
        assert ctx.active_file == "test.py"
        assert ctx.selected_text == "hello"
        assert connected_connector.state == ConnectorState.ACTIVE

    @pytest.mark.asyncio
    async def test_get_context_when_disconnected(self, connector: VSCodeConnector) -> None:
        """测试未连接时获取上下文返回空对象。"""
        ctx = await connector.get_context()
        assert ctx.active_file is None
        assert ctx.selected_text is None


class TestExecuteAction:
    """执行操作测试。"""

    @pytest.mark.asyncio
    async def test_execute_action_success(self, connected_connector: VSCodeConnector) -> None:
        """测试执行操作成功。"""
        connected_connector.channel.send_request = AsyncMock(
            return_value={"success": True, "data": {"opened": True}}
        )
        action = ConnectorAction(action_type="open_file", parameters={"file_path": "a.py"})
        result = await connected_connector.execute_action(action)
        assert result.success is True
        assert result.data == {"opened": True}

    @pytest.mark.asyncio
    async def test_execute_action_failure_response(self, connected_connector: VSCodeConnector) -> None:
        """测试操作返回失败响应。"""
        connected_connector.channel.send_request = AsyncMock(
            return_value={"success": False, "error": "文件不存在"}
        )
        action = ConnectorAction(action_type="open_file", parameters={"file_path": "x.py"})
        result = await connected_connector.execute_action(action)
        assert result.success is False
        assert "文件不存在" in (result.error or "")

    @pytest.mark.asyncio
    async def test_execute_action_when_disconnected(self, connector: VSCodeConnector) -> None:
        """测试未连接时执行操作返回失败。"""
        action = ConnectorAction(action_type="open_file")
        result = await connector.execute_action(action)
        assert result.success is False
        assert "未连接" in (result.error or "")

    @pytest.mark.asyncio
    async def test_execute_action_connection_error_sets_error_state(
        self, connected_connector: VSCodeConnector
    ) -> None:
        """测试连接异常导致状态变为 ERROR。"""
        connected_connector.channel.send_request = AsyncMock(side_effect=ConnectionError("断连"))
        action = ConnectorAction(action_type="open_file")
        result = await connected_connector.execute_action(action)
        assert result.success is False
        assert connected_connector.state == ConnectorState.ERROR

    @pytest.mark.asyncio
    async def test_execute_action_assigns_action_id_if_empty(
        self, connected_connector: VSCodeConnector
    ) -> None:
        """测试空 action_id 时自动分配 UUID。"""
        connected_connector.channel.send_request = AsyncMock(
            return_value={"success": True, "data": None}
        )
        action = ConnectorAction(action_type="open_file", action_id="")
        await connected_connector.execute_action(action)
        assert len(action.action_id) > 0


class TestConnectorInfo:
    """连接器信息测试。"""

    def test_connector_type(self, connector: VSCodeConnector) -> None:
        """测试连接器类型为 vscode。"""
        assert connector.connector_type == "vscode"

    def test_get_info(self, connector: VSCodeConnector) -> None:
        """测试 get_info 返回正确的信息。"""
        info = connector.get_info()
        assert info.connector_type == "vscode"
        assert info.display_name == "Visual Studio Code"
        assert "open_file" in info.capabilities
        assert "show_diff" in info.capabilities
        assert info.priority == 10
