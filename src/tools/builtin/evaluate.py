"""
评估工具

暴露接口：
- get_tool_definition() -> Tool：get_tool_definition功能
- EvaluateTool：EvaluateTool类
"""

import json
from typing import Any

import jsonschema

from core.results import ToolExecutionResult
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class EvaluateTool:
    """
    评估工具

    提供：
    - Schema 验证
    - 格式检查
    - 非空检查
    - 数据完整性验证
    """

    def __init__(self):
        """初始化评估工具"""

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="evaluate",
            description="评估工具：提供Schema验证、格式检查、非空检查、JSON验证和自定义验证功能。支持操作：validate_schema(验证JSON Schema)、check_format(检查字符串格式如邮箱/URL/日期)、check_not_empty(验证必填字段非空)、validate_json(验证JSON格式)、custom_validation(自定义规则验证)。Schema验证使用jsonschema库，需符合JSON Schema Draft 7规范。",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "validate_schema",
                            "check_format",
                            "check_not_empty",
                            "validate_json",
                            "custom_validation",
                        ],
                        "description": "评估操作类型：validate_schema-验证JSON Schema，check_format-检查字符串格式，check_not_empty-验证必填字段非空，validate_json-验证JSON格式，custom_validation-自定义规则验证",
                    },
                    "data": {
                        "description": "要验证的数据，可以是任意类型",
                    },
                    "schema": {
                        "type": "object",
                        "description": "JSON Schema定义，validate_schema操作时必填，需符合JSON Schema Draft 7规范",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["json", "email", "url", "date", "datetime"],
                        "description": "期望的格式类型：json-JSON格式，email-邮箱格式，url-URL格式，date-日期格式(YYYY-MM-DD)，datetime-日期时间格式(YYYY-MM-DD HH:MM:SS)",
                    },
                    "field_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要检查非空的字段名列表，check_not_empty操作时使用，为空则检查所有字段",
                    },
                    "rules": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string", "description": "要验证的字段名"},
                                "rule": {"type": "string", "description": "规则类型：required(必填)、type(类型)、min_length(最小长度)、max_length(最大长度)、range(范围)、regex(正则)、enum(枚举)"},
                                "value": {"description": "规则值，根据rule类型不同而不同"},
                            },
                        },
                        "description": "自定义验证规则列表，custom_validation操作时使用，支持required、type、min_length、max_length、range、regex、enum等规则",
                    },
                },
                "required": ["action", "data"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.EVALUATION,
            level=ToolLevel.SYSTEM,
            tags=["validation", "evaluation", "schema"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行工具"""
        action = inputs.get("action")
        inputs.get("data")

        if action == "validate_schema":
            return await self._validate_schema(inputs)
        elif action == "check_format":
            return await self._check_format(inputs)
        elif action == "check_not_empty":
            return await self._check_not_empty(inputs)
        elif action == "validate_json":
            return await self._validate_json(inputs)
        elif action == "custom_validation":
            return await self._custom_validation(inputs)
        else:
            return create_failure_result(
                error=f"不支持的操作: {action}",
                error_code="INVALID_ACTION",
            )

    async def _validate_schema(self, inputs: dict[str, Any]) -> ToolResult:
        """验证 JSON Schema"""
        try:
            data = inputs.get("data")
            schema = inputs.get("schema")

            if schema is None:
                return create_failure_result(
                    error="Schema 不能为空",
                    error_code="MISSING_SCHEMA",
                )

            # 执行验证
            try:
                jsonschema.validate(instance=data, schema=schema)
            except jsonschema.ValidationError as e:
                return create_success_result(
                    data={
                        "valid": False,
                        "errors": [
                            {
                                "path": ".".join(str(p) for p in e.path),
                                "message": e.message,
                                "validator": e.validator,
                            }
                        ],
                    },
                    metadata={"action": "validate_schema"},
                )

            return create_success_result(
                data={
                    "valid": True,
                    "errors": [],
                },
                metadata={"action": "validate_schema"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"Schema 验证失败: {str(e)}",
                error_code="VALIDATION_FAILED",
            )

    async def _check_format(self, inputs: dict[str, Any]) -> ToolResult:
        """检查格式"""
        try:
            data = inputs.get("data")
            format_type = inputs.get("format")

            if format_type is None:
                return create_failure_result(
                    error="格式类型不能为空",
                    error_code="MISSING_FORMAT",
                )

            # 检查格式
            is_valid, error_msg = self._validate_format(data, format_type)

            return create_success_result(
                data={
                    "valid": is_valid,
                    "format": format_type,
                    "value": data,
                    "error": error_msg if not is_valid else None,
                },
                metadata={"action": "check_format"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"格式检查失败: {str(e)}",
                error_code="CHECK_FAILED",
            )

    def _validate_format(self, value: Any, format_type: str) -> tuple[bool, str | None]:
        """验证单个值格式"""
        import re
        from datetime import datetime

        if value is None or value == "":
            return False, "值为空"

        if format_type == "json":
            try:
                if isinstance(value, str):
                    json.loads(value)
                elif isinstance(value, (dict, list)):
                    json.dumps(value)
                else:
                    return False, "不是有效的 JSON 类型"
                return True, None
            except json.JSONDecodeError as e:
                return False, f"JSON 解析失败: {str(e)}"

        elif format_type == "email":
            if not isinstance(value, str):
                return False, "不是字符串"
            pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
            if re.match(pattern, value):
                return True, None
            return False, "邮箱格式不正确"

        elif format_type == "url":
            if not isinstance(value, str):
                return False, "不是字符串"
            pattern = r"^https?://[^\s/$.?#].[^\s]*$"
            if re.match(pattern, value):
                return True, None
            return False, "URL 格式不正确"

        elif format_type == "date":
            if not isinstance(value, str):
                return False, "不是字符串"
            try:
                datetime.strptime(value, "%Y-%m-%d")
                return True, None
            except ValueError:
                return False, "日期格式不正确（应为 YYYY-MM-DD）"

        elif format_type == "datetime":
            if not isinstance(value, str):
                return False, "不是字符串"
            try:
                datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
                return True, None
            except ValueError:
                return False, "日期时间格式不正确（应为 YYYY-MM-DD HH:MM:SS）"

        else:
            return False, f"不支持的格式类型: {format_type}"

    async def _check_not_empty(self, inputs: dict[str, Any]) -> ToolResult:
        """检查字段非空"""
        try:
            data = inputs.get("data")
            field_names = inputs.get("field_names", [])

            if not isinstance(data, dict):
                return create_failure_result(
                    error="数据必须是字典类型",
                    error_code="INVALID_DATA_TYPE",
                )

            if not field_names:
                # 检查所有字段
                field_names = list(data.keys())

            empty_fields = []
            valid_fields = []

            for field in field_names:
                value = data.get(field)

                if (
                    value is None
                    or value == ""
                    or (isinstance(value, list) and len(value) == 0)
                ):
                    empty_fields.append(field)
                else:
                    valid_fields.append(field)

            is_valid = len(empty_fields) == 0

            return create_success_result(
                data={
                    "valid": is_valid,
                    "empty_fields": empty_fields,
                    "valid_fields": valid_fields,
                    "total_fields": len(field_names),
                },
                metadata={"action": "check_not_empty"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"非空检查失败: {str(e)}",
                error_code="CHECK_FAILED",
            )

    async def _validate_json(self, inputs: dict[str, Any]) -> ToolResult:
        """验证 JSON 格式"""
        try:
            data = inputs.get("data")

            if isinstance(data, str):
                try:
                    parsed = json.loads(data)
                    is_valid = True
                    error_msg = None
                    parsed_data = parsed
                except json.JSONDecodeError as e:
                    is_valid = False
                    error_msg = str(e)
                    parsed_data = None
            elif isinstance(data, (dict, list)):
                try:
                    # 尝试序列化
                    json.dumps(data)
                    is_valid = True
                    error_msg = None
                    parsed_data = data
                except TypeError as e:
                    is_valid = False
                    error_msg = str(e)
                    parsed_data = None
            else:
                is_valid = False
                error_msg = f"不支持的类型: {type(data)}"
                parsed_data = None

            return create_success_result(
                data={
                    "valid": is_valid,
                    "error": error_msg,
                    "data": parsed_data,
                },
                metadata={"action": "validate_json"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"JSON 验证失败: {str(e)}",
                error_code="VALIDATION_FAILED",
            )

    async def _custom_validation(self, inputs: dict[str, Any]) -> ToolResult:
        """自定义验证"""
        try:
            data = inputs.get("data")
            rules = inputs.get("rules", [])

            if not isinstance(data, dict):
                return create_failure_result(
                    error="数据必须是字典类型",
                    error_code="INVALID_DATA_TYPE",
                )

            errors = []
            passed = []

            for rule_item in rules:
                field = rule_item.get("field")
                rule_type = rule_item.get("rule")
                rule_value = rule_item.get("value")

                if not field or not rule_type:
                    continue

                field_value = data.get(field)

                # 执行规则验证
                is_valid, error_msg = self._execute_rule(
                    field_value, rule_type, rule_value
                )

                if is_valid:
                    passed.append(
                        {
                            "field": field,
                            "rule": rule_type,
                        }
                    )
                else:
                    errors.append(
                        {
                            "field": field,
                            "rule": rule_type,
                            "error": error_msg,
                        }
                    )

            is_valid = len(errors) == 0

            return create_success_result(
                data={
                    "valid": is_valid,
                    "passed": passed,
                    "errors": errors,
                    "total_rules": len(rules),
                },
                metadata={"action": "custom_validation"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"自定义验证失败: {str(e)}",
                error_code="VALIDATION_FAILED",
            )

    def _execute_rule(
        self,
        value: Any,
        rule_type: str,
        rule_value: Any,
    ) -> tuple[bool, str | None]:
        """执行单个验证规则"""
        try:
            if rule_type == "required":
                if value is None or value == "":
                    return False, "字段必填"
                return True, None

            elif rule_type == "type":
                expected_type = rule_value
                if not isinstance(value, expected_type):
                    return False, f"类型应为 {expected_type.__name__}"
                return True, None

            elif rule_type == "min_length":
                if not isinstance(value, (str, list)):
                    return False, "不支持的类型"
                if len(value) < rule_value:
                    return False, f"长度应大于等于 {rule_value}"
                return True, None

            elif rule_type == "max_length":
                if not isinstance(value, (str, list)):
                    return False, "不支持的类型"
                if len(value) > rule_value:
                    return False, f"长度应小于等于 {rule_value}"
                return True, None

            elif rule_type == "range":
                if not isinstance(value, (int, float)):
                    return False, "不支持的类型"
                min_val, max_val = rule_value
                if not (min_val <= value <= max_val):
                    return False, f"值应在 [{min_val}, {max_val}] 范围内"
                return True, None

            elif rule_type == "regex":
                import re

                if not isinstance(value, str):
                    return False, "不支持的类型"
                if not re.match(rule_value, value):
                    return False, f"不匹配正则表达式: {rule_value}"
                return True, None

            elif rule_type == "enum":
                if value not in rule_value:
                    return False, f"值应为以下之一: {rule_value}"
                return True, None

            else:
                return False, f"不支持的规则类型: {rule_type}"

        except Exception as e:
            return False, f"规则执行失败: {str(e)}"
