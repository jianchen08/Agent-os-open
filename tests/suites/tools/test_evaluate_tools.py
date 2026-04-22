"""EvaluateTool / YamlValidateTool / StateUpdateTool 全面单元测试。

覆盖范围：
- EvaluateTool：validate_schema / check_format / check_not_empty / validate_json / custom_validation
- YamlValidateTool：YAML 语法验证、必需字段、schema_type 验证、文件路径验证
- StateUpdateTool：直接赋值、increment/append 操作、context 注入
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from tools.builtin.evaluate import EvaluateTool
from tools.builtin.state_update import StateUpdateTool
from tools.builtin.yaml_validate import YamlValidateTool


# ═══════════════════════════════════════════════════════════
# EvaluateTool
# ═══════════════════════════════════════════════════════════


class TestEvaluateToolDefinition:
    """EvaluateTool 工具定义测试。"""

    def test_tool_name(self) -> None:
        """工具名称为 evaluate。"""
        tool_def = EvaluateTool.get_tool_definition()
        assert tool_def.name == "evaluate"

    def test_tool_actions(self) -> None:
        """支持 5 种 action。"""
        tool_def = EvaluateTool.get_tool_definition()
        actions = tool_def.input_schema["properties"]["action"]["enum"]
        assert set(actions) == {
            "validate_schema",
            "check_format",
            "check_not_empty",
            "validate_json",
            "custom_validation",
        }


class TestValidateSchema:
    """validate_schema 操作测试。"""

    @pytest.mark.asyncio
    async def test_valid_data(self) -> None:
        """数据符合 Schema 时返回 valid=True。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "validate_schema",
            "data": {"name": "test", "age": 25},
            "schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "age": {"type": "integer"},
                },
                "required": ["name"],
            },
        })
        assert result.success
        assert result.output["valid"] is True
        assert result.output["errors"] == []

    @pytest.mark.asyncio
    async def test_invalid_data(self) -> None:
        """数据不符合 Schema 时返回 valid=False 和错误信息。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "validate_schema",
            "data": {"name": 123},
            "schema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        })
        assert result.success
        assert result.output["valid"] is False
        assert len(result.output["errors"]) > 0

    @pytest.mark.asyncio
    async def test_missing_required_field(self) -> None:
        """缺少必填字段时验证失败。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "validate_schema",
            "data": {},
            "schema": {
                "type": "object",
                "required": ["name"],
            },
        })
        assert result.success
        assert result.output["valid"] is False

    @pytest.mark.asyncio
    async def test_missing_schema(self) -> None:
        """缺少 schema 参数返回失败。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "validate_schema",
            "data": {"name": "test"},
        })
        assert result.success is False
        assert result.error_code == "MISSING_SCHEMA"


class TestCheckFormat:
    """check_format 操作测试。"""

    @pytest.mark.asyncio
    async def test_valid_email(self) -> None:
        """合法邮箱格式。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "check_format",
            "data": "user@example.com",
            "format": "email",
        })
        assert result.success
        assert result.output["valid"] is True

    @pytest.mark.asyncio
    async def test_invalid_email(self) -> None:
        """非法邮箱格式。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "check_format",
            "data": "not-an-email",
            "format": "email",
        })
        assert result.success
        assert result.output["valid"] is False

    @pytest.mark.asyncio
    async def test_valid_url(self) -> None:
        """合法 URL。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "check_format",
            "data": "https://example.com/path",
            "format": "url",
        })
        assert result.success
        assert result.output["valid"] is True

    @pytest.mark.asyncio
    async def test_invalid_url(self) -> None:
        """非法 URL。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "check_format",
            "data": "not a url",
            "format": "url",
        })
        assert result.success
        assert result.output["valid"] is False

    @pytest.mark.asyncio
    async def test_valid_date(self) -> None:
        """合法日期格式。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "check_format",
            "data": "2026-04-18",
            "format": "date",
        })
        assert result.success
        assert result.output["valid"] is True

    @pytest.mark.asyncio
    async def test_invalid_date(self) -> None:
        """非法日期格式。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "check_format",
            "data": "2026/04/18",
            "format": "date",
        })
        assert result.success
        assert result.output["valid"] is False

    @pytest.mark.asyncio
    async def test_valid_datetime(self) -> None:
        """合法日期时间格式。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "check_format",
            "data": "2026-04-18 12:30:00",
            "format": "datetime",
        })
        assert result.success
        assert result.output["valid"] is True

    @pytest.mark.asyncio
    async def test_valid_json_string(self) -> None:
        """合法 JSON 字符串。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "check_format",
            "data": '{"key": "value"}',
            "format": "json",
        })
        assert result.success
        assert result.output["valid"] is True

    @pytest.mark.asyncio
    async def test_invalid_json_string(self) -> None:
        """非法 JSON 字符串。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "check_format",
            "data": "{not json}",
            "format": "json",
        })
        assert result.success
        assert result.output["valid"] is False

    @pytest.mark.asyncio
    async def test_empty_value(self) -> None:
        """空值格式检查失败。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "check_format",
            "data": "",
            "format": "email",
        })
        assert result.success
        assert result.output["valid"] is False

    @pytest.mark.asyncio
    async def test_missing_format(self) -> None:
        """缺少 format 参数返回失败。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "check_format",
            "data": "test",
        })
        assert result.success is False
        assert result.error_code == "MISSING_FORMAT"


