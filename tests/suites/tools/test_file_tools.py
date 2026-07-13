"""
FileReadTool 和 FileWriteTool 全面单元测试

覆盖范围：
- FileReadTool: read（读文件）、list（列目录）操作的成功/失败/边界场景
- FileWriteTool: write、search_replace、insert、delete_lines、append 操作的成功/失败/边界场景
- 参数校验：缺少必填参数、无效参数
- 错误处理：文件不存在、路径类型不匹配、行号越界、模式未找到等
"""

import json

from tools.builtin.file_read import FileReadTool
from tools.builtin.file_write import FileWriteTool


# =====================================================================
# FileReadTool 测试
# =====================================================================


class TestFileReadToolDefinition:
    """FileReadTool 工具定义测试"""

    def test_get_tool_definition_returns_correct_name(self):
        """验证工具定义的名称为 file_read"""
        tool_def = FileReadTool.get_tool_definition()
        assert tool_def.name == "file_read"

    def test_get_tool_definition_actions_include_read_and_list(self):
        """验证工具定义包含 read 和 list 两种操作"""
        tool_def = FileReadTool.get_tool_definition()
        action_enum = tool_def.input_schema["properties"]["action"]["enum"]
        assert "read" in action_enum
        assert "list" in action_enum


class TestFileReadToolInit:
    """FileReadTool 初始化测试"""

    def test_init_with_base_path(self, tmp_path):
        """验证使用 base_path 初始化时正确设置"""
        tool = FileReadTool(base_path=str(tmp_path))
        assert tool.base_path == tmp_path

    def test_init_without_base_path_uses_cwd(self):
        """验证不传 base_path 时使用当前工作目录"""
        from pathlib import Path

        tool = FileReadTool()
        assert tool.base_path == Path.cwd()


