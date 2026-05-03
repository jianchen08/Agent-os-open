"""代码生成与契约校验模块测试。

覆盖 CodeGenerator 的核心功能：
- generate_builtin_tool: BuiltinTool 代码生成
- generate_mcp_server: MCP Server 代码生成
- validate_contract: 契约校验（AST 解析）
- _safe_format_value: 花括号转义（MF-02 修复验证）
- _escape_string: 字符串转义
"""

from __future__ import annotations

import pytest

from evolution.code_generator import CodeGenerator, _escape_string, _safe_format_value
from evolution.types import GeneratedArtifact, GenerationType


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def generator() -> CodeGenerator:
    """代码生成器实例。"""
    return CodeGenerator()


@pytest.fixture
def simple_params() -> dict:
    """简单参数 schema。"""
    return {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query",
            }
        },
        "required": ["query"],
    }


# =========================================================================
# generate_builtin_tool 测试
# =========================================================================


class TestGenerateBuiltinTool:
    """BuiltinTool 代码生成测试。"""

    def test_generate_builtin_tool_basic(
        self, generator: CodeGenerator, simple_params: dict,
    ) -> None:
        """基本工具代码生成。"""
        artifact = generator.generate_builtin_tool(
            name="my_search",
            description="Search files in workspace",
            parameters=simple_params,
            implementation_hint="Use regex to search",
        )

        assert isinstance(artifact, GeneratedArtifact)
        assert artifact.generation_type == GenerationType.BUILTIN_TOOL
        assert "MySearch" in artifact.code
        assert "my_search" in artifact.code
        assert "get_tool_definition" in artifact.code
        assert "execute" in artifact.code
        assert artifact.file_path == "src/tools/builtin/my_search.py"

    def test_generate_builtin_tool_class_name_conversion(self, generator: CodeGenerator) -> None:
        """snake_case 转 PascalCase 类名。"""
        artifact = generator.generate_builtin_tool(
            name="file_converter",
            description="Convert files",
            parameters={"type": "object", "properties": {}},
        )
        assert "FileConverter" in artifact.code

    def test_generate_builtin_tool_with_curly_braces(
        self, generator: CodeGenerator,
    ) -> None:
        """含花括号的 description 不报错（MF-02修复验证）。

        花括号在 format() 模板中是特殊字符，应被正确转义。
        """
        artifact = generator.generate_builtin_tool(
            name="test_tool",
            description="A tool with {curly} braces and {nested} stuff",
            parameters={"type": "object", "properties": {}},
        )

        # 不应抛出 KeyError 或 ValueError
        assert isinstance(artifact.code, str)
        assert len(artifact.code) > 0

    def test_generate_builtin_tool_with_complex_curly_braces(
        self, generator: CodeGenerator,
    ) -> None:
        """含复杂花括号模式的 description。"""
        artifact = generator.generate_builtin_tool(
            name="json_tool",
            description="Format JSON like {'key': 'value'} with {{double}} braces",
            parameters={"type": "object", "properties": {}},
        )
        assert isinstance(artifact.code, str)

    def test_generate_builtin_tool_empty_description(
        self, generator: CodeGenerator,
    ) -> None:
        """空描述不报错。"""
        artifact = generator.generate_builtin_tool(
            name="empty_desc",
            description="",
            parameters={"type": "object", "properties": {}},
        )
        assert isinstance(artifact.code, str)

    def test_generate_builtin_tool_with_special_chars(
        self, generator: CodeGenerator,
    ) -> None:
        """描述含特殊字符（引号、换行、反斜杠）。"""
        artifact = generator.generate_builtin_tool(
            name="special_tool",
            description='He said "hello" and left\\nwith backslash',
            parameters={"type": "object", "properties": {}},
        )
        assert isinstance(artifact.code, str)

    def test_generate_builtin_tool_single_word_name(
        self, generator: CodeGenerator,
    ) -> None:
        """单字名称的类名转换。"""
        artifact = generator.generate_builtin_tool(
            name="search",
            description="Search",
            parameters={"type": "object", "properties": {}},
        )
        assert "Search" in artifact.code

    def test_generate_builtin_tool_auto_tags(
        self, generator: CodeGenerator,
    ) -> None:
        """根据名称和描述自动推断标签。"""
        artifact = generator.generate_builtin_tool(
            name="file_search",
            description="Search files",
            parameters={"type": "object", "properties": {}},
        )
        assert "auto-generated" in artifact.code

    def test_generate_builtin_tool_default_result(
        self, generator: CodeGenerator,
    ) -> None:
        """生成的代码包含默认结果占位。"""
        artifact = generator.generate_builtin_tool(
            name="test",
            description="Test",
            parameters={"type": "object", "properties": {}},
        )
        assert "status" in artifact.code
        assert "ok" in artifact.code


