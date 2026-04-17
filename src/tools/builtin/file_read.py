"""
文件读取工具

暴露接口：
- get_tool_definition() -> Tool：get_tool_definition功能
- FileReadTool：FileReadTool类
"""

import json
from pathlib import Path
from typing import Any

import yaml

from tools.builtin.shared import format_size
from tools.types import (
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
    - 读取文件内容
    - 列出目录结构

    注意：文件搜索功能已移至 EnhancedSearchTool（search_type=filename）
    """

    def __init__(self, base_path: str | None = None):
        """初始化文件读取工具"""
        self.base_path = Path(base_path) if base_path else Path.cwd()

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        from tools.types import ToolLevel

        return Tool(
            name="file_read",
            description="读取文件内容、列出目录结构。"
            "适用场景：需要读取文件内容、浏览目录结构。"
            "不适用场景：需要写入文件（使用 file_write）、执行代码（使用 bash_execute）、搜索文件内容（使用 enhanced_search）、搜索文件名（使用 enhanced_search 的 filename 模式）。"
            "注意：路径访问受 base_path 限制；大文件读取可能影响性能。"
            "fields 参数：读取 YAML/JSON 文件的特定字段，节省 token。例如：fields=['id', 'name'] 只返回这两个字段。",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["read", "list"],
                        "description": "操作类型：read(读取文件内容)、list(列出目录内容)",
                    },
                    "path": {
                        "type": "string",
                        "description": "文件或目录路径（相对路径或绝对路径）。action=read 时为文件路径；action=list 时为目录路径",
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要读取的字段列表（仅支持 YAML/JSON 文件）。例如：['id', 'name', 'expected_input']。支持嵌套字段，用点号分隔：['agent.tools', 'agent.config.timeout']。不指定则返回完整内容。",
                    },
                },
                "required": ["action"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.FILE,
            level=ToolLevel.USER,
            tags=["file", "io", "read"],
            injected_params=["workspace"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """执行工具"""
        workspace = inputs.get("workspace")
        if workspace:
            self.base_path = Path(workspace)

        action = inputs.get("action")

        if action == "read":
            return await self._read_file(inputs)
        elif action == "list":
            return await self._list_directory(inputs)
        else:
            return create_failure_result(
                error=f"不支持的操作: {action}。支持的操作：read(读取文件)、list(列出目录)。如需搜索文件，请使用 enhanced_search 工具。",
                error_code="INVALID_ACTION",
            )

    def _resolve_path(self, path_str: str) -> Path:
        """解析路径（路径边界检查由中间层统一控制）"""
        path = Path(path_str)
        if not path.is_absolute():
            path = self.base_path / path
        return path.resolve()

    async def _read_file(self, inputs: dict[str, Any]) -> ToolResult:
        """读取文件"""
        try:
            path_str = inputs.get("path")
            if not path_str:
                return create_failure_result(
                    error="文件路径不能为空",
                    error_code="MISSING_PATH",
                )

            path = self._resolve_path(path_str)

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

            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = path.read_text(encoding="gbk", errors="ignore")

            file_size = path.stat().st_size
            lines = content.count("\n") + (
                1 if content and not content.endswith("\n") else 0
            )

            fields = inputs.get("fields")
            if fields:
                return self._extract_fields(content, path, fields)

            return create_success_result(
                data={
                    "file": path.name,
                    "lines": lines,
                    "size": format_size(file_size),
                    "content": content,
                },
                metadata={"action": "read_file"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"读取文件失败: {str(e)}",
                error_code="READ_FAILED",
            )

    def _extract_fields(
        self, content: str, path: Path, fields: list[str]
    ) -> ToolResult:
        """从 YAML/JSON 文件中提取特定字段"""
        suffix = path.suffix.lower()
        data: dict[str, Any] = {}

        try:
            if suffix in [".yaml", ".yml"]:
                data = yaml.safe_load(content) or {}
            elif suffix == ".json":
                data = json.loads(content)
            else:
                return create_failure_result(
                    error=f"fields 参数仅支持 YAML/JSON 文件，当前文件类型: {suffix}",
                    error_code="FIELDS_NOT_SUPPORTED",
                )
        except (yaml.YAMLError, json.JSONDecodeError) as e:
            return create_failure_result(
                error=f"解析文件失败: {str(e)}",
                error_code="PARSE_ERROR",
            )

        if not isinstance(data, dict):
            return create_failure_result(
                error="fields 参数仅支持字典类型的 YAML/JSON 文件",
                error_code="FIELDS_NOT_SUPPORTED",
            )

        result: dict[str, Any] = {}
        for field in fields:
            value = self._get_nested_field(data, field)
            if value is not None:
                self._set_nested_field(result, field, value)

        return create_success_result(
            data=result,
            metadata={"action": "read_file_fields", "fields": fields},
        )

    def _get_nested_field(self, data: dict[str, Any], field: str) -> Any:
        """获取嵌套字段的值"""
        keys = field.split(".")
        current = data
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return None
        return current

    def _set_nested_field(
        self, data: dict[str, Any], field: str, value: Any
    ) -> None:
        """设置嵌套字段的值"""
        keys = field.split(".")
        current = data
        for key in keys[:-1]:
            if key not in current:
                current[key] = {}
            current = current[key]
        current[keys[-1]] = value

    async def _list_directory(self, inputs: dict[str, Any]) -> ToolResult:
        """列出目录内容"""
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

            dirs = []
            file_names = []
            file_sizes = []

            for item in path.iterdir():
                try:
                    if item.is_dir():
                        dirs.append(item.name)
                    else:
                        stat = item.stat()
                        file_names.append(item.name)
                        file_sizes.append(format_size(stat.st_size))
                except Exception:
                    continue

            # 按文件名排序，保持 names 和 sizes 对应
            if file_names:
                sorted_pairs = sorted(zip(file_names, file_sizes, strict=False), key=lambda x: x[0])
                file_names = [p[0] for p in sorted_pairs]
                file_sizes = [p[1] for p in sorted_pairs]

            return create_success_result(
                data={
                    "h": ["dir", "file_name", "file_size"],
                    "d": [[dirs[i] if i < len(dirs) else "", file_names[i] if i < len(file_names) else "", file_sizes[i] if i < len(file_sizes) else ""] for i in range(max(len(dirs), len(file_names)))],
                    "c": len(dirs) + len(file_names),
                },
                metadata={"action": "list_directory"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"列出目录失败: {str(e)}",
                error_code="LIST_FAILED",
            )
