"""enhanced_search 闸2 改动 — workspace 边界检查已删除。

验证 _validate_search_path 在 host 模式下的契约：
- workspace 边界检查已删除：访问 workspace 外的有效路径不再被拦截
- 路径存在性检查保留：不存在的路径仍返回 PATH_NOT_FOUND
- 敏感系统目录黑名单保留：OS 核心目录仍被拦截
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.builtin.enhanced_search import EnhancedSearchTool


@pytest.fixture
def tool(tmp_path: Path) -> EnhancedSearchTool:
    """创建 EnhancedSearchTool，base_path 指向 tmp_path（模拟 workspace）。"""
    t = EnhancedSearchTool(base_path=str(tmp_path))
    t._workspace = tmp_path
    t.base_path = tmp_path
    return t


class TestWorkspaceBoundaryRemoved:
    """改动：workspace 边界检查已删除，host 模式不管路径。"""

    def test_path_outside_workspace_not_blocked(self, tool, tmp_path) -> None:
        """workspace 外的有效路径不再被边界拦截。

        改动前：返回 PATH_OUTSIDE_WORKSPACE。
        改动后：返回 None（通过）。
        """
        outside = tmp_path.parent / "outside_workspace_dir"
        outside.mkdir(exist_ok=True)
        try:
            result = tool._validate_search_path(str(outside))
            assert result is None  # 通过，不拦截
        finally:
            outside.rmdir()

    def test_path_inside_workspace_passes(self, tool, tmp_path) -> None:
        """workspace 内的路径正常通过。"""
        result = tool._validate_search_path(str(tmp_path))
        assert result is None


class TestExistenceCheckKept:
    """保留：路径存在性检查。"""

    def test_nonexistent_path_blocked(self, tool, tmp_path) -> None:
        """不存在的路径返回 PATH_NOT_FOUND。"""
        result = tool._validate_search_path(str(tmp_path / "does_not_exist"))
        assert result is not None
        assert result.success is False
        assert "搜索路径不存在" in result.error


class TestSensitivePathCheckKept:
    """保留：敏感系统目录黑名单。"""

    @pytest.mark.skipif(os.name != "nt", reason="Windows 敏感目录仅 Windows 测试")
    def test_windows_sensitive_blocked(self, tool) -> None:
        """C:/Windows 命中敏感目录黑名单。"""
        # System32 真实存在，所以能通过存在性检查，再被敏感目录拦
        result = tool._validate_search_path("C:\\Windows\\System32")
        assert result is not None
        assert result.success is False
        assert "系统目录" in result.error