class TestFileReadToolRead:
    """FileReadTool read 操作测试"""

    async def test_read_text_file_success(self, tmp_path):
        """测试成功读取文本文件内容"""
        # 准备测试文件
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello World\nSecond line", encoding="utf-8")

        tool = FileReadTool(base_path=str(tmp_path))
        result = await tool.execute({"action": "read", "path": str(test_file)})

        assert result.success is True
        assert result.output["content"] == "Hello World\nSecond line"
        assert result.output["file"] == "test.txt"
        assert result.output["lines"] == 2

    async def test_read_empty_file(self, tmp_path):
        """测试读取空文件"""
        test_file = tmp_path / "empty.txt"
        test_file.write_text("", encoding="utf-8")

        tool = FileReadTool(base_path=str(tmp_path))
        result = await tool.execute({"action": "read", "path": str(test_file)})

        assert result.success is True
        assert result.output["content"] == ""
        assert result.output["file"] == "empty.txt"

    async def test_read_file_with_trailing_newline(self, tmp_path):
        """测试读取末尾有换行的文件，行数统计正确"""
        test_file = tmp_path / "newline.txt"
        test_file.write_text("line1\nline2\n", encoding="utf-8")

        tool = FileReadTool(base_path=str(tmp_path))
        result = await tool.execute({"action": "read", "path": str(test_file)})

        assert result.success is True
        assert result.output["content"] == "line1\nline2\n"
        assert result.output["lines"] == 2

    async def test_read_file_missing_path(self, tmp_path):
        """测试缺少 path 参数时返回 MISSING_PATH 错误"""
        tool = FileReadTool(base_path=str(tmp_path))
        result = await tool.execute({"action": "read"})

        assert result.success is False
        assert result.error_code == "MISSING_PATH"

    async def test_read_file_not_found(self, tmp_path):
        """测试读取不存在的文件时返回 FILE_NOT_FOUND 错误"""
        tool = FileReadTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "read",
            "path": str(tmp_path / "nonexistent.txt"),
        })

        assert result.success is False
        assert result.error_code == "FILE_NOT_FOUND"

    async def test_read_directory_instead_of_file(self, tmp_path):
        """测试读取目录路径时返回 NOT_A_FILE 错误"""
        subdir = tmp_path / "subdir"
        subdir.mkdir()

        tool = FileReadTool(base_path=str(tmp_path))
        result = await tool.execute({"action": "read", "path": str(subdir)})

        assert result.success is False
        assert result.error_code == "NOT_A_FILE"

    async def test_read_file_with_relative_path(self, tmp_path):
        """测试使用相对路径读取文件"""
        test_file = tmp_path / "relative.txt"
        test_file.write_text("relative content", encoding="utf-8")

        tool = FileReadTool(base_path=str(tmp_path))
        result = await tool.execute({"action": "read", "path": "relative.txt"})

        assert result.success is True
        assert result.output["content"] == "relative content"

    async def test_read_json_file_with_fields(self, tmp_path):
        """测试读取 JSON 文件并提取指定字段"""
        test_file = tmp_path / "data.json"
        test_file.write_text(
            json.dumps({"id": 1, "name": "test", "value": 42}),
            encoding="utf-8",
        )

        tool = FileReadTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "read",
            "path": str(test_file),
            "fields": ["id", "name"],
        })

        assert result.success is True
        assert result.output["id"] == 1
        assert result.output["name"] == "test"
        assert "value" not in result.output

    async def test_read_yaml_file_with_fields(self, tmp_path):
        """测试读取 YAML 文件并提取指定字段"""
        import yaml

        test_file = tmp_path / "config.yaml"
        test_file.write_text(
            yaml.dump({"host": "localhost", "port": 8080, "debug": True}),
            encoding="utf-8",
        )

        tool = FileReadTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "read",
            "path": str(test_file),
            "fields": ["host", "port"],
        })

        assert result.success is True
        assert result.output["host"] == "localhost"
        assert result.output["port"] == 8080

    async def test_read_yaml_file_with_nested_fields(self, tmp_path):
        """测试读取 YAML 文件并提取嵌套字段（点号分隔）"""
        import yaml

        test_file = tmp_path / "nested.yaml"
        test_file.write_text(
            yaml.dump({"agent": {"tools": ["read", "write"], "config": {"timeout": 30}}}),
            encoding="utf-8",
        )

        tool = FileReadTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "read",
            "path": str(test_file),
            "fields": ["agent.tools"],
        })

        assert result.success is True
        assert result.output["agent"]["tools"] == ["read", "write"]

    async def test_read_fields_on_unsupported_file_type(self, tmp_path):
        """测试对不支持的文件类型使用 fields 参数返回 FIELDS_NOT_SUPPORTED"""
        test_file = tmp_path / "data.txt"
        test_file.write_text("plain text content", encoding="utf-8")

        tool = FileReadTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "read",
            "path": str(test_file),
            "fields": ["name"],
        })

        assert result.success is False
        assert result.error_code == "FIELDS_NOT_SUPPORTED"

    async def test_read_json_file_with_parse_error(self, tmp_path):
        """测试读取格式错误的 JSON 文件使用 fields 时返回 PARSE_ERROR"""
        test_file = tmp_path / "bad.json"
        test_file.write_text("{invalid json content", encoding="utf-8")

        tool = FileReadTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "read",
            "path": str(test_file),
            "fields": ["id"],
        })

        assert result.success is False
        assert result.error_code == "PARSE_ERROR"

    async def test_read_json_array_with_fields_returns_error(self, tmp_path):
        """测试对 JSON 数组文件使用 fields 参数返回 FIELDS_NOT_SUPPORTED"""
        test_file = tmp_path / "array.json"
        test_file.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

        tool = FileReadTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "read",
            "path": str(test_file),
            "fields": ["id"],
        })

        assert result.success is False
        assert result.error_code == "FIELDS_NOT_SUPPORTED"


class TestFileReadToolList:
    """FileReadTool list 操作测试"""

    async def test_list_directory_success(self, tmp_path):
        """测试成功列出目录内容"""
        # 准备目录结构
        (tmp_path / "file1.txt").write_text("content1", encoding="utf-8")
        (tmp_path / "file2.txt").write_text("content2", encoding="utf-8")
        (tmp_path / "subdir").mkdir()

        tool = FileReadTool(base_path=str(tmp_path))
        result = await tool.execute({"action": "list", "path": str(tmp_path)})

        assert result.success is True
        assert result.output["c"] == 3  # 2 files + 1 dir

    async def test_list_directory_with_default_path(self, tmp_path):
        """测试不传 path 参数时使用 base_path 列出目录"""
        (tmp_path / "default_file.txt").write_text("content", encoding="utf-8")

        tool = FileReadTool(base_path=str(tmp_path))
        result = await tool.execute({"action": "list"})

        assert result.success is True
        assert result.output["c"] >= 1

    async def test_list_directory_contains_header(self, tmp_path):
        """测试返回数据包含正确的表头"""
        (tmp_path / "test.txt").write_text("hello", encoding="utf-8")

        tool = FileReadTool(base_path=str(tmp_path))
        result = await tool.execute({"action": "list", "path": str(tmp_path)})

        assert result.success is True
        assert result.output["h"] == ["dir", "file_name", "file_size"]

    async def test_list_nonexistent_path(self, tmp_path):
        """测试列出不存在的目录时返回 PATH_NOT_FOUND 错误"""
        tool = FileReadTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "list",
            "path": str(tmp_path / "nonexistent_dir"),
        })

        assert result.success is False
        assert result.error_code == "PATH_NOT_FOUND"

    async def test_list_file_instead_of_directory(self, tmp_path):
        """测试对文件路径执行 list 操作时返回 NOT_A_DIRECTORY 错误"""
        test_file = tmp_path / "file.txt"
        test_file.write_text("content", encoding="utf-8")

        tool = FileReadTool(base_path=str(tmp_path))
        result = await tool.execute({"action": "list", "path": str(test_file)})

        assert result.success is False
        assert result.error_code == "NOT_A_DIRECTORY"

    async def test_list_empty_directory(self, tmp_path):
        """测试列出空目录"""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        tool = FileReadTool(base_path=str(tmp_path))
        result = await tool.execute({"action": "list", "path": str(empty_dir)})

        assert result.success is True
        assert result.output["c"] == 0
        assert result.output["d"] == []


