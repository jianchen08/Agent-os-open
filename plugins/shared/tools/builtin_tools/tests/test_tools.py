# @feature: FP-0.2.二 内部模块 manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""集成测试——覆盖 8 个内置工具的核心场景（bash_execute/web_operate 双轨已收敛到 bash/ 与 web_ext/ 插件）。

通过直接调用工具函数验证行为等价性（AC-08-3/4）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentos_builtin_tools.fs_tools import (
    FILE_WRITE_OUTPUT_SCHEMA,
    copy_file,
    create_directory,
    delete_file,
    file_read,
    file_write,
    list_directory,
    move_file,
)
from agentos_builtin_tools.search_tool import enhanced_search
from agentos_builtin_tools.server import TOOL_REGISTRY

pytestmark = pytest.mark.unit



# ═════════════════════════════════════════════════════════════
# AC-08-1: 10 个工具注册
# ═════════════════════════════════════════════════════════════

class TestToolRegistration:
    def test_all_8_tools_registered(self) -> None:
        expected = {
            "file_read", "file_write", "enhanced_search",
            "list_directory", "create_directory", "copy_file", "move_file",
            "delete_file",
        }
        assert set(TOOL_REGISTRY.keys()) == expected

    def test_all_tools_have_schema(self) -> None:
        for name, (schema, _) in TOOL_REGISTRY.items():
            assert schema["type"] == "object", f"{name} schema missing type"
            assert "properties" in schema, f"{name} schema missing properties"

    def test_all_tools_callable(self) -> None:
        for name, (_, handler) in TOOL_REGISTRY.items():
            assert callable(handler), f"{name} handler not callable"


# ═════════════════════════════════════════════════════════════
# AC-08-2: JSON Schema 验证
# ═════════════════════════════════════════════════════════════

class TestSchemaConsistency:
    def test_file_read_schema_has_required_path(self) -> None:
        schema = TOOL_REGISTRY["file_read"][0]
        assert "path" in schema.get("required", [])

    def test_file_write_schema_has_action(self) -> None:
        schema = TOOL_REGISTRY["file_write"][0]
        assert "path" in schema.get("required", [])

    def test_search_schema_has_required_query(self) -> None:
        schema = TOOL_REGISTRY["enhanced_search"][0]
        assert "query" in schema.get("required", [])

    def test_list_directory_schema_has_required_path(self) -> None:
        schema = TOOL_REGISTRY["list_directory"][0]
        assert "path" in schema.get("required", [])


# ═════════════════════════════════════════════════════════════
# AC-08-3: 核心逻辑行为等价
# ═════════════════════════════════════════════════════════════

class TestFileRead:
    @pytest.mark.asyncio
    async def test_read_text_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("hello\nworld\n")
        result = await file_read(str(f))
        assert result.success
        assert "hello" in result.output["content"]

    @pytest.mark.asyncio
    async def test_read_nonexistent(self) -> None:
        result = await file_read("/nonexistent/path/xyz.txt")
        assert not result.success
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_read_with_line_range(self, tmp_path: Path) -> None:
        f = tmp_path / "lines.txt"
        f.write_text("L1\nL2\nL3\nL4\nL5\n")
        result = await file_read(str(f), start_line=2, end_line=4)
        assert result.success
        assert "L2" in result.output["content"]
        assert "L4" in result.output["content"]
        assert "L1" not in result.output["content"]

    @pytest.mark.asyncio
    async def test_read_tail(self, tmp_path: Path) -> None:
        f = tmp_path / "tail.txt"
        f.write_text("A\nB\nC\nD\n")
        result = await file_read(str(f), tail=2)
        assert result.success
        content = result.output["content"]
        assert "C" in content and "D" in content
        assert "A" not in content


