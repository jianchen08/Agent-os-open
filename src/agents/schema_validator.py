"""Agent 配置 Schema 验证器。

基于 input_schema 和 output_schema 对数据进行简化验证，
检查 required 字段是否存在、类型是否匹配。

采用简化的 JSON Schema 验证策略，不引入第三方库，
仅支持 object 类型及其 properties 的 required 和 type 检查。

典型用法::

    from agents.schema_validator import SchemaValidator

    validator = SchemaValidator()
    errors = validator.validate_input(config, data)
    if errors:
        print("验证失败:", errors)
"""

from __future__ import annotations

from typing import Any

from .types import AgentConfig

# JSON Schema 类型到 Python 类型的映射
_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


class SchemaValidator:
    """Agent 配置 Schema 验证器，基于简化 JSON Schema 进行校验。"""

    @staticmethod
    def _validate_schema(schema: dict[str, Any], data: dict[str, Any], prefix: str = "") -> list[str]:
        """根据 Schema 验证数据。

        支持的检查：
        - required: 必填字段是否存在
        - properties.<name>.type: 字段类型是否匹配
        - properties.<name>.enum: 枚举值是否在列表中（可选检查）

        Args:
            schema: JSON Schema 字典。
            data: 待验证的数据字典。
            prefix: 错误信息前缀（用于嵌套字段）。

        Returns:
            错误列表，空列表表示验证通过。
        """
        errors: list[str] = []

        if not schema:
            return errors

        # 检查 required 字段
        required_fields = schema.get("required", [])
        for field_name in required_fields:
            if field_name not in data:
                errors.append(f"{prefix}缺少必填字段: {field_name}")

        # 检查 properties 类型
        properties = schema.get("properties", {})
        for prop_name, prop_schema in properties.items():
            if prop_name not in data:
                continue  # 非必填字段可以缺失

            value = data[prop_name]
            prop_type = prop_schema.get("type", "")
            field_path = f"{prefix}{prop_name}"

            # 类型检查
            if prop_type and prop_type in _TYPE_MAP:
                expected_type = _TYPE_MAP[prop_type]
                if not isinstance(value, expected_type):
                    errors.append(f"{field_path}: 类型错误，期望 {prop_type}，实际 {type(value).__name__}")

            # 枚举值检查
            enum_values = prop_schema.get("enum")
            if enum_values and value not in enum_values:
                errors.append(f"{field_path}: 值 {value!r} 不在枚举 {enum_values} 中")

            # 嵌套 object 递归检查
            if prop_type == "object" and isinstance(value, dict):
                nested_errors = SchemaValidator._validate_schema(prop_schema, value, prefix=f"{field_path}.")
                errors.extend(nested_errors)

        return errors

    def validate_input(self, config: AgentConfig, data: dict[str, Any]) -> list[str]:
        """验证输入数据是否符合 Agent 的 input_schema。

        Args:
            config: Agent 配置。
            data: 待验证的输入数据。

        Returns:
            错误列表，空列表表示验证通过。
        """
        if not config.input_schema:
            return []
        return self._validate_schema(config.input_schema, data, prefix="input.")

    def validate_output(self, config: AgentConfig, data: dict[str, Any]) -> list[str]:
        """验证输出数据是否符合 Agent 的 output_schema。

        Args:
            config: Agent 配置。
            data: 待验证的输出数据。

        Returns:
            错误列表，空列表表示验证通过。
        """
        if not config.output_schema:
            return []
        return self._validate_schema(config.output_schema, data, prefix="output.")
