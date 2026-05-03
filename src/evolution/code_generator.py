"""代码生成与契约校验模块。

负责根据能力缺口生成 BuiltinTool 或 MCP Server 代码，
并使用 AST 解析进行契约校验（不执行代码）。

暴露接口：
- generate_builtin_tool(name, description, parameters, implementation_hint) -> GeneratedArtifact
- generate_mcp_server(name, tools, description) -> GeneratedArtifact
- validate_contract(artifact) -> GeneratedArtifact
- CodeGenerator: 代码生成器类
"""

from __future__ import annotations

import ast
import json
import logging
from typing import Any

from evolution.types import GeneratedArtifact, GenerationType

logger = logging.getLogger(__name__)

# BuiltinTool 代码模板
BUILTIN_TOOL_TEMPLATE = '''"""自动生成的内置工具: {class_name}。"""

from __future__ import annotations

from typing import Any

from tools.builtin.base import BuiltinTool
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class {class_name}(BuiltinTool):
    """{description}。"""

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义。"""
        return Tool(
            name="{tool_name}",
            description="{description}",
            input_schema={input_schema},
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            level=ToolLevel.USER,
            tags={tags},
        )

    async def execute(self, inputs: dict[str, Any]):
        """执行工具。

        Args:
            inputs: 工具输入参数

        Returns:
            执行结果
        """
        try:
            # {implementation_hint}
            result_data = {default_result}
            return create_success_result(
                data=result_data,
                metadata={{"action": "{tool_name}"}},
            )
        except Exception as e:
            return create_failure_result(
                error=f"执行失败: {{str(e)}}",
                error_code="EXECUTION_ERROR",
            )
'''

# MCP Server 代码模板
MCP_SERVER_TEMPLATE = '''"""自动生成的 MCP Server: {class_name}。"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class {class_name}:
    """{description} - MCP Server 实现。"""

    def __init__(self) -> None:
        """初始化 MCP Server。"""
        self._tools: list[dict[str, Any]] = {tools_definition}

    def get_tools(self) -> list[dict[str, Any]]:
        """获取 MCP Server 提供的工具列表。

        Returns:
            工具定义列表（MCP 格式）
        """
        return self._tools

    async def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """处理 MCP 请求。

        Args:
            request: MCP 请求体

        Returns:
            MCP 响应体
        """
        method = request.get("method", "")
        tool_name = request.get("params", {{}}).get("name", "")

        handler_map = {{
            "tools/list": self._handle_list_tools,
            "tools/call": self._handle_call_tool,
        }}

        handler = handler_map.get(method)
        if handler is None:
            return {{
                "error": {{"code": -32601, "message": f"Method not found: {{method}}"}}
            }}

        return await handler(request)

    async def _handle_list_tools(self, request: dict[str, Any]) -> dict[str, Any]:
        """处理 tools/list 请求。"""
        return {{"tools": self._tools}}

    async def _handle_call_tool(self, request: dict[str, Any]) -> dict[str, Any]:
        """处理 tools/call 请求。"""
        params = request.get("params", {{}})
        tool_name = params.get("name", "")

        # TODO: 实现具体的工具调用逻辑
        return {{
            "content": [
                {{"type": "text", "text": f"Tool {{tool_name}} executed successfully"}}
            ]
        }}
'''


def _safe_format_value(value: str) -> str:
    """对用户输入中的花括号进行转义，防止 format() 注入。

    Args:
        value: 用户提供的原始字符串

    Returns:
        转义后的安全字符串
    """
    return value.replace('{', '{{').replace('}', '}}')


def _escape_string(s: str) -> str:
    """转义字符串用于安全嵌入到生成代码中。

    转义顺序：先转义反斜杠，再转义引号，最后转义控制字符。

    Args:
        s: 原始字符串

    Returns:
        转义后的字符串
    """
    safe = s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\t', '\\t')
    return safe