class TestFileWrite:
    @pytest.mark.asyncio
    async def test_write_new_file(self, tmp_path: Path) -> None:
        f = tmp_path / "new.txt"
        result = await file_write(str(f), action="write", content="hello")
        assert result.success
        assert f.read_text() == "hello"

    @pytest.mark.asyncio
    async def test_append(self, tmp_path: Path) -> None:
        f = tmp_path / "append.txt"
        f.write_text("line1\n")
        result = await file_write(str(f), action="append", content="line2\n", create_backup=False)
        assert result.success
        assert f.read_text() == "line1\nline2\n"

    @pytest.mark.asyncio
    async def test_search_replace(self, tmp_path: Path) -> None:
        f = tmp_path / "replace.txt"
        f.write_text("hello world\nfoo bar")
        result = await file_write(
            str(f), action="search_replace", old_str="world", new_str="Rust", create_backup=False
        )
        assert result.success
        assert "Rust" in f.read_text()

    @pytest.mark.asyncio
    async def test_insert_line(self, tmp_path: Path) -> None:
        f = tmp_path / "insert.txt"
        f.write_text("A\nC\n")
        result = await file_write(str(f), action="insert", content="B", line=1, create_backup=False)
        assert result.success
        lines = f.read_text().split("\n")
        assert lines[1] == "B"

    @pytest.mark.asyncio
    async def test_delete_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "del.txt"
        f.write_text("L1\nL2\nL3\n")
        result = await file_write(str(f), action="delete_lines", start_line=2, end_line=2, create_backup=False)
        assert result.success
        assert "L2" not in f.read_text()


class TestFileWriteDiffOutput:
    """file_write 输出 old_content/new_content（plugin.json ui.chat_card diff 块数据源）。

    契约：每个 action 的成功输出必须带写前全文（old_content，新建为空串）与
    写后全文（new_content）——前端 diff 卡与"完整文件"视图据此渲染。
    """

    @pytest.mark.asyncio
    async def test_write_new_file_diff_output(self, tmp_path: Path) -> None:
        f = tmp_path / "new.txt"
        result = await file_write(str(f), action="write", content="hello")
        assert result.success
        assert result.output["old_content"] == ""
        assert result.output["new_content"] == "hello"

    @pytest.mark.asyncio
    async def test_overwrite_diff_output(self, tmp_path: Path) -> None:
        f = tmp_path / "exist.txt"
        f.write_text("old text")
        result = await file_write(str(f), action="write", content="new text", create_backup=False)
        assert result.success
        assert result.output["old_content"] == "old text"
        assert result.output["new_content"] == "new text"

    @pytest.mark.asyncio
    async def test_append_diff_output(self, tmp_path: Path) -> None:
        f = tmp_path / "append.txt"
        f.write_text("line1\n")
        result = await file_write(str(f), action="append", content="line2\n", create_backup=False)
        assert result.success
        assert result.output["old_content"] == "line1\n"
        assert result.output["new_content"] == "line1\nline2\n"

    @pytest.mark.asyncio
    async def test_search_replace_diff_output(self, tmp_path: Path) -> None:
        f = tmp_path / "replace.txt"
        f.write_text("hello world")
        result = await file_write(
            str(f), action="search_replace", old_str="world", new_str="Rust", create_backup=False
        )
        assert result.success
        assert result.output["old_content"] == "hello world"
        assert result.output["new_content"] == "hello Rust"

    @pytest.mark.asyncio
    async def test_insert_diff_output(self, tmp_path: Path) -> None:
        f = tmp_path / "insert.txt"
        f.write_text("A\nC")
        result = await file_write(str(f), action="insert", content="B", line=1, create_backup=False)
        assert result.success
        assert result.output["old_content"] == "A\nC"
        assert result.output["new_content"] == "A\nB\nC"

    @pytest.mark.asyncio
    async def test_delete_lines_diff_output(self, tmp_path: Path) -> None:
        f = tmp_path / "del.txt"
        f.write_text("L1\nL2\nL3")
        result = await file_write(str(f), action="delete_lines", start_line=2, end_line=2, create_backup=False)
        assert result.success
        assert result.output["old_content"] == "L1\nL2\nL3"
        assert result.output["new_content"] == "L1\nL3"

    def test_output_schema_declares_diff_fields(self) -> None:
        """diff 数据源字段必须在 output_schema 声明（契约锁步，防再断链）。"""
        schema = FILE_WRITE_OUTPUT_SCHEMA
        props = schema["properties"]
        assert "old_content" in props
        assert "new_content" in props


