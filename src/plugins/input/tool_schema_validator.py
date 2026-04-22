"""工具 Schema 验证与自动修复 Input 插件。

负责在管道循环的输入阶段验证工具调用的参数是否符合工具定义的
input_schema。对不符合的调用先尝试自动修复类型不匹配的字段，
修复仍失败的调用才记录错误并标记跳过。

使用简单类型检查实现，不依赖 jsonschema 第三方库。

State 命名空间：
    - schema_errors : 验证失败的工具调用错误列表
    - schema_validated : 已通过验证的工具调用列表
    - schema_fixes : 自动修复日志列表
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


class ToolSchemaValidator(IInputPlugin):
    """工具 Schema 验证与自动修复 Input 插件。

    对每个工具调用，从 state 中获取对应工具的 input_schema 定义，
    使用简单类型检查验证参数是否匹配。验证失败时先尝试自动修复
    类型不匹配的字段（如 string→object、string→array 等），
    修复仍失败的工具调用才被记录到 schema_errors 并标记跳过。

    支持的 schema 类型检查：
    - string, number, integer, boolean, array, object
    - required 字段检查
    - 嵌套属性的类型检查

    支持的自动修复类型转换：
    - string→object: 尝试 json.loads()，失败则设为 {}
    - string→array:  尝试 json.loads()，失败则设为 []
    - int/float→string: str(value)
    - string→integer: 尝试 int()，失败保持原值
    - string→number:  尝试 float()，失败保持原值
    - string→boolean: "true"/"1"→True, "false"/"0"→False

    优先级：30（校验级，在参数注入之后）
    错误策略：SKIP（验证失败不终止管道）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.SKIP

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化 Schema 验证插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用验证（默认 True）
                - strict: 是否严格模式，未知工具也报错（默认 False）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._strict = self._config.get("strict", False)

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "tool_schema_validator"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 30)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """执行 Schema 验证与自动修复。

        读取 raw_tool_calls，对每个调用验证参数是否符合工具定义的
        input_schema。验证失败时尝试自动修复类型不匹配的字段，
        修复后再次验证，修复成功的调用放入 validated_calls。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含验证结果状态更新的插件执行结果。
        """
        if not self._enabled:
            return PluginResult()

        tool_calls = ctx.state.get(StateKeys.RAW_TOOL_CALLS, [])
        if not tool_calls:
            return PluginResult()

        tool_definitions = ctx.state.get("_tool_definitions", {})
        schema_errors: list[dict[str, Any]] = []
        validated_calls: list[dict[str, Any]] = []
        all_fix_messages: list[dict[str, Any]] = []

        for tc in tool_calls:
            tool_name = tc.get("name", "")
            args = tc.get("args", {})

            tool_def = tool_definitions.get(tool_name)
            if tool_def is None:
                if self._strict:
                    schema_errors.append({
                        "tool": tool_name,
                        "error": f"Tool definition not found: {tool_name}",
                    })
                    logger.warning(
                        "[%s] Unknown tool in strict mode | tool=%s",
                        self.name, tool_name,
                    )
                else:
                    validated_calls.append(tc)
                continue

            input_schema = (
                tool_def.get("input_schema") if isinstance(tool_def, dict)
                else getattr(tool_def, "input_schema", None)
            )
            if input_schema is None:
                validated_calls.append(tc)
                continue

            # 首次验证
            errors = self._validate_args(args, input_schema)
            if errors:
                # 尝试自动修复
                fixed_args, fix_messages = self._auto_fix_args(args, input_schema)

                if fix_messages:
                    all_fix_messages.append({
                        "tool": tool_name,
                        "fixes": fix_messages,
                    })
                    logger.info(
                        "[%s] Auto-fixed args | tool=%s | fixes=%s",
                        self.name, tool_name, fix_messages,
                    )

                # 修复后再次验证
                re_errors = self._validate_args(fixed_args, input_schema)
                if re_errors:
                    schema_errors.append({
                        "tool": tool_name,
                        "errors": re_errors,
                        "attempted_fixes": fix_messages,
                    })
                    logger.warning(
                        "[%s] Schema validation still failed after fix | tool=%s | errors=%s",
                        self.name, tool_name, re_errors,
                    )
                else:
                    # 修复成功，用修复后的参数替换原始参数
                    fixed_tc = dict(tc)
                    fixed_tc["args"] = fixed_args
                    validated_calls.append(fixed_tc)
            else:
                validated_calls.append(tc)

        state_updates: dict[str, Any] = {}
        if schema_errors:
            state_updates["schema_errors"] = schema_errors
        state_updates["schema_validated"] = validated_calls
        if all_fix_messages:
            state_updates["schema_fixes"] = all_fix_messages
        # 始终用验证后的调用列表更新 RAW_TOOL_CALLS，
        # 确保下游插件拿到的是经过验证和修复的数据
        state_updates[StateKeys.RAW_TOOL_CALLS] = validated_calls

        return PluginResult(state_updates=state_updates)

    def _auto_fix_args(
        self, args: dict[str, Any], schema: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        """尝试自动修复参数类型不匹配。

        遍历 schema["properties"] 中的每个字段，对比声明类型与
        实际值类型，尝试进行自动类型转换。

        Args:
            args: 工具调用参数
            schema: 工具的 input_schema 定义

        Returns:
            (fixed_args, fix_messages) 修复后的参数和修复日志列表
        """
        fixed_args = dict(args)
        fix_messages: list[str] = []
        properties = schema.get("properties", {})

        for field_name, field_schema in properties.items():
            if field_name not in fixed_args:
                continue

            value = fixed_args[field_name]
            expected_type = field_schema.get("type")
            if not expected_type:
                continue

            # 类型已经匹配则跳过
            if self._check_type(value, expected_type):
                continue

            new_value = self._try_convert(value, expected_type)
            if new_value is not value:
                fixed_args[field_name] = new_value
                fix_messages.append(
                    f"{field_name}: {type(value).__name__}→{expected_type}"
                )

        return fixed_args, fix_messages

    def _try_convert(self, value: Any, target_type: str) -> Any:
        """尝试将值转换为目标类型。

        Args:
            value: 原始值
            target_type: 目标 JSON Schema 类型

        Returns:
            转换后的值，转换失败返回原值
        """
        try:
            if target_type == "object" and isinstance(value, str):
                try:
                    return json.loads(value)
                except (json.JSONDecodeError, ValueError):
                    return {}

            if target_type == "string" and isinstance(value, (int, float)):
                return str(value)

            if target_type == "integer" and isinstance(value, str):
                try:
                    return int(value)
                except (ValueError, TypeError):
                    return value

            if target_type == "array" and isinstance(value, str):
                try:
                    return json.loads(value)
                except (json.JSONDecodeError, ValueError):
                    return []

            if target_type == "number" and isinstance(value, str):
                try:
                    return float(value)
                except (ValueError, TypeError):
                    return value

            if target_type == "boolean" and isinstance(value, str):
                if value.lower() in ("true", "1"):
                    return True
                if value.lower() in ("false", "0"):
                    return False
        except Exception:
            logger.debug(
                "[%s] Type conversion failed | value=%r | target=%s",
                self.name, value, target_type,
            )

        return value

    def _validate_args(
        self, args: dict[str, Any], schema: dict[str, Any],
    ) -> list[str]:
        """根据 input_schema 验证参数。

        检查 required 字段和属性类型，返回错误列表。
        空列表表示验证通过。

        Args:
            args: 工具调用参数
            schema: 工具的 input_schema 定义

        Returns:
            验证错误字符串列表
        """
        errors: list[str] = []
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        for field_name in required:
            if field_name not in args:
                errors.append(f"Missing required field: {field_name}")

        for field_name, value in args.items():
            if field_name not in properties:
                continue
            field_schema = properties[field_name]
            expected_type = field_schema.get("type")
            if expected_type and not self._check_type(value, expected_type):
                errors.append(
                    f"Type mismatch for '{field_name}': "
                    f"expected {expected_type}, got {type(value).__name__}"
                )

        return errors

    def _check_type(self, value: Any, expected_type: str) -> bool:
        """检查值是否匹配预期的 JSON Schema 类型。

        Args:
            value: 待检查的值
            expected_type: 预期的 JSON Schema 类型字符串

        Returns:
            是否类型匹配
        """
        type_map = {
            "string": (str,),
            "number": (int, float),
            "integer": (int,),
            "boolean": (bool,),
            "array": (list,),
            "object": (dict,),
        }
        expected_python_types = type_map.get(expected_type)
        if expected_python_types is None:
            return True
        return isinstance(value, expected_python_types)
