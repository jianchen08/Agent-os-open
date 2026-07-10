"""
文件读取工具

暴露接口：
- get_tool_definition() -> Tool：get_tool_definition功能
- FileReadTool：FileReadTool类
"""

import asyncio
import json
import re
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


def _try_match_value(actual_val: Any, filter_val: str) -> bool:
    """尝试将筛选字符串值与实际值匹配，支持数字自动转换。

    比较策略：
    1. 字符串直接比较
    2. 若实际值为 int/float，尝试将筛选值转为数字后比较
    3. 若实际值为 bool，转换为布尔值比较

    Args:
        actual_val: 列表项中的实际值
        filter_val: 筛选条件中的字符串值

    Returns:
        是否匹配
    """
    # 字符串直接比较
    if str(actual_val) == filter_val:
        return True

    # 数字类型自动转换比较
    if isinstance(actual_val, bool):
        # bool 必须在 int 之前判断，因为 bool 是 int 的子类
        return filter_val.lower() in ("true", "1") if actual_val else filter_val.lower() in ("false", "0")

    if isinstance(actual_val, int):
        try:
            return actual_val == int(filter_val)
        except (ValueError, TypeError):
            pass

    if isinstance(actual_val, float):
        try:
            return actual_val == float(filter_val)
        except (ValueError, TypeError):
            pass

    return False


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
        from tools.types import ToolLevel  # noqa: PLC0415

        return Tool(
            name="file_read",
            description="读取文件内容。自动识别文本和二进制文件：文本文件直接读取，"
            "PDF/DOCX/XLSX/PPTX/图片等通过 markitdown 转换为 Markdown。"
            "适用场景：需要读取文件内容。"
            "不适用场景：需要写入文件（使用 file_write）、列出目录（使用 list_directory）、"
            "搜索文件内容（使用 enhanced_search）。"
            "fields 参数：读取 YAML/JSON 文件的特定字段，节省 token。"
            "例如：fields=['id', 'name'] 只返回这两个字段。"
            "支持列表筛选：fields=['records{type=error}'] 从列表中按条件筛选。",
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
                        "例如：['id', 'name']。支持嵌套字段，用点号分隔，"
                        "如 'summary.total_tokens'。"
                        "支持列表筛选语法：'records{record_id=xxx}' 按条件从列表中筛选，"
                        "筛选后可接 '.field' 取子字段，如 'records{iteration=13}.thinking_content'。"
                        "多条匹配返回列表，单条匹配返回对象。"
                        "不指定则返回完整内容。",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号（从1开始），仅读取指定行范围。不指定则从第1行开始。",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号（从1开始，包含该行），仅读取指定行范围。不指定则到文件末尾。",
                    },
                    "tail": {
                        "type": "integer",
                        "description": "仅读取文件最后 N 行（仅文本文件有效）。不指定则返回完整内容。",
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
            results.append(
                {
                    "path": path_str,
                    "success": result.success,
                    "data": result.output if result.success else None,
                    "error": result.error if not result.success else None,
                }
            )

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

    async def _read_file(self, inputs: dict[str, Any]) -> ToolResult:  # noqa: PLR0911
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
                return await self._read_line_range(path, display_path, start_line, end_line)

            # 全量读取
            content = await asyncio.to_thread(self._read_text_safe, path)
            file_size = path.stat().st_size
            lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)

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

    async def _read_tail(self, path: Path, display_path: str, tail: int) -> ToolResult:
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
            with open(path, encoding="utf-8", errors="replace") as f:
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
            with open(path, encoding="utf-8", errors="replace") as f:
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

    def _extract_fields(self, content: str, path: Path, fields: list[str]) -> ToolResult:
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

    @staticmethod
    def _parse_field_path(field: str) -> list[tuple]:
        """将字段路径字符串解析为操作序列。

        支持两种路径段：
        - 普通键访问：'key' → ("key", "key")
        - 列表筛选：'key{filter_key=filter_val}' → ("filter", "key", "filter_key", "filter_val")

        示例：
            'records{record_id=abc}.thinking_content' 解析为：
            [("filter", "records", "record_id", "abc"), ("key", "thinking_content")]

            'summary.total' 解析为：
            [("key", "summary"), ("key", "total")]

        Args:
            field: 字段路径字符串，支持点号分隔和 {key=value} 筛选语法

        Returns:
            操作序列列表，每个元素为元组：
            - ("key", key_name) 表示普通键访问
            - ("filter", list_key, filter_key, filter_val) 表示列表筛选
        """
        # 匹配两种模式：key{filter} 或 纯 key
        pattern = r"([^.{}]+)\{([^}=]+)=([^}]+)\}|([^.{}]+)"
        segments: list[tuple] = []
        for match in re.finditer(pattern, field):
            if match.group(1):
                # key{filter_key=filter_val} 模式
                list_key = match.group(1)
                filter_key = match.group(2)
                filter_val = match.group(3)
                segments.append(("filter", list_key, filter_key, filter_val))
            elif match.group(4):
                # 纯 key 模式
                segments.append(("key", match.group(4)))
        return segments

    @staticmethod
    def _resolve_segment(current: Any, seg: tuple) -> Any:  # noqa: PLR0911,PLR0912
        """执行单个路径段操作，支持键访问和列表筛选。

        对于 filter 段，在列表中按 key=value 筛选：
        - 筛选值会尝试与列表项中的实际值进行类型匹配（数字自动转换）
        - 多条匹配返回列表，单条匹配返回对象，无匹配返回 None

        Args:
            current: 当前数据对象（dict / list / 其他）
            seg: 路径段元组，来自 _parse_field_path 的输出

        Returns:
            解析后的值：普通访问返回对应值，筛选返回匹配项（对象或列表）
        """
        if seg[0] == "key":
            # 普通键访问：从字典中取值
            key = seg[1]
            if isinstance(current, dict):
                return current.get(key)
            # 当 current 是列表时（上一段筛选返回了多条），对每个元素取子字段
            if isinstance(current, list):
                results = []
                for item in current:
                    if isinstance(item, dict) and key in item:
                        results.append(item[key])
                return results if results else None
            return None

        if seg[0] == "filter":
            # 列表筛选：在指定列表中按 key=value 过滤
            _, list_key, filter_key, filter_val = seg

            # 先获取列表
            if isinstance(current, dict):
                target_list = current.get(list_key)
            else:
                return None

            if not isinstance(target_list, list):
                return None

            # 在列表中筛选匹配项，支持数字值自动转换
            matched = []
            for item in target_list:
                if not isinstance(item, dict):
                    continue
                actual_val = item.get(filter_key)
                if actual_val is None:
                    continue
                # 尝试将筛选值转换为与实际值相同的类型进行比较
                if _try_match_value(actual_val, filter_val):
                    matched.append(item)

            if len(matched) == 0:
                return None
            if len(matched) == 1:
                return matched[0]
            return matched

        return None

    def _get_nested_field(self, data: dict[str, Any], field: str) -> Any:
        """根据字段路径从嵌套数据中获取值。

        支持两种语法：
        - 普通点号分隔：'summary.total_tokens' → 逐层字典访问
        - 列表筛选：'records{record_id=abc}.thinking' → 先按条件筛选列表再取子字段

        不含 {} 的路径走原有简单逻辑，含 {} 的路径走新的解析逻辑。

        Args:
            data: 解析后的 YAML/JSON 数据字典
            field: 字段路径字符串

        Returns:
            字段对应的值，未找到返回 None
        """
        # 不含筛选语法 {} 时，走原有简单逻辑，保证向后兼容
        if "{" not in field:
            keys = field.split(".")
            current: Any = data
            for key in keys:
                if isinstance(current, dict) and key in current:
                    current = current[key]
                else:
                    return None
            return current

        # 含筛选语法 {} 时，走新的路径解析逻辑
        segments = self._parse_field_path(field)
        current: Any = data
        for seg in segments:
            current = self._resolve_segment(current, seg)
            if current is None:
                return None
        return current

    def _set_nested_field(self, data: dict[str, Any], field: str, value: Any) -> None:
        """将值设置到结果字典中，使用原始字段路径作为键。

        直接用原始 field 字符串作为扁平键存储，避免列表筛选路径无法还原为嵌套结构的问题。

        Args:
            data: 结果字典
            field: 字段路径字符串（作为存储键）
            value: 要存储的值
        """
        data[field] = value