class TestFileReadToolInvalidAction:
    """FileReadTool 无效操作测试"""

    async def test_invalid_action_returns_error(self, tmp_path):
        """测试无效的 action 返回 INVALID_ACTION 错误"""
        tool = FileReadTool(base_path=str(tmp_path))
        result = await tool.execute({"action": "delete"})

        assert result.success is False
        assert result.error_code == "INVALID_ACTION"

    async def test_missing_action_returns_error(self, tmp_path):
        """测试缺少 action 参数时返回 INVALID_ACTION 错误"""
        tool = FileReadTool(base_path=str(tmp_path))
        result = await tool.execute({})

        assert result.success is False
        assert result.error_code == "INVALID_ACTION"


class TestFileReadToolWorkspace:
    """FileReadTool workspace 注入参数测试"""

    async def test_workspace_overrides_base_path(self, tmp_path):
        """测试 workspace 参数覆盖 base_path"""
        # 在 tmp_path 下创建文件
        test_file = tmp_path / "workspace_test.txt"
        test_file.write_text("workspace content", encoding="utf-8")

        # 使用不同的 base_path 初始化，但通过 workspace 覆盖
        tool = FileReadTool(base_path=".")
        result = await tool.execute({
            "action": "read",
            "path": "workspace_test.txt",
            "workspace": str(tmp_path),
        })

        assert result.success is True
        assert result.output["content"] == "workspace content"


# =====================================================================
# FileWriteTool 测试
# =====================================================================


class TestFileWriteToolDefinition:
    """FileWriteTool 工具定义测试"""

    def test_get_tool_definition_returns_correct_name(self):
        """验证工具定义的名称为 file_write"""
        tool_def = FileWriteTool.get_tool_definition()
        assert tool_def.name == "file_write"

    def test_get_tool_definition_actions(self):
        """验证工具定义包含所有五种操作"""
        tool_def = FileWriteTool.get_tool_definition()
        action_enum = tool_def.input_schema["properties"]["action"]["enum"]
        expected = {"write", "search_replace", "insert", "delete_lines", "append"}
        assert set(action_enum) == expected


class TestFileWriteToolInit:
    """FileWriteTool 初始化测试"""

    def test_init_with_base_path(self, tmp_path):
        """验证使用 base_path 初始化时正确设置"""
        tool = FileWriteTool(base_path=str(tmp_path))
        assert tool.base_path == tmp_path

    def test_init_without_base_path_uses_cwd(self):
        """验证不传 base_path 时使用当前工作目录"""
        from pathlib import Path

        tool = FileWriteTool()
        assert tool.base_path == Path.cwd()