class TestCheckNotEmpty:
    """check_not_empty 操作测试。"""

    @pytest.mark.asyncio
    async def test_all_fields_present(self) -> None:
        """所有字段都有值时通过。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "check_not_empty",
            "data": {"name": "test", "age": 25},
        })
        assert result.success
        assert result.output["valid"] is True
        assert result.output["empty_fields"] == []

    @pytest.mark.asyncio
    async def test_some_fields_empty(self) -> None:
        """部分字段为空时报告。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "check_not_empty",
            "data": {"name": "test", "email": "", "items": []},
        })
        assert result.success
        assert result.output["valid"] is False
        assert "email" in result.output["empty_fields"]
        assert "items" in result.output["empty_fields"]

    @pytest.mark.asyncio
    async def test_specific_field_names(self) -> None:
        """指定字段名检查。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "check_not_empty",
            "data": {"name": "test", "email": ""},
            "field_names": ["name"],
        })
        assert result.success
        assert result.output["valid"] is True

    @pytest.mark.asyncio
    async def test_non_dict_data(self) -> None:
        """非字典数据返回失败。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "check_not_empty",
            "data": "not a dict",
        })
        assert result.success is False
        assert result.error_code == "INVALID_DATA_TYPE"

    @pytest.mark.asyncio
    async def test_none_value_treated_as_empty(self) -> None:
        """None 值视为空。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "check_not_empty",
            "data": {"name": None},
        })
        assert result.success
        assert result.output["valid"] is False
        assert "name" in result.output["empty_fields"]


class TestValidateJson:
    """validate_json 操作测试。"""

    @pytest.mark.asyncio
    async def test_valid_json_string(self) -> None:
        """合法 JSON 字符串。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "validate_json",
            "data": '{"key": "value"}',
        })
        assert result.success
        assert result.output["valid"] is True
        assert result.output["data"] == {"key": "value"}

    @pytest.mark.asyncio
    async def test_invalid_json_string(self) -> None:
        """非法 JSON 字符串。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "validate_json",
            "data": "{invalid}",
        })
        assert result.success
        assert result.output["valid"] is False
        assert result.output["error"] is not None

    @pytest.mark.asyncio
    async def test_dict_data(self) -> None:
        """字典类型数据。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "validate_json",
            "data": {"key": "value"},
        })
        assert result.success
        assert result.output["valid"] is True

    @pytest.mark.asyncio
    async def test_list_data(self) -> None:
        """列表类型数据。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "validate_json",
            "data": [1, 2, 3],
        })
        assert result.success
        assert result.output["valid"] is True

    @pytest.mark.asyncio
    async def test_unsupported_type(self) -> None:
        """不支持的类型。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "validate_json",
            "data": 42,
        })
        assert result.success
        assert result.output["valid"] is False


