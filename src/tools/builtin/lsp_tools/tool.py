"""
LSP 工具

暴露接口：
- get_tools() -> list[Tool]：get_tools功能
- get_tool_definitions() -> dict[str, Tool]：get_tool_definitions功能
- LSPTools：LSPTools类
"""

import logging
from pathlib import Path
from typing import Any

from core.results import ToolExecutionResult  # noqa: F401
from tools.types import (
    Tool,
    ToolCategory,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)


class LSPTools:
    """LSP 工具集"""

    def __init__(self, base_path: str | None = None):
        """初始化 LSP 工具"""
        self.base_path = Path(base_path) if base_path else Path.cwd()
        self.workspace = None

    def _validate_file_path(self, file_path: str) -> tuple[Path | None, str | None]:
        """验证文件路径（路径边界检查由中间层统一控制）"""
        try:
            path = Path(file_path)
            if not path.is_absolute():
                path = self.base_path / path
            path = path.resolve()
            if not path.exists():
                return None, f"文件不存在: {path}"
            return path, None
        except (ValueError, OSError) as e:
            return None, f"路径解析失败: {str(e)}"

    @staticmethod
    def _check_lsp_unavailable(gateway: Any, file_path: str) -> ToolResult | None:
        """检查 LSP 服务器是否不可用，若不可用返回失败结果，否则返回 None"""
        language = gateway._detect_language(file_path)
        if not gateway.get_client(language):
            from lsp.gateway import LSP_SERVERS  # noqa: PLC0415

            server_config = LSP_SERVERS.get(language)
            server_name = server_config.name if server_config else language
            hint = gateway.get_install_hint(language)
            return create_failure_result(
                f"LSP 服务器未启动: {language} 语言的服务器 ({server_name}) 不可用。\n安装提示: {hint}",
                error_code="LSP_SERVER_NOT_AVAILABLE",
            )
        return None

    @staticmethod
    def get_tools() -> list[Tool]:
        """获取所有 LSP 工具"""
        return [
            LSPTools._lsp_definition_tool(),
            LSPTools._lsp_references_tool(),
            LSPTools._lsp_diagnostics_tool(),
            LSPTools._file_jump_tool(),
        ]

    @staticmethod
    def get_tool_definitions() -> dict[str, Tool]:
        """获取工具定义字典（用于注册）"""
        tools = {
            "lsp_definition": LSPTools._lsp_definition_tool(),
            "lsp_references": LSPTools._lsp_references_tool(),
            "lsp_diagnostics": LSPTools._lsp_diagnostics_tool(),
            "file_jump": LSPTools._file_jump_tool(),
        }
        return tools

    @staticmethod
    def _lsp_definition_tool() -> Tool:
        """跳转到定义工具"""
        return Tool(
            name="lsp_definition",
            description="跳转到符号定义位置。需要提供文件路径、行号和列号。使用场景：查看函数、类、变量等符号的定义位置；理解符号声明位置；跳转到类型定义理解结构。限制：需要 LSP 服务器支持对应语言；行号和列号从 0 开始计数；如果符号未找到定义会返回空结果。",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "文件路径，支持绝对路径或相对路径",
                    },
                    "line": {
                        "type": "integer",
                        "description": "行号（从 0 开始），指定符号所在的行",
                    },
                    "character": {
                        "type": "integer",
                        "description": "列号（从 0 开始，可选），指定符号所在的列，默认为 0",
                    },
                },
                "required": ["file_path", "line"],
            },
            category=ToolCategory.ANALYSIS,
            source=ToolSource.CODE,
            level="user",
            injected_params=["workspace"],
        )

    async def _lsp_definition(self, inputs: dict[str, Any]) -> ToolResult:  # noqa: PLR0911
        """执行跳转到定义"""
        workspace = inputs.get("workspace")
        if workspace:
            self.base_path = Path(workspace)

        file_path = inputs.get("file_path")
        line = inputs.get("line", 0)
        character = inputs.get("character", 0)

        if not file_path:
            return create_failure_result("缺少 file_path 参数", error_code="MISSING_FILE_PATH")

        validated_path, error = self._validate_file_path(file_path)
        if error:
            return create_failure_result(error, error_code="INVALID_PATH")

        try:
            from lsp.file_jump import FileJumpProtocol  # noqa: PLC0415
            from lsp.gateway import get_lsp_gateway  # noqa: PLC0415
            from lsp.types import Position  # noqa: PLC0415

            gateway = await get_lsp_gateway()

            position = Position(line=line, character=character)
            locations = await gateway.go_to_definition(str(validated_path), position)

            if not locations:
                unavailable = self._check_lsp_unavailable(gateway, str(validated_path))
                if unavailable:
                    return unavailable
                return create_success_result(
                    data="未找到定义",
                    metadata={
                        "file_path": str(validated_path),
                        "line": line,
                        "character": character,
                    },
                )

            first_location = locations[0]
            success = await FileJumpProtocol.jump_from_uri(first_location.uri)

            if success:
                return create_success_result(
                    data=f"已跳转到定义: {first_location.uri}",
                    metadata={
                        "uri": first_location.uri,
                        "range": first_location.range.dict(),
                    },
                )
            return create_failure_result("跳转失败", error_code="JUMP_FAILED")

        except ImportError:
            logger.error("lsp_definition 执行失败: LSP 模块未安装")
            return create_failure_result("LSP 模块未安装", error_code="LSP_NOT_INSTALLED")
        except ConnectionError as e:
            logger.error(f"lsp_definition 执行失败: LSP 服务器连接错误 - {str(e)}")
            return create_failure_result(f"LSP 服务器连接错误: {str(e)}", error_code="LSP_CONNECTION_ERROR")
        except (ValueError, TypeError) as e:
            logger.error(f"lsp_definition 执行失败: 参数错误 - {str(e)}")
            return create_failure_result(f"参数错误: {str(e)}", error_code="INVALID_ARGUMENT")
        except Exception as e:
            logger.exception("lsp_definition 执行失败")
            return create_failure_result(f"执行失败: {str(e)}", error_code="EXECUTION_FAILED")

    @staticmethod
    def _lsp_references_tool() -> Tool:
        """查找引用工具"""
        return Tool(
            name="lsp_references",
            description="查找符号的所有引用位置。需要提供文件路径、行号和列号。使用场景：查找函数、类、变量在哪些地方被使用；重构前了解符号影响范围；分析代码依赖关系；查找未使用的代码；理解功能调用链。限制：需要 LSP 服务器支持对应语言；行号和列号从 0 开始计数；大型代码库中查找可能耗时较长；结果默认只显示前10个。",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "文件路径，支持绝对路径或相对路径",
                    },
                    "line": {
                        "type": "integer",
                        "description": "行号（从 0 开始），指定符号所在的行",
                    },
                    "character": {
                        "type": "integer",
                        "description": "列号（从 0 开始，可选），指定符号所在的列，默认为 0",
                    },
                },
                "required": ["file_path", "line"],
            },
            category=ToolCategory.ANALYSIS,
            source=ToolSource.CODE,
            level="user",
            injected_params=["workspace"],
        )

    async def _lsp_references(self, inputs: dict[str, Any]) -> ToolResult:  # noqa: PLR0911
        """执行查找引用"""
        workspace = inputs.get("workspace")
        if workspace:
            self.base_path = Path(workspace)

        file_path = inputs.get("file_path")
        line = inputs.get("line", 0)
        character = inputs.get("character", 0)

        if not file_path:
            return create_failure_result("缺少 file_path 参数", error_code="MISSING_FILE_PATH")

        validated_path, error = self._validate_file_path(file_path)
        if error:
            return create_failure_result(error, error_code="INVALID_PATH")

        try:
            from lsp.gateway import get_lsp_gateway  # noqa: PLC0415
            from lsp.types import Position  # noqa: PLC0415

            gateway = await get_lsp_gateway()
            position = Position(line=line, character=character)
            references = await gateway.find_references(str(validated_path), position)

            if not references:
                unavailable = self._check_lsp_unavailable(gateway, str(validated_path))
                if unavailable:
                    return unavailable
                return create_success_result(
                    data="未找到引用",
                    metadata={"count": 0},
                )

            result_text = f"找到 {len(references)} 个引用:\n"
            for ref in references[:10]:
                result_text += f"- {ref.uri}\n"

            return create_success_result(
                data=result_text,
                metadata={
                    "count": len(references),
                    "references": [r.dict() for r in references],
                },
            )

        except ImportError:
            logger.error("lsp_references 执行失败: LSP 模块未安装")
            return create_failure_result("LSP 模块未安装", error_code="LSP_NOT_INSTALLED")
        except ConnectionError as e:
            logger.error(f"lsp_references 执行失败: LSP 服务器连接错误 - {str(e)}")
            return create_failure_result(f"LSP 服务器连接错误: {str(e)}", error_code="LSP_CONNECTION_ERROR")
        except (ValueError, TypeError) as e:
            logger.error(f"lsp_references 执行失败: 参数错误 - {str(e)}")
            return create_failure_result(f"参数错误: {str(e)}", error_code="INVALID_ARGUMENT")
        except Exception as e:
            logger.exception("lsp_references 执行失败")
            return create_failure_result(f"执行失败: {str(e)}", error_code="EXECUTION_FAILED")

    @staticmethod
    def _lsp_diagnostics_tool() -> Tool:
        """获取诊断工具"""
        return Tool(
            name="lsp_diagnostics",
            description="获取文件的诊断信息（错误、警告、提示）。使用场景：检查代码中的错误和警告；代码审查前验证代码质量；排查编译或运行问题；了解代码中的潜在问题；验证修复是否解决了所有问题。限制：需要 LSP 服务器支持对应语言；诊断信息依赖于 LSP 服务器的配置和规则；某些诊断可能需要文件保存后才能更新。严重级别说明：1=错误, 2=警告, 3=信息, 4=提示。",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "文件路径，支持绝对路径或相对路径",
                    },
                },
                "required": ["file_path"],
            },
            category=ToolCategory.ANALYSIS,
            source=ToolSource.CODE,
            level="user",
            injected_params=["workspace"],
        )

    async def _lsp_diagnostics(self, inputs: dict[str, Any]) -> ToolResult:  # noqa: PLR0911
        """执行获取诊断"""
        workspace = inputs.get("workspace")
        if workspace:
            self.base_path = Path(workspace)

        file_path = inputs.get("file_path")

        if not file_path:
            return create_failure_result("缺少 file_path 参数", error_code="MISSING_FILE_PATH")

        validated_path, error = self._validate_file_path(file_path)
        if error:
            return create_failure_result(error, error_code="INVALID_PATH")

        try:
            from lsp.gateway import get_lsp_gateway  # noqa: PLC0415

            gateway = await get_lsp_gateway()
            diagnostics = await gateway.get_diagnostics(str(validated_path))

            if not diagnostics:
                unavailable = self._check_lsp_unavailable(gateway, str(validated_path))
                if unavailable:
                    return unavailable
                return create_success_result(
                    data="没有诊断信息",
                    metadata={"count": 0},
                )

            severity_map = {1: "错误", 2: "警告", 3: "信息", 4: "提示"}
            result_text = f"找到 {len(diagnostics)} 个诊断:\n"
            for diag in diagnostics:
                severity = severity_map.get(diag.severity, "未知")
                result_text += f"- [{severity}] {diag.message}\n"

            return create_success_result(
                data=result_text,
                metadata={
                    "count": len(diagnostics),
                    "diagnostics": [d.dict() for d in diagnostics],
                },
            )

        except ImportError:
            logger.error("lsp_diagnostics 执行失败: LSP 模块未安装")
            return create_failure_result("LSP 模块未安装", error_code="LSP_NOT_INSTALLED")
        except ConnectionError as e:
            logger.error(f"lsp_diagnostics 执行失败: LSP 服务器连接错误 - {str(e)}")
            return create_failure_result(f"LSP 服务器连接错误: {str(e)}", error_code="LSP_CONNECTION_ERROR")
        except (ValueError, TypeError) as e:
            logger.error(f"lsp_diagnostics 执行失败: 参数错误 - {str(e)}")
            return create_failure_result(f"参数错误: {str(e)}", error_code="INVALID_ARGUMENT")
        except Exception as e:
            logger.exception("lsp_diagnostics 执行失败")
            return create_failure_result(f"执行失败: {str(e)}", error_code="EXECUTION_FAILED")

    @staticmethod
    def _file_jump_tool() -> Tool:
        """文件跳转工具"""
        return Tool(
            name="file_jump",
            description="在 IDE 中打开文件并跳转到指定位置。支持 VSCode、JetBrains、Neovim 等。使用场景：在 IDE 中打开特定文件；跳转到指定文件的特定行；配合其他工具（如 lsp_definition）在 IDE 中查看结果；在编辑器中定位到具体代码位置。限制：需要本地安装支持的 IDE（VSCode、JetBrains 系列、Neovim 等）；需要正确配置 IDE 的协议处理器；跳转操作会改变当前 IDE 的焦点和视图；行号和列号从 0 开始计数。",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "文件路径，支持绝对路径或相对路径",
                    },
                    "line": {
                        "type": "integer",
                        "description": "行号（从 0 开始，可选），指定跳转到的行，默认为文件开头",
                    },
                    "character": {
                        "type": "integer",
                        "description": "列号（从 0 开始，可选），指定跳转到的列，默认为 0",
                    },
                },
                "required": ["file_path"],
            },
            category=ToolCategory.ANALYSIS,
            source=ToolSource.CODE,
            level="user",
            injected_params=["workspace"],
        )

    async def _file_jump(self, inputs: dict[str, Any]) -> ToolResult:  # noqa: PLR0911
        """执行文件跳转"""
        workspace = inputs.get("workspace")
        if workspace:
            self.base_path = Path(workspace)

        file_path = inputs.get("file_path")
        line = inputs.get("line")
        character = inputs.get("character")

        if not file_path:
            return create_failure_result("缺少 file_path 参数", error_code="MISSING_FILE_PATH")

        validated_path, error = self._validate_file_path(file_path)
        if error:
            return create_failure_result(error, error_code="INVALID_PATH")

        try:
            from lsp.file_jump import FileJumpProtocol  # noqa: PLC0415
            from lsp.types import Position  # noqa: PLC0415

            position = None
            if line is not None:
                position = Position(line=line, character=character or 0)

            success = await FileJumpProtocol.jump_to_file(str(validated_path), position)

            if success:
                return create_success_result(
                    data=f"已打开文件: {str(validated_path)}",
                    metadata={
                        "file_path": str(validated_path),
                        "line": line,
                        "character": character,
                    },
                )
            return create_failure_result("打开文件失败", error_code="JUMP_FAILED")

        except ImportError:
            logger.error("file_jump 执行失败: LSP 模块未安装")
            return create_failure_result("LSP 模块未安装", error_code="LSP_NOT_INSTALLED")
        except FileNotFoundError:
            logger.error(f"file_jump 执行失败: 文件不存在 - {validated_path}")
            return create_failure_result(f"文件不存在: {validated_path}", error_code="FILE_NOT_FOUND")
        except (ValueError, TypeError) as e:
            logger.error(f"file_jump 执行失败: 参数错误 - {str(e)}")
            return create_failure_result(f"参数错误: {str(e)}", error_code="INVALID_ARGUMENT")
        except Exception as e:
            logger.exception("file_jump 执行失败")
            return create_failure_result(f"执行失败: {str(e)}", error_code="EXECUTION_FAILED")
