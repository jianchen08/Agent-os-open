"""
YAML 验证工具

暴露接口：
- get_tool_definition() -> Tool：get_tool_definition功能
- YamlValidateTool：YamlValidateTool类
"""

from pathlib import Path
from typing import Any

import yaml

from tools.builtin.base import BuiltinTool
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class YamlValidateTool(BuiltinTool):
    """YAML 验证工具"""

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="yaml_validate",
            description="验证 YAML 配置文件的格式和内容。支持验证 YAML 语法、检查必需字段、以及针对 agent/workflow/ui_scene 类型的 Schema 验证。使用时提供 content 或 file_path 之一，可指定 schema_type 进行特定格式验证。",
            input_schema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "YAML 内容字符串，与 file_path 二选一",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "YAML 文件路径（与 content 二选一），提供文件路径时会读取文件内容进行验证",
                    },
                    "schema_type": {
                        "type": "string",
                        "enum": ["agent", "workflow", "ui_scene", "generic"],
                        "description": "Schema 类型，用于特定格式验证。agent 验证 Agent 配置，workflow 验证工作流配置，ui_scene 验证 UI 场景配置，generic 仅验证 YAML 语法",
                        "default": "generic",
                    },
                    "required_fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "必需字段列表，验证 YAML 中是否包含这些字段",
                    },
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "valid": {"type": "boolean", "description": "YAML 是否有效"},
                    "errors": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "错误信息列表，包含语法错误和缺失字段等",
                    },
                    "warnings": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "警告信息列表",
                    },
                    "parsed": {"type": "object", "description": "解析后的 YAML 内容对象"},
                },
            },
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            level=ToolLevel.SYSTEM,
            tags=["yaml", "validate", "config"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolResult:  # noqa: PLR0911,PLR0912
        """执行验证"""
        content = inputs.get("content")
        file_path = inputs.get("file_path")
        schema_type = inputs.get("schema_type", "generic")
        required_fields = inputs.get("required_fields", [])

        errors: list[str] = []
        warnings: list[str] = []
        parsed: dict | None = None

        # 获取 YAML 内容
        if content:
            yaml_content = content
        elif file_path:
            path = Path(file_path)
            if not path.exists():
                return create_failure_result(f"文件不存在: {file_path}")
            try:
                yaml_content = path.read_text(encoding="utf-8")
            except Exception as e:
                return create_failure_result(f"读取文件失败: {e}")
        else:
            return create_failure_result("必须提供 content 或 file_path")

        # 解析 YAML
        try:
            parsed = yaml.safe_load(yaml_content)
        except yaml.YAMLError as e:
            errors.append(f"YAML 语法错误: {e}")
            return create_failure_result(
                error=f"YAML 语法错误: {e}",
                error_code="YAML_SYNTAX_ERROR",
            )

        # 检查是否为字典
        if not isinstance(parsed, dict):
            errors.append("YAML 内容必须是对象/字典类型")
            return create_failure_result(
                error="YAML 内容必须是对象/字典类型",
                error_code="YAML_TYPE_ERROR",
            )

        # 检查必需字段
        for field in required_fields:
            if field not in parsed:
                errors.append(f"缺少必需字段: {field}")

        # Schema 特定验证
        if schema_type == "ui_scene":
            self._validate_ui_scene(parsed, errors, warnings)
        elif schema_type == "agent":
            self._validate_agent(parsed, errors, warnings)
        elif schema_type == "workflow":
            self._validate_workflow(parsed, errors, warnings)

        if errors:
            return create_failure_result(
                error=f"YAML 验证失败: {'; '.join(errors)}",
                error_code="YAML_VALIDATION_FAILED",
            )

        return create_success_result(
            {
                "valid": True,
                "errors": [],
                "warnings": warnings,
                "parsed": parsed,
            }
        )

    def _validate_ui_scene(self, data: dict, errors: list[str], warnings: list[str]) -> None:
        """验证 UI 场景配置"""
        required = ["scene_id", "display_name"]
        for field in required:
            if field not in data:
                errors.append(f"UI 场景缺少必需字段: {field}")

        # 检查 quick_actions 配置
        if "quick_actions" in data:
            actions = data["quick_actions"]
            if not isinstance(actions, list):
                errors.append("quick_actions 必须是数组")
            else:
                for i, action in enumerate(actions):
                    if not isinstance(action, dict):
                        errors.append(f"quick_actions[{i}] 必须是对象")
                    elif "id" not in action:
                        errors.append(f"quick_actions[{i}] 缺少 id 字段")
                    elif "action_type" not in action:
                        errors.append(f"quick_actions[{i}] 缺少 action_type 字段")

    def _validate_agent(self, data: dict, errors: list[str], warnings: list[str]) -> None:
        """验证 Agent 配置"""
        required = ["name"]
        for field in required:
            if field not in data:
                errors.append(f"Agent 缺少必需字段: {field}")

        # 检查 tools 配置
        if "tools" in data and not isinstance(data["tools"], list):
            errors.append("tools 必须是数组")

    def _validate_workflow(self, data: dict, errors: list[str], warnings: list[str]) -> None:
        """验证工作流配置"""
        required = ["name"]
        for field in required:
            if field not in data:
                errors.append(f"工作流缺少必需字段: {field}")

        # 检查 nodes 配置
        if "nodes" in data:
            if not isinstance(data["nodes"], list):
                errors.append("nodes 必须是数组")
            else:
                for i, node in enumerate(data["nodes"]):
                    if not isinstance(node, dict):
                        errors.append(f"nodes[{i}] 必须是对象")
                    elif "name" not in node:
                        warnings.append(f"nodes[{i}] 建议添加 name 字段")


# 创建单例实例
yaml_validate_tool = YamlValidateTool()
