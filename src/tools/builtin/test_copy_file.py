"""
copy_file 工具测试
"""

import tempfile
from pathlib import Path

import pytest

from tools.builtin.copy_file import CopyFileTool


class TestCopyFileTool:
    """copy_file 工具测试类"""

    @pytest.fixture
    def tool(self):
        """创建工具实例"""
        return CopyFileTool()

    @pytest.fixture
    def temp_dir(self):
        """创建临时测试目录"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def source_file(self, temp_dir):
        """创建源文件"""
        file_path = temp_dir / "source.txt"
        file_path.write_text("source content")
        return file_path

    @pytest.fixture
    def source_dir(self, temp_dir):
        """创建源目录"""
        dir_path = temp_dir / "source_dir"
        dir_path.mkdir()
        (dir_path / "file1.txt").write_text("content1")
        (dir_path / "file2.txt").write_text("content2")
        (dir_path / "subdir").mkdir()
        (dir_path / "subdir" / "nested.txt").write_text("nested")
        return dir_path

    @pytest.mark.asyncio
    async def test_copy_file_basic(self, tool, temp_dir, source_file):
        """测试基本文件复制"""
        dest = temp_dir / "dest.txt"
        result = await tool.execute({
            "source": str(source_file),
            "destination": str(dest)
        })

        assert result.success is True
        assert dest.exists()
        assert dest.read_text() == "source content"
        assert result.data["type"] == "file"

    @pytest.mark.asyncio
    async def test_copy_file_to_directory(self, tool, temp_dir, source_file):
        """测试复制到目录（自动使用原文件名）"""
        dest_dir = temp_dir / "dest_dir"
        dest_dir.mkdir()

        dest = dest_dir / "source.txt"
        result = await tool.execute({
            "source": str(source_file),
            "destination": str(dest_dir)
        })

        assert result.success is True
        assert dest.exists()
        assert dest.read_text() == "source content"

    @pytest.mark.asyncio
    async def test_copy_file_overwrite(self, tool, temp_dir, source_file):
        """测试覆盖已存在的目标"""
        dest = temp_dir / "dest.txt"
        dest.write_text("original content")

        result = await tool.execute({
            "source": str(source_file),
            "destination": str(dest),
            "overwrite": True
        })

        assert result.success is True
        assert dest.read_text() == "source content"

    @pytest.mark.asyncio
    async def test_copy_file_no_overwrite(self, tool, temp_dir, source_file):
        """测试不覆盖已存在的目标"""
        dest = temp_dir / "dest.txt"
        dest.write_text("original content")

        result = await tool.execute({
            "source": str(source_file),
            "destination": str(dest),
            "overwrite": False
        })

        assert result.success is False
        assert result.error_code == "DESTINATION_EXISTS"
        assert dest.read_text() == "original content"  # 未被覆盖

    @pytest.mark.asyncio
    async def test_copy_directory(self, tool, temp_dir, source_dir):
        """测试复制目录"""
        dest = temp_dir / "dest_dir"
        result = await tool.execute({
            "source": str(source_dir),
            "destination": str(dest)
        })

        assert result.success is True
        assert dest.exists()
        assert dest.is_dir()
        assert (dest / "file1.txt").exists()
        assert (dest / "file2.txt").exists()
        assert (dest / "subdir" / "nested.txt").exists()

    @pytest.mark.asyncio
    async def test_copy_source_not_found(self, tool, temp_dir):
        """测试源文件不存在"""
        result = await tool.execute({
            "source": str(temp_dir / "nonexistent.txt"),
            "destination": str(temp_dir / "dest.txt")
        })

        assert result.success is False
        assert result.error_code == "SOURCE_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_copy_missing_source(self, tool, temp_dir):
        """测试源路径为空"""
        result = await tool.execute({
            "destination": str(temp_dir / "dest.txt")
        })

        assert result.success is False
        assert result.error_code == "MISSING_SOURCE"

    @pytest.mark.asyncio
    async def test_copy_missing_destination(self, tool, temp_dir, source_file):
        """测试目标路径为空"""
        result = await tool.execute({
            "source": str(source_file)
        })

        assert result.success is False
        assert result.error_code == "MISSING_DESTINATION"

    @pytest.mark.asyncio
    async def test_copy_to_nested_path(self, tool, temp_dir, source_file):
        """测试复制到深层嵌套路径（自动创建父目录）"""
        dest = temp_dir / "a" / "b" / "c" / "dest.txt"
        result = await tool.execute({
            "source": str(source_file),
            "destination": str(dest)
        })

        assert result.success is True
        assert dest.exists()
        assert dest.read_text() == "source content"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
