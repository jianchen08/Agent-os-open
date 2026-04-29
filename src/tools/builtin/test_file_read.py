"""
file_read 工具测试
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from tools.builtin.file_read import FileReadTool, MAX_FILE_SIZE


class TestFileReadTool:
    """file_read 工具测试类"""

    @pytest.fixture
    def tool(self):
        """创建工具实例"""
        return FileReadTool()

    @pytest.fixture
    def temp_dir(self):
        """创建临时测试目录"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    # ── 基本读写 ──

    @pytest.mark.asyncio
    async def test_read_normal_text_file(self, tool, temp_dir):
        """测试读取普通文本文件"""
        file_path = temp_dir / "hello.txt"
        file_path.write_text("hello world\nsecond line", encoding="utf-8")

        result = await tool.execute({"path": str(file_path)})

        assert result.success is True
        assert result.data["content"] == "hello world\nsecond line"
        assert result.data["lines"] == 2
        assert "size" in result.data
        assert result.data["file"] == str(file_path)

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self, tool, temp_dir):
        """测试读取不存在的文件 → FILE_NOT_FOUND"""
        result = await tool.execute({"path": str(temp_dir / "no_such.txt")})

        assert result.success is False
        assert result.error_code == "FILE_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_read_directory_path(self, tool, temp_dir):
        """测试路径是目录而非文件 → NOT_A_FILE"""
        result = await tool.execute({"path": str(temp_dir)})

        assert result.success is False
        assert result.error_code == "NOT_A_FILE"

    @pytest.mark.asyncio
    async def test_read_empty_path(self, tool):
        """测试空路径 → MISSING_PATH"""
        result = await tool.execute({"path": ""})

        assert result.success is False
        assert result.error_code == "MISSING_PATH"

    @pytest.mark.asyncio
    async def test_read_missing_path_key(self, tool):
        """测试缺少 path 键 → MISSING_PATH"""
        result = await tool.execute({})

        assert result.success is False
        assert result.error_code == "MISSING_PATH"

    # ── 二进制文件拒绝 ──

    @pytest.mark.asyncio
    async def test_read_rejected_binary_exe(self, tool, temp_dir):
        """测试读取 .exe 文件 → BINARY_FILE_NOT_SUPPORTED"""
        file_path = temp_dir / "program.exe"
        file_path.write_bytes(b"\x00\x01\x02")

        result = await tool.execute({"path": str(file_path)})

        assert result.success is False
        assert result.error_code == "BINARY_FILE_NOT_SUPPORTED"

    @pytest.mark.asyncio
    async def test_read_rejected_binary_zip(self, tool, temp_dir):
        """测试读取 .zip 文件 → BINARY_FILE_NOT_SUPPORTED"""
        file_path = temp_dir / "archive.zip"
        file_path.write_bytes(b"PK\x03\x04")

        result = await tool.execute({"path": str(file_path)})

        assert result.success is False
        assert result.error_code == "BINARY_FILE_NOT_SUPPORTED"

    @pytest.mark.asyncio
    async def test_read_rejected_binary_pyc(self, tool, temp_dir):
        """测试读取 .pyc 文件 → BINARY_FILE_NOT_SUPPORTED"""
        file_path = temp_dir / "module.pyc"
        file_path.write_bytes(b"\x00\x00\x00\x00")

        result = await tool.execute({"path": str(file_path)})

        assert result.success is False
        assert result.error_code == "BINARY_FILE_NOT_SUPPORTED"

    # ── 二进制内容嗅探 ──

    @pytest.mark.asyncio
    async def test_read_file_with_null_bytes(self, tool, temp_dir):
        """测试文本文件含有 null 字节 → BINARY_CONTENT_DETECTED"""
        file_path = temp_dir / "data.log"
        file_path.write_bytes(b"some text\x00more text")

        result = await tool.execute({"path": str(file_path)})

        assert result.success is False
        assert result.error_code == "BINARY_CONTENT_DETECTED"

    # ── 文件大小限制 ──

    @pytest.mark.asyncio
    async def test_read_file_too_large(self, tool, temp_dir):
        """测试读取超过 2MB 的文件 → FILE_TOO_LARGE"""
        file_path = temp_dir / "big.txt"
        file_path.write_text("x" * 100, encoding="utf-8")

        mock_stat = MagicMock()
        mock_stat.st_size = MAX_FILE_SIZE + 1

        with patch.object(Path, "stat", return_value=mock_stat):
            result = await tool.execute({"path": str(file_path)})

        assert result.success is False
        assert result.error_code == "FILE_TOO_LARGE"

    # ── 编码 ──

    @pytest.mark.asyncio
    async def test_read_utf8_chinese(self, tool, temp_dir):
        """测试读取 UTF-8 编码的中文文件"""
        file_path = temp_dir / "chinese.txt"
        file_path.write_text("你好世界\n第二行", encoding="utf-8")

        result = await tool.execute({"path": str(file_path)})

        assert result.success is True
        assert result.data["content"] == "你好世界\n第二行"
        assert result.data["lines"] == 2

    @pytest.mark.asyncio
    async def test_read_gbk_fallback(self, tool, temp_dir):
        """测试 GBK 编码文件读取（UTF-8 失败后回退 GBK）"""
        file_path = temp_dir / "gbk.txt"
        file_path.write_text("中文内容测试", encoding="gbk")

        result = await tool.execute({"path": str(file_path)})

        assert result.success is True
        assert "中文" in result.data["content"]

    # ── fields 参数 ──

    @pytest.mark.asyncio
    async def test_fields_yaml(self, tool, temp_dir):
        """测试 YAML 文件 fields 参数"""
        data = {"id": 42, "name": "test", "extra": "skip_me"}
        file_path = temp_dir / "config.yaml"
        file_path.write_text(yaml.dump(data), encoding="utf-8")

        result = await tool.execute({
            "path": str(file_path),
            "fields": ["id", "name"],
        })

        assert result.success is True
        assert result.data == {"id": 42, "name": "test"}

    @pytest.mark.asyncio
    async def test_fields_json(self, tool, temp_dir):
        """测试 JSON 文件 fields 参数"""
        data = {"id": 99, "name": "hello", "unused": "value"}
        file_path = temp_dir / "data.json"
        file_path.write_text(json.dumps(data), encoding="utf-8")

        result = await tool.execute({
            "path": str(file_path),
            "fields": ["id", "name"],
        })

        assert result.success is True
        assert result.data == {"id": 99, "name": "hello"}

    @pytest.mark.asyncio
    async def test_fields_nested_dot_notation(self, tool, temp_dir):
        """测试嵌套字段 a.b.c 提取"""
        data = {"a": {"b": {"c": "deep_value"}}, "x": 1}
        file_path = temp_dir / "nested.yaml"
        file_path.write_text(yaml.dump(data), encoding="utf-8")

        result = await tool.execute({
            "path": str(file_path),
            "fields": ["a.b.c"],
        })

        assert result.success is True
        assert result.data == {"a": {"b": {"c": "deep_value"}}}

    @pytest.mark.asyncio
    async def test_fields_non_yaml_json(self, tool, temp_dir):
        """测试 fields 参数用于非 YAML/JSON 文件 → FIELDS_NOT_SUPPORTED"""
        file_path = temp_dir / "script.py"
        file_path.write_text("x = 1", encoding="utf-8")

        result = await tool.execute({
            "path": str(file_path),
            "fields": ["x"],
        })

        assert result.success is False
        assert result.error_code == "FIELDS_NOT_SUPPORTED"

    @pytest.mark.asyncio
    async def test_fields_missing_nested_key(self, tool, temp_dir):
        """测试 fields 提取不存在的嵌套键 → 不包含该字段"""
        data = {"a": {"b": 1}}
        file_path = temp_dir / "partial.yaml"
        file_path.write_text(yaml.dump(data), encoding="utf-8")

        result = await tool.execute({
            "path": str(file_path),
            "fields": ["a.b", "a.c"],
        })

        assert result.success is True
        assert result.data == {"a": {"b": 1}}

    # ── tail 参数 ──

    @pytest.mark.asyncio
    async def test_tail_param(self, tool, temp_dir):
        """测试 tail 参数：返回最后 N 行"""
        lines = [f"line {i}" for i in range(10)]
        file_path = temp_dir / "log.txt"
        file_path.write_text("\n".join(lines), encoding="utf-8")

        result = await tool.execute({
            "path": str(file_path),
            "tail": 3,
        })

        assert result.success is True
        assert result.data["total_lines"] == 10
        assert result.data["lines"] == 3
        assert "line 9" in result.data["content"]
        assert "line 0" not in result.data["content"]

    @pytest.mark.asyncio
    async def test_tail_greater_than_total(self, tool, temp_dir):
        """测试 tail 大于文件总行数 → 返回全部内容"""
        file_path = temp_dir / "short.txt"
        file_path.write_text("only line", encoding="utf-8")

        result = await tool.execute({
            "path": str(file_path),
            "tail": 100,
        })

        assert result.success is True
        # tail >= total_lines → 走常规路径
        assert result.data["content"] == "only line"

    # ── 二进制路由（document/image）──

    @pytest.mark.asyncio
    async def test_document_routing(self, tool, temp_dir):
        """测试 PDF 文件路由到 convert_binary_to_markdown"""
        file_path = temp_dir / "report.pdf"
        file_path.write_bytes(b"%PDF-1.4 fake")

        from tools.types import create_success_result

        with patch(
            "tools.builtin.file_read.convert_binary_to_markdown",
            return_value=create_success_result(
                data={"file": str(file_path), "content": "# Report", "format": "document", "size": "13B"},
            ),
        ) as mock_convert:
            result = await tool.execute({"path": str(file_path)})

            mock_convert.assert_called_once_with(file_path)
            assert result.success is True
            assert result.data["content"] == "# Report"

    @pytest.mark.asyncio
    async def test_image_routing(self, tool, temp_dir):
        """测试 PNG 文件路由到 convert_binary_to_markdown"""
        file_path = temp_dir / "photo.png"
        file_path.write_bytes(b"\x89PNG\r\n\x1a\n")

        from tools.types import create_success_result

        with patch(
            "tools.builtin.file_read.convert_binary_to_markdown",
            return_value=create_success_result(
                data={"file": str(file_path), "content": "An image description", "format": "image", "size": "8B"},
            ),
        ) as mock_convert:
            result = await tool.execute({"path": str(file_path)})

            mock_convert.assert_called_once_with(file_path)
            assert result.success is True
            assert result.data["format"] == "image"

    # ── 解析错误 ──

    @pytest.mark.asyncio
    async def test_yaml_parse_error(self, tool, temp_dir):
        """测试无效 YAML + fields → PARSE_ERROR"""
        file_path = temp_dir / "bad.yaml"
        file_path.write_text(":\n  :\n    - {", encoding="utf-8")

        result = await tool.execute({
            "path": str(file_path),
            "fields": ["x"],
        })

        assert result.success is False
        assert result.error_code == "PARSE_ERROR"

    @pytest.mark.asyncio
    async def test_json_parse_error(self, tool, temp_dir):
        """测试无效 JSON + fields → PARSE_ERROR"""
        file_path = temp_dir / "bad.json"
        file_path.write_text("{invalid json", encoding="utf-8")

        result = await tool.execute({
            "path": str(file_path),
            "fields": ["x"],
        })

        assert result.success is False
        assert result.error_code == "PARSE_ERROR"

    # ── workspace 参数 ──

    @pytest.mark.asyncio
    async def test_read_with_workspace(self, tool, temp_dir):
        """测试通过 workspace 参数读取相对路径文件"""
        file_path = temp_dir / "notes.txt"
        file_path.write_text("workspace test", encoding="utf-8")

        result = await tool.execute({
            "path": "notes.txt",
            "workspace": str(temp_dir),
        })

        assert result.success is True
        assert result.data["content"] == "workspace test"

    @pytest.mark.asyncio
    async def test_fields_on_non_dict_yaml(self, tool, temp_dir):
        """测试 fields 用于非字典类型 YAML（如列表）→ FIELDS_NOT_SUPPORTED"""
        file_path = temp_dir / "list.yaml"
        file_path.write_text("- item1\n- item2\n", encoding="utf-8")

        result = await tool.execute({
            "path": str(file_path),
            "fields": ["item1"],
        })

        assert result.success is False
        assert result.error_code == "FIELDS_NOT_SUPPORTED"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