class TestFileWriteToolWrite:
    """FileWriteTool write 操作测试"""

    async def test_write_new_file_success(self, tmp_path):
        """测试成功写入新文件"""
        test_file = tmp_path / "new_file.txt"

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "write",
            "path": "new_file.txt",
            "content": "Hello World",
            "create_backup": False,
            "workspace": str(tmp_path),
            "project_root": str(tmp_path),
        })

        assert result.success is True
        assert result.output["file"] == "new_file.txt"
        assert test_file.read_text(encoding="utf-8") == "Hello World"

    async def test_write_overwrite_existing_file(self, tmp_path):
        """测试覆盖写入已有文件"""
        test_file = tmp_path / "existing.txt"
        test_file.write_text("old content", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "write",
            "path": str(test_file),
            "content": "new content",
            "create_backup": False,
        })

        assert result.success is True
        assert test_file.read_text(encoding="utf-8") == "new content"

    async def test_write_with_backup(self, tmp_path):
        """测试写入时创建备份文件"""
        test_file = tmp_path / "backup_test.txt"
        test_file.write_text("original content", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "write",
            "path": str(test_file),
            "content": "updated content",
            "create_backup": True,
        })

        assert result.success is True
        assert result.output["backup"] == "backup_test.txt.bak"
        backup_file = tmp_path / "backup_test.txt.bak"
        assert backup_file.exists()
        assert backup_file.read_text(encoding="utf-8") == "original content"

    async def test_write_replace_line_range(self, tmp_path):
        """测试替换指定行范围"""
        test_file = tmp_path / "lines.txt"
        test_file.write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "write",
            "path": str(test_file),
            "content": "replaced",
            "start_line": 2,
            "end_line": 3,
            "create_backup": False,
        })

        assert result.success is True
        content = test_file.read_text(encoding="utf-8")
        assert content == "line1\nreplaced\nline4\n"

    async def test_write_replace_single_line(self, tmp_path):
        """测试替换单行（仅指定 start_line）"""
        test_file = tmp_path / "single.txt"
        test_file.write_text("line1\nline2\nline3\n", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "write",
            "path": str(test_file),
            "content": "replaced",
            "start_line": 2,
            "create_backup": False,
        })

        assert result.success is True
        content = test_file.read_text(encoding="utf-8")
        assert content == "line1\nreplaced\nline3\n"

    async def test_write_missing_path(self, tmp_path):
        """测试缺少 path 参数时返回 MISSING_PATH 错误"""
        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({"action": "write", "content": "test"})

        assert result.success is False
        assert result.error_code == "MISSING_PATH"

    async def test_write_missing_content(self, tmp_path):
        """测试缺少 content 参数时返回 MISSING_CONTENT 错误"""
        test_file = tmp_path / "test.txt"

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "write",
            "path": str(test_file),
        })

        assert result.success is False
        assert result.error_code == "MISSING_CONTENT"

    async def test_write_line_replace_file_not_found(self, tmp_path):
        """测试行替换时文件不存在返回 FILE_NOT_FOUND 错误"""
        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "write",
            "path": str(tmp_path / "nonexistent.txt"),
            "content": "test",
            "start_line": 1,
        })

        assert result.success is False
        assert result.error_code == "FILE_NOT_FOUND"

    async def test_write_line_replace_not_a_file(self, tmp_path):
        """测试行替换时路径是目录返回 NOT_A_FILE 错误"""
        subdir = tmp_path / "subdir"
        subdir.mkdir()

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "write",
            "path": str(subdir),
            "content": "test",
            "start_line": 1,
        })

        assert result.success is False
        assert result.error_code == "NOT_A_FILE"

    async def test_write_start_line_out_of_range(self, tmp_path):
        """测试起始行号越界返回 LINE_OUT_OF_RANGE 错误"""
        test_file = tmp_path / "short.txt"
        test_file.write_text("only one line\n", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "write",
            "path": str(test_file),
            "content": "test",
            "start_line": 5,
            "create_backup": False,
        })

        assert result.success is False
        assert result.error_code == "LINE_OUT_OF_RANGE"

    async def test_write_end_line_out_of_range(self, tmp_path):
        """测试结束行号越界返回 LINE_OUT_OF_RANGE 错误"""
        test_file = tmp_path / "short.txt"
        test_file.write_text("line1\nline2\n", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "write",
            "path": str(test_file),
            "content": "test",
            "start_line": 1,
            "end_line": 10,
            "create_backup": False,
        })

        assert result.success is False
        assert result.error_code == "LINE_OUT_OF_RANGE"

    async def test_write_end_line_less_than_start_line(self, tmp_path):
        """测试结束行号小于起始行号返回 INVALID_LINE_RANGE 错误"""
        test_file = tmp_path / "test.txt"
        test_file.write_text("line1\nline2\nline3\n", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "write",
            "path": str(test_file),
            "content": "test",
            "start_line": 3,
            "end_line": 1,
            "create_backup": False,
        })

        assert result.success is False
        assert result.error_code == "INVALID_LINE_RANGE"

    async def test_write_multiline_content_replaces_range(self, tmp_path):
        """测试多行内容替换行范围"""
        test_file = tmp_path / "multi.txt"
        test_file.write_text("A\nB\nC\nD\nE\n", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "write",
            "path": str(test_file),
            "content": "X\nY\nZ",
            "start_line": 2,
            "end_line": 4,
            "create_backup": False,
        })

        assert result.success is True
        content = test_file.read_text(encoding="utf-8")
        assert content == "A\nX\nY\nZ\nE\n"


