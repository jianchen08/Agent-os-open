"""降级管理器的单元测试。

测试 DegradationManager 的 can_handle_locally、execute_with_fallback 对各种 action_type 的处理，
以及 open_file / show_diff / get_selection 的降级行为。
"""

from __future__ import annotations

import pathlib

import pytest

from connectors.degradation import DegradationManager


@pytest.fixture
def manager() -> DegradationManager:
    """创建降级管理器实例。"""
    return DegradationManager()


class TestCanHandleLocally:
    """can_handle_locally 测试。"""

    @pytest.mark.parametrize(
        "action_type",
        ["open_file", "get_selection", "show_diff", "insert_content", "jump_to"],
    )
    def test_degradable_actions(self, manager: DegradationManager, action_type: str) -> None:
        """测试支持降级的操作类型。"""
        assert manager.can_handle_locally(action_type) is True

    def test_unknown_action_not_degradable(self, manager: DegradationManager) -> None:
        """测试不支持降级的操作类型。"""
        assert manager.can_handle_locally("unknown_action") is False

    def test_empty_string_not_degradable(self, manager: DegradationManager) -> None:
        """测试空字符串不可降级。"""
        assert manager.can_handle_locally("") is False


class TestExecuteWithFallback:
    """execute_with_fallback 综合测试。"""

    def test_unknown_action_returns_failure(self, manager: DegradationManager) -> None:
        """测试未知操作类型返回失败。"""
        result = manager.execute_with_fallback("unknown", {})
        assert result.success is False
        assert "不支持的操作类型" in (result.error or "")


class TestFallbackOpenFile:
    """open_file 降级测试。"""

    def test_reads_existing_file(self, manager: DegradationManager, tmp_path: pathlib.Path) -> None:
        """测试降级读取存在的文件。"""
        test_file = tmp_path / "hello.py"
        test_file.write_text("print('hello')", encoding="utf-8")

        result = manager.execute_with_fallback("open_file", {"file_path": str(test_file)})
        assert result.success is True
        assert result.data is not None
        assert result.data["degraded"] is True
        assert "print('hello')" in result.data["content"]

    def test_missing_file_path_returns_failure(self, manager: DegradationManager) -> None:
        """测试缺少 file_path 参数返回失败。"""
        result = manager.execute_with_fallback("open_file", {})
        assert result.success is False
        assert "file_path" in (result.error or "")

    def test_empty_file_path_returns_failure(self, manager: DegradationManager) -> None:
        """测试空 file_path 返回失败。"""
        result = manager.execute_with_fallback("open_file", {"file_path": ""})
        assert result.success is False

    def test_nonexistent_file_returns_failure(self, manager: DegradationManager) -> None:
        """测试文件不存在返回失败。"""
        result = manager.execute_with_fallback("open_file", {"file_path": "/nonexistent/file.py"})
        assert result.success is False
        assert "不存在" in (result.error or "")


class TestFallbackShowDiff:
    """show_diff 降级测试。"""

    def test_generates_unified_diff(self, manager: DegradationManager) -> None:
        """测试降级生成 unified diff 文本。"""
        result = manager.execute_with_fallback("show_diff", {
            "file_path": "a.py",
            "original_content": "line1\nline2\n",
            "new_content": "line1\nline3\n",
        })
        assert result.success is True
        assert result.data is not None
        assert result.data["degraded"] is True
        diff_text = result.data["diff_text"]
        assert "-line2" in diff_text
        assert "+line3" in diff_text

    def test_no_difference(self, manager: DegradationManager) -> None:
        """测试无差异时输出提示。"""
        result = manager.execute_with_fallback("show_diff", {
            "file_path": "a.py",
            "original_content": "same",
            "new_content": "same",
        })
        assert result.success is True
        assert "无差异" in result.data["diff_text"]

    def test_with_title(self, manager: DegradationManager) -> None:
        """测试带标题的 diff 输出。"""
        result = manager.execute_with_fallback("show_diff", {
            "file_path": "a.py",
            "original_content": "old",
            "new_content": "new",
            "title": "修改概览",
        })
        assert result.success is True
        assert "修改概览" in result.data["diff_text"]

    def test_empty_contents(self, manager: DegradationManager) -> None:
        """测试空内容生成 diff。"""
        result = manager.execute_with_fallback("show_diff", {
            "file_path": "a.py",
            "original_content": "",
            "new_content": "new line\n",
        })
        assert result.success is True
        assert "+new line" in result.data["diff_text"]


class TestFallbackGetSelection:
    """get_selection 降级测试。"""

    def test_returns_degraded_hint(self, manager: DegradationManager) -> None:
        """测试降级返回提示信息。"""
        result = manager.execute_with_fallback("get_selection", {})
        assert result.success is True
        assert result.data is not None
        assert result.data["degraded"] is True
        assert result.data["active_file"] is None
        assert result.data["selected_text"] is None


class TestFallbackUnsupported:
    """不支持操作的降级测试。"""

    def test_insert_content_degraded(self, manager: DegradationManager) -> None:
        """测试 insert_content 降级为提示。"""
        result = manager.execute_with_fallback("insert_content", {"content": "x"})
        assert result.success is True
        assert result.data is not None
        assert result.data["degraded"] is True

    def test_jump_to_degraded(self, manager: DegradationManager) -> None:
        """测试 jump_to 降级为提示。"""
        result = manager.execute_with_fallback("jump_to", {"line": 10})
        assert result.success is True
        assert result.data is not None
        assert result.data["degraded"] is True
