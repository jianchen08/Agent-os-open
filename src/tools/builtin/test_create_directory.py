"""
create_directory 工具测试
"""

import tempfile
from pathlib import Path

import pytest

from tools.builtin.create_directory import CreateDirectoryTool


class TestCreateDirectoryTool:
    """create_directory 工具测试类"""

    @pytest.fixture
    def tool(self):
        """创建工具实例"""
        return CreateDirectoryTool()

    @pytest.fixture
    def temp_dir(self):
        """创建临时测试目录"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.mark.asyncio
    async def test_create_directory_basic(self, tool, temp_dir):
        """测试基本目录创建"""
        new_dir = temp_dir / "new_dir"
        result = await tool.execute({"path": str(new_dir)})

        assert result.success is True
        assert new_dir.exists()
        assert new_dir.is_dir()
        assert result.data["created"] is True
        assert result.data["existed"] is False

    @pytest.mark.asyncio
    async def test_create_directory_with_parents(self, tool, temp_dir):
        """测试创建多层嵌套目录（自动创建父目录）"""
        nested_dir = temp_dir / "a" / "b" / "c"
        result = await tool.execute({
            "path": str(nested_dir),
            "parents": True
        })

        assert result.success is True
        assert nested_dir.exists()
        assert nested_dir.is_dir()

    @pytest.mark.asyncio
    async def test_create_directory_without_parents(self, tool, temp_dir):
        """测试不创建父目录（应失败）"""
        nested_dir = temp_dir / "nonexistent_parent" / "child"
        result = await tool.execute({
            "path": str(nested_dir),
            "parents": False
        })

        assert result.success is False
        assert not nested_dir.exists()

    @pytest.mark.asyncio
    async def test_create_directory_exist_ok_true(self, tool, temp_dir):
        """测试 exist_ok=True（目录存在时报错）"""
        existing_dir = temp_dir / "existing"
        existing_dir.mkdir()

        result = await tool.execute({
            "path": str(existing_dir),
            "exist_ok": True
        })

        assert result.success is False
        assert result.error_code == "DIRECTORY_EXISTS"

    @pytest.mark.asyncio
    async def test_create_directory_exist_ok_false(self, tool, temp_dir):
        """测试 exist_ok=False（目录存在时不报错）"""
        existing_dir = temp_dir / "existing"
        existing_dir.mkdir()

        result = await tool.execute({
            "path": str(existing_dir),
            "exist_ok": False
        })

        assert result.success is True
        assert result.data["created"] is False
        assert result.data["existed"] is True

    @pytest.mark.asyncio
    async def test_create_directory_path_is_file(self, tool, temp_dir):
        """测试路径已存在但不是目录"""
        file_path = temp_dir / "afile.txt"
        file_path.write_text("content")

        result = await tool.execute({"path": str(file_path)})

        assert result.success is False
        assert result.error_code == "PATH_EXISTS_NOT_DIRECTORY"

    @pytest.mark.asyncio
    async def test_create_directory_missing_path(self, tool):
        """测试路径为空"""
        result = await tool.execute({})

        assert result.success is False
        assert result.error_code == "MISSING_PATH"

    @pytest.mark.asyncio
    async def test_create_directory_default_params(self, tool, temp_dir):
        """测试默认参数（parents=True, exist_ok=True）"""
        # 测试默认 parents=True
        nested_dir = temp_dir / "default" / "nested"
        result = await tool.execute({"path": str(nested_dir)})

        assert result.success is True
        assert nested_dir.exists()

        # 测试默认 exist_ok=True 对已存在目录会报错
        result2 = await tool.execute({"path": str(nested_dir)})
        assert result2.success is False
        assert result2.error_code == "DIRECTORY_EXISTS"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
