"""IDE 工具的单元测试。

测试 ide_open_file、ide_show_diff、ide_get_selection 三个工具在有连接器/降级两种场景下的行为。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from connectors.types import ActionResult, ConnectorContext, CursorPosition
from tools.builtin.ide_get_selection import IDEGetSelectionTool
from tools.builtin.ide_open_file import IDEOpenFileTool
from tools.builtin.ide_show_diff import IDEShowDiffTool


def _make_registry_with_connector(connector: MagicMock) -> MagicMock:
    """创建带有活跃连接器的注册表 mock。"""
    registry = MagicMock()
    registry.get_active_connector.return_value = connector
    return registry


def _make_registry_without_connector() -> MagicMock:
    """创建无活跃连接器的注册表 mock。"""
    registry = MagicMock()
    registry.get_active_connector.return_value = None
    return registry


def _make_mock_connector(
    execute_result: ActionResult | None = None,
    context: ConnectorContext | None = None,
) -> MagicMock:
    """创建模拟连接器。

    Args:
        execute_result: execute_action 的返回值
        context: get_context 的返回值

    Returns:
        配置好的 MagicMock
    """
    conn = MagicMock()
    conn.connector_type = "vscode"
    conn.execute_action = AsyncMock(return_value=execute_result or ActionResult(success=True, data={}))
    conn.get_context = AsyncMock(return_value=context or ConnectorContext())
    return conn


# ============================================================
# IDEOpenFileTool 测试
# ============================================================


class TestIDEOpenFile:
    """ide_open_file 工具测试。"""

    @pytest.mark.asyncio
    async def test_with_connector_success(self) -> None:
        """测试有连接器时打开文件成功。"""
        connector = _make_mock_connector(
            execute_result=ActionResult(success=True, data={"opened": True})
        )
        registry = _make_registry_with_connector(connector)
        tool = IDEOpenFileTool(registry=registry)

        result = await tool.execute({"file_path": "/tmp/test.py"})
        assert result.success is True
        connector.execute_action.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_with_connector_failure_falls_back(self) -> None:
        """测试有连接器但执行失败时降级处理。"""
        connector = _make_mock_connector(
            execute_result=ActionResult(success=False, error="IDE 错误")
        )
        registry = _make_registry_with_connector(connector)
        tool = IDEOpenFileTool(registry=registry)

        result = await tool.execute({"file_path": "/tmp/test.py"})
        # 连接器返回失败 → 走 create_failure_result 分支
        assert result.success is False

    @pytest.mark.asyncio
    async def test_without_connector_reads_file(self, tmp_path: object) -> None:
        """测试无连接器时降级为读取文件。"""
        import pathlib

        test_file = pathlib.Path(str(tmp_path)) / "sample.py"
        test_file.write_text("print('hello')", encoding="utf-8")

        registry = _make_registry_without_connector()
        tool = IDEOpenFileTool(registry=registry)

        result = await tool.execute({"file_path": str(test_file)})
        assert result.success is True
        assert result.output is not None

    @pytest.mark.asyncio
    async def test_empty_file_path_returns_failure(self) -> None:
        """测试空 file_path 参数返回失败。"""
        tool = IDEOpenFileTool(registry=None)
        result = await tool.execute({"file_path": ""})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_none_registry_uses_degradation(self) -> None:
        """测试无注册表时使用降级。"""
        tool = IDEOpenFileTool(registry=None)
        result = await tool.execute({"file_path": ""})
        assert result.success is False


# ============================================================
# IDEShowDiffTool 测试
# ============================================================


class TestIDEShowDiff:
    """ide_show_diff 工具测试。"""

    @pytest.mark.asyncio
    async def test_with_connector_success(self) -> None:
        """测试有连接器时显示差异成功。"""
        connector = _make_mock_connector(
            execute_result=ActionResult(success=True, data={"shown": True})
        )
        registry = _make_registry_with_connector(connector)
        tool = IDEShowDiffTool(registry=registry)

        result = await tool.execute({
            "file_path": "a.py",
            "original_content": "old",
            "new_content": "new",
        })
        assert result.success is True
        connector.execute_action.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_without_connector_generates_diff(self) -> None:
        """测试无连接器时降级为生成 diff 文本。"""
        registry = _make_registry_without_connector()
        tool = IDEShowDiffTool(registry=registry)

        result = await tool.execute({
            "file_path": "a.py",
            "original_content": "line1\n",
            "new_content": "line2\n",
        })
        assert result.success is True
        assert result.output is not None
        assert "degraded" in str(result.output)

    @pytest.mark.asyncio
    async def test_empty_file_path_returns_failure(self) -> None:
        """测试空 file_path 参数返回失败。"""
        tool = IDEShowDiffTool(registry=None)
        result = await tool.execute({
            "file_path": "",
            "original_content": "a",
            "new_content": "b",
        })
        assert result.success is False

    @pytest.mark.asyncio
    async def test_no_diff_content(self) -> None:
        """测试无差异时输出"(无差异)"。"""
        registry = _make_registry_without_connector()
        tool = IDEShowDiffTool(registry=registry)

        result = await tool.execute({
            "file_path": "a.py",
            "original_content": "same",
            "new_content": "same",
        })
        assert result.success is True


# ============================================================
# IDEGetSelectionTool 测试
# ============================================================


class TestIDEGetSelection:
    """ide_get_selection 工具测试。"""

    @pytest.mark.asyncio
    async def test_with_connector_returns_context(self) -> None:
        """测试有连接器时返回上下文信息。"""
        ctx = ConnectorContext(
            active_file="test.py",
            selected_text="hello",
            cursor_position=CursorPosition(line=5, column=3),
            open_files=["test.py"],
        )
        connector = _make_mock_connector(context=ctx)
        registry = _make_registry_with_connector(connector)
        tool = IDEGetSelectionTool(registry=registry)

        result = await tool.execute({})
        assert result.success is True
        assert result.output is not None

    @pytest.mark.asyncio
    async def test_without_connector_returns_hint(self) -> None:
        """测试无连接器时降级为提示信息。"""
        registry = _make_registry_without_connector()
        tool = IDEGetSelectionTool(registry=registry)

        result = await tool.execute({})
        assert result.success is True
        assert result.output is not None
        assert "degraded" in str(result.output)

    @pytest.mark.asyncio
    async def test_connector_exception_falls_back(self) -> None:
        """测试连接器异常时降级处理。"""
        connector = MagicMock()
        connector.get_context = AsyncMock(side_effect=RuntimeError("连接断开"))
        registry = _make_registry_with_connector(connector)
        tool = IDEGetSelectionTool(registry=registry)

        result = await tool.execute({})
        assert result.success is True
        assert "degraded" in str(result.output)
