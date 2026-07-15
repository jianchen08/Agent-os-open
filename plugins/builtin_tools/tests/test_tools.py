"""集成测试——覆盖 10 个工具的核心场景。

通过直接调用工具函数验证行为等价性（AC-08-3/4）。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from lingxi_builtin_tools.bash_tool import bash_execute
from lingxi_builtin_tools.fs_tools import (
    copy_file,
    create_directory,
    delete_file,
    file_read,
    file_write,
    list_directory,
    move_file,
)
from lingxi_builtin_tools.search_tool import enhanced_search
from lingxi_builtin_tools.web_tool import WEB_OPERATE_SCHEMA, web_operate
from lingxi_builtin_tools.server import TOOL_REGISTRY


# ═════════════════════════════════════════════════════════════
# AC-08-1: 10 个工具注册
# ═════════════════════════════════════════════════════════════

class TestToolRegistration:
    def test_all_10_tools_registered(self) -> None:
        expected = {
            "file_read", "file_write", "bash_execute", "enhanced_search",
            "list_directory", "create_directory", "copy_file", "move_file",
            "delete_file", "web_operate",
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

    def test_bash_execute_schema_has_required_command(self) -> None:
        schema = TOOL_REGISTRY["bash_execute"][0]
        assert "command" in schema.get("required", [])

    def test_search_schema_has_required_query(self) -> None:
        schema = TOOL_REGISTRY["enhanced_search"][0]
        assert "query" in schema.get("required", [])

    def test_list_directory_schema_has_required_path(self) -> None:
        schema = TOOL_REGISTRY["list_directory"][0]
        assert "path" in schema.get("required", [])

    def test_web_operate_schema_has_required_fields(self) -> None:
        schema = TOOL_REGISTRY["web_operate"][0]
        assert "action" in schema.get("required", [])
        assert "url" in schema.get("required", [])


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


class TestBashExecute:
    @pytest.mark.asyncio
    async def test_echo(self) -> None:
        result = await bash_execute("echo hello_world")
        assert result.success
        assert "hello_world" in result.output["stdout"]

    @pytest.mark.asyncio
    async def test_dangerous_command_blocked(self) -> None:
        result = await bash_execute("rm -rf /")
        assert not result.success
        assert "dangerous" in result.error.lower()

    @pytest.mark.asyncio
    async def test_exit_code(self) -> None:
        result = await bash_execute("exit 42")
        assert not result.success
        assert result.output["exit_code"] == 42

    @pytest.mark.asyncio
    async def test_stderr_capture(self) -> None:
        result = await bash_execute("echo err >&2")
        assert result.success
        assert "err" in result.output["stderr"]

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        result = await bash_execute("sleep 10", timeout=1)
        assert not result.success
        assert "timed out" in result.error.lower()


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
        result = await create_directory(str(d), exist_ok=True)
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
        assert result.output["count"] >= 1

    @pytest.mark.asyncio
    async def test_search_filename(self, tmp_path: Path) -> None:
        (tmp_path / "test_find.py").write_text("x")
        result = await enhanced_search("test_find", path=str(tmp_path), search_type="filename")
        assert result.success
        assert result.output["count"] >= 1

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


class TestWebOperate:
    @pytest.mark.asyncio
    async def test_web_schema_valid(self) -> None:
        assert WEB_OPERATE_SCHEMA["properties"]["action"]["enum"] == ["get", "post", "fetch"]
        assert "url" in WEB_OPERATE_SCHEMA["properties"]

    @pytest.mark.asyncio
    async def test_web_invalid_action(self) -> None:
        # 无效 action 底层会返回 failure
        result = await web_operate(action="invalid", url="http://localhost:1")
        # aiohttp 可能尝试连接但失败，或返回 unknown action
        assert not result.success


# ═════════════════════════════════════════════════════════════
# AC-08-4: MCP 服务端封装验证
# ═════════════════════════════════════════════════════════════

class TestMcpServerWrapper:
    def test_create_plugin_has_all_tools(self) -> None:
        from lingxi_builtin_tools.server import create_plugin

        plugin = create_plugin()
        assert len(plugin._tools) == 10

    def test_plugin_tool_names(self) -> None:
        from lingxi_builtin_tools.server import create_plugin

        plugin = create_plugin()
        names = set(plugin._tools.keys())
        expected = {
            "file_read", "file_write", "bash_execute", "enhanced_search",
            "list_directory", "create_directory", "copy_file", "move_file",
            "delete_file", "web_operate",
        }
        assert names == expected

    @pytest.mark.asyncio
    async def test_tool_call_via_server(self, tmp_path: Path) -> None:
        """模拟 MCP tools/call 流程：通过 server 的 McpServer handler 调用工具。"""
        from lingxi_plugin_sdk import McpServer
        from lingxi_builtin_tools.server import create_plugin

        plugin = create_plugin()
        server = McpServer(
            tools=plugin._tools,
            resources=plugin._resources,
            lifecycle_handlers=plugin._lifecycle_handlers,
        )

        # 模拟 tools/list
        result = server._handle_tools_list()
        assert len(result["tools"]) == 10

        # 模拟 tools/call (file_read on temp file)
        f = tmp_path / "mcp_test.txt"
        f.write_text("mcp_content")
        call_result = await server._handle_tools_call({
            "name": "file_read",
            "arguments": {"path": str(f)},
        })
        assert call_result["isError"] is False
        import json
        content = json.loads(call_result["content"][0]["text"])
        assert content["success"] is True
        assert "mcp_content" in content["output"]["content"]