class TestFileWriteToolSearchReplace:
    """FileWriteTool search_replace 操作测试"""

    async def test_search_replace_success(self, tmp_path):
        """测试成功搜索并替换文本"""
        test_file = tmp_path / "replace.txt"
        test_file.write_text("Hello World\nHello Python", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "search_replace",
            "path": str(test_file),
            "old_str": "Hello",
            "new_str": "Hi",
            "create_backup": False,
        })

        assert result.success is True
        content = test_file.read_text(encoding="utf-8")
        assert content == "Hi World\nHi Python"
        assert result.output["replacements"] == 2

    async def test_search_replace_with_count(self, tmp_path):
        """测试限制替换次数"""
        test_file = tmp_path / "count.txt"
        test_file.write_text("aaa bbb aaa ccc aaa", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "search_replace",
            "path": str(test_file),
            "old_str": "aaa",
            "new_str": "xxx",
            "count": 2,
            "create_backup": False,
        })

        assert result.success is True
        content = test_file.read_text(encoding="utf-8")
        assert content == "xxx bbb xxx ccc aaa"
        assert result.output["replacements"] == 2

    async def test_search_replace_multiline_pattern(self, tmp_path):
        """测试多行文本的搜索替换"""
        test_file = tmp_path / "multi.txt"
        test_file.write_text("line1\nline2\nline3\nline4", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "search_replace",
            "path": str(test_file),
            "old_str": "line2\nline3",
            "new_str": "replaced",
            "create_backup": False,
        })

        assert result.success is True
        content = test_file.read_text(encoding="utf-8")
        assert content == "line1\nreplaced\nline4"

    async def test_search_replace_missing_path(self, tmp_path):
        """测试缺少 path 参数时返回 MISSING_PATH 错误"""
        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "search_replace",
            "old_str": "test",
            "new_str": "new",
        })

        assert result.success is False
        assert result.error_code == "MISSING_PATH"

    async def test_search_replace_missing_old_str(self, tmp_path):
        """测试缺少 old_str 参数时返回 MISSING_OLD_STR 错误"""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "search_replace",
            "path": str(test_file),
            "new_str": "new",
        })

        assert result.success is False
        assert result.error_code == "MISSING_OLD_STR"

    async def test_search_replace_file_not_found(self, tmp_path):
        """测试文件不存在时返回 FILE_NOT_FOUND 错误"""
        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "search_replace",
            "path": str(tmp_path / "nonexistent.txt"),
            "old_str": "test",
            "new_str": "new",
        })

        assert result.success is False
        assert result.error_code == "FILE_NOT_FOUND"

    async def test_search_replace_pattern_not_found(self, tmp_path):
        """测试搜索文本不存在时返回 PATTERN_NOT_FOUND 错误"""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello World", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "search_replace",
            "path": str(test_file),
            "old_str": "not_found_text",
            "new_str": "new",
            "create_backup": False,
        })

        assert result.success is False
        assert result.error_code == "PATTERN_NOT_FOUND"

    async def test_search_replace_not_a_file(self, tmp_path):
        """测试路径是目录时返回 NOT_A_FILE 错误"""
        subdir = tmp_path / "dir"
        subdir.mkdir()

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "search_replace",
            "path": str(subdir),
            "old_str": "test",
            "new_str": "new",
        })

        assert result.success is False
        assert result.error_code == "NOT_A_FILE"

    async def test_search_replace_with_empty_new_str(self, tmp_path):
        """测试替换为空字符串（等同于删除匹配文本）"""
        test_file = tmp_path / "delete.txt"
        test_file.write_text("Hello World", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "search_replace",
            "path": str(test_file),
            "old_str": "Hello ",
            "new_str": "",
            "create_backup": False,
        })

        assert result.success is True
        assert test_file.read_text(encoding="utf-8") == "World"

    async def test_search_replace_with_backup(self, tmp_path):
        """测试搜索替换时创建备份"""
        test_file = tmp_path / "backup.txt"
        original_content = "Hello World"
        test_file.write_text(original_content, encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "search_replace",
            "path": str(test_file),
            "old_str": "Hello",
            "new_str": "Hi",
            "create_backup": True,
        })

        assert result.success is True
        backup_file = tmp_path / "backup.txt.bak"
        assert backup_file.exists()
        assert backup_file.read_text(encoding="utf-8") == original_content


