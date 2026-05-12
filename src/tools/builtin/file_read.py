"""
文件读取工具

提供文件的读取、搜索、列出目录功能
"""

from pathlib import Path
from typing import Any

from src.tools.types import (
    Tool,
    ToolCategory,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class FileReadTool:
    """
    文件读取工具

    提供：
    - 读取文件
    - 搜索文件
    - 列出目录
    """

    def __init__(self, base_path: str | None = None):
        """
        初始化文件读取工具

        Args:
            base_path: 基础路径（用于限制访问范围）
        """
        self.base_path = Path(base_path) if base_path else Path.cwd()

    @staticmethod
    def get_tool_definition() -> Tool:
        """
        获取工具定义

        Returns:
            工具定义
        """
        from src.tools.types import ToolLevel

        return Tool(
            name="file_read",
            description="读取文件、搜索文件、列出目录内容。"
            "适用场景：需要读取文件内容、搜索特定模式文件、列出目录结构。"
            "不适用场景：需要写入文件（使用 file_write）、执行代码（使用 bash_execute）、搜索文件内容（使用 code_search）。"
            "注意：路径访问受 base_path 限制；大文件读取可能影响性能；搜索结果默认限制 100 条。",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["read", "search", "list"],
                        "description": "操作类型：read(读取文件内容)、search(搜索文件)、list(列出目录内容)",
                    },
                    "path": {
                        "type": "string",
                        "description": "文件或目录路径（相对路径或绝对路径）。action=read 时为文件路径；action=list 时为目录路径；action=search 时为搜索根目录，默认为当前目录",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "搜索模式，支持通配符如 *.py、**/*.json（action=search 时必填）",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最大结果数，用于限制返回的文件数量（action=search 时有效），默认 100",
                        "default": 100,
                    },
                },
                "required": ["action"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.FILE,
            level=ToolLevel.USER,
            requires_approval=False,
            dangerous_operations=[
                "read:/etc/",
                "read:/sys/",
                "read:C:\\Windows\\",
            ],
            tags=["file", "io", "read", "search"],
            isolation_required=False,
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """
        执行工具

        Args:
            inputs: 输入参数

        Returns:
            执行结果
        """
        action = inputs.get("action")

        if action == "read":
            return await self._read_file(inputs)
        elif action == "search":
            return await self._search_files(inputs)
        elif action == "list":
            return await self._list_directory(inputs)
        else:
            return create_failure_result(
                error=f"不支持的操作: {action}",
                error_code="INVALID_ACTION",
            )

    def _resolve_path(self, path_str: str) -> Path:
        """
        解析路径，确保在基础路径内

        Args:
            path_str: 路径字符串

        Returns:
            解析后的路径
        """
        import tempfile

        path = Path(path_str)

        # 如果是相对路径，基于 base_path
        if not path.is_absolute():
            path = self.base_path / path

        # 规范化路径
        path = path.resolve()

        # 安全检查：确保路径在 base_path 内或临时目录内
        base_resolved = self.base_path.resolve()
        temp_dir = Path(tempfile.gettempdir()).resolve()

        # 检查是否在允许的路径内
        in_base_path = False
        in_temp_dir = False

        try:
            path.relative_to(base_resolved)
            in_base_path = True
        except ValueError:
            pass

        try:
            path.relative_to(temp_dir)
            in_temp_dir = True
        except ValueError:
            pass

        if not (in_base_path or in_temp_dir):
            raise PermissionError(f"访问路径超出允许范围: {path}")

        return path

    async def _read_file(self, inputs: dict[str, Any]) -> ToolResult:
        """
        读取文件

        Args:
            inputs: 输入参数

        Returns:
            文件内容
        """
        try:
            path_str = inputs.get("path")
            if not path_str:
                return create_failure_result(
                    error="文件路径不能为空",
                    error_code="MISSING_PATH",
                )

            path = self._resolve_path(path_str)

            # 检查文件是否存在
            if not path.exists():
                return create_failure_result(
                    error=f"文件不存在: {path}",
                    error_code="FILE_NOT_FOUND",
                )

            if not path.is_file():
                return create_failure_result(
                    error=f"路径不是文件: {path}",
                    error_code="NOT_A_FILE",
                )

            # 读取文件内容
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                # 尝试其他编码
                content = path.read_text(encoding="gbk", errors="ignore")

            # 获取文件大小
            file_size = path.stat().st_size

            return create_success_result(
                data={
                    "path": str(path),
                    "content": content,
                    "size": file_size,
                },
                metadata={"action": "read_file"},
            )

        except PermissionError as e:
            return create_failure_result(
                error=f"权限错误: {str(e)}",
                error_code="PERMISSION_DENIED",
            )
        except Exception as e:
            return create_failure_result(
                error=f"读取文件失败: {str(e)}",
                error_code="READ_FAILED",
            )

    async def _search_files(self, inputs: dict[str, Any]) -> ToolResult:
        """
        搜索文件

        Args:
            inputs: 输入参数

        Returns:
            搜索结果
        """
        try:
            pattern = inputs.get("pattern")
            if not pattern:
                return create_failure_result(
                    error="搜索模式不能为空",
                    error_code="MISSING_PATTERN",
                )

            max_results = inputs.get("max_results", 100)
            search_path_str = inputs.get("path", str(self.base_path))

            search_path = self._resolve_path(search_path_str)

            if not search_path.exists():
                return create_failure_result(
                    error=f"搜索路径不存在: {search_path}",
                    error_code="PATH_NOT_FOUND",
                )

            # 搜索文件
            results = []
            for file_path in search_path.rglob(pattern):
                if file_path.is_file():
                    try:
                        stat = file_path.stat()
                        results.append(
                            {
                                "path": str(file_path),
                                "size": stat.st_size,
                                "modified": stat.st_mtime,
                            }
                        )
                        if len(results) >= max_results:
                            break
                    except Exception:
                        continue

            return create_success_result(
                data={
                    "pattern": pattern,
                    "results": results,
                    "count": len(results),
                },
                metadata={"action": "search_files"},
            )

        except PermissionError as e:
            return create_failure_result(
                error=f"权限错误: {str(e)}",
                error_code="PERMISSION_DENIED",
            )
        except Exception as e:
            return create_failure_result(
                error=f"搜索文件失败: {str(e)}",
                error_code="SEARCH_FAILED",
            )

    async def _list_directory(self, inputs: dict[str, Any]) -> ToolResult:
        """
        列出目录内容

        Args:
            inputs: 输入参数

        Returns:
            目录内容
        """
        try:
            path_str = inputs.get("path", str(self.base_path))
            path = self._resolve_path(path_str)

            if not path.exists():
                return create_failure_result(
                    error=f"路径不存在: {path}",
                    error_code="PATH_NOT_FOUND",
                )

            if not path.is_dir():
                return create_failure_result(
                    error=f"路径不是目录: {path}",
                    error_code="NOT_A_DIRECTORY",
                )

            # 列出目录内容
            items = []
            for item in path.iterdir():
                try:
                    stat = item.stat()
                    items.append(
                        {
                            "name": item.name,
                            "path": str(item),
                            "is_dir": item.is_dir(),
                            "size": stat.st_size if item.is_file() else 0,
                            "modified": stat.st_mtime,
                        }
                    )
                except Exception:
                    continue

            return create_success_result(
                data={
                    "path": str(path),
                    "items": items,
                    "count": len(items),
                },
                metadata={"action": "list_directory"},
            )

        except PermissionError as e:
            return create_failure_result(
                error=f"权限错误: {str(e)}",
                error_code="PERMISSION_DENIED",
            )
        except Exception as e:
            return create_failure_result(
                error=f"列出目录失败: {str(e)}",
                error_code="LIST_FAILED",
            )
