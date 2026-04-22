"""
文件写入工具

暴露接口：
- get_tool_definition() -> Tool：get_tool_definition功能
- FileWriteTool：FileWriteTool类
"""

import shutil
import tempfile
from pathlib import Path
from typing import Any

from tools.builtin.base import BuiltinTool
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


class FileWriteTool(BuiltinTool, WorkspaceAwareMixin):
    """
    文件写入工具

    提供文件的创建、写入、编辑功能：
    - write: 全量写入或指定行替换
    - search_replace: 搜索替换内容
    - insert: 在指定行后插入
    - delete_lines: 删除行范围
    - append: 追加到文件末尾
    """

    def __init__(self, base_path: str | None = None):
        """初始化文件写入工具"""
        self.base_path = Path(base_path) if base_path else Path.cwd()

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        from tools.types import ToolLevel

        return Tool(
            name="file_write",
            description="创建、写入、编辑文件内容。支持 write(全量写入/替换行)、search_replace(搜索替换)、insert(插入)、delete_lines(删除行)、append(追加)。"
            "适用场景：创建新文件、全量覆盖写入、修改特定行、搜索替换内容、插入内容、删除行、追加内容。"
            "不适用场景：仅读取文件（使用 file_read）、列出目录（使用 file_read）。"
            "注意：write 会覆盖已有内容；行号从 1 开始；默认创建 .bak 备份；操作具有原子性。",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["write", "search_replace", "insert", "delete_lines", "append"],
                        "description": "编辑操作类型：write(全量写入或替换指定行)、search_replace(搜索并替换文本)、insert(在指定行后插入)、delete_lines(删除行范围)、append(追加到文件末尾)",
                    },
                    "path": {
                        "type": "string",
                        "description": "文件路径（相对路径或绝对路径）",
                    },
                    "content": {
                        "type": "string",
                        "description": "文件内容。用于 write(无行号时全量写入/有行号时替换内容)、insert(插入内容)、append(追加内容)",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号，从 1 开始。用于 write(替换起始行)、delete_lines(删除起始行)",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号，从 1 开始，包含该行。用于 write(替换到结束行)、delete_lines(删除到结束行)",
                    },
                    "line": {
                        "type": "integer",
                        "description": "行号，从 1 开始。用于 insert(在该行后插入内容)，line=0 表示在文件开头插入",
                    },
                    "old_str": {
                        "type": "string",
                        "description": "要搜索并替换的原始文本（search_replace 使用），支持多行文本匹配",
                    },
                    "new_str": {
                        "type": "string",
                        "description": "用于替换的新文本（search_replace 使用）",
                    },
                    "count": {
                        "type": "integer",
                        "description": "最大替换次数（search_replace 使用），0 或省略表示替换所有匹配项",
                        "default": 0,
                    },
                    "create_backup": {
                        "type": "boolean",
                        "description": "是否创建 .bak 备份文件，默认为 true",
                        "default": True,
                    },
                },
                "required": ["action", "path"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.FILE,
            level=ToolLevel.USER,
            tags=["file", "edit", "write", "replace", "insert", "delete"],
            injected_params=["workspace"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        """执行工具"""
        self._init_workspace(inputs)

        action = inputs.get("action")

        if action == "write":
            return await self._write(inputs)
        elif action == "search_replace":
            return await self._search_replace(inputs)
        elif action == "insert":
            return await self._insert(inputs)
        elif action == "delete_lines":
            return await self._delete_lines(inputs)
        elif action == "append":
            return await self._append(inputs)
        else:
            return create_failure_result(
                error=f"不支持的操作: {action}",
                error_code="INVALID_ACTION",
            )

    def _create_backup(self, path: Path) -> Path | None:
        """创建备份文件"""
        if not path.exists():
            return None

        backup_path = Path(str(path) + ".bak")
        shutil.copy2(path, backup_path)
        return backup_path

    def _atomic_write(self, path: Path, content: str) -> None:
        """原子性写入文件"""
        # 确保父目录存在
        path.parent.mkdir(parents=True, exist_ok=True)

        # 创建临时文件
        fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
        try:
            # 写入内容到临时文件
            with open(fd, "w", encoding="utf-8") as f:
                f.write(content)

            # 重命名临时文件为目标文件（原子操作）
            shutil.move(temp_path, path)
        except Exception:
            # 清理临时文件
            try:
                import os

                os.close(fd)
                if Path(temp_path).exists():
                    Path(temp_path).unlink()
            except Exception:
                pass
            raise

    async def _write(self, inputs: dict[str, Any]) -> ToolResult:
        """写入操作"""
        try:
            path_str = inputs.get("path")
            content = inputs.get("content")
            start_line = inputs.get("start_line")
            end_line = inputs.get("end_line")
            create_backup = inputs.get("create_backup", True)

            if not path_str:
                return create_failure_result(
                    error="文件路径不能为空",
                    error_code="MISSING_PATH",
                )

            if content is None:
                return create_failure_result(
                    error="文件内容不能为空",
                    error_code="MISSING_CONTENT",
                )

            path = self.resolve_path(path_str)

            # 无行号参数：全量写入
            if start_line is None and end_line is None:
                backup_path = None
                if create_backup and path.exists():
                    backup_path = self._create_backup(path)

                self._atomic_write(path, content)

                lines_affected = len(content.splitlines()) if content else 0

                return create_success_result(
                    data={
                        "file": path.name,
                        "lines": lines_affected,
                        "backup": backup_path.name if backup_path else None,
                    },
                    metadata={"action": "write"},
                )

            # 有行号参数：需要文件存在
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

            # 读取原文件内容
            try:
                original_content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                original_content = path.read_text(encoding="gbk", errors="ignore")

            lines = original_content.splitlines()
            total_lines = len(lines)

            # 处理行号
            if start_line is not None:
                # 行号从1开始，转换为0索引
                start_idx = start_line - 1
                if start_idx < 0 or start_idx >= total_lines:
                    return create_failure_result(
                        error=f"起始行号越界: {start_line}，文件共 {total_lines} 行",
                        error_code="LINE_OUT_OF_RANGE",
                    )

                if end_line is not None:
                    # 指定了起始和结束行：替换行范围
                    end_idx = end_line - 1
                    if end_idx < 0 or end_idx >= total_lines:
                        return create_failure_result(
                            error=f"结束行号越界: {end_line}，文件共 {total_lines} 行",
                            error_code="LINE_OUT_OF_RANGE",
                        )
                    if end_idx < start_idx:
                        return create_failure_result(
                            error=f"结束行号不能小于起始行号: {end_line} < {start_line}",
                            error_code="INVALID_LINE_RANGE",
                        )

                    # 替换行范围
                    new_lines = lines[:start_idx] + content.splitlines() + lines[end_idx + 1:]
                    lines_affected = end_idx - start_idx + 1
                else:
                    # 只指定了起始行：替换单行
                    new_lines = lines[:start_idx] + content.splitlines() + lines[start_idx + 1:]
                    lines_affected = 1

                # 创建备份
                backup_path = None
                if create_backup:
                    backup_path = self._create_backup(path)

                # 写入新内容
                new_content = "\n".join(new_lines)
                # 保留原文件末尾的换行符
                if original_content.endswith("\n"):
                    new_content += "\n"
                self._atomic_write(path, new_content)

                return create_success_result(
                    data={
                        "file": path.name,
                        "lines": lines_affected,
                        "backup": backup_path.name if backup_path else None,
                    },
                    metadata={"action": "write"},
                )

        except Exception as e:
            return create_failure_result(
                error=f"写入文件失败: {str(e)}",
                error_code="WRITE_FAILED",
            )

    async def _search_replace(self, inputs: dict[str, Any]) -> ToolResult:
        """搜索替换操作"""
        try:
            path_str = inputs.get("path")
            old_str = inputs.get("old_str")
            new_str = inputs.get("new_str")
            count = inputs.get("count", 0)
            create_backup = inputs.get("create_backup", True)

            if not path_str:
                return create_failure_result(
                    error="文件路径不能为空",
                    error_code="MISSING_PATH",
                )

            if old_str is None:
                return create_failure_result(
                    error="搜索文本不能为空",
                    error_code="MISSING_OLD_STR",
                )

            if new_str is None:
                new_str = ""

            path = self.resolve_path(path_str)

            # 文件必须存在
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

            # 读取原文件内容
            try:
                original_content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                original_content = path.read_text(encoding="gbk", errors="ignore")

            # 检查是否包含搜索文本
            if old_str not in original_content:
                return create_failure_result(
                    error=f"未找到匹配文本: {old_str[:50]}..." if len(old_str) > 50 else f"未找到匹配文本: {old_str}",
                    error_code="PATTERN_NOT_FOUND",
                )

            # 执行替换
            if count > 0:
                new_content = original_content.replace(old_str, new_str, count)
                lines_affected = original_content.count(old_str)
                lines_affected = min(lines_affected, count)
            else:
                new_content = original_content.replace(old_str, new_str)
                lines_affected = original_content.count(old_str)

            # 创建备份
            backup_path = None
            if create_backup:
                backup_path = self._create_backup(path)

            # 写入新内容
            self._atomic_write(path, new_content)

            return create_success_result(
                data={
                    "file": path.name,
                    "replacements": lines_affected,
                    "backup": backup_path.name if backup_path else None,
                },
                metadata={"action": "search_replace"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"搜索替换失败: {str(e)}",
                error_code="SEARCH_REPLACE_FAILED",
            )

    async def _insert(self, inputs: dict[str, Any]) -> ToolResult:
        """插入操作"""
        try:
            path_str = inputs.get("path")
            line = inputs.get("line")
            content = inputs.get("content")
            create_backup = inputs.get("create_backup", True)

            if not path_str:
                return create_failure_result(
                    error="文件路径不能为空",
                    error_code="MISSING_PATH",
                )

            if line is None:
                return create_failure_result(
                    error="行号不能为空",
                    error_code="MISSING_LINE",
                )

            if content is None:
                return create_failure_result(
                    error="插入内容不能为空",
                    error_code="MISSING_CONTENT",
                )

            path = self.resolve_path(path_str)

            # 文件必须存在
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

            # 读取原文件内容
            try:
                original_content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                original_content = path.read_text(encoding="gbk", errors="ignore")

            lines = original_content.splitlines()
            total_lines = len(lines)

            # 处理行号（line=0 表示在开头插入）
            if line < 0 or line > total_lines:
                return create_failure_result(
                    error=f"行号越界: {line}，有效范围是 0-{total_lines}",
                    error_code="LINE_OUT_OF_RANGE",
                )

            # 在指定行后插入内容
            insert_lines = content.splitlines()
            new_lines = lines[:line] + insert_lines + lines[line:]

            # 创建备份
            backup_path = None
            if create_backup:
                backup_path = self._create_backup(path)

            # 写入新内容
            new_content = "\n".join(new_lines)
            # 保留原文件末尾的换行符
            if original_content.endswith("\n"):
                new_content += "\n"
            self._atomic_write(path, new_content)

            return create_success_result(
                data={
                    "file": path.name,
                    "inserted_at": line,
                    "lines": len(insert_lines),
                    "backup": backup_path.name if backup_path else None,
                },
                metadata={"action": "insert"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"插入内容失败: {str(e)}",
                error_code="INSERT_FAILED",
            )

    async def _delete_lines(self, inputs: dict[str, Any]) -> ToolResult:
        """删除行操作"""
        try:
            path_str = inputs.get("path")
            start_line = inputs.get("start_line")
            end_line = inputs.get("end_line")
            create_backup = inputs.get("create_backup", True)

            if not path_str:
                return create_failure_result(
                    error="文件路径不能为空",
                    error_code="MISSING_PATH",
                )

            if start_line is None:
                return create_failure_result(
                    error="起始行号不能为空",
                    error_code="MISSING_START_LINE",
                )

            if end_line is None:
                return create_failure_result(
                    error="结束行号不能为空",
                    error_code="MISSING_END_LINE",
                )

            path = self.resolve_path(path_str)

            # 文件必须存在
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

            # 读取原文件内容
            try:
                original_content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                original_content = path.read_text(encoding="gbk", errors="ignore")

            lines = original_content.splitlines()
            total_lines = len(lines)

            # 行号从1开始，转换为0索引
            start_idx = start_line - 1
            end_idx = end_line - 1

            # 检查行号范围
            if start_idx < 0 or start_idx >= total_lines:
                return create_failure_result(
                    error=f"起始行号越界: {start_line}，文件共 {total_lines} 行",
                    error_code="LINE_OUT_OF_RANGE",
                )

            if end_idx < 0 or end_idx >= total_lines:
                return create_failure_result(
                    error=f"结束行号越界: {end_line}，文件共 {total_lines} 行",
                    error_code="LINE_OUT_OF_RANGE",
                )

            if end_idx < start_idx:
                return create_failure_result(
                    error=f"结束行号不能小于起始行号: {end_line} < {start_line}",
                    error_code="INVALID_LINE_RANGE",
                )

            # 删除指定行范围
            new_lines = lines[:start_idx] + lines[end_idx + 1:]
            lines_affected = end_idx - start_idx + 1

            # 创建备份
            backup_path = None
            if create_backup:
                backup_path = self._create_backup(path)

            # 写入新内容
            new_content = "\n".join(new_lines)
            # 保留原文件末尾的换行符
            if original_content.endswith("\n"):
                new_content += "\n"
            self._atomic_write(path, new_content)

            return create_success_result(
                data={
                    "file": path.name,
                    "deleted_lines": f"{start_line}-{end_line}",
                    "count": lines_affected,
                    "backup": backup_path.name if backup_path else None,
                },
                metadata={"action": "delete_lines"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"删除行失败: {str(e)}",
                error_code="DELETE_FAILED",
            )

    async def _append(self, inputs: dict[str, Any]) -> ToolResult:
        """追加操作"""
        try:
            path_str = inputs.get("path")
            content = inputs.get("content")
            create_backup = inputs.get("create_backup", True)

            if not path_str:
                return create_failure_result(
                    error="文件路径不能为空",
                    error_code="MISSING_PATH",
                )

            if content is None:
                return create_failure_result(
                    error="追加内容不能为空",
                    error_code="MISSING_CONTENT",
                )

            path = self.resolve_path(path_str)

            # 如果文件不存在，创建新文件（等同于全量写入）
            if not path.exists():
                backup_path = None
                if create_backup:
                    backup_path = self._create_backup(path)

                self._atomic_write(path, content)

                lines_affected = len(content.splitlines()) if content else 0

                return create_success_result(
                    data={
                        "file": path.name,
                        "lines": lines_affected,
                        "backup": backup_path.name if backup_path else None,
                    },
                    metadata={"action": "append"},
                )

            if not path.is_file():
                return create_failure_result(
                    error=f"路径不是文件: {path}",
                    error_code="NOT_A_FILE",
                )

            # 读取原文件内容
            try:
                original_content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                original_content = path.read_text(encoding="gbk", errors="ignore")

            # 创建备份
            backup_path = None
            if create_backup:
                backup_path = self._create_backup(path)

            # 追加内容（确保有换行符分隔）
            if original_content and not original_content.endswith("\n"):
                new_content = original_content + "\n" + content
            else:
                new_content = original_content + content

            # 写入新内容
            self._atomic_write(path, new_content)

            lines_affected = len(content.splitlines()) if content else 0

            return create_success_result(
                data={
                    "file": path.name,
                    "lines": lines_affected,
                    "backup": backup_path.name if backup_path else None,
                },
                metadata={"action": "append"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"追加内容失败: {str(e)}",
                error_code="APPEND_FAILED",
            )