class TestFileWriteToolInsert:
    """FileWriteTool insert 操作测试"""

    async def test_insert_after_specific_line(self, tmp_path):
        """测试在指定行后插入内容"""
        test_file = tmp_path / "insert.txt"
        test_file.write_text("line1\nline2\nline3\n", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "insert",
            "path": str(test_file),
            "line": 1,
            "content": "inserted",
            "create_backup": False,
        })

        assert result.success is True
        content = test_file.read_text(encoding="utf-8")
        assert content == "line1\ninserted\nline2\nline3\n"
        assert result.output["inserted_at"] == 1

    async def test_insert_at_beginning(self, tmp_path):
        """测试在文件开头插入内容（line=0）"""
        test_file = tmp_path / "begin.txt"
        test_file.write_text("existing\n", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "insert",
            "path": str(test_file),
            "line": 0,
            "content": "first",
            "create_backup": False,
        })

        assert result.success is True
        content = test_file.read_text(encoding="utf-8")
        assert content == "first\nexisting\n"

    async def test_insert_multiline_content(self, tmp_path):
        """测试插入多行内容"""
        test_file = tmp_path / "multi.txt"
        test_file.write_text("line1\nline2\n", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "insert",
            "path": str(test_file),
            "line": 1,
            "content": "new_a\nnew_b\nnew_c",
            "create_backup": False,
        })

        assert result.success is True
        content = test_file.read_text(encoding="utf-8")
        assert content == "line1\nnew_a\nnew_b\nnew_c\nline2\n"
        assert result.output["lines"] == 3

    async def test_insert_missing_path(self, tmp_path):
        """测试缺少 path 参数时返回 MISSING_PATH 错误"""
        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "insert",
            "line": 1,
            "content": "test",
        })

        assert result.success is False
        assert result.error_code == "MISSING_PATH"

    async def test_insert_missing_line(self, tmp_path):
        """测试缺少 line 参数时返回 MISSING_LINE 错误"""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "insert",
            "path": str(test_file),
            "content": "test",
        })

        assert result.success is False
        assert result.error_code == "MISSING_LINE"

    async def test_insert_missing_content(self, tmp_path):
        """测试缺少 content 参数时返回 MISSING_CONTENT 错误"""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "insert",
            "path": str(test_file),
            "line": 1,
        })

        assert result.success is False
        assert result.error_code == "MISSING_CONTENT"

    async def test_insert_file_not_found(self, tmp_path):
        """测试文件不存在时返回 FILE_NOT_FOUND 错误"""
        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "insert",
            "path": str(tmp_path / "nonexistent.txt"),
            "line": 1,
            "content": "test",
        })

        assert result.success is False
        assert result.error_code == "FILE_NOT_FOUND"

    async def test_insert_line_out_of_range(self, tmp_path):
        """测试行号越界时返回 LINE_OUT_OF_RANGE 错误"""
        test_file = tmp_path / "short.txt"
        test_file.write_text("only one line\n", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "insert",
            "path": str(test_file),
            "line": 5,
            "content": "test",
            "create_backup": False,
        })

        assert result.success is False
        assert result.error_code == "LINE_OUT_OF_RANGE"

    async def test_insert_negative_line_out_of_range(self, tmp_path):
        """测试负行号返回 LINE_OUT_OF_RANGE 错误"""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content\n", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "insert",
            "path": str(test_file),
            "line": -1,
            "content": "test",
        })

        assert result.success is False
        assert result.error_code == "LINE_OUT_OF_RANGE"

    async def test_insert_not_a_file(self, tmp_path):
        """测试路径是目录时返回 NOT_A_FILE 错误"""
        subdir = tmp_path / "dir"
        subdir.mkdir()

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "insert",
            "path": str(subdir),
            "line": 1,
            "content": "test",
        })

        assert result.success is False
        assert result.error_code == "NOT_A_FILE"


