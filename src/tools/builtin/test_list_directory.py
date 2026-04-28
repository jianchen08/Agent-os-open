"""
list_directory 工具测试
"""

import asyncio
import tempfile
import os
from pathlib import Path

import pytest

from tools.builtin.list_directory import ListDirectoryTool


class TestListDirectoryTool:
    """list_directory 工具测试类"""

    @pytest.fixture
    def tool(self):
        """创建工具实例"""
        return ListDirectoryTool()

    @pytest.fixture
    def temp_dir(self):
        """创建临时测试目录"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def populated_dir(self, temp_dir):
        """创建包含文件的测试目录"""
        # 创建子目录
        (temp_dir / "subdir").mkdir()
        (temp_dir / "subdir" / "nested").mkdir()

        # 创建文件
        (temp_dir / "file1.txt").write_text("content1")
        (temp_dir / "file2.py").write_text("print('hello')")
        (temp_dir / "subdir" / "nested" / "nested.txt").write_text("nested content")

        # 创建隐藏文件
        (temp_dir / ".hidden").write_text("hidden")
        (temp_dir / ".config").write_text("config")

        return temp_dir

    @pytest.mark.asyncio
    async def test_list_directory_basic(self, tool, populated_dir):
        """测试基本目录列表功能"""
        result = await tool.execute({"path": str(populated_dir)})

        assert result.success is True
        assert "items" in result.data
        assert result.data["item_count"] == 4  # 2 dirs + 2 files (hidden excluded)

        names = [item["name"] for item in result.data["items"]]
        assert "file1.txt" in names
        assert "file2.py" in names
        assert "subdir" in names

    @pytest.mark.asyncio
    async def test_list_directory_include_hidden(self, tool, populated_dir):
        """测试包含隐藏文件"""
        result = await tool.execute({
            "path": str(populated_dir),
            "include_hidden": True
        })

        assert result.success is True
        assert result.data["item_count"] == 6  # 4 + 2 hidden

        names = [item["name"] for item in result.data["items"]]
        assert ".hidden" in names
        assert ".config" in names

    @pytest.mark.asyncio
    async def test_list_directory_recursive(self, tool, populated_dir):
        """测试递归列出目录"""
        result = await tool.execute({
            "path": str(populated_dir),
            "recursive": True
        })

        assert result.success is True
        # 4 items at root + 2 items in subdir + 1 item in nested
        assert result.data["item_count"] == 7

    @pytest.mark.asyncio
    async def test_list_directory_with_pattern(self, tool, populated_dir):
        """测试文件名模式匹配"""
        result = await tool.execute({
            "path": str(populated_dir),
            "pattern": "*.py"
        })

        assert result.success is True
        names = [item["name"] for item in result.data["items"]]
        assert names == ["file2.py"]

    @pytest.mark.asyncio
    async def test_list_directory_recursive_with_pattern(self, tool, populated_dir):
        """测试递归+模式匹配"""
        result = await tool.execute({
            "path": str(populated_dir),
            "recursive": True,
            "pattern": "*.txt"
        })

        assert result.success is True
        names = [item["name"] for item in result.data["items"]]
        assert "file1.txt" in names
        assert "nested.txt" in names
        assert "file2.py" not in names

    @pytest.mark.asyncio
    async def test_list_directory_not_found(self, tool, temp_dir):
        """测试目录不存在"""
        result = await tool.execute({"path": str(temp_dir / "nonexistent")})

        assert result.success is False
        assert result.error_code == "DIRECTORY_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_list_directory_file_instead(self, tool, temp_dir):
        """测试路径是文件而非目录"""
        file_path = temp_dir / "afile.txt"
        file_path.write_text("content")

        result = await tool.execute({"path": str(file_path)})

        assert result.success is False
        assert result.error_code == "NOT_A_DIRECTORY"

    @pytest.mark.asyncio
    async def test_list_directory_empty(self, tool, temp_dir):
        """测试空目录"""
        empty_dir = temp_dir / "empty"
        empty_dir.mkdir()

        result = await tool.execute({"path": str(empty_dir)})

        assert result.success is True
        assert result.data["item_count"] == 0
        assert result.data["items"] == []

    @pytest.mark.asyncio
    async def test_list_directory_missing_path(self, tool):
        """测试路径为空"""
        result = await tool.execute({})

        assert result.success is False
        assert result.error_code == "MISSING_PATH"

    @pytest.mark.asyncio
    async def test_list_directory_item_info(self, tool, temp_dir):
        """测试返回项包含正确的信息"""
        file_path = temp_dir / "testfile.txt"
        file_path.write_text("test content")

        result = await tool.execute({"path": str(temp_dir)})

        assert result.success is True
        item = result.data["items"][0]
        assert "name" in item
        assert "type" in item
        assert "size" in item
        assert "modified" in item
        assert item["type"] == "file"

    @pytest.mark.asyncio
    async def test_list_directory_subdir_type(self, tool, temp_dir):
        """测试子目录类型正确"""
        subdir = temp_dir / "subdir"
        subdir.mkdir()

        result = await tool.execute({"path": str(temp_dir)})

        items = result.data["items"]
        subdir_item = next(i for i in items if i["name"] == "subdir")
        assert subdir_item["type"] == "directory"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
