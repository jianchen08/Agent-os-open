"""
delete_file 工具测试
"""

import os
import stat
import tempfile
from pathlib import Path

import pytest

from tools.builtin.delete_file import DeleteFileTool


class TestDeleteFileTool:
    """delete_file 工具测试类"""

    @pytest.fixture
    def tool(self):
        """创建工具实例"""
        return DeleteFileTool()

    @pytest.fixture
    def temp_dir(self):
        """创建临时测试目录"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def test_file(self, temp_dir):
        """创建测试文件"""
        file_path = temp_dir / "test.txt"
        file_path.write_text("test content")
        return file_path

    @pytest.fixture
    def test_dir(self, temp_dir):
        """创建测试目录"""
        dir_path = temp_dir / "test_dir"
        dir_path.mkdir()
        (dir_path / "file1.txt").write_text("content1")
        (dir_path / "file2.txt").write_text("content2")
        (dir_path / "subdir").mkdir()
        (dir_path / "subdir" / "nested.txt").write_text("nested")
        return dir_path

    @pytest.mark.asyncio
    async def test_delete_file_basic(self, tool, temp_dir, test_file):
        """测试基本文件删除"""
        result = await tool.execute({"path": str(test_file)})

        assert result.success is True
        assert not test_file.exists()
        assert result.data["deleted"] is True
        assert result.data["type"] == "file"

    @pytest.mark.asyncio
    async def test_delete_empty_directory(self, tool, temp_dir):
        """测试删除空目录"""
        dir_path = temp_dir / "empty_dir"
        dir_path.mkdir()

        result = await tool.execute({"path": str(dir_path)})

        assert result.success is True
        assert not dir_path.exists()

    @pytest.mark.asyncio
    async def test_delete_directory_recursive(self, tool, temp_dir, test_dir):
        """测试递归删除非空目录"""
        result = await tool.execute({
            "path": str(test_dir),
            "recursive": True
        })

        assert result.success is True
        assert not test_dir.exists()
        assert result.data["type"] == "directory"

    @pytest.mark.asyncio
    async def test_delete_directory_non_recursive(self, tool, temp_dir, test_dir):
        """测试非递归删除非空目录（应失败）"""
        result = await tool.execute({
            "path": str(test_dir),
            "recursive": False
        })

        assert result.success is False
        assert test_dir.exists()  # 目录仍然存在

    @pytest.mark.asyncio
    async def test_delete_readonly_file(self, tool, temp_dir):
        """测试删除只读文件"""
        file_path = temp_dir / "readonly.txt"
        file_path.write_text("readonly content")

        # 设置只读属性
        os.chmod(file_path, stat.S_IRUSR)

        result = await tool.execute({
            "path": str(file_path),
            "force": True
        })

        assert result.success is True
        assert not file_path.exists()

    @pytest.mark.asyncio
    async def test_delete_readonly_file_no_force(self, tool, temp_dir):
        """测试不强制删除只读文件（应失败）"""
        file_path = temp_dir / "readonly.txt"
        file_path.write_text("readonly content")

        # 设置只读属性
        os.chmod(file_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

        result = await tool.execute({
            "path": str(file_path),
            "force": False
        })

        assert result.success is False
        assert file_path.exists()

    @pytest.mark.asyncio
    async def test_delete_not_found(self, tool, temp_dir):
        """测试删除不存在的文件"""
        result = await tool.execute({
            "path": str(temp_dir / "nonexistent.txt")
        })

        assert result.success is False
        assert result.error_code == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_delete_missing_path(self, tool):
        """测试路径为空"""
        result = await tool.execute({})

        assert result.success is False
        assert result.error_code == "MISSING_PATH"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