class TestFileWriteToolDeleteLines:
    """FileWriteTool delete_lines 操作测试"""

    async def test_delete_lines_success(self, tmp_path):
        """测试成功删除指定行范围"""
        test_file = tmp_path / "delete.txt"
        test_file.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "delete_lines",
            "path": str(test_file),
            "start_line": 2,
            "end_line": 3,
            "create_backup": False,
        })

        assert result.success is True
        content = test_file.read_text(encoding="utf-8")
        assert content == "line1\nline4\nline5\n"
        assert result.output["count"] == 2
        assert result.output["deleted_lines"] == "2-3"

    async def test_delete_single_line(self, tmp_path):
        """测试删除单行（start_line == end_line）"""
        test_file = tmp_path / "single.txt"
        test_file.write_text("A\nB\nC\n", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "delete_lines",
            "path": str(test_file),
            "start_line": 2,
            "end_line": 2,
            "create_backup": False,
        })

        assert result.success is True
        content = test_file.read_text(encoding="utf-8")
        assert content == "A\nC\n"

    async def test_delete_all_lines(self, tmp_path):
        """测试删除所有行"""
        test_file = tmp_path / "all.txt"
        test_file.write_text("A\nB\nC\n", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "delete_lines",
            "path": str(test_file),
            "start_line": 1,
            "end_line": 3,
            "create_backup": False,
        })

        assert result.success is True
        content = test_file.read_text(encoding="utf-8")
        assert content == "\n"  # 保留末尾换行符

    async def test_delete_lines_missing_path(self, tmp_path):
        """测试缺少 path 参数时返回 MISSING_PATH 错误"""
        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "delete_lines",
            "start_line": 1,
            "end_line": 2,
        })

        assert result.success is False
        assert result.error_code == "MISSING_PATH"

    async def test_delete_lines_missing_start_line(self, tmp_path):
        """测试缺少 start_line 参数时返回 MISSING_START_LINE 错误"""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content\n", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "delete_lines",
            "path": str(test_file),
            "end_line": 2,
        })

        assert result.success is False
        assert result.error_code == "MISSING_START_LINE"

    async def test_delete_lines_missing_end_line(self, tmp_path):
        """测试缺少 end_line 参数时返回 MISSING_END_LINE 错误"""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content\n", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "delete_lines",
            "path": str(test_file),
            "start_line": 1,
        })

        assert result.success is False
        assert result.error_code == "MISSING_END_LINE"

    async def test_delete_lines_file_not_found(self, tmp_path):
        """测试文件不存在时返回 FILE_NOT_FOUND 错误"""
        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "delete_lines",
            "path": str(tmp_path / "nonexistent.txt"),
            "start_line": 1,
            "end_line": 2,
        })

        assert result.success is False
        assert result.error_code == "FILE_NOT_FOUND"

    async def test_delete_lines_start_out_of_range(self, tmp_path):
        """测试起始行号越界返回 LINE_OUT_OF_RANGE 错误"""
        test_file = tmp_path / "short.txt"
        test_file.write_text("one line\n", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "delete_lines",
            "path": str(test_file),
            "start_line": 5,
            "end_line": 6,
            "create_backup": False,
        })

        assert result.success is False
        assert result.error_code == "LINE_OUT_OF_RANGE"

    async def test_delete_lines_end_less_than_start(self, tmp_path):
        """测试结束行号小于起始行号返回 INVALID_LINE_RANGE 错误"""
        test_file = tmp_path / "test.txt"
        test_file.write_text("A\nB\nC\n", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "delete_lines",
            "path": str(test_file),
            "start_line": 3,
            "end_line": 1,
            "create_backup": False,
        })

        assert result.success is False
        assert result.error_code == "INVALID_LINE_RANGE"

    async def test_delete_lines_not_a_file(self, tmp_path):
        """测试路径是目录时返回 NOT_A_FILE 错误"""
        subdir = tmp_path / "dir"
        subdir.mkdir()

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "delete_lines",
            "path": str(subdir),
            "start_line": 1,
            "end_line": 2,
        })

        assert result.success is False
        assert result.error_code == "NOT_A_FILE"


