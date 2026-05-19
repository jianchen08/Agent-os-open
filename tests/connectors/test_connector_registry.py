"""连接器注册表的单元测试。

测试 ConnectorRegistry 的注册、注销、查询、优先级排序和能力匹配功能。
"""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

import pytest

from connectors.registry import ConnectorRegistry
from connectors.types import ConnectorInfo


def _make_mock_connector(
    conn_type: str = "test",
    connected: bool = False,
    capabilities: list[str] | None = None,
    priority: int = 0,
) -> MagicMock:
    """创建模拟连接器。

    Args:
        conn_type: 连接器类型
        connected: 是否处于连接状态
        capabilities: 能力列表
        priority: 优先级

    Returns:
        配置好的 MagicMock 连接器实例
    """
    conn = MagicMock()
    type(conn).connector_type = PropertyMock(return_value=conn_type)
    conn.is_connected = connected
    conn.get_info.return_value = ConnectorInfo(
        connector_type=conn_type,
        display_name=conn_type,
        capabilities=capabilities or [],
        priority=priority,
    )
    return conn


class TestRegister:
    """注册连接器测试。"""

    def test_register_single_connector(self) -> None:
        """测试注册单个连接器。"""
        registry = ConnectorRegistry()
        conn = _make_mock_connector("vscode")
        registry.register(conn)
        assert registry.count() == 1
        assert registry.has("vscode")

    def test_register_multiple_connectors(self) -> None:
        """测试注册多个连接器。"""
        registry = ConnectorRegistry()
        registry.register(_make_mock_connector("vscode"))
        registry.register(_make_mock_connector("jetbrains"))
        assert registry.count() == 2

    def test_register_overwrites_existing(self) -> None:
        """测试注册同名连接器会覆盖。"""
        registry = ConnectorRegistry()
        conn1 = _make_mock_connector("vscode", priority=1)
        conn2 = _make_mock_connector("vscode", priority=2)
        registry.register(conn1)
        registry.register(conn2)
        assert registry.count() == 1
        assert registry.get_connector("vscode") is conn2


class TestUnregister:
    """注销连接器测试。"""

    def test_unregister_existing(self) -> None:
        """测试注销已存在的连接器。"""
        registry = ConnectorRegistry()
        registry.register(_make_mock_connector("vscode"))
        registry.unregister("vscode")
        assert registry.count() == 0

    def test_unregister_nonexistent_raises_key_error(self) -> None:
        """测试注销不存在的连接器抛出 KeyError。"""
        registry = ConnectorRegistry()
        with pytest.raises(KeyError, match="不存在"):
            registry.unregister("ghost")


class TestGetConnector:
    """获取连接器测试。"""

    def test_get_existing_connector(self) -> None:
        """测试获取已注册的连接器。"""
        registry = ConnectorRegistry()
        conn = _make_mock_connector("vscode")
        registry.register(conn)
        assert registry.get_connector("vscode") is conn

    def test_get_nonexistent_returns_none(self) -> None:
        """测试获取不存在的连接器返回 None。"""
        registry = ConnectorRegistry()
        assert registry.get_connector("vscode") is None


class TestGetActiveConnector:
    """获取活跃连接器测试。"""

    def test_no_active_connectors_returns_none(self) -> None:
        """测试没有活跃连接器时返回 None。"""
        registry = ConnectorRegistry()
        registry.register(_make_mock_connector("vscode", connected=False))
        assert registry.get_active_connector() is None

    def test_empty_registry_returns_none(self) -> None:
        """测试空注册表返回 None。"""
        registry = ConnectorRegistry()
        assert registry.get_active_connector() is None

    def test_returns_connected_highest_priority(self) -> None:
        """测试返回优先级最高的已连接连接器。"""
        registry = ConnectorRegistry()
        conn_low = _make_mock_connector("a_low", connected=True, priority=1)
        conn_high = _make_mock_connector("z_high", connected=True, priority=10)
        registry.register(conn_low)
        registry.register(conn_high)
        active = registry.get_active_connector()
        assert active is conn_high

    def test_same_priority_uses_alphabetical_order(self) -> None:
        """测试同优先级时按类型名字母序。"""
        registry = ConnectorRegistry()
        conn_b = _make_mock_connector("b_connector", connected=True, priority=5)
        conn_a = _make_mock_connector("a_connector", connected=True, priority=5)
        registry.register(conn_b)
        registry.register(conn_a)
        active = registry.get_active_connector()
        assert active is conn_a

    def test_skips_disconnected(self) -> None:
        """测试跳过未连接的连接器。"""
        registry = ConnectorRegistry()
        registry.register(_make_mock_connector("disconnected", connected=False, priority=100))
        conn_connected = _make_mock_connector("connected", connected=True, priority=1)
        registry.register(conn_connected)
        active = registry.get_active_connector()
        assert active is conn_connected


class TestListConnectors:
    """列出连接器测试。"""

    def test_empty_registry(self) -> None:
        """测试空注册表返回空列表。"""
        registry = ConnectorRegistry()
        assert registry.list_connectors() == []

    def test_lists_all_by_priority(self) -> None:
        """测试按优先级降序列出所有连接器。"""
        registry = ConnectorRegistry()
        registry.register(_make_mock_connector("low", priority=1))
        registry.register(_make_mock_connector("high", priority=10))
        infos = registry.list_connectors()
        assert len(infos) == 2
        assert infos[0].priority >= infos[1].priority


class TestGetBestConnectorFor:
    """按能力匹配最佳连接器测试。"""

    def test_match_by_capability(self) -> None:
        """测试按能力匹配连接器。"""
        registry = ConnectorRegistry()
        conn = _make_mock_connector(
            "vscode", connected=True, capabilities=["open_file", "show_diff"], priority=5
        )
        registry.register(conn)
        best = registry.get_best_connector_for("open_file")
        assert best is conn

    def test_no_match_returns_none(self) -> None:
        """测试无匹配能力时返回 None。"""
        registry = ConnectorRegistry()
        conn = _make_mock_connector("vscode", connected=True, capabilities=["show_diff"])
        registry.register(conn)
        assert registry.get_best_connector_for("open_file") is None

    def test_skips_disconnected(self) -> None:
        """测试跳过未连接的连接器。"""
        registry = ConnectorRegistry()
        conn = _make_mock_connector(
            "vscode", connected=False, capabilities=["open_file"], priority=10
        )
        registry.register(conn)
        assert registry.get_best_connector_for("open_file") is None

    def test_selects_highest_priority_among_matches(self) -> None:
        """测试在多个匹配中选择优先级最高的。"""
        registry = ConnectorRegistry()
        conn_low = _make_mock_connector(
            "low", connected=True, capabilities=["open_file"], priority=1
        )
        conn_high = _make_mock_connector(
            "high", connected=True, capabilities=["open_file"], priority=10
        )
        registry.register(conn_low)
        registry.register(conn_high)
        best = registry.get_best_connector_for("open_file")
        assert best is conn_high


class TestClear:
    """清空注册表测试。"""

    def test_clear_removes_all(self) -> None:
        """测试清空所有连接器。"""
        registry = ConnectorRegistry()
        registry.register(_make_mock_connector("a"))
        registry.register(_make_mock_connector("b"))
        registry.clear()
        assert registry.count() == 0
        assert registry.list_connectors() == []