class TestCustomValidation:
    """custom_validation 操作测试。"""

    @pytest.mark.asyncio
    async def test_required_rule_pass(self) -> None:
        """required 规则通过。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "custom_validation",
            "data": {"name": "test"},
            "rules": [{"field": "name", "rule": "required"}],
        })
        assert result.success
        assert result.output["valid"] is True

    @pytest.mark.asyncio
    async def test_required_rule_fail(self) -> None:
        """required 规则失败。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "custom_validation",
            "data": {"name": ""},
            "rules": [{"field": "name", "rule": "required"}],
        })
        assert result.success
        assert result.output["valid"] is False

    @pytest.mark.asyncio
    async def test_min_length_rule(self) -> None:
        """min_length 规则。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "custom_validation",
            "data": {"name": "ab"},
            "rules": [{"field": "name", "rule": "min_length", "value": 3}],
        })
        assert result.success
        assert result.output["valid"] is False

    @pytest.mark.asyncio
    async def test_max_length_rule(self) -> None:
        """max_length 规则。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "custom_validation",
            "data": {"name": "abcde"},
            "rules": [{"field": "name", "rule": "max_length", "value": 3}],
        })
        assert result.success
        assert result.output["valid"] is False

    @pytest.mark.asyncio
    async def test_range_rule(self) -> None:
        """range 规则。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "custom_validation",
            "data": {"age": 50},
            "rules": [{"field": "age", "rule": "range", "value": [0, 100]}],
        })
        assert result.success
        assert result.output["valid"] is True

    @pytest.mark.asyncio
    async def test_regex_rule(self) -> None:
        """regex 规则。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "custom_validation",
            "data": {"code": "ABC123"},
            "rules": [{"field": "code", "rule": "regex", "value": r"^[A-Z]+\d+$"}],
        })
        assert result.success
        assert result.output["valid"] is True

    @pytest.mark.asyncio
    async def test_enum_rule(self) -> None:
        """enum 规则。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "custom_validation",
            "data": {"status": "active"},
            "rules": [{"field": "status", "rule": "enum", "value": ["active", "inactive"]}],
        })
        assert result.success
        assert result.output["valid"] is True

    @pytest.mark.asyncio
    async def test_enum_rule_fail(self) -> None:
        """enum 规则失败。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "custom_validation",
            "data": {"status": "unknown"},
            "rules": [{"field": "status", "rule": "enum", "value": ["active", "inactive"]}],
        })
        assert result.success
        assert result.output["valid"] is False

    @pytest.mark.asyncio
    async def test_non_dict_data(self) -> None:
        """非字典数据返回失败。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "custom_validation",
            "data": "not a dict",
            "rules": [],
        })
        assert result.success is False
        assert result.error_code == "INVALID_DATA_TYPE"

    @pytest.mark.asyncio
    async def test_multiple_rules(self) -> None:
        """多规则同时验证。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "custom_validation",
            "data": {"name": "test", "age": 25},
            "rules": [
                {"field": "name", "rule": "required"},
                {"field": "age", "rule": "range", "value": [0, 100]},
            ],
        })
        assert result.success
        assert result.output["valid"] is True
        assert result.output["total_rules"] == 2


class TestEvaluateToolInvalidAction:
    """无效 action 测试。"""

    @pytest.mark.asyncio
    async def test_invalid_action(self) -> None:
        """无效的 action 返回失败。"""
        tool = EvaluateTool()
        result = await tool.execute({
            "action": "unknown_action",
            "data": {},
        })
        assert result.success is False
        assert result.error_code == "INVALID_ACTION"


# ═══════════════════════════════════════════════════════════
# YamlValidateTool
# ═══════════════════════════════════════════════════════════


class TestYamlValidateToolDefinition:
    """YamlValidateTool 工具定义测试。"""

    def test_tool_name(self) -> None:
        """工具名称为 yaml_validate。"""
        tool_def = YamlValidateTool.get_tool_definition()
        assert tool_def.name == "yaml_validate"


