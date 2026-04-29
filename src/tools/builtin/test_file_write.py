"""
file_write 工具测试
"""

import tempfile
from pathlib import Path

import pytest

from tools.builtin.file_write import FileWriteTool


class TestFileWriteTool:
    """file_write 工具测试类"""

    @pytest.fixture
    def tool(self):
        """创建工具实例"""
        return FileWriteTool()

    @pytest.fixture
    def temp_dir(self):
        """创建临时测试目录"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def _make_inputs(self, tool, temp_dir, **overrides):
        """构造带 workspace 的 inputs 字典"""
        base = {"workspace": str(temp_dir)}
        if "path" in overrides and not Path(overrides["path"]).is_absolute():
            overrides["path"] = str(temp_dir / overrides["path"])
        base.update(overrides)
        return base

    # ---- write action ----

    @pytest.mark.asyncio
    async def test_write_full_to_new_file(self, tool, temp_dir):
        """全量写入新文件"""
        path = temp_dir / "new.txt"
        result = await tool.execute(
            self._make_inputs(tool, temp_dir, action="write", path=str(path), content="hello\nworld")
        )

        assert result.success is True
        assert result.data["lines"] == 2
        assert result.data["backup"] is None
        assert path.read_text(encoding="utf-8") == "hello\nworld"

    @pytest.mark.asyncio
    async def test_write_full_to_existing_file_creates_backup(self, tool, temp_dir):
        """全量写入已有文件，创建 .bak 备份"""
        path = temp_dir / "existing.txt"
        path.write_text("old content", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(tool, temp_dir, action="write", path=str(path), content="new content")
        )

        assert result.success is True
        assert result.data["backup"] is not None
        assert path.read_text(encoding="utf-8") == "new content"
        backup = Path(str(path) + ".bak")
        assert backup.exists()
        assert backup.read_text(encoding="utf-8") == "old content"

    @pytest.mark.asyncio
    async def test_write_without_content(self, tool, temp_dir):
        """全量写入缺少 content"""
        path = temp_dir / "missing.txt"
        result = await tool.execute(
            self._make_inputs(tool, temp_dir, action="write", path=str(path))
        )

        assert result.success is False
        assert result.error_code == "MISSING_CONTENT"

    @pytest.mark.asyncio
    async def test_write_no_backup(self, tool, temp_dir):
        """全量写入 create_backup=False，不创建 .bak"""
        path = temp_dir / "nobackup.txt"
        path.write_text("original", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="write", path=str(path), content="updated",
                create_backup=False,
            )
        )

        assert result.success is True
        assert result.data["backup"] is None
        assert path.read_text(encoding="utf-8") == "updated"
        assert not Path(str(path) + ".bak").exists()

    @pytest.mark.asyncio
    async def test_write_replace_single_line(self, tool, temp_dir):
        """write 替换单行（仅 start_line）"""
        path = temp_dir / "lines.txt"
        path.write_text("aaa\nbbb\nccc", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="write", path=str(path), content="BBB",
                start_line=2,
            )
        )

        assert result.success is True
        assert result.data["lines"] == 1
        assert path.read_text(encoding="utf-8") == "aaa\nBBB\nccc"

    @pytest.mark.asyncio
    async def test_write_replace_line_range(self, tool, temp_dir):
        """write 替换行范围（start_line + end_line）"""
        path = temp_dir / "range.txt"
        path.write_text("line1\nline2\nline3\nline4", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="write", path=str(path), content="NEW",
                start_line=2, end_line=3,
            )
        )

        assert result.success is True
        assert result.data["lines"] == 2
        assert path.read_text(encoding="utf-8") == "line1\nNEW\nline4"

    @pytest.mark.asyncio
    async def test_write_line_out_of_range(self, tool, temp_dir):
        """write 行号越界"""
        path = temp_dir / "short.txt"
        path.write_text("only\n two\nlines", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="write", path=str(path), content="x",
                start_line=10,
            )
        )

        assert result.success is False
        assert result.error_code == "LINE_OUT_OF_RANGE"

    @pytest.mark.asyncio
    async def test_write_end_line_less_than_start(self, tool, temp_dir):
        """write end_line < start_line"""
        path = temp_dir / "inv.txt"
        path.write_text("a\nb\nc", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="write", path=str(path), content="x",
                start_line=3, end_line=1,
            )
        )

        assert result.success is False
        assert result.error_code == "INVALID_LINE_RANGE"

    @pytest.mark.asyncio
    async def test_write_with_line_params_file_not_found(self, tool, temp_dir):
        """write 带行号参数但文件不存在"""
        path = temp_dir / "noexist.txt"

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="write", path=str(path), content="x",
                start_line=1,
            )
        )

        assert result.success is False
        assert result.error_code == "FILE_NOT_FOUND"

    # ---- search_replace action ----

    @pytest.mark.asyncio
    async def test_search_replace_simple(self, tool, temp_dir):
        """简单搜索替换"""
        path = temp_dir / "sr.txt"
        path.write_text("hello world hello", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="search_replace", path=str(path),
                old_str="hello", new_str="hi",
            )
        )

        assert result.success is True
        assert result.data["replacements"] == 2
        assert path.read_text(encoding="utf-8") == "hi world hi"

    @pytest.mark.asyncio
    async def test_search_replace_count_zero(self, tool, temp_dir):
        """count=0 替换所有匹配项"""
        path = temp_dir / "all.txt"
        path.write_text("aaa bbb aaa bbb aaa", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="search_replace", path=str(path),
                old_str="aaa", new_str="ccc", count=0,
            )
        )

        assert result.success is True
        assert result.data["replacements"] == 3
        assert path.read_text(encoding="utf-8") == "ccc bbb ccc bbb ccc"

    @pytest.mark.asyncio
    async def test_search_replace_limited_count(self, tool, temp_dir):
        """count=1 只替换第一个匹配项"""
        path = temp_dir / "limit.txt"
        path.write_text("foo bar foo bar", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="search_replace", path=str(path),
                old_str="foo", new_str="baz", count=1,
            )
        )

        assert result.success is True
        assert result.data["replacements"] == 1
        assert path.read_text(encoding="utf-8") == "baz bar foo bar"

    @pytest.mark.asyncio
    async def test_search_replace_pattern_not_found(self, tool, temp_dir):
        """搜索文本不存在"""
        path = temp_dir / "nf.txt"
        path.write_text("some content", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="search_replace", path=str(path),
                old_str="NOT_HERE", new_str="x",
            )
        )

        assert result.success is False
        assert result.error_code == "PATTERN_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_search_replace_missing_old_str(self, tool, temp_dir):
        """缺少 old_str"""
        path = temp_dir / "mo.txt"
        path.write_text("x", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="search_replace", path=str(path),
                new_str="y",
            )
        )

        assert result.success is False
        assert result.error_code == "MISSING_OLD_STR"

    @pytest.mark.asyncio
    async def test_search_replace_missing_path(self, tool, temp_dir):
        """search_replace 缺少 path"""
        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="search_replace",
                old_str="a", new_str="b",
            )
        )

        assert result.success is False
        assert result.error_code == "MISSING_PATH"

    @pytest.mark.asyncio
    async def test_search_replace_file_not_found(self, tool, temp_dir):
        """search_replace 文件不存在"""
        path = temp_dir / "nofile.txt"

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="search_replace", path=str(path),
                old_str="a", new_str="b",
            )
        )

        assert result.success is False
        assert result.error_code == "FILE_NOT_FOUND"

    # ---- insert action ----

    @pytest.mark.asyncio
    async def test_insert_at_line_zero(self, tool, temp_dir):
        """insert line=0 在文件开头插入"""
        path = temp_dir / "ins0.txt"
        path.write_text("second", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="insert", path=str(path),
                line=0, content="first",
            )
        )

        assert result.success is True
        assert result.data["inserted_at"] == 0
        assert path.read_text(encoding="utf-8") == "first\nsecond"

    @pytest.mark.asyncio
    async def test_insert_at_middle(self, tool, temp_dir):
        """insert 在中间行后插入"""
        path = temp_dir / "insmid.txt"
        path.write_text("a\nb\nc", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="insert", path=str(path),
                line=2, content="INSERTED",
            )
        )

        assert result.success is True
        assert result.data["inserted_at"] == 2
        assert path.read_text(encoding="utf-8") == "a\nb\nINSERTED\nc"

    @pytest.mark.asyncio
    async def test_insert_at_end(self, tool, temp_dir):
        """insert 在最后一行后插入（追加效果）"""
        path = temp_dir / "insend.txt"
        path.write_text("a\nb\nc", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="insert", path=str(path),
                line=3, content="tail",
            )
        )

        assert result.success is True
        assert path.read_text(encoding="utf-8") == "a\nb\nc\ntail"

    @pytest.mark.asyncio
    async def test_insert_line_out_of_range(self, tool, temp_dir):
        """insert 行号越界"""
        path = temp_dir / "insoor.txt"
        path.write_text("only", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="insert", path=str(path),
                line=99, content="x",
            )
        )

        assert result.success is False
        assert result.error_code == "LINE_OUT_OF_RANGE"

    @pytest.mark.asyncio
    async def test_insert_missing_line(self, tool, temp_dir):
        """insert 缺少 line"""
        path = temp_dir / "insml.txt"
        path.write_text("x", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="insert", path=str(path),
                content="y",
            )
        )

        assert result.success is False
        assert result.error_code == "MISSING_LINE"

    @pytest.mark.asyncio
    async def test_insert_missing_content(self, tool, temp_dir):
        """insert 缺少 content"""
        path = temp_dir / "insmc.txt"
        path.write_text("x", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="insert", path=str(path),
                line=1,
            )
        )

        assert result.success is False
        assert result.error_code == "MISSING_CONTENT"

    # ---- delete_lines action ----

    @pytest.mark.asyncio
    async def test_delete_line_range(self, tool, temp_dir):
        """删除行范围"""
        path = temp_dir / "delrng.txt"
        path.write_text("a\nb\nc\nd\ne", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="delete_lines", path=str(path),
                start_line=2, end_line=4,
            )
        )

        assert result.success is True
        assert result.data["count"] == 3
        assert result.data["deleted_lines"] == "2-4"
        assert path.read_text(encoding="utf-8") == "a\ne"

    @pytest.mark.asyncio
    async def test_delete_single_line(self, tool, temp_dir):
        """删除单行（start_line == end_line）"""
        path = temp_dir / "delsingle.txt"
        path.write_text("x\ny\nz", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="delete_lines", path=str(path),
                start_line=2, end_line=2,
            )
        )

        assert result.success is True
        assert result.data["count"] == 1
        assert path.read_text(encoding="utf-8") == "x\nz"

    @pytest.mark.asyncio
    async def test_delete_invalid_range(self, tool, temp_dir):
        """删除行范围无效（end < start）"""
        path = temp_dir / "delinv.txt"
        path.write_text("a\nb\nc", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="delete_lines", path=str(path),
                start_line=3, end_line=1,
            )
        )

        assert result.success is False
        assert result.error_code == "INVALID_LINE_RANGE"

    @pytest.mark.asyncio
    async def test_delete_missing_start_line(self, tool, temp_dir):
        """delete_lines 缺少 start_line"""
        path = temp_dir / "delms.txt"
        path.write_text("x", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="delete_lines", path=str(path),
                end_line=1,
            )
        )

        assert result.success is False
        assert result.error_code == "MISSING_START_LINE"

    @pytest.mark.asyncio
    async def test_delete_missing_end_line(self, tool, temp_dir):
        """delete_lines 缺少 end_line"""
        path = temp_dir / "delme.txt"
        path.write_text("x", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="delete_lines", path=str(path),
                start_line=1,
            )
        )

        assert result.success is False
        assert result.error_code == "MISSING_END_LINE"

    # ---- append action ----

    @pytest.mark.asyncio
    async def test_append_to_existing_file(self, tool, temp_dir):
        """追加到已有文件"""
        path = temp_dir / "app.txt"
        path.write_text("first", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="append", path=str(path), content="second",
            )
        )

        assert result.success is True
        assert path.read_text(encoding="utf-8") == "first\nsecond"

    @pytest.mark.asyncio
    async def test_append_creates_new_file(self, tool, temp_dir):
        """追加到不存在的文件，创建新文件"""
        path = temp_dir / "newappend.txt"

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="append", path=str(path), content="created",
            )
        )

        assert result.success is True
        assert path.exists()
        assert path.read_text(encoding="utf-8") == "created"

    @pytest.mark.asyncio
    async def test_append_no_double_newline(self, tool, temp_dir):
        """追加到以换行结尾的文件，不产生双换行"""
        path = temp_dir / "trail.txt"
        path.write_text("first\n", encoding="utf-8")

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="append", path=str(path), content="second",
            )
        )

        assert result.success is True
        content = path.read_text(encoding="utf-8")
        assert content == "first\nsecond"
        assert "first\n\nsecond" not in content

    @pytest.mark.asyncio
    async def test_append_missing_content(self, tool, temp_dir):
        """append 缺少 content"""
        path = temp_dir / "appmc.txt"

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="append", path=str(path),
            )
        )

        assert result.success is False
        assert result.error_code == "MISSING_CONTENT"

    # ---- general ----

    @pytest.mark.asyncio
    async def test_invalid_action(self, tool, temp_dir):
        """不支持的操作类型"""
        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="unknown", path=str(temp_dir / "x.txt"), content="y",
            )
        )

        assert result.success is False
        assert result.error_code == "INVALID_ACTION"

    @pytest.mark.asyncio
    async def test_missing_path(self, tool, temp_dir):
        """缺少 path"""
        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="write", content="data",
            )
        )

        assert result.success is False
        assert result.error_code == "MISSING_PATH"

    @pytest.mark.asyncio
    async def test_path_is_directory(self, tool, temp_dir):
        """路径是目录而非文件"""
        subdir = temp_dir / "subdir"
        subdir.mkdir()

        result = await tool.execute(
            self._make_inputs(
                tool, temp_dir,
                action="write", path=str(subdir), content="data",
                start_line=1,
            )
        )

        assert result.success is False
        assert result.error_code == "NOT_A_FILE"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