# =========================================================================
# generate_mcp_server 测试
# =========================================================================


class TestGenerateMCPServer:
    """MCP Server 代码生成测试。"""

    def test_generate_mcp_server_basic(self, generator: CodeGenerator) -> None:
        """基本 MCP Server 代码生成。"""
        tools = [
            {
                "name": "tool1",
                "description": "First tool",
                "inputSchema": {"type": "object"},
            }
        ]
        artifact = generator.generate_mcp_server(
            name="my_server",
            tools=tools,
            description="My MCP Server",
        )

        assert artifact.generation_type == GenerationType.MCP_SERVER
        assert "MyServer" in artifact.code
        assert "handle_request" in artifact.code
        assert "get_tools" in artifact.code
        assert artifact.file_path == "src/tools/mcp_servers/my_server_server.py"

    def test_generate_mcp_server_multiple_tools(self, generator: CodeGenerator) -> None:
        """多工具 MCP Server 生成。"""
        tools = [
            {"name": "tool1", "description": "Tool 1"},
            {"name": "tool2", "description": "Tool 2"},
        ]
        artifact = generator.generate_mcp_server(
            name="multi_server",
            tools=tools,
        )
        assert "tool1" in artifact.code
        assert "tool2" in artifact.code

    def test_generate_mcp_server_empty_tools(self, generator: CodeGenerator) -> None:
        """空工具列表不报错。"""
        artifact = generator.generate_mcp_server(
            name="empty_server",
            tools=[],
        )
        assert isinstance(artifact.code, str)

    def test_generate_mcp_server_with_curly_description(
        self, generator: CodeGenerator,
    ) -> None:
        """含花括号的描述不报错。"""
        artifact = generator.generate_mcp_server(
            name="test_server",
            tools=[],
            description="Server with {curly} braces",
        )
        assert isinstance(artifact.code, str)


# =========================================================================
# validate_contract 测试
# =========================================================================


class TestValidateContract:
    """契约校验测试。"""

    def test_validate_contract_valid_code(
        self, generator: CodeGenerator,
    ) -> None:
        """合法 BuiltinTool 代码通过契约校验。"""
        artifact = generator.generate_builtin_tool(
            name="test_tool",
            description="Test tool",
            parameters={"type": "object", "properties": {}},
        )
        result = generator.validate_contract(artifact)

        assert result.contract_valid is True
        assert result.contract_errors == []

    def test_validate_contract_missing_execute(
        self, generator: CodeGenerator,
    ) -> None:
        """缺少 execute 方法不通过。"""
        code = '''
class MyTool:
    @staticmethod
    def get_tool_definition():
        return None
'''
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code=code,
            file_path="test.py",
        )
        result = generator.validate_contract(artifact)

        assert result.contract_valid is False
        assert any("execute" in err for err in result.contract_errors)

    def test_validate_contract_missing_get_tool_definition(
        self, generator: CodeGenerator,
    ) -> None:
        """缺少 get_tool_definition 方法不通过。"""
        code = '''
class MyTool(SomeBase):
    async def execute(self, inputs):
        pass
'''
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code=code,
            file_path="test.py",
        )
        result = generator.validate_contract(artifact)

        assert result.contract_valid is False
        assert any("get_tool_definition" in err for err in result.contract_errors)

    def test_validate_contract_no_class(
        self, generator: CodeGenerator,
    ) -> None:
        """无类定义不通过。"""
        code = "x = 1"
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code=code,
            file_path="test.py",
        )
        result = generator.validate_contract(artifact)

        assert result.contract_valid is False

    def test_validate_contract_syntax_error(
        self, generator: CodeGenerator,
    ) -> None:
        """语法错误不通过。"""
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code="this is not valid python {{{",
            file_path="test.py",
        )
        result = generator.validate_contract(artifact)

        assert result.contract_valid is False
        assert len(result.contract_errors) > 0

    def test_validate_contract_mcp_server_valid(
        self, generator: CodeGenerator,
    ) -> None:
        """有效 MCP Server 通过契约校验。"""
        tools = [{"name": "tool1", "description": "test"}]
        artifact = generator.generate_mcp_server(
            name="test_server",
            tools=tools,
            description="Test server",
        )
        result = generator.validate_contract(artifact)

        assert result.contract_valid is True

    def test_validate_contract_mcp_server_missing_method(
        self, generator: CodeGenerator,
    ) -> None:
        """MCP Server 缺少方法不通过。"""
        code = '''
class MyServer:
    def __init__(self):
        pass
'''
        artifact = GeneratedArtifact(
            generation_type=GenerationType.MCP_SERVER,
            code=code,
            file_path="test.py",
        )
        result = generator.validate_contract(artifact)

        assert result.contract_valid is False
        assert any("get_tools" in err for err in result.contract_errors)
        assert any("handle_request" in err for err in result.contract_errors)

    def test_validate_contract_no_base_class(
        self, generator: CodeGenerator,
    ) -> None:
        """BuiltinTool 无基类继承不通过。"""
        code = '''
class MyTool:
    @staticmethod
    def get_tool_definition():
        return None
    async def execute(self, inputs):
        pass
'''
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code=code,
            file_path="test.py",
        )
        result = generator.validate_contract(artifact)

        assert result.contract_valid is False

    def test_validate_contract_returns_same_artifact(
        self, generator: CodeGenerator,
    ) -> None:
        """validate_contract 返回更新后的同一产物。"""
        artifact = generator.generate_builtin_tool(
            name="test", description="test",
            parameters={"type": "object", "properties": {}},
        )
        result = generator.validate_contract(artifact)

        assert result is artifact


