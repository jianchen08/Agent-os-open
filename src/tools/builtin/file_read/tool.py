"""
文件读取工具

暴露接口：
- get_tool_definition() -> Tool：get_tool_definition功能
- FileReadTool：FileReadTool类
"""

import asyncio
import json
from collections import deque
from pathlib import Path
from typing import Any

import yaml

from tools.builtin.base import BuiltinTool
from tools.builtin.binary_converter import (
    convert_binary_to_markdown,
    get_file_category,
)
from tools.builtin.shared import format_size
from tools.builtin.workspace_aware import WorkspaceAwareMixin
from tools.types import (
    Tool,
    ToolCategory,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)

MAX_FILE_SIZE = 2 * 1024 * 1024
BINARY_SNIFF_SIZE = 8192


class FileReadTool(BuiltinTool, WorkspaceAwareMixin):
    """文件读取工具

    提供读取文件内容功能。自动路由文本/二进制文件：
    - 文本文件：直接读取内容（支持 fields/tail/start_line/end_line 参数）
    - 文档文件（PDF/DOCX/XLSX/PPTX）：通过 markitdown 转 Markdown
    - 图片文件（PNG/JPG 等）：通过 markitdown 转 Markdown 描述
    """

    def __init__(self, base_path: str | None = None):
        self.base_path = Path(base_path) if base_path else Path.cwd()

    @staticmethod
    def get_tool_definition() -> Tool:
        from tools.types import ToolLevel

        return Tool(
            name="file_read",
            description="读取文件内容。自动识别文本和二进制文件：文本文件直接读取，"
            "PDF/DOCX/XLSX/PPTX/图片等通过 markitdown 转换为 Markdown。"
            "适用场景：需要读取文件内容。"
            "不适用场景：需要写入文件（使用 file_write）、列出目录（使用 list_directory）、"
            "搜索文件内容（使用 enhanced_search）。"
            "fields 参数：读取 YAML/JSON 文件的特定字段，节省 token。"
            "例如：fields=['id', 'name'] 只返回这两个字段。",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径（相对路径或绝对路径），与 paths 二选一",
                    },
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "批量读取文件路径列表（与 path 二选一，优先使用 paths）",
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要读取的字段列表（仅支持 YAML/JSON 文件）。"
                        "例如：['id', 'name']。支持嵌套字段，用点号分隔。"
                        "不指定则返回完整内容。",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号（从1开始），仅读取指定行范围。"
                        "不指定则从第1行开始。",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号（从1开始，包含该行），仅读取指定行范围。"
                        "不指定则到文件末尾。",
                    },
                    "tail": {
                        "type": "integer",
                        "description": "仅读取文件最后 N 行（仅文本文件有效）。"
                        "不指定则返回完整内容。",
                    },
                },
                "required": [],
            },
            source=ToolSource.CODE,
            category=ToolCategory.FILE,
            level=ToolLevel.USER,
            tags=["file", "io", "read"],
            injected_params=["workspace"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        self._init_workspace(inputs)
        self.base_path = self._workspace

        # 优先使用 paths 批量参数
        paths = inputs.get("paths")
        if paths and isinstance(paths, list):
            return await self._read_files(inputs, paths)

        # 单文件模式
        return await self._read_file(inputs)
    
    async def _read_files(self, inputs: dict[str, Any], paths: list[str]) -> ToolResult:
        """批量读取文件，每个文件独立返回结果"""
        results = []
        fields = inputs.get("fields")
        start_line = inputs.get("start_line")
        end_line = inputs.get("end_line")
        tail = inputs.get("tail")

        for path_str in paths:
            file_inputs = {
                "path": path_str,
                "fields": fields,
                "start_line": start_line,
                "end_line": end_line,
                "tail": tail,
            }
            result = await self._read_file(file_inputs)
            results.append({
                "path": path_str,
                "success": result.success,
                "data": result.data if result.success else None,
                "error": result.error if not result.success else None,
            })
        
        # 汇总结果
        success_count = sum(1 for r in results if r["success"])
        failed_count = len(results) - success_count
        
        return create_success_result(
            data={
                "results": results,
                "summary": {
                    "total": len(results),
                    "success": success_count,
                    "failed": failed_count,
                },
            },
            metadata={"action": "batch_read_files"},
        )

    async def _read_file(self, inputs: dict[str, Any]) -> ToolResult:
        try:
            path_str = inputs.get("path")
            if not path_str:
                return create_failure_result(
                    error="文件路径不能为空",
                    error_code="MISSING_PATH",
                )

            path = self.resolve_path(path_str)
            display_path = self._format_output_path(path, path_str)

            if not path.exists():
                return create_failure_result(
                    error=f"文件不存在: {display_path}",
                    error_code="FILE_NOT_FOUND",
                )

            if not path.is_file():
                return create_failure_result(
                    error=f"路径不是文件: {display_path}",
                    error_code="NOT_A_FILE",
                )

            category = get_file_category(path)
            if category in ("document", "image"):
                return convert_binary_to_markdown(path)

            if category == "rejected":
                return create_failure_result(
                    error=f"不支持读取此类型文件: {path.name}。"
                    f"支持的二进制文件：PDF、DOCX、XLSX、PPTX、"
                    f"PNG、JPG 等图片。"
                    f"列出目录请使用 list_directory 工具。",
                    error_code="BINARY_FILE_NOT_SUPPORTED",
                )

            filter_error = await self._check_text_file_filter(path)
            if filter_error:
                return filter_error

            # 获取行范围参数
            start_line = inputs.get("start_line")
            end_line = inputs.get("end_line")
            tail = inputs.get("tail")

            # tail 模式：用 deque 高效读取最后 N 行，避免全量读取
            if tail and isinstance(tail, int) and tail > 0:
                return await self._read_tail(path, display_path, tail)

            # 行范围模式：只读取指定行范围
            if start_line is not None or end_line is not None:
                return await self._read_line_range(
                    path, display_path, start_line, end_line
                )

            # 全量读取
            content = await asyncio.to_thread(self._read_text_safe, path)
            file_size = path.stat().st_size
            lines = content.count("\n") + (
                1 if content and not content.endswith("\n") else 0
            )

            fields = inputs.get("fields")
            if fields:
                return self._extract_fields(content, path, fields)

            return create_success_result(
                data={
                    "file": display_path,
                    "lines": lines,
                    "size": format_size(file_size),
                    "content": self._add_line_numbers(content),
                },
                metadata={"action": "read_file"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"读取文件失败: {str(e)}",
                error_code="READ_FAILED",
            )

    async def _read_tail(
        self, path: Path, display_path: str, tail: int
    ) -> ToolResult:
        """高效读取文件末尾N行，使用 deque 避免全量加载到内存。

        Args:
            path: 文件路径
            display_path: 显示用路径
            tail: 要读取的末尾行数
        """
        file_size = path.stat().st_size
        # 先获取总行数（用于显示）
        total_lines = 0
        tail_lines: deque[str] = deque(maxlen=tail)

        # 使用 deque 只保留最后 tail 行
        def _scan_tail() -> tuple[int, list[str]]:
            nonlocal total_lines
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    total_lines += 1
                    tail_lines.append(line.rstrip("\n"))
            return total_lines, list(tail_lines)

        total_lines, lines = await asyncio.to_thread(_scan_tail)

        if tail < total_lines:
            content = "\n".join(lines)
            content = self._add_line_numbers(content, start_line=total_lines - tail + 1)
            return create_success_result(
                data={
                    "file": display_path,
                    "total_lines": total_lines,
                    "lines": tail,
                    "size": format_size(file_size),
                    "content": content,
                },
                metadata={"action": "read_file_tail"},
            )

        # 文件总行数 <= tail，返回全部内容
        content = "\n".join(lines)
        content = self._add_line_numbers(content)
        return create_success_result(
            data={
                "file": display_path,
                "total_lines": total_lines,
                "lines": total_lines,
                "size": format_size(file_size),
                "content": content,
            },
            metadata={"action": "read_file_tail"},
        )

    async def _read_line_range(
        self,
        path: Path,
        display_path: str,
        start_line: int | None,
        end_line: int | None,
    ) -> ToolResult:
        """按行范围读取文件，避免全量加载。

        Args:
            path: 文件路径
            display_path: 显示用路径
            start_line: 起始行号（1-based），None表示从第1行
            end_line: 结束行号（1-based，包含），None表示到末尾
        """
        file_size = path.stat().st_size
        start = start_line or 1
        if start < 1:
            return create_failure_result(
                error=f"起始行号不能小于1: {start}",
                error_code="LINE_OUT_OF_RANGE",
            )

        def _read_range() -> tuple[int, list[str]]:
            total = 0
            collected: list[str] = []
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    total += 1
                    if total >= start:
                        collected.append(line.rstrip("\n"))
                    if end_line is not None and total >= end_line:
                        break
            return total, collected

        total_lines, lines = await asyncio.to_thread(_read_range)

        if start > total_lines:
            return create_failure_result(
                error=f"起始行号越界: {start}，文件共 {total_lines} 行",
                error_code="LINE_OUT_OF_RANGE",
            )

        content = "\n".join(lines)
        content = self._add_line_numbers(content, start_line=start)
        return create_success_result(
            data={
                "file": display_path,
                "total_lines": total_lines,
                "lines": len(lines),
                "size": format_size(file_size),
                "content": content,
            },
            metadata={"action": "read_file_range"},
        )

    @staticmethod
    def _read_text_safe(path: Path) -> str:
        """安全读取文本，自动处理编码。"""
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="gbk", errors="ignore")

    def _add_line_numbers(self, content: str, start_line: int = 1) -> str:
        """将文本内容添加 cat -n 风格行号"""
        lines = content.splitlines()
        total = start_line + len(lines) - 1
        width = len(str(total))
        result = []
        for i, line in enumerate(lines):
            line_num = start_line + i
            result.append(f"{line_num:>{width}}\u2192{line}")
        return "\n".join(result)

    async def _check_text_file_filter(self, path: Path) -> ToolResult | None:
        """检查文本文件是否应被过滤（超大文件/二进制内容嗅探）。

        仅对判定为 text 类型的文件调用。
        """
        file_size = path.stat().st_size
        if file_size > MAX_FILE_SIZE:
            return create_failure_result(
                error=f"文件过大 ({format_size(file_size)})，"
                f"超过限制 ({format_size(MAX_FILE_SIZE)}): {path.name}。"
                f"请使用 fields 参数读取特定字段，"
                f"或使用 start_line/end_line/tail 参数分段读取。",
                error_code="FILE_TOO_LARGE",
            )

        try:
            with open(path, "rb") as f:
                header = f.read(BINARY_SNIFF_SIZE)
            if b"\x00" in header:
                return create_failure_result(
                    error=f"检测到二进制文件内容: {path.name}。"
                    f"支持读取文本文件（如 .py, .js, .yaml, "
                    f".json, .md, .txt 等）。",
                    error_code="BINARY_CONTENT_DETECTED",
                )
        except Exception:
            pass

        return None

    def _extract_fields(
        self, content: str, path: Path, fields: list[str]
    ) -> ToolResult:
        suffix = path.suffix.lower()
        data: dict[str, Any] = {}

        try:
            if suffix in [".yaml", ".yml"]:
                data = yaml.safe_load(content) or {}
            elif suffix == ".json":
                data = json.loads(content)
            else:
                return create_failure_result(
                    error=f"fields 参数仅支持 YAML/JSON 文件，"
                    f"当前文件类型: {suffix}",
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
        keys = field.split(".")
        current = data
        for key in keys[:-1]:
            if key not in current:
                current[key] = {}
            current = current[key]
        current[keys[-1]] = value