class TestListDirectory:
    @pytest.mark.asyncio
    async def test_list_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x")
        (tmp_path / "b.py").write_text("y")
        result = await list_directory(str(tmp_path))
        assert result.success
        names = [i["name"] for i in result.output["items"]]
        assert "a.txt" in names
        assert "b.py" in names

    @pytest.mark.asyncio
    async def test_list_hidden_excluded(self, tmp_path: Path) -> None:
        (tmp_path / ".hidden").write_text("x")
        (tmp_path / "visible.txt").write_text("y")
        result = await list_directory(str(tmp_path), include_hidden=False)
        names = [i["name"] for i in result.output["items"]]
        assert ".hidden" not in names
        assert "visible.txt" in names

    @pytest.mark.asyncio
    async def test_list_pattern_filter(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.txt").write_text("y")
        result = await list_directory(str(tmp_path), pattern="*.py")
        names = [i["name"] for i in result.output["items"]]
        assert "a.py" in names
        assert "b.txt" not in names

    @pytest.mark.asyncio
    async def test_list_nonexistent(self) -> None:
        result = await list_directory("/nonexistent_dir_xyz")
        assert not result.success


class TestCreateDirectory:
    @pytest.mark.asyncio
    async def test_create_simple(self, tmp_path: Path) -> None:
        d = tmp_path / "newdir"
        result = await create_directory(str(d))
        assert result.success
        assert d.is_dir()

    @pytest.mark.asyncio
    async def test_create_nested(self, tmp_path: Path) -> None:
        d = tmp_path / "a" / "b" / "c"
        result = await create_directory(str(d), parents=True)
        assert result.success
        assert d.is_dir()

    @pytest.mark.asyncio
    async def test_create_exist_ok(self, tmp_path: Path) -> None:
        d = tmp_path / "exists"
        d.mkdir()
        result = await create_directory(str(d))
        assert result.success


class TestCopyFile:
    @pytest.mark.asyncio
    async def test_copy_file(self, tmp_path: Path) -> None:
        src = tmp_path / "src.txt"
        src.write_text("content")
        dst = tmp_path / "dst.txt"
        result = await copy_file(str(src), str(dst))
        assert result.success
        assert dst.read_text() == "content"

    @pytest.mark.asyncio
    async def test_copy_nonexistent(self, tmp_path: Path) -> None:
        result = await copy_file(str(tmp_path / "nope.txt"), str(tmp_path / "dst.txt"))
        assert not result.success

    @pytest.mark.asyncio
    async def test_copy_no_overwrite(self, tmp_path: Path) -> None:
        src = tmp_path / "s.txt"
        src.write_text("new")
        dst = tmp_path / "d.txt"
        dst.write_text("old")
        result = await copy_file(str(src), str(dst), overwrite=False)
        assert not result.success


class TestMoveFile:
    @pytest.mark.asyncio
    async def test_move_file(self, tmp_path: Path) -> None:
        src = tmp_path / "m.txt"
        src.write_text("data")
        dst = tmp_path / "moved.txt"
        result = await move_file(str(src), str(dst))
        assert result.success
        assert not src.exists()
        assert dst.read_text() == "data"

    @pytest.mark.asyncio
    async def test_move_nonexistent(self, tmp_path: Path) -> None:
        result = await move_file(str(tmp_path / "nope"), str(tmp_path / "dst"))
        assert not result.success


class TestDeleteFile:
    @pytest.mark.asyncio
    async def test_delete_file(self, tmp_path: Path) -> None:
        f = tmp_path / "del.txt"
        f.write_text("x")
        result = await delete_file(str(f))
        assert result.success
        assert not f.exists()

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, tmp_path: Path) -> None:
        result = await delete_file(str(tmp_path / "nope.txt"))
        assert not result.success

    @pytest.mark.asyncio
    async def test_delete_recursive(self, tmp_path: Path) -> None:
        d = tmp_path / "deldir"
        d.mkdir()
        (d / "inner.txt").write_text("x")
        result = await delete_file(str(d), recursive=True)
        assert result.success
        assert not d.exists()

    @pytest.mark.asyncio
    async def test_delete_non_empty_without_recursive(self, tmp_path: Path) -> None:
        d = tmp_path / "nonempty"
        d.mkdir()
        (d / "inner.txt").write_text("x")
        result = await delete_file(str(d), recursive=False)
        assert not result.success


