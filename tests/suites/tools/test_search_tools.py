"""EnhancedSearchTool 全面单元测试。

覆盖范围：
- 文本搜索（search_type=text）正常场景
- 文件名搜索（search_type=filename）正常场景
- 缺少 query 参数的错误处理
- 不支持的 search_type 错误处理
- case_sensitive 大小写敏感参数
- file_pattern 文件过滤参数
- max_results 结果数量限制（限制 match 行数，context 行不计入）
- 搜索路径不存在的错误处理
- use_regex 正则表达式参数
- 回归测试：context 截断 / max_depth / 字面量换行 / match_count 字段

测试走真实 ripgrep 路径（不再 mock）。rg 不可用的环境会自动 skip，
避免假失败。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tools.builtin.enhanced_search import EnhancedSearchTool


# ═══════════════════════════════════════════════════════════
# Fixture
# ═══════════════════════════════════════════════════════════


def _ripgrep_available() -> bool:
    """检测系统是否安装 ripgrep。"""
    return shutil.which("rg") is not None


@pytest.fixture
def search_tool(tmp_path: Path) -> EnhancedSearchTool:
    """创建走真实 ripgrep 路径的 EnhancedSearchTool 实例。

    不再 mock _check_ripgrep。若环境未安装 ripgrep，整个用例 skip，
    而不是退化到旧 Python 回退路径（该路径已移除）。

    Args:
        tmp_path: pytest 内置临时目录 fixture

    Returns:
        配置好的 EnhancedSearchTool 实例，base_path 指向 tmp_path
    """
    if not _ripgrep_available():
        pytest.skip("ripgrep 未安装，enhanced_search 测试需要真实 rg")
    return EnhancedSearchTool(base_path=str(tmp_path))


@pytest.fixture
def sample_files(tmp_path: Path) -> Path:
    """在临时目录中创建用于搜索测试的示例文件。

    目录结构：
        tmp_path/
        ├── main.py        （包含 hello world 和 search_test_marker）
        ├── utils.py       （包含 helper 函数和 search_test_marker）
        ├── config.json    （JSON 配置文件）
        ├── readme.txt     （普通文本文件）
        └── sub/
            └── module.py  （子目录中的 Python 文件，包含 deep_search_target）

    Args:
        tmp_path: pytest 内置临时目录 fixture

    Returns:
        包含示例文件的临时目录路径
    """
    # main.py
    (tmp_path / "main.py").write_text(
        "# main file\n"
        "def hello():\n"
        '    print("Hello World")\n'
        "# search_test_marker\n",
        encoding="utf-8",
    )

    # utils.py
    (tmp_path / "utils.py").write_text(
        "# utils file\n"
        "def helper():\n"
        "    return 42\n"
        "# search_test_marker\n",
        encoding="utf-8",
    )

    # config.json
    (tmp_path / "config.json").write_text(
        '{"name": "test_project", "version": "1.0.0"}',
        encoding="utf-8",
    )

    # readme.txt
    (tmp_path / "readme.txt").write_text(
        "This is a readme file.\n"
        "SEARCH_TEST_MARKER in uppercase.\n",
        encoding="utf-8",
    )

    # sub/module.py
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "module.py").write_text(
        "# submodule\n"
        "def deep_func():\n"
        "    # deep_search_target\n"
        "    pass\n",
        encoding="utf-8",
    )

    return tmp_path


# ═══════════════════════════════════════════════════════════
# 文本搜索测试
# ═══════════════════════════════════════════════════════════


class TestTextSearch:
    """文本搜索（search_type=text）相关测试。"""

    async def test_text_search_basic_match(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试基本文本搜索能正确匹配并返回结果。

        验证：
        - 搜索成功
        - output 包含 file_paths、line_numbers、contents
        - 结果计数正确
        - 匹配的文件路径包含在结果中
        """
        result = await search_tool.execute({
            "query": "search_test_marker",
            "search_type": "text",
            "path": str(sample_files),
        })

        assert result.success is True
        data = result.output
        # 默认不区分大小写，"search_test_marker" 同时匹配 .py 文件中的小写和
        # readme.txt 中的 "SEARCH_TEST_MARKER"，因此 c >= 2
        assert data["c"] >= 2
        assert len(data["d"]) >= 2
        # 确认匹配的文件包含 main.py 和 utils.py
        file_paths_str = " ".join(row[0] for row in data["d"])
        assert "main.py" in file_paths_str
        assert "utils.py" in file_paths_str

    async def test_text_search_returns_line_numbers(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试文本搜索返回正确的行号。

        验证：
        - line_numbers 列表非空
        - 行号均为正整数
        """
        result = await search_tool.execute({
            "query": "def hello",
            "search_type": "text",
            "path": str(sample_files),
        })

        assert result.success is True
        data = result.output
        line_numbers = [row[1] for row in data["d"]]
        assert len(line_numbers) > 0
        for ln in line_numbers:
            assert isinstance(ln, int)
            assert ln > 0

    async def test_text_search_returns_contents(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试文本搜索返回匹配行的内容。

        验证：
        - contents 列表非空
        - 匹配行内容包含搜索关键词
        """
        result = await search_tool.execute({
            "query": "Hello World",
            "search_type": "text",
            "path": str(sample_files),
        })

        assert result.success is True
        data = result.output
        contents = [row[2] for row in data["d"]]
        assert len(contents) > 0
        matched = " ".join(contents)
        assert "Hello World" in matched

    async def test_text_search_no_match(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试搜索不存在的关键词时返回空结果。

        验证：
        - 搜索仍然成功（success=True）
        - count 为 0
        - file_paths、line_numbers、contents 均为空列表
        """
        result = await search_tool.execute({
            "query": "zzz_nonexistent_pattern_xyz",
            "search_type": "text",
            "path": str(sample_files),
        })

        assert result.success is True
        data = result.output
        assert data["c"] == 0
        assert data["d"] == []

    async def test_text_search_recursive(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试文本搜索能递归搜索子目录。

        验证：
        - 子目录 sub/ 中的匹配内容也能被找到
        """
        result = await search_tool.execute({
            "query": "deep_search_target",
            "search_type": "text",
            "path": str(sample_files),
            "context_lines": 0,
        })

        assert result.success is True
        data = result.output
        assert data["c"] == 1
        assert "module.py" in data["d"][0][0]


# ═══════════════════════════════════════════════════════════
# 文件名搜索测试
# ═══════════════════════════════════════════════════════════


class TestFilenameSearch:
    """文件名搜索（search_type=filename）相关测试。"""

    async def test_filename_search_basic(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试基本文件名搜索能找到匹配的文件。

        验证：
        - 搜索成功
        - output 包含 h（表头）、d（数据行）、c（计数）
        - 找到的文件数量大于 0
        """
        result = await search_tool.execute({
            "query": "main",
            "search_type": "filename",
            "path": str(sample_files),
        })

        assert result.success is True
        data = result.output
        assert data["c"] >= 1
        # 表头应为 file_name、file_size、file_path
        assert data["h"] == ["file_name", "file_size", "file_path"]
        # 数据行中应包含 main.py
        file_names = [row[0] for row in data["d"]]
        assert any("main" in name for name in file_names)

    async def test_filename_search_with_extension(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试按扩展名搜索文件。

        验证：
        - 搜索 ".json" 能找到 config.json
        - 搜索结果计数正确
        """
        result = await search_tool.execute({
            "query": ".json",
            "search_type": "filename",
            "path": str(sample_files),
        })

        assert result.success is True
        data = result.output
        assert data["c"] == 1
        file_names = [row[0] for row in data["d"]]
        assert "config.json" in file_names

    async def test_filename_search_no_match(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试搜索不存在的文件名时返回空结果。

        验证：
        - 搜索仍然成功
        - c（计数）为 0
        - d（数据行）为空列表
        """
        result = await search_tool.execute({
            "query": "zzz_nonexistent_file",
            "search_type": "filename",
            "path": str(sample_files),
        })

        assert result.success is True
        data = result.output
        assert data["c"] == 0
        assert data["d"] == []

    async def test_filename_search_recursive(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试文件名搜索能递归搜索子目录。

        验证：
        - 搜索 "module" 能找到 sub/module.py
        """
        result = await search_tool.execute({
            "query": "module",
            "search_type": "filename",
            "path": str(sample_files),
        })

        assert result.success is True
        data = result.output
        assert data["c"] >= 1
        file_paths = [row[2] for row in data["d"]]
        assert any("module.py" in fp for fp in file_paths)

    async def test_filename_search_result_format(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试文件名搜索结果的数据格式正确。

        验证：
        - 每行数据包含 3 个元素：file_name、file_size、file_path
        - file_size 是字符串（由 format_size 格式化）
        """
        result = await search_tool.execute({
            "query": ".py",
            "search_type": "filename",
            "path": str(sample_files),
        })

        assert result.success is True
        data = result.output
        for row in data["d"]:
            assert len(row) == 3
            assert isinstance(row[0], str)  # file_name
            assert isinstance(row[1], str)  # file_size（格式化后的字符串）
            assert isinstance(row[2], str)  # file_path


# ═══════════════════════════════════════════════════════════
# 错误处理测试
# ═══════════════════════════════════════════════════════════


class TestErrorHandling:
    """错误处理相关测试。"""

    async def test_missing_query_parameter(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试缺少必填 query 参数时返回错误。

        验证：
        - success 为 False
        - error 包含提示信息
        - error_code 为 MISSING_QUERY
        """
        result = await search_tool.execute({
            "search_type": "text",
            "path": str(sample_files),
        })

        assert result.success is False
        assert result.error is not None
        assert "搜索查询不能为空" in result.error
        assert result.error_code == "MISSING_QUERY"

    async def test_empty_query_parameter(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试 query 参数为空字符串时返回错误。

        验证：
        - success 为 False
        - error_code 为 MISSING_QUERY
        """
        result = await search_tool.execute({
            "query": "",
            "search_type": "text",
            "path": str(sample_files),
        })

        assert result.success is False
        assert result.error_code == "MISSING_QUERY"

    async def test_invalid_search_type(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试不支持的 search_type 返回错误。

        验证：
        - success 为 False
        - error 包含不支持的搜索类型信息
        - error_code 为 INVALID_SEARCH_TYPE
        """
        result = await search_tool.execute({
            "query": "test",
            "search_type": "binary",
            "path": str(sample_files),
        })

        assert result.success is False
        assert result.error is not None
        assert "binary" in result.error
        assert result.error_code == "INVALID_SEARCH_TYPE"

    async def test_text_search_nonexistent_path(
        self, search_tool: EnhancedSearchTool
    ) -> None:
        """测试文本搜索时路径不存在返回错误。

        验证：
        - success 为 False
        - error 包含路径不存在信息
        - error_code 为 PATH_NOT_FOUND
        """
        result = await search_tool.execute({
            "query": "test",
            "search_type": "text",
            "path": "/nonexistent/path/that/does/not/exist",
        })

        assert result.success is False
        assert result.error is not None
        assert "不存在" in result.error
        assert result.error_code == "PATH_NOT_FOUND"

    async def test_filename_search_nonexistent_path(
        self, search_tool: EnhancedSearchTool
    ) -> None:
        """测试文件名搜索时路径不存在返回错误。

        验证：
        - success 为 False
        - error_code 为 PATH_NOT_FOUND
        """
        result = await search_tool.execute({
            "query": "test",
            "search_type": "filename",
            "path": "/nonexistent/path/that/does/not/exist",
        })

        assert result.success is False
        assert result.error_code == "PATH_NOT_FOUND"


# ═══════════════════════════════════════════════════════════
# 参数功能测试
# ═══════════════════════════════════════════════════════════


class TestCaseSensitive:
    """case_sensitive 大小写敏感参数测试。"""

    async def test_case_insensitive_by_default(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试默认不区分大小写，小写查询能匹配大写内容。

        验证：
        - 用小写 "hello world" 能匹配 "Hello World"
        - 搜索成功且有结果
        """
        result = await search_tool.execute({
            "query": "hello world",
            "search_type": "text",
            "path": str(sample_files),
        })

        assert result.success is True
        assert result.output["c"] >= 1

    async def test_case_sensitive_text_search(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试区分大小写的文本搜索。

        验证：
        - case_sensitive=True 时，小写查询不匹配大写内容
        - 大写 "SEARCH_TEST_MARKER" 仅在 readme.txt 中匹配
        """
        result_lower = await search_tool.execute({
            "query": "search_test_marker",
            "search_type": "text",
            "path": str(sample_files),
            "case_sensitive": True,
            "context_lines": 0,
        })

        result_upper = await search_tool.execute({
            "query": "SEARCH_TEST_MARKER",
            "search_type": "text",
            "path": str(sample_files),
            "case_sensitive": True,
            "context_lines": 0,
        })

        assert result_upper.output["c"] == 1
        readme_found = any(
            "readme" in row[0]
            for row in result_lower.output["d"]
        )
        assert readme_found is False

    async def test_case_insensitive_filename_search(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试文件名搜索默认不区分大小写。"""
        result = await search_tool.execute({
            "query": "MAIN",
            "search_type": "filename",
            "path": str(sample_files),
        })

        assert result.success is True
        assert result.output["c"] >= 1

    async def test_case_sensitive_filename_search(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试文件名搜索区分大小写。

        验证：
        - case_sensitive=True 时，大写 "MAIN" 不匹配小写 "main.py"
        """
        result = await search_tool.execute({
            "query": "MAIN",
            "search_type": "filename",
            "path": str(sample_files),
            "case_sensitive": True,
        })

        assert result.success is True
        # 区分大小写时，大写 MAIN 不应匹配 main.py
        file_names = [row[0] for row in result.output["d"]]
        assert all("MAIN" not in name for name in file_names)


class TestFilePattern:
    """file_pattern 文件过滤参数测试。"""

    async def test_file_pattern_python_only(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试 file_pattern 限制只搜索 Python 文件。

        验证：
        - 搜索 "test" 时仅返回 .py 文件中的结果
        - 不包含 .json 或 .txt 文件
        """
        result = await search_tool.execute({
            "query": "test",
            "search_type": "text",
            "path": str(sample_files),
            "file_pattern": "*.py",
        })

        assert result.success is True
        for row in result.output["d"]:
            assert row[0].endswith(".py"), f"非 Python 文件出现在结果中: {row[0]}"

    async def test_file_pattern_json_only(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试 file_pattern 限制只搜索 JSON 文件。

        验证：
        - 搜索 "test" 时仅返回 .json 文件中的结果
        """
        result = await search_tool.execute({
            "query": "test",
            "search_type": "text",
            "path": str(sample_files),
            "file_pattern": "*.json",
        })

        assert result.success is True
        for row in result.output["d"]:
            assert row[0].endswith(".json")

    async def test_file_pattern_default_all_files(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试默认 file_pattern 搜索所有文件类型。

        验证：
        - 不指定 file_pattern 时搜索所有文件
        - 搜索 "test_project" 应在 config.json 中找到
        """
        result = await search_tool.execute({
            "query": "test_project",
            "search_type": "text",
            "path": str(sample_files),
        })

        assert result.success is True
        assert result.output["c"] >= 1


class TestMaxResults:
    """max_results 结果数量限制测试。"""

    async def test_max_results_limits_output(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试 max_results 限制 match 行数（context 行不计入）。

        准备：写入 5 行匹配内容，max_results=1、context_lines=0（无上下文）。
        验证：
        - match_count 不超过 max_results
        - 无 context 时 c == match_count
        """
        # 创建一个包含多行匹配内容的文件
        (sample_files / "multi_match.py").write_text(
            "marker_line_1\n"
            "marker_line_2\n"
            "marker_line_3\n"
            "marker_line_4\n"
            "marker_line_5\n",
            encoding="utf-8",
        )

        result = await search_tool.execute({
            "query": "marker_line",
            "search_type": "text",
            "path": str(sample_files),
            "max_results": 1,
            "context_lines": 0,
        })

        assert result.success is True
        # max_results 限制的是 match 行数
        assert result.output["match_count"] == 1
        # context_lines=0 时无上下文行，总行数 == match 数
        assert result.output["c"] == 1

    async def test_max_results_filename_search(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试 max_results 限制文件名搜索结果数量。

        验证：
        - 文件名搜索也受 max_results 限制
        """
        # 创建多个可匹配文件
        for i in range(5):
            (sample_files / f"findme_{i}.txt").write_text(f"file {i}", encoding="utf-8")

        result = await search_tool.execute({
            "query": "findme",
            "search_type": "filename",
            "path": str(sample_files),
            "max_results": 2,
        })

        assert result.success is True
        assert result.output["c"] <= 2


class TestUseRegex:
    """use_regex 正则表达式参数测试。"""

    async def test_regex_search_match(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试正则表达式搜索能正确匹配。

        验证：
        - 使用正则 def\\s+\\w+ 能匹配函数定义行
        """
        result = await search_tool.execute({
            "query": r"def\s+\w+",
            "search_type": "text",
            "path": str(sample_files),
            "use_regex": True,
        })

        assert result.success is True
        assert result.output["c"] >= 1
        # 匹配内容应包含函数定义
        all_contents = " ".join(row[2] for row in result.output["d"])
        assert "def " in all_contents

    async def test_regex_search_pattern_error(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试无效正则表达式时返回错误。

        验证：
        - success 为 False
        - error_code 为 REGEX_ERROR
        """
        result = await search_tool.execute({
            "query": r"[invalid(regex",
            "search_type": "text",
            "path": str(sample_files),
            "use_regex": True,
        })

        assert result.success is False
        assert result.error_code == "REGEX_ERROR"

    async def test_literal_search_by_default(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试默认（use_regex=False）进行字面量搜索。

        验证：
        - 特殊正则字符被当作普通字符处理
        - 搜索 "test_project" 能精确匹配 config.json 中的内容
        """
        # 在文件中写入包含正则特殊字符的文本
        (sample_files / "regex_test.txt").write_text(
            "price is $100.00\nother line\n",
            encoding="utf-8",
        )

        result = await search_tool.execute({
            "query": "$100.00",
            "search_type": "text",
            "path": str(sample_files),
            "use_regex": False,
        })

        assert result.success is True
        assert result.output["c"] >= 1

    async def test_regex_vs_literal_difference(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试正则搜索与字面量搜索的行为差异。

        验证：
        - 字面量搜索 "def.*" 作为普通字符串搜索
        - 正则搜索 "def.*" 作为模式匹配
        """
        # 创建包含字面 "def.*" 文本的文件
        (sample_files / "pattern.txt").write_text(
            "this line has def.* literally\n",
            encoding="utf-8",
        )

        # 字面量搜索：应匹配 "def.*" 字面文本
        result_literal = await search_tool.execute({
            "query": "def.*",
            "search_type": "text",
            "path": str(sample_files),
            "use_regex": False,
        })

        # 正则搜索：应匹配所有以 "def" 开头后跟任意字符的行
        result_regex = await search_tool.execute({
            "query": "def.*",
            "search_type": "text",
            "path": str(sample_files),
            "use_regex": True,
        })

        assert result_literal.success is True
        assert result_regex.success is True
        # 正则搜索应找到更多结果（包括函数定义行）
        assert result_regex.output["c"] >= result_literal.output["c"]


# ═══════════════════════════════════════════════════════════
# workspace 注入测试
# ═══════════════════════════════════════════════════════════


class TestWorkspaceInjection:
    """workspace 注入参数测试。"""

    async def test_workspace_overrides_base_path(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """测试 workspace 参数能覆盖 base_path。

        验证：
        - 传入 workspace 参数后，搜索路径被更新
        - 能在 workspace 指定的路径中搜索到内容
        """
        result = await search_tool.execute({
            "query": "search_test_marker",
            "search_type": "text",
            "workspace": str(sample_files),
        })

        assert result.success is True
        # 默认不区分大小写，匹配 .py 文件和 readme.txt 中的大小写变体
        assert result.output["c"] >= 2


# ═══════════════════════════════════════════════════════════
# 工具定义测试
# ═══════════════════════════════════════════════════════════


class TestToolDefinition:
    """工具定义元数据测试。"""

    def test_tool_name(self) -> None:
        """测试工具定义名称为 enhanced_search。"""
        tool_def = EnhancedSearchTool.get_tool_definition()
        assert tool_def.name == "enhanced_search"

    def test_tool_required_params(self) -> None:
        """测试工具定义中 query 为必填参数。"""
        tool_def = EnhancedSearchTool.get_tool_definition()
        assert "query" in tool_def.input_schema["required"]

    def test_tool_search_type_enum(self) -> None:
        """测试工具定义中 search_type 枚举值正确。"""
        tool_def = EnhancedSearchTool.get_tool_definition()
        search_type_schema = tool_def.input_schema["properties"]["search_type"]
        assert set(search_type_schema["enum"]) == {"text", "filename"}

    def test_tool_injected_params(self) -> None:
        """测试工具定义中包含 workspace 注入参数。"""
        tool_def = EnhancedSearchTool.get_tool_definition()
        assert "workspace" in tool_def.injected_params

    def test_tool_default_values(self) -> None:
        """测试工具定义中各参数的默认值正确。"""
        tool_def = EnhancedSearchTool.get_tool_definition()
        props = tool_def.input_schema["properties"]

        assert props["search_type"]["default"] == "text"
        assert props["case_sensitive"]["default"] is False
        assert props["context_lines"]["default"] == 2
        assert props["max_results"]["default"] == 100
        assert props["use_regex"]["default"] is False
        assert props["file_pattern"]["default"] == "*"


# ═══════════════════════════════════════════════════════════
# 回归测试：审查中实测出的 4 个 bug（均已修复）
# ═══════════════════════════════════════════════════════════


class TestRegressionBugFix:
    """回归测试，锁定审查中发现并修复的 4 个 bug。

    每个测试对应一个具体 bug，确保其不再复发。
    """

    async def test_context_after_not_truncated(
        self, search_tool: EnhancedSearchTool, tmp_path: Path
    ) -> None:
        """Bug#1：max_results 达上限后不能丢掉当前 match 的后置 context。

        复现条件：单 match，context_lines=2，max_results=1。
        修复前：rg 路径在 match 计数达上限后立即 break，丢失 after-context。
        修复后：drain 完当前 match 的 context-after 再停。
        """
        (tmp_path / "a.py").write_text(
            "L0\nL1\nmatch\nL3\nL4\n", encoding="utf-8"
        )
        result = await search_tool.execute({
            "query": "match",
            "search_type": "text",
            "path": str(tmp_path),
            "max_results": 1,
            "context_lines": 2,
            "case_sensitive": True,
        })

        assert result.success is True
        data = result.output
        # match 行数被 max_results 限制为 1
        assert data["match_count"] == 1
        # 返回行内容应同时包含 match 本身和它前后的上下文 L1/L3/L4
        line_contents = {row[2] for row in data["d"]}
        assert "match" in line_contents  # 匹配行
        assert "L3" in line_contents      # after-context 未丢失
        assert "L4" in line_contents      # after-context 未丢失

    async def test_max_depth_honored_in_rg(
        self, search_tool: EnhancedSearchTool, tmp_path: Path
    ) -> None:
        """Bug#3：max_depth 在 rg 路径必须生效。

        复现条件：深层目录（4 层）放 needle.py，顶层放 top.py，max_depth=1。
        修复前：rg 路径根本没传 -d，深层文件照样命中。
        修复后：只命中顶层 top.py。
        """
        deep = tmp_path
        for d in ("l1", "l2", "l3", "l4"):
            deep = deep / d
            deep.mkdir()
        (deep / "needle.py").write_text("target_match\n", encoding="utf-8")
        (tmp_path / "top.py").write_text("target_match\n", encoding="utf-8")

        result = await search_tool.execute({
            "query": "target_match",
            "search_type": "text",
            "path": str(tmp_path),
            "max_depth": 1,
            "context_lines": 0,
        })

        assert result.success is True
        matched_files = {row[0] for row in result.output["d"]}
        # 只命中顶层 top.py，深层 needle.py 被深度限制排除
        assert any("top.py" in f for f in matched_files)
        assert not any("needle.py" in f for f in matched_files)

    async def test_literal_multiline_query(
        self, search_tool: EnhancedSearchTool, tmp_path: Path
    ) -> None:
        """Bug#4：字面量模式下含换行的 query 不能抛 SEARCH_FAILED。

        复现条件：use_regex=False，query 含换行（常见多行粘贴）。
        修复前：rg 字面量模式遇换行报 "the literal is not allowed in a regex"，
                被当成 SEARCH_FAILED 返回。
        修复后：自动加 -U multiline 消除该报错；查询以正常结果返回
                （可能 0 匹配——rg 对字面量跨行匹配能力有限，属固有行为，
                不在本工具职责内）。
        """
        (tmp_path / "a.py").write_text("foo bar\nbaz\n", encoding="utf-8")
        result = await search_tool.execute({
            "query": "foo bar\nbaz",
            "search_type": "text",
            "path": str(tmp_path),
            "use_regex": False,
            "context_lines": 0,
        })

        # 核心断言：不再抛含混的 SEARCH_FAILED
        assert result.success is True, f"字面量换行查询不应失败: {result.error}"
        # match_count 可能为 0（rg 字面量跨行匹配有限），只要不报错即可
        assert "match_count" in result.output

    async def test_match_count_field_present(
        self, search_tool: EnhancedSearchTool, sample_files: Path
    ) -> None:
        """Bug#5：输出必须含 match_count 字段，供区分匹配数与总行数。

        修复前：max_results 语义混乱（c 可超 max_results 却无字段说明）。
        修复后：match_count 表示匹配行数，受 max_results 限制；c 含 context 行。
        """
        result = await search_tool.execute({
            "query": "search_test_marker",
            "search_type": "text",
            "path": str(sample_files),
            "max_results": 100,
            "context_lines": 2,
        })

        assert result.success is True
        data = result.output
        # match_count 字段存在且为非负整数
        assert "match_count" in data
        assert isinstance(data["match_count"], int)
        assert data["match_count"] >= 0
        # match_count 受 max_results 限制
        assert data["match_count"] <= 100
        # 当 context_lines > 0 时，c（含上下文）可以 >= match_count
        assert data["c"] >= data["match_count"]