class TestYamlValidateContent:
    """YAML 内容验证测试。"""

    @pytest.mark.asyncio
    async def test_valid_yaml(self) -> None:
        """合法 YAML 内容。"""
        tool = YamlValidateTool()
        result = await tool.execute({
            "content": "name: test\nage: 25\n",
        })
        assert result.success
        assert result.output["valid"] is True
        assert result.output["parsed"]["name"] == "test"

    @pytest.mark.asyncio
    async def test_invalid_yaml_syntax(self) -> None:
        """非法 YAML 语法。"""
        tool = YamlValidateTool()
        result = await tool.execute({
            "content": "name: test\n  bad: [unclosed\n",
        })
        assert result.success is False

    @pytest.mark.asyncio
    async def test_yaml_list_content(self) -> None:
        """YAML 列表内容（非字典）返回失败。"""
        tool = YamlValidateTool()
        result = await tool.execute({
            "content": "- item1\n- item2\n",
        })
        assert result.success is False

    @pytest.mark.asyncio
    async def test_required_fields_present(self) -> None:
        """必需字段都存在。"""
        tool = YamlValidateTool()
        result = await tool.execute({
            "content": "name: test\nversion: 1.0\n",
            "required_fields": ["name", "version"],
        })
        assert result.success
        assert result.output["valid"] is True

    @pytest.mark.asyncio
    async def test_required_fields_missing(self) -> None:
        """缺少必需字段。"""
        tool = YamlValidateTool()
        result = await tool.execute({
            "content": "name: test\n",
            "required_fields": ["name", "version"],
        })
        assert result.success is False


