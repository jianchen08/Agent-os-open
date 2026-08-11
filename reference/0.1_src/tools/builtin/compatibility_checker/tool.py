"""
兼容性检查工具

暴露接口：
- get_tool_definition() -> Tool：get_tool_definition功能
- CompatibilityCheckerTool：CompatibilityCheckerTool类
"""

from typing import Any

from core.results import ToolExecutionResult
from tools.builtin.base import BuiltinTool
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class CompatibilityCheckerTool(BuiltinTool):
    """
    兼容性检查工具

    提供：
    - 检查资源配置兼容性
    - 检查依赖兼容性
    - 检查接口兼容性
    - 识别破坏性变更
    """

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="compatibility_checker",
            description="检查修改后的资源与系统的兼容性，识别配置变更是否会导致破坏性变更。支持配置兼容性、接口兼容性和依赖兼容性检查。适用于修改 Agent、工具或工作流配置后验证变更安全性。",
            input_schema={
                "type": "object",
                "properties": {
                    "original_resource": {
                        "type": "object",
                        "description": "原始资源信息对象，包含修改前的资源配置",
                    },
                    "modified_resource": {
                        "type": "object",
                        "description": "修改后的资源信息对象，包含修改后的资源配置",
                    },
                    "system_dependencies": {
                        "type": "object",
                        "description": "系统依赖信息对象，包含可用的工具、接口等系统级依赖",
                    },
                    "check_types": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["config", "interface", "dependency", "all"],
                        },
                        "description": "检查类型列表。config 检查配置字段变更，interface 检查接口 Schema 兼容性，dependency 检查依赖关系，all 执行所有检查",
                        "default": ["all"],
                    },
                },
                "required": ["original_resource", "modified_resource"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            level=ToolLevel.SYSTEM,
            tags=["compatibility", "check", "system"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行兼容性检查"""
        original = inputs.get("original_resource", {})
        modified = inputs.get("modified_resource", {})
        dependencies = inputs.get("system_dependencies", {})
        check_types = inputs.get("check_types", ["all"])

        try:
            results = {
                "compatible": True,
                "breaking_changes": [],
                "warnings": [],
                "checks": {},
            }

            # 配置兼容性检查
            if "all" in check_types or "config" in check_types:
                config_result = self._check_config_compatibility(original, modified)
                results["checks"]["config"] = config_result
                if not config_result["compatible"]:
                    results["compatible"] = False
                results["breaking_changes"].extend(config_result.get("breaking_changes", []))
                results["warnings"].extend(config_result.get("warnings", []))

            # 接口兼容性检查
            if "all" in check_types or "interface" in check_types:
                interface_result = self._check_interface_compatibility(original, modified)
                results["checks"]["interface"] = interface_result
                if not interface_result["compatible"]:
                    results["compatible"] = False
                results["breaking_changes"].extend(interface_result.get("breaking_changes", []))
                results["warnings"].extend(interface_result.get("warnings", []))

            # 依赖兼容性检查
            if "all" in check_types or "dependency" in check_types:
                dep_result = self._check_dependency_compatibility(original, modified, dependencies)
                results["checks"]["dependency"] = dep_result
                if not dep_result["compatible"]:
                    results["compatible"] = False
                results["breaking_changes"].extend(dep_result.get("breaking_changes", []))
                results["warnings"].extend(dep_result.get("warnings", []))

            # 判断是否需要迁移
            results["migration_required"] = len(results["breaking_changes"]) > 0

            return create_success_result(
                data=results,
                metadata={"action": "compatibility_checker"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"兼容性检查失败: {str(e)}",
                metadata={"action": "compatibility_checker"},
            )

    def _check_config_compatibility(
        self,
        original: dict[str, Any],
        modified: dict[str, Any],
    ) -> dict[str, Any]:
        """检查配置兼容性"""
        result = {
            "compatible": True,
            "breaking_changes": [],
            "warnings": [],
        }

        original_info = original.get("resource_info", {})
        modified_info = modified.get("resource_info", modified)

        # 检查必需字段是否被删除
        required_fields = ["name", "config_id", "id"]
        for field in required_fields:
            if field in original_info and field not in modified_info:
                result["compatible"] = False
                result["breaking_changes"].append(
                    {
                        "type": "field_removed",
                        "field": field,
                        "message": f"必需字段 {field} 被删除",
                    }
                )

        # 检查类型变更
        if original_info.get("agent_type") != modified_info.get("agent_type"):
            result["warnings"].append(
                {
                    "type": "type_changed",
                    "field": "agent_type",
                    "original": original_info.get("agent_type"),
                    "modified": modified_info.get("agent_type"),
                    "message": "Agent类型发生变更",
                }
            )

        return result

    def _check_interface_compatibility(
        self,
        original: dict[str, Any],
        modified: dict[str, Any],
    ) -> dict[str, Any]:
        """检查接口兼容性"""
        result = {
            "compatible": True,
            "breaking_changes": [],
            "warnings": [],
        }

        original_info = original.get("resource_info", {})
        modified_info = modified.get("resource_info", modified)

        # 检查输入Schema变更
        original_input = original_info.get("input_schema", {})
        modified_input = modified_info.get("input_schema", {})

        original_required = set(original_input.get("required", []))
        modified_required = set(modified_input.get("required", []))

        # 新增必需参数是破坏性变更
        new_required = modified_required - original_required
        if new_required:
            result["compatible"] = False
            result["breaking_changes"].append(
                {
                    "type": "new_required_params",
                    "params": list(new_required),
                    "message": f"新增了必需参数: {', '.join(new_required)}",
                }
            )

        # 检查输出Schema变更
        original_output = original_info.get("output_schema", {})
        modified_output = modified_info.get("output_schema", {})

        original_props = set(original_output.get("properties", {}).keys())
        modified_props = set(modified_output.get("properties", {}).keys())

        # 删除输出字段是破坏性变更
        removed_props = original_props - modified_props
        if removed_props:
            result["compatible"] = False
            result["breaking_changes"].append(
                {
                    "type": "output_fields_removed",
                    "fields": list(removed_props),
                    "message": f"删除了输出字段: {', '.join(removed_props)}",
                }
            )

        return result

    def _check_dependency_compatibility(
        self,
        original: dict[str, Any],
        modified: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> dict[str, Any]:
        """检查依赖兼容性"""
        result = {
            "compatible": True,
            "breaking_changes": [],
            "warnings": [],
        }

        original_info = original.get("resource_info", {})
        modified_info = modified.get("resource_info", modified)

        # 检查工具依赖变更
        original_tools = set(original_info.get("tool_ids", []))
        modified_tools = set(modified_info.get("tool_ids", []))

        removed_tools = original_tools - modified_tools
        if removed_tools:
            result["warnings"].append(
                {
                    "type": "tools_removed",
                    "tools": list(removed_tools),
                    "message": f"移除了工具依赖: {', '.join(removed_tools)}",
                }
            )

        added_tools = modified_tools - original_tools
        if added_tools:
            result["warnings"].append(
                {
                    "type": "tools_added",
                    "tools": list(added_tools),
                    "message": f"新增了工具依赖: {', '.join(added_tools)}",
                }
            )

        return result