class TestFileWriteToolAppend:
    """FileWriteTool append 操作测试"""

    async def test_append_to_existing_file(self, tmp_path):
        """测试成功追加内容到已有文件"""
        test_file = tmp_path / "append.txt"
        test_file.write_text("line1\n", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "append",
            "path": str(test_file),
            "content": "line2\nline3",
            "create_backup": False,
        })

        assert result.success is True
        content = test_file.read_text(encoding="utf-8")
        assert content == "line1\nline2\nline3"

    async def test_append_to_file_without_trailing_newline(self, tmp_path):
        """测试追加到没有末尾换行的文件时自动添加换行分隔"""
        test_file = tmp_path / "no_newline.txt"
        test_file.write_text("line1", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "append",
            "path": str(test_file),
            "content": "line2",
            "create_backup": False,
        })

        assert result.success is True
        content = test_file.read_text(encoding="utf-8")
        assert content == "line1\nline2"

    async def test_append_to_nonexistent_file(self, tmp_path):
        """测试追加到不存在的文件时创建新文件"""
        test_file = tmp_path / "new_append.txt"

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "append",
            "path": str(test_file),
            "content": "first content",
            "create_backup": False,
        })

        assert result.success is True
        assert test_file.exists()
        assert test_file.read_text(encoding="utf-8") == "first content"

    async def test_append_missing_path(self, tmp_path):
        """测试缺少 path 参数时返回 MISSING_PATH 错误"""
        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "append",
            "content": "test",
        })

        assert result.success is False
        assert result.error_code == "MISSING_PATH"

    async def test_append_missing_content(self, tmp_path):
        """测试缺少 content 参数时返回 MISSING_CONTENT 错误"""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content", encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "append",
            "path": str(test_file),
        })

        assert result.success is False
        assert result.error_code == "MISSING_CONTENT"

    async def test_append_to_directory_returns_error(self, tmp_path):
        """测试追加到目录路径时返回 NOT_A_FILE 错误"""
        subdir = tmp_path / "dir"
        subdir.mkdir()

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "append",
            "path": str(subdir),
            "content": "test",
        })

        assert result.success is False
        assert result.error_code == "NOT_A_FILE"

    async def test_append_with_backup(self, tmp_path):
        """测试追加时创建备份"""
        test_file = tmp_path / "backup.txt"
        original_content = "original"
        test_file.write_text(original_content, encoding="utf-8")

        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "append",
            "path": str(test_file),
            "content": "appended",
            "create_backup": True,
        })

        assert result.success is True
        backup_file = tmp_path / "backup.txt.bak"
        assert backup_file.exists()
        assert backup_file.read_text(encoding="utf-8") == original_content


class TestFileWriteToolInvalidAction:
    """FileWriteTool action 校验测试。

    区分两种错误：缺失 action → MISSING_ACTION（明确告知必填 + 合法值）；
    非法 action → INVALID_ACTION（告知传入值 + 合法枚举）。
    """

    async def test_invalid_action_returns_error(self, tmp_path):
        """非法 action 值返回 INVALID_ACTION，错误信息含合法枚举。"""
        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "invalid",
            "path": str(tmp_path / "test.txt"),
        })

        assert result.success is False
        assert result.error_code == "INVALID_ACTION"
        assert "invalid" in result.error
        assert "write" in result.error

    async def test_missing_action_returns_missing_action_error(self, tmp_path):
        """缺失 action 参数返回 MISSING_ACTION（非含糊的 INVALID_ACTION）。"""
        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "path": str(tmp_path / "test.txt"),
        })

        assert result.success is False
        assert result.error_code == "MISSING_ACTION"
        assert "action" in result.error

    async def test_none_action_returns_missing_action_error(self, tmp_path):
        """action 显式为 None 同样返回 MISSING_ACTION。"""
        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": None,
            "path": str(tmp_path / "test.txt"),
        })

        assert result.success is False
        assert result.error_code == "MISSING_ACTION"


class TestFileWriteToolWorkspace:
    """FileWriteTool workspace 注入参数测试"""

    async def test_workspace_overrides_base_path(self, tmp_path):
        """测试 workspace 参数覆盖 base_path"""
        test_file = tmp_path / "workspace_write.txt"

        tool = FileWriteTool(base_path=".")
        result = await tool.execute({
            "action": "write",
            "path": "workspace_write.txt",
            "content": "workspace content",
            "workspace": str(tmp_path),
            "create_backup": False,
        })

        assert result.success is True
        assert test_file.read_text(encoding="utf-8") == "workspace content"


class TestFileWriteToolRelativePath:
    """FileWriteTool 相对路径测试"""

    async def test_write_with_relative_path(self, tmp_path):
        """测试使用相对路径写入文件"""
        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "write",
            "path": "relative.txt",
            "content": "relative write",
            "create_backup": False,
            "workspace": str(tmp_path),
            "project_root": str(tmp_path),
        })

        assert result.success is True
        assert (tmp_path / "relative.txt").read_text(encoding="utf-8") == "relative write"

    async def test_write_to_subdirectory(self, tmp_path):
        """测试写入到子目录（自动创建目录）"""
        tool = FileWriteTool(base_path=str(tmp_path))
        result = await tool.execute({
            "action": "write",
            "path": str(tmp_path / "sub" / "deep" / "file.txt"),
            "content": "deep write",
            "create_backup": False,
        })

        assert result.success is True
        assert (tmp_path / "sub" / "deep" / "file.txt").read_text(encoding="utf-8") == "deep write"
