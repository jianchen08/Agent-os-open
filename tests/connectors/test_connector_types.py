"""连接器类型定义的单元测试。

测试所有数据类型的创建和属性访问，包括：
- CursorPosition 的创建
- ConnectorContext 的默认值和自定义值
- ConnectorAction 的参数
- ActionResult 的成功/失败状态
- ConnectorState 枚举值
- ConnectorInfo 的字段
"""

from __future__ import annotations

import pytest

from connectors.types import (
    ActionResult,
    ConnectorAction,
    ConnectorContext,
    ConnectorInfo,
    ConnectorState,
    CursorPosition,
)


class TestCursorPosition:
    """CursorPosition 测试。"""

    def test_create_with_line_and_column(self) -> None:
        """测试创建 CursorPosition 并访问属性。"""
        pos = CursorPosition(line=10, column=5)
        assert pos.line == 10
        assert pos.column == 5

    def test_create_with_zero_values(self) -> None:
        """测试行列均为 0 的合法值。"""
        pos = CursorPosition(line=0, column=0)
        assert pos.line == 0
        assert pos.column == 0

    def test_frozen_immutability(self) -> None:
        """测试 CursorPosition 是不可变的（frozen=True）。"""
        pos = CursorPosition(line=1, column=2)
        with pytest.raises(AttributeError):
            pos.line = 99  # type: ignore[misc]

    def test_equality(self) -> None:
        """测试相同值的 CursorPosition 相等。"""
        pos1 = CursorPosition(line=5, column=3)
        pos2 = CursorPosition(line=5, column=3)
        assert pos1 == pos2


class TestConnectorContext:
    """ConnectorContext 测试。"""

    def test_default_values(self) -> None:
        """测试默认值全部为空/空列表/空字典。"""
        ctx = ConnectorContext()
        assert ctx.active_file is None
        assert ctx.selected_text is None
        assert ctx.cursor_position is None
        assert ctx.open_files == []
        assert ctx.metadata == {}

    def test_custom_values(self) -> None:
        """测试自定义值。"""
        cursor = CursorPosition(line=1, column=0)
        ctx = ConnectorContext(
            active_file="/tmp/test.py",
            selected_text="hello",
            cursor_position=cursor,
            open_files=["/tmp/test.py", "/tmp/other.py"],
            metadata={"key": "value"},
        )
        assert ctx.active_file == "/tmp/test.py"
        assert ctx.selected_text == "hello"
        assert ctx.cursor_position == cursor
        assert ctx.open_files == ["/tmp/test.py", "/tmp/other.py"]
        assert ctx.metadata == {"key": "value"}

    def test_default_list_is_independent(self) -> None:
        """测试每个实例的 open_files 列表独立。"""
        ctx1 = ConnectorContext()
        ctx2 = ConnectorContext()
        ctx1.open_files.append("a.py")
        assert ctx2.open_files == []

    def test_default_metadata_is_independent(self) -> None:
        """测试每个实例的 metadata 字典独立。"""
        ctx1 = ConnectorContext()
        ctx2 = ConnectorContext()
        ctx1.metadata["key"] = "value"
        assert ctx2.metadata == {}


class TestConnectorAction:
    """ConnectorAction 测试。"""

    def test_create_with_type_and_params(self) -> None:
        """测试创建 ConnectorAction。"""
        action = ConnectorAction(
            action_type="open_file",
            parameters={"file_path": "/tmp/a.py"},
            action_id="id-123",
        )
        assert action.action_type == "open_file"
        assert action.parameters == {"file_path": "/tmp/a.py"}
        assert action.action_id == "id-123"

    def test_default_empty_action_id(self) -> None:
        """测试默认 action_id 为空字符串。"""
        action = ConnectorAction(action_type="jump_to")
        assert action.action_id == ""

    def test_default_empty_parameters(self) -> None:
        """测试默认 parameters 为空字典。"""
        action = ConnectorAction(action_type="show_diff")
        assert action.parameters == {}

    def test_default_parameters_are_independent(self) -> None:
        """测试每个实例的 parameters 独立。"""
        a1 = ConnectorAction(action_type="open_file")
        a2 = ConnectorAction(action_type="open_file")
        a1.parameters["key"] = "val"
        assert a2.parameters == {}


class TestActionResult:
    """ActionResult 测试。"""

    def test_success_result(self) -> None:
        """测试成功的操作结果。"""
        result = ActionResult(success=True, data={"file": "a.py"})
        assert result.success is True
        assert result.data == {"file": "a.py"}
        assert result.error is None

    def test_failure_result(self) -> None:
        """测试失败的操作结果。"""
        result = ActionResult(success=False, error="文件不存在")
        assert result.success is False
        assert result.error == "文件不存在"
        assert result.data is None

    def test_default_data_and_error(self) -> None:
        """测试 data 和 error 的默认值。"""
        result = ActionResult(success=True)
        assert result.data is None
        assert result.error is None


class TestConnectorState:
    """ConnectorState 枚举测试。"""

    def test_all_enum_values(self) -> None:
        """测试所有枚举值存在且正确。"""
        assert ConnectorState.DISCONNECTED == "disconnected"
        assert ConnectorState.CONNECTING == "connecting"
        assert ConnectorState.CONNECTED == "connected"
        assert ConnectorState.ACTIVE == "active"
        assert ConnectorState.DISCONNECTING == "disconnecting"
        assert ConnectorState.ERROR == "error"

    def test_enum_count(self) -> None:
        """测试枚举值数量。"""
        assert len(ConnectorState) == 6

    def test_enum_from_value(self) -> None:
        """测试通过字符串值获取枚举。"""
        assert ConnectorState("connected") is ConnectorState.CONNECTED
        assert ConnectorState("error") is ConnectorState.ERROR


class TestConnectorInfo:
    """ConnectorInfo 测试。"""

    def test_create_with_required_fields(self) -> None:
        """测试创建 ConnectorInfo。"""
        info = ConnectorInfo(
            connector_type="vscode",
            display_name="Visual Studio Code",
            capabilities=["open_file", "show_diff"],
            priority=10,
        )
        assert info.connector_type == "vscode"
        assert info.display_name == "Visual Studio Code"
        assert info.capabilities == ["open_file", "show_diff"]
        assert info.priority == 10

    def test_default_capabilities(self) -> None:
        """测试默认 capabilities 为空列表。"""
        info = ConnectorInfo(connector_type="test", display_name="Test")
        assert info.capabilities == []

    def test_default_priority(self) -> None:
        """测试默认 priority 为 0。"""
        info = ConnectorInfo(connector_type="test", display_name="Test")
        assert info.priority == 0
