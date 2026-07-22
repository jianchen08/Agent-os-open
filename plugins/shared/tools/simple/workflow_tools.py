"""工作流工具——状态更新 + 兼容性检查。

[来源: src/tools/builtin/state_update/tool.py, src/tools/builtin/compatibility_checker/tool.py]
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

STATE_UPDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "updates": {
            "type": "object",
            "description": "要更新的状态变量键值对。支持 increment/append 操作模式",
            "additionalProperties": True,
        },
    },
    "required": ["updates"],
}

COMPATIBILITY_CHECKER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "original_resource": {
            "type": "object",
            "description": "原始资源信息对象",
        },
        "modified_resource": {
            "type": "object",
            "description": "修改后的资源信息对象",
        },
        "system_dependencies": {
            "type": "object",
            "description": "系统依赖信息对象",
        },
        "check_types": {
            "type": "array",
            "items": {"type": "string", "enum": ["config", "interface", "dependency", "all"]},
            "default": ["all"],
        },
    },
    "required": ["original_resource", "modified_resource"],
}


async def state_update(updates: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """更新工作流共享状态变量。"""
    try:
        result_updates: dict[str, Any] = {}
        for key, value in updates.items():
            if isinstance(value, dict) and "operation" in value:
                operation = value.get("operation")
                operand = value.get("value", 0)
                if operation == "increment":
                    result_updates[key] = operand
                elif operation == "append":
                    result_updates[key] = [operand]
                else:
                    logger.warning("未知操作: %s", operation)
                    result_updates[key] = value
            else:
                result_updates[key] = value

        return {
            "success": True,
            "updates": result_updates,
        }
    except Exception as e:
        return {"success": False, "error": f"状态更新失败: {e}"}


def _check_config_compatibility(original: dict[str, Any], modified: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"compatible": True, "breaking_changes": [], "warnings": []}
    original_info = original.get("resource_info", {})
    modified_info = modified.get("resource_info", modified)

    for field in ("name", "config_id", "id"):
        if field in original_info and field not in modified_info:
            result["compatible"] = False
            result["breaking_changes"].append(
                {"type": "field_removed", "field": field, "message": f"必需字段 {field} 被删除"}
            )

    if original_info.get("agent_type") != modified_info.get("agent_type"):
        result["warnings"].append(
            {"type": "type_changed", "field": "agent_type", "message": "Agent类型发生变更"}
        )
    return result


def _check_interface_compatibility(original: dict[str, Any], modified: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"compatible": True, "breaking_changes": [], "warnings": []}
    original_info = original.get("resource_info", {})
    modified_info = modified.get("resource_info", modified)

    original_required = set(original_info.get("input_schema", {}).get("required", []))
    modified_required = set(modified_info.get("input_schema", {}).get("required", []))

    new_required = modified_required - original_required
    if new_required:
        result["compatible"] = False
        result["breaking_changes"].append(
            {"type": "new_required_params", "params": list(new_required)}
        )

    original_props = set(original_info.get("output_schema", {}).get("properties", {}).keys())
    modified_props = set(modified_info.get("output_schema", {}).get("properties", {}).keys())
    removed_props = original_props - modified_props
    if removed_props:
        result["compatible"] = False
        result["breaking_changes"].append(
            {"type": "output_fields_removed", "fields": list(removed_props)}
        )
    return result


def _check_dependency_compatibility(
    original: dict[str, Any], modified: dict[str, Any], dependencies: dict[str, Any]
) -> dict[str, Any]:
    result: dict[str, Any] = {"compatible": True, "breaking_changes": [], "warnings": []}
    original_info = original.get("resource_info", {})
    modified_info = modified.get("resource_info", modified)

    original_tools = set(original_info.get("tool_ids", []))
    modified_tools = set(modified_info.get("tool_ids", []))

    removed_tools = original_tools - modified_tools
    if removed_tools:
        result["warnings"].append({"type": "tools_removed", "tools": list(removed_tools)})
    added_tools = modified_tools - original_tools
    if added_tools:
        result["warnings"].append({"type": "tools_added", "tools": list(added_tools)})
    return result


async def compatibility_checker(
    original_resource: dict[str, Any],
    modified_resource: dict[str, Any],
    system_dependencies: dict[str, Any] | None = None,
    check_types: list[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """检查资源修改兼容性。"""
    original = original_resource
    modified = modified_resource
    dependencies = system_dependencies or {}
    if check_types is None:
        check_types = ["all"]

    try:
        results: dict[str, Any] = {
            "compatible": True,
            "breaking_changes": [],
            "warnings": [],
            "checks": {},
        }

        if "all" in check_types or "config" in check_types:
            config_result = _check_config_compatibility(original, modified)
            results["checks"]["config"] = config_result
            if not config_result["compatible"]:
                results["compatible"] = False
            results["breaking_changes"].extend(config_result["breaking_changes"])
            results["warnings"].extend(config_result["warnings"])

        if "all" in check_types or "interface" in check_types:
            iface_result = _check_interface_compatibility(original, modified)
            results["checks"]["interface"] = iface_result
            if not iface_result["compatible"]:
                results["compatible"] = False
            results["breaking_changes"].extend(iface_result["breaking_changes"])
            results["warnings"].extend(iface_result["warnings"])

        if "all" in check_types or "dependency" in check_types:
            dep_result = _check_dependency_compatibility(original, modified, dependencies)
            results["checks"]["dependency"] = dep_result
            if not dep_result["compatible"]:
                results["compatible"] = False
            results["breaking_changes"].extend(dep_result["breaking_changes"])
            results["warnings"].extend(dep_result["warnings"])

        results["migration_required"] = len(results["breaking_changes"]) > 0
        return results
    except Exception as e:
        return {"error": f"兼容性检查失败: {e}"}