# =========================================================================
# 辅助函数测试
# =========================================================================


class TestSafeFormatValue:
    """_safe_format_value 花括号转义测试。"""

    def test_safe_format_value_no_braces(self) -> None:
        """无花括号不变。"""
        assert _safe_format_value("hello world") == "hello world"

    def test_safe_format_value_single_braces(self) -> None:
        """单花括号转义为双花括号。"""
        assert _safe_format_value("{value}") == "{{value}}"

    def test_safe_format_value_double_braces_preserved(self) -> None:
        """已有的双花括号变为四花括号。"""
        result = _safe_format_value("{{value}}")
        assert "{{{{value}}}}" == result

    def test_safe_format_value_empty_string(self) -> None:
        """空字符串不变。"""
        assert _safe_format_value("") == ""

    def test_safe_format_value_mixed(self) -> None:
        """混合花括号转义。"""
        result = _safe_format_value("text {a} and {b}")
        assert result == "text {{a}} and {{b}}"


class TestEscapeString:
    """_escape_string 字符串转义测试。"""

    def test_string_escaping_backslash(self) -> None:
        """反斜杠转义。"""
        assert _escape_string("a\\b") == "a\\\\b"

    def test_string_escaping_quote(self) -> None:
        """双引号转义。"""
        assert _escape_string('say "hi"') == 'say \\"hi\\"'

    def test_string_escaping_newline(self) -> None:
        """换行符转义。"""
        assert _escape_string("line1\nline2") == "line1\\nline2"

    def test_string_escaping_tab(self) -> None:
        """制表符转义。"""
        assert _escape_string("col1\tcol2") == "col1\\tcol2"

    def test_string_escaping_complete(self) -> None:
        """反斜杠、引号、换行符、制表符转义完整。"""
        result = _escape_string('a\\b\nc\td"e')
        assert "\\\\" in result
        assert "\\n" in result
        assert "\\t" in result
        assert '\\"' in result

    def test_string_escaping_no_special(self) -> None:
        """无特殊字符不变。"""
        assert _escape_string("plain text") == "plain text"

    def test_string_escaping_empty(self) -> None:
        """空字符串不变。"""
        assert _escape_string("") == ""

    def test_string_escaping_order_backslash_first(self) -> None:
        """反斜杠先于其他字符转义（避免双重转义）。"""
        # 如果先转义 \n 为 \\n，再转义 \ 为 \\，结果是 \\\\n，这是错误的
        # 正确顺序：先转义 \ 为 \\，再转义 \n 为 \\n
        result = _escape_string("a\\nb")
        assert result == "a\\\\nb"


class TestToClassName:
    """_to_class_name 测试。"""

    def test_single_word(self, generator: CodeGenerator) -> None:
        assert generator._to_class_name("search") == "Search"

    def test_two_words(self, generator: CodeGenerator) -> None:
        assert generator._to_class_name("my_tool") == "MyTool"

    def test_three_words(self, generator: CodeGenerator) -> None:
        assert generator._to_class_name("file_converter_tool") == "FileConverterTool"

    def test_trailing_underscore(self, generator: CodeGenerator) -> None:
        """尾部下划线被忽略。"""
        assert generator._to_class_name("test_") == "Test"
