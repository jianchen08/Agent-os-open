"""
Schema 评估器

暴露接口：
- get_tool_definition() -> Tool：get_tool_definition功能
- SchemaEvaluator：SchemaEvaluator类
"""

import json
import re
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from tools.types import (
    Tool,
    ToolCategory,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class SchemaEvaluator:
    """Schema 评估器"""

    # 文件扩展名 → 格式类型映射
    _EXT_FORMAT_MAP: dict[str, str] = {
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".md": "regex",
        ".markdown": "regex",
        ".txt": "regex",
        ".toml": "yaml",
        ".xml": "regex",
        ".csv": "regex",
        ".html": "regex",
        ".css": "regex",
        ".py": "regex",
        ".js": "regex",
        ".ts": "regex",
        ".go": "regex",
        ".rs": "regex",
        ".java": "regex",
    }

    @staticmethod
    def get_tool_definition() -> Tool:
        return Tool(
            name="schema_evaluator",
            description="Schema 评估器：验证 JSON/YAML/Schema 格式或使用正则模式校验文本",
            input_schema={
                "type": "object",
                "properties": {
                    "data": {"description": "要验证的数据"},
                    "path": {"type": "string", "description": "或指定文件路径"},
                    "format": {
                        "type": "string",
                        "enum": ["auto", "json", "yaml", "schema", "regex"],
                        "default": "auto",
                        "description": "验证格式类型：auto(自动检测)/json/yaml/schema/regex",
                    },
                    "schema": {"type": "object", "description": "JSON Schema"},
                    "patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "正则表达式列表（format=regex 时使用）",
                    },
                    "pattern_mode": {
                        "type": "string",
                        "enum": ["all", "any"],
                        "default": "all",
                        "description": "patterns 匹配模式：all(全部匹配) / any(任一匹配)",
                    },
                },
            },
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            tags=["evaluator", "schema", "json", "yaml", "regex"],
        )

    def _detect_format(self, path: str | None, data: Any) -> str:
        """根据文件扩展名或数据类型自动检测格式。

        auto 格式类型按文件扩展名映射校验格式（有 path 时），
        或按数据类型推断（无 path 时），避免所有文件都以 JSON 格式校验。

        Args:
            path: 文件路径（可能为 None）
            data: 数据内容

        Returns:
            检测到的格式类型: json/yaml/regex
        """
        if path:
            ext = Path(path).suffix.lower()
            detected = self._EXT_FORMAT_MAP.get(ext)
            if detected:
                return detected

        # 无 path 或扩展名未识别时，按数据类型推断
        if isinstance(data, (dict, list)):
            return "json"
        if isinstance(data, str):
            # 尝试 JSON 解析，成功则视为 JSON
            try:
                json.loads(data)
                return "json"
            except (json.JSONDecodeError, ValueError):
                pass
            # YAML 内容检测：仅当文本包含 key: value 结构时才判定为 yaml
            # （yaml.safe_load 过于宽松，纯文本也会解析成功）
            if re.search(r"^\s*\w[\w.-]*\s*:\s*.+", data, re.MULTILINE):
                return "yaml"

        return "regex"

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:  # noqa: PLR0911
        """执行格式验证"""
        data = inputs.get("data")
        path = inputs.get("path")
        workspace = inputs.get("workspace")
        format_type = inputs.get("format", "auto")
        schema = inputs.get("schema")
        patterns = inputs.get("patterns", [])
        pattern_mode = inputs.get("pattern_mode", "all")

        # 从文件读取数据
        if path and not data:
            try:
                p = Path(path)
                if not p.is_absolute() and workspace:
                    p = Path(workspace) / p
                file_path = p.resolve()
                if not file_path.exists():
                    return create_success_result(
                        data={
                            "passed": False,
                            "score": 0,
                            "feedback": f"文件不存在: {path}",
                        }
                    )
                data = file_path.read_text(encoding="utf-8")
            except Exception as e:
                return create_failure_result(error=f"读取文件失败: {str(e)}")

        if data is None:
            return create_failure_result(error="数据不能为空")

        # auto 格式：根据文件扩展名或数据类型自动检测
        if format_type == "auto":
            format_type = self._detect_format(path, data)

        try:
            # 如果提供了 schema，优先进行 schema 验证
            if schema is not None:
                return await self._validate_schema(data, schema)
            if format_type == "json":
                return await self._validate_json(data)
            if format_type == "yaml":
                return await self._validate_yaml(data)
            if format_type == "schema":
                return await self._validate_schema(data, schema)
            if format_type == "regex":
                return await self._validate_regex(data, patterns, pattern_mode)
            return create_failure_result(error=f"不支持的格式: {format_type}")
        except Exception as e:
            return create_failure_result(error=f"验证失败: {str(e)}")

    async def _validate_json(self, data: Any) -> ToolResult:
        """验证 JSON 格式"""
        try:
            if isinstance(data, str):
                json.loads(data)
            elif isinstance(data, (dict, list)):
                json.dumps(data)
            else:
                return create_success_result(
                    data={
                        "passed": False,
                        "score": 0,
                        "feedback": f"不是有效的 JSON 类型: {type(data).__name__}",
                    }
                )

            return create_success_result(data={"passed": True, "score": 100, "feedback": "JSON 格式有效"})
        except json.JSONDecodeError as e:
            return create_success_result(
                data={
                    "passed": False,
                    "score": 0,
                    "feedback": f"JSON 格式无效: {str(e)}",
                    "details": {"error": str(e)},
                }
            )

    async def _validate_yaml(self, data: Any) -> ToolResult:
        """验证 YAML 格式"""
        try:
            if isinstance(data, str):
                yaml.safe_load(data)
            else:
                yaml.safe_dump(data)

            return create_success_result(data={"passed": True, "score": 100, "feedback": "YAML 格式有效"})
        except yaml.YAMLError as e:
            return create_success_result(
                data={
                    "passed": False,
                    "score": 0,
                    "feedback": f"YAML 格式无效: {str(e)}",
                    "details": {"error": str(e)},
                }
            )

    async def _validate_schema(self, data: Any, schema: dict) -> ToolResult:
        """验证 JSON Schema"""
        if not schema:
            return create_failure_result(error="Schema 不能为空")

        # 如果 data 是字符串，先解析
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                try:
                    data = yaml.safe_load(data)
                except yaml.YAMLError:
                    return create_success_result(data={"passed": False, "score": 0, "feedback": "数据格式无效"})

        try:
            jsonschema.validate(instance=data, schema=schema)
            return create_success_result(data={"passed": True, "score": 100, "feedback": "数据符合 Schema"})
        except jsonschema.ValidationError as e:
            return create_success_result(
                data={
                    "passed": False,
                    "score": 0,
                    "feedback": f"Schema 验证失败: {e.message}",
                    "details": {
                        "path": list(e.path),
                        "message": e.message,
                        "validator": e.validator,
                    },
                }
            )

    async def _validate_regex(self, data: str, patterns: list[str], pattern_mode: str) -> ToolResult:
        """使用正则表达式验证文本"""
        if not patterns:
            return create_success_result(
                data={
                    "passed": False,
                    "score": 0,
                    "feedback": "未提供正则表达式 patterns",
                    "details": {"error": "patterns is empty"},
                }
            )

        if not isinstance(data, str):
            return create_success_result(
                data={
                    "passed": False,
                    "score": 0,
                    "feedback": f"正则验证需要字符串类型，实际类型: {type(data).__name__}",
                }
            )

        matched = []
        unmatched = []

        for pattern in patterns:
            try:
                flags = re.MULTILINE | re.IGNORECASE
                if re.search(pattern, data, flags):
                    matched.append(pattern)
                else:
                    unmatched.append(pattern)
            except re.error as e:
                return create_success_result(
                    data={
                        "passed": False,
                        "score": 0,
                        "feedback": f"正则表达式错误: {pattern}",
                        "details": {"error": str(e), "pattern": pattern},
                    }
                )

        all_matched = len(unmatched) == 0
        any_matched = len(matched) > 0

        if pattern_mode == "all":
            passed = all_matched
            matched_count = len(matched)
            total_count = len(patterns)
        else:  # any
            passed = any_matched
            matched_count = len(matched)
            total_count = len(patterns)

        score = int((matched_count / total_count) * 100) if total_count > 0 else 0

        if passed:
            feedback = f"正则验证通过（{matched_count}/{total_count} 匹配）"
            if unmatched:
                feedback += f"，未匹配: {unmatched}"
        else:
            feedback = f"正则验证未通过（{matched_count}/{total_count} 匹配）"
            if unmatched:
                feedback += f"，未匹配模式: {unmatched}"

        return create_success_result(
            data={
                "passed": passed,
                "score": score,
                "feedback": feedback,
                "details": {
                    "matched_patterns": matched,
                    "unmatched_patterns": unmatched,
                    "pattern_mode": pattern_mode,
                    "total_patterns": total_count,
                    "matched_count": matched_count,
                },
            }
        )