class TestEnhancedSearch:
    @pytest.mark.asyncio
    async def test_search_content(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("def hello():\n    pass\n")
        result = await enhanced_search("hello", path=str(tmp_path))
        assert result.success
        # count 在 metadata（ToolResult 设计：output=数据载荷, metadata=附加信息）
        assert result.metadata["count"] >= 1
        assert len(result.output["results"]) >= 1

    @pytest.mark.asyncio
    async def test_search_filename(self, tmp_path: Path) -> None:
        (tmp_path / "test_find.py").write_text("x")
        result = await enhanced_search("test_find", path=str(tmp_path), search_type="filename")
        assert result.success
        assert result.metadata["count"] >= 1
        assert len(result.output["results"]) >= 1

    @pytest.mark.asyncio
    async def test_search_pattern_filter(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("target")
        (tmp_path / "b.txt").write_text("target")
        result = await enhanced_search("target", path=str(tmp_path), file_pattern="*.py")
        assert result.success
        assert all(r["file_path"].endswith(".py") for r in result.output["results"])

    @pytest.mark.asyncio
    async def test_search_nonexistent_path(self) -> None:
        result = await enhanced_search("x", path="/nonexistent_search_path")
        assert not result.success

    @pytest.mark.asyncio
    async def test_search_single_file_path(self, tmp_path: Path) -> None:
        """指向具体文件时应直接搜该文件，而非返回空。

        回归：os.walk 对文件路径产出空迭代，导致文件级搜索恒空。修复后 is_file
        分支会构造单次遍历。
        """
        target = tmp_path / "single.py"
        target.write_text("def findme():\n    return 'hit'\n")

        result = await enhanced_search("findme", path=str(target))
        assert result.success
        matches = result.output["results"]
        assert len(matches) == 1
        assert matches[0]["file_path"] == str(target)
        assert matches[0]["line_number"] == 1

    @pytest.mark.asyncio
    async def test_search_single_file_filename_match(self, tmp_path: Path) -> None:
        """单文件 + filename 搜索：文件名命中时返回该文件。"""
        target = tmp_path / "needle.py"
        target.write_text("x")

        result = await enhanced_search("needle", path=str(target), search_type="filename")
        assert result.success
        assert len(result.output["results"]) == 1
        assert result.output["results"][0]["file_path"] == str(target)

    @pytest.mark.asyncio
    async def test_file_read_string_line_range_via_dispatcher(self, tmp_path: Path) -> None:
        """LLM 把 start_line/end_line 以字符串传入时，分发层应强转为 int。

        回归：直接 file_read(start_line=2) 用原生 int 调用永远过测，掩盖了 MCP
        透传字符串值导致 ``start_line - 1`` 抛 TypeError 的真问题。这里经由
        McpServer._handle_tools_call（与内核→sidecar 同路径）传字符串验证。
        """
        from agentos_plugin_sdk import McpServer

        from agentos_builtin_tools.server import create_plugin

        f = tmp_path / "lines.txt"
        f.write_text("L1\nL2\nL3\nL4\nL5\n")

        plugin = create_plugin()
        server = McpServer(plugin._tools, plugin._resources, plugin._lifecycle_handlers)
        call_result = await server._handle_tools_call({
            "name": "file_read",
            "arguments": {"path": str(f), "start_line": "2", "end_line": "4"},
        })
        assert call_result.is_error is False
        content = json.loads(call_result.content[0].text)
        assert content["success"] is True
        body = content["output"]["content"]
        assert "L2" in body and "L4" in body
        assert "L1" not in body


# ═════════════════════════════════════════════════════════════
# AC-08-4: MCP 服务端封装验证
# ═════════════════════════════════════════════════════════════

class TestMcpServerWrapper:
    def test_create_plugin_has_all_tools(self) -> None:
        from agentos_builtin_tools.server import create_plugin

        plugin = create_plugin()
        assert len(plugin._tools) == 8

    def test_plugin_tool_names(self) -> None:
        from agentos_builtin_tools.server import create_plugin

        plugin = create_plugin()
        names = set(plugin._tools.keys())
        expected = {
            "file_read", "file_write", "enhanced_search",
            "list_directory", "create_directory", "copy_file", "move_file",
            "delete_file",
        }
        assert names == expected

    @pytest.mark.asyncio
    async def test_tool_call_via_server(self, tmp_path: Path) -> None:
        """模拟 MCP tools/call 流程：通过 server 的 McpServer handler 调用工具。"""
        from agentos_plugin_sdk import McpServer

        from agentos_builtin_tools.server import create_plugin

        plugin = create_plugin()
        server = McpServer(
            tools=plugin._tools,
            resources=plugin._resources,
            lifecycle_handlers=plugin._lifecycle_handlers,
        )

        # 模拟 tools/list
        result = await server._on_list_tools(None, None)
        assert len(result.tools) == 8

        # 模拟 tools/call (file_read on temp file)
        f = tmp_path / "mcp_test.txt"
        f.write_text("mcp_content")
        call_result = await server._handle_tools_call({
            "name": "file_read",
            "arguments": {"path": str(f)},
        })
        assert call_result.is_error is False
        import json

        content = json.loads(call_result.content[0].text)
        assert content["success"] is True
        assert "mcp_content" in content["output"]["content"]