class TestYamlValidateFile:
    """YAML 文件验证测试。"""

    @pytest.mark.asyncio
    async def test_valid_file(self, tmp_path: Path) -> None:
        """验证合法 YAML 文件。"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text("name: test\nage: 25\n", encoding="utf-8")

        tool = YamlValidateTool()
        result = await tool.execute({
            "file_path": str(yaml_file),
        })
        assert result.success
        assert result.output["valid"] is True

    @pytest.mark.asyncio
    async def test_file_not_found(self) -> None:
        """文件不存在返回失败。"""
        tool = YamlValidateTool()
        result = await tool.execute({
            "file_path": "/nonexistent/file.yaml",
        })
        assert result.success is False

    @pytest.mark.asyncio
    async def test_missing_content_and_file(self) -> None:
        """缺少 content 和 file_path 返回失败。"""
        tool = YamlValidateTool()
        result = await tool.execute({})
        assert result.success is False


class TestYamlValidateSchemaType:
    """schema_type 特定验证测试。"""

    @pytest.mark.asyncio
    async def test_agent_schema_valid(self) -> None:
        """合法 Agent 配置。"""
        tool = YamlValidateTool()
        result = await tool.execute({
            "content": "name: my_agent\ntools:\n  - bash_execute\n",
            "schema_type": "agent",
        })
        assert result.success

    @pytest.mark.asyncio
    async def test_agent_schema_missing_name(self) -> None:
        """Agent 配置缺少 name。"""
        tool = YamlValidateTool()
        result = await tool.execute({
            "content": "tools:\n  - bash_execute\n",
            "schema_type": "agent",
        })
        assert result.success is False

    @pytest.mark.asyncio
    async def test_workflow_schema_valid(self) -> None:
        """合法工作流配置。"""
        tool = YamlValidateTool()
        result = await tool.execute({
            "content": "name: my_workflow\nnodes:\n  - name: step1\n",
            "schema_type": "workflow",
        })
        assert result.success

    @pytest.mark.asyncio
    async def test_workflow_schema_missing_name(self) -> None:
        """工作流配置缺少 name。"""
        tool = YamlValidateTool()
        result = await tool.execute({
            "content": "nodes:\n  - name: step1\n",
            "schema_type": "workflow",
        })
        assert result.success is False

    @pytest.mark.asyncio
    async def test_ui_scene_schema_valid(self) -> None:
        """合法 UI 场景配置。"""
        tool = YamlValidateTool()
        result = await tool.execute({
            "content": "scene_id: main\ndisplay_name: Main\n",
            "schema_type": "ui_scene",
        })
        assert result.success

    @pytest.mark.asyncio
    async def test_ui_scene_schema_missing_fields(self) -> None:
        """UI 场景缺少必需字段。"""
        tool = YamlValidateTool()
        result = await tool.execute({
            "content": "other: value\n",
            "schema_type": "ui_scene",
        })
        assert result.success is False

    @pytest.mark.asyncio
    async def test_ui_scene_invalid_quick_actions(self) -> None:
        """UI 场景 quick_actions 不是数组。"""
        tool = YamlValidateTool()
        result = await tool.execute({
            "content": "scene_id: main\ndisplay_name: Main\nquick_actions: invalid\n",
            "schema_type": "ui_scene",
        })
        assert result.success is False


# ═══════════════════════════════════════════════════════════
# StateUpdateTool
# ═══════════════════════════════════════════════════════════


class TestStateUpdateToolDefinition:
    """StateUpdateTool 工具定义测试。"""

    def test_tool_name(self) -> None:
        """工具名称为 state_update。"""
        tool_def = StateUpdateTool.get_tool_definition()
        assert tool_def.name == "state_update"


class TestStateUpdateDirectAssignment:
    """直接赋值测试。"""

    @pytest.mark.asyncio
    async def test_simple_assignment(self) -> None:
        """简单键值对赋值。"""
        tool = StateUpdateTool()
        result = await tool.execute({
            "updates": {"retry_count": 0, "status": "initialized"},
        })
        assert result.success
        assert result.output["success"] is True
        assert "retry_count" in result.output["updated"]
        assert result.output["retry_count"] == 0

    @pytest.mark.asyncio
    async def test_nested_value(self) -> None:
        """嵌套值赋值。"""
        tool = StateUpdateTool()
        result = await tool.execute({
            "updates": {"config": {"timeout": 30}},
        })
        assert result.success
        assert result.output["config"] == {"timeout": 30}

    @pytest.mark.asyncio
    async def test_empty_updates(self) -> None:
        """空 updates。"""
        tool = StateUpdateTool()
        result = await tool.execute({
            "updates": {},
        })
        assert result.success
        assert result.output["updated"] == []


class TestStateUpdateIncrement:
    """increment 操作测试。"""

    @pytest.mark.asyncio
    async def test_increment_from_zero(self) -> None:
        """从 0 开始增量。"""
        tool = StateUpdateTool()
        result = await tool.execute({
            "updates": {"counter": {"operation": "increment", "value": 1}},
        })
        assert result.success
        assert result.output["counter"] == 1

    @pytest.mark.asyncio
    async def test_increment_with_context(self) -> None:
        """基于上下文当前值增量。"""
        tool = StateUpdateTool()
        context = MagicMock()
        context.metadata = {"shared_variables": {"counter": 5}}

        result = await tool.execute(
            inputs={"updates": {"counter": {"operation": "increment", "value": 3}}},
            context=context,
        )
        assert result.success
        assert result.output["counter"] == 8

    @pytest.mark.asyncio
    async def test_increment_non_int_current(self) -> None:
        """当前值非整数时使用 operand。"""
        tool = StateUpdateTool()
        context = MagicMock()
        context.metadata = {"shared_variables": {"counter": "not_int"}}

        result = await tool.execute(
            inputs={"updates": {"counter": {"operation": "increment", "value": 10}}},
            context=context,
        )
        assert result.success
        assert result.output["counter"] == 10


class TestStateUpdateAppend:
    """append 操作测试。"""

    @pytest.mark.asyncio
    async def test_append_to_new_list(self) -> None:
        """追加到新列表。"""
        tool = StateUpdateTool()
        result = await tool.execute({
            "updates": {"items": {"operation": "append", "value": "first"}},
        })
        assert result.success
        assert result.output["items"] == ["first"]

    @pytest.mark.asyncio
    async def test_append_to_existing_list(self) -> None:
        """追加到已有列表。"""
        tool = StateUpdateTool()
        context = MagicMock()
        context.metadata = {"shared_variables": {"items": ["a", "b"]}}

        result = await tool.execute(
            inputs={"updates": {"items": {"operation": "append", "value": "c"}}},
            context=context,
        )
        assert result.success
        assert result.output["items"] == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_append_to_non_list(self) -> None:
        """追加到非列表时创建新列表。"""
        tool = StateUpdateTool()
        context = MagicMock()
        context.metadata = {"shared_variables": {"items": "not_a_list"}}

        result = await tool.execute(
            inputs={"updates": {"items": {"operation": "append", "value": "val"}}},
            context=context,
        )
        assert result.success
        assert result.output["items"] == ["val"]


class TestStateUpdateUnknownOperation:
    """未知操作测试。"""

    @pytest.mark.asyncio
    async def test_unknown_operation_stores_raw(self) -> None:
        """未知操作存储原始值。"""
        tool = StateUpdateTool()
        result = await tool.execute({
            "updates": {"key": {"operation": "unknown_op", "value": 42}},
        })
        assert result.success
        assert result.output["key"] == {"operation": "unknown_op", "value": 42}