class CodeGenerator:
    """代码生成器。

    根据能力缺口生成 BuiltinTool 或 MCP Server 代码，
    并通过 AST 解析进行契约校验。

    生成的代码遵循 src/tools/builtin/ 中现有工具的模式。
    """

    def generate_builtin_tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        implementation_hint: str = "",
    ) -> GeneratedArtifact:
        """生成 BuiltinTool 代码。

        按照 src/tools/builtin/ 中现有工具的模式生成代码：
        - 继承 BuiltinTool 基类
        - 实现 get_tool_definition() 静态方法
        - 实现 execute() 异步方法
        - 包含完整的参数 schema

        Args:
            name: 工具名称（snake_case）
            description: 工具功能描述
            parameters: 参数 schema（JSON Schema 格式）
            implementation_hint: 实现提示

        Returns:
            生成的代码产物
        """
        # 生成类名（snake_case → PascalCase）
        class_name = self._to_class_name(name)

        # 格式化参数 schema
        input_schema = self._format_schema(parameters)

        # 生成标签
        tags = self._infer_tags(name, description)

        # 默认结果占位
        default_result = '{"status": "ok", "message": "工具执行成功"}'

        code = BUILTIN_TOOL_TEMPLATE.format(
            class_name=class_name,
            tool_name=_safe_format_value(name),
            description=_escape_string(_safe_format_value(description)),
            input_schema=input_schema,
            tags=tags,
            implementation_hint=_safe_format_value(implementation_hint or "默认实现"),
            default_result=default_result,
        )

        file_path = f"src/tools/builtin/{name}.py"

        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code=code,
            file_path=file_path,
        )

        logger.info(
            "[CodeGenerator] 生成 BuiltinTool: name='%s', class='%s'",
            name,
            class_name,
        )
        return artifact

    def generate_mcp_server(
        self,
        name: str,
        tools: list[dict[str, Any]],
        description: str = "",
    ) -> GeneratedArtifact:
        """生成 MCP Server 代码。

        按照 MCP 协议格式生成服务代码，包含工具列表和请求处理。

        Args:
            name: MCP Server 名称
            tools: 工具定义列表（MCP 格式）
            description: 服务描述

        Returns:
            生成的代码产物
        """
        class_name = self._to_class_name(name)

        # 格式化工具定义
        tools_definition = self._format_tools_definition(tools)

        code = MCP_SERVER_TEMPLATE.format(
            class_name=class_name,
            description=_escape_string(_safe_format_value(description)),
            tools_definition=tools_definition,
        )

        file_path = f"src/tools/mcp_servers/{name}_server.py"

        artifact = GeneratedArtifact(
            generation_type=GenerationType.MCP_SERVER,
            code=code,
            file_path=file_path,
        )

        logger.info(
            "[CodeGenerator] 生成 MCP Server: name='%s', tools_count=%d",
            name,
            len(tools),
        )
        return artifact

    def validate_contract(self, artifact: GeneratedArtifact) -> GeneratedArtifact:
        """使用 AST 解析校验代码契约。

        检查代码是否符合工具接口契约：
        - BuiltinTool: 必须有类定义、get_tool_definition 方法、execute 方法
        - MCP Server: 必须有类定义、get_tools 方法、handle_request 方法

        使用 AST 解析，不执行代码。

        Args:
            artifact: 待校验的代码产物

        Returns:
            更新了 contract_valid 和 contract_errors 的产物
        """
        errors: list[str] = []

        # Step 1: 检查代码能否被解析为合法的 AST
        try:
            tree = ast.parse(artifact.code)
        except SyntaxError as exc:
            artifact.contract_valid = False
            artifact.contract_errors = [f"语法错误: {exc}"]
            return artifact

        # Step 2: 根据生成类型进行契约校验
        if artifact.generation_type == GenerationType.BUILTIN_TOOL:
            errors = self._validate_builtin_tool_contract(tree)
        elif artifact.generation_type == GenerationType.MCP_SERVER:
            errors = self._validate_mcp_server_contract(tree)

        artifact.contract_valid = len(errors) == 0
        artifact.contract_errors = errors

        logger.info(
            "[CodeGenerator] 契约校验: type=%s, valid=%s, errors=%d",
            artifact.generation_type.value,
            artifact.contract_valid,
            len(errors),
        )
        return artifact

    def _validate_builtin_tool_contract(self, tree: ast.Module) -> list[str]:
        """校验 BuiltinTool 代码契约。

        检查项：
        1. 存在类定义
        2. 存在 get_tool_definition 静态方法
        3. 存在 execute 异步方法
        4. 类继承自某个基类

        Args:
            tree: AST 解析树

        Returns:
            错误列表（空列表表示通过）
        """
        errors: list[str] = []

        class_defs = [
            node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        ]

        if not class_defs:
            errors.append("缺少类定义")
            return errors

        # 找到主要的工具类（非基类）
        tool_class = None
        for cls in class_defs:
            if cls.bases:
                tool_class = cls
                break

        if tool_class is None:
            errors.append("工具类缺少基类继承")
            # 仍然继续在首个类上检查方法，避免遗漏其他契约错误
            tool_class = class_defs[0]

        # 检查方法定义
        method_names: set[str] = set()
        has_get_tool_def = False
        has_execute = False

        for node in ast.walk(tool_class):
            if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                method_names.add(node.name)
                if node.name == "get_tool_definition":
                    has_get_tool_def = True
                    # 检查是否是静态方法
                    for decorator in node.decorator_list:
                        if isinstance(decorator, ast.Name) and decorator.id == "staticmethod":
                            break

                if node.name == "execute":
                    has_execute = True

        if not has_get_tool_def:
            errors.append("缺少 get_tool_definition 静态方法")

        if not has_execute:
            errors.append("缺少 execute 异步方法")

        return errors

    def _validate_mcp_server_contract(self, tree: ast.Module) -> list[str]:
        """校验 MCP Server 代码契约。

        检查项：
        1. 存在类定义
        2. 存在 __init__ 方法
        3. 存在 get_tools 方法
        4. 存在 handle_request 异步方法

        Args:
            tree: AST 解析树

        Returns:
            错误列表（空列表表示通过）
        """
        errors: list[str] = []

        class_defs = [
            node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        ]

        if not class_defs:
            errors.append("缺少类定义")
            return errors

        server_class = class_defs[0]

        method_names: set[str] = set()
        for node in ast.walk(server_class):
            if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                method_names.add(node.name)

        if "__init__" not in method_names:
            errors.append("缺少 __init__ 方法")

        if "get_tools" not in method_names:
            errors.append("缺少 get_tools 方法")

        if "handle_request" not in method_names:
            errors.append("缺少 handle_request 方法")

        return errors

    @staticmethod
    def _to_class_name(name: str) -> str:
        """将 snake_case 名称转换为 PascalCase 类名。

        Args:
            name: snake_case 名称

        Returns:
            PascalCase 类名
        """
        parts = name.split("_")
        return "".join(part.capitalize() for part in parts if part)

    @staticmethod
    def _format_schema(schema: dict[str, Any]) -> str:
        """将参数 schema 格式化为代码字符串。

        Args:
            schema: JSON Schema 字典

        Returns:
            格式化后的字符串表示
        """
        return json.dumps(schema, indent=8, ensure_ascii=False)

    @staticmethod
    def _format_tools_definition(tools: list[dict[str, Any]]) -> str:
        """将工具定义列表格式化为代码字符串。

        Args:
            tools: 工具定义列表

        Returns:
            格式化后的字符串表示
        """
        return json.dumps(tools, indent=4, ensure_ascii=False)

    @staticmethod
    def _infer_tags(name: str, description: str) -> str:
        """根据名称和描述推断标签。

        Args:
            name: 工具名称
            description: 工具描述

        Returns:
            标签列表的字符串表示
        """
        tags: list[str] = ["auto-generated"]

        # 基于名称和描述添加标签
        text = f"{name} {description}".lower()
        tag_keywords = {
            "file": "file",
            "search": "search",
            "web": "web",
            "http": "network",
            "api": "api",
            "data": "data",
            "convert": "converter",
            "parse": "parser",
            "analyze": "analysis",
        }

        for keyword, tag in tag_keywords.items():
            if keyword in text:
                tags.append(tag)

        return json.dumps(tags)
