"""热替换与回滚工具 — 0.2 迁移（FP-MIGR / F-MIGR-2）。

支持操作（schema 面保留，与 LLM 契约不变）：
- swap_plugin: 热替换管道中的插件（自动快照+健康检查+失败回滚）
- rollback_plugin: 回滚到上一个插件版本
- save_config_version: 保存配置版本快照
- rollback_config: 回滚配置到指定版本
- list_versions: 列出配置版本

0.2 迁移说明：0.1 的 HotSwapManager / PluginRegistry / RollbackManager
（src/tools/tool_context）与 channels.cli 服务注册表均已删除；0.2 的插件/配置
管理走内核 HTTP API（require_admin_role 鉴权面）与 plugin-loader 注册表，
sidecar 无热替换等价能力注入 → 本工具提供完整参数校验 + 明确的
UNAVAILABLE 降级错误（不静默空转、不假装成功），供 0.2 管理端接线后恢复。

暴露接口：
- hot_swap_schema: 工具参数 JSON Schema
- hot_swap_func: 工具执行函数（简单场景助手，纯校验路径）
- HotSwapTool: 0.2 工具类（server.py 入口使用）
"""

from __future__ import annotations

import logging
from typing import Any

from agentos_plugin_sdk import (
    BuiltinTool,
    Tool,
    ToolCategory,
    ToolExecutionResult,
    ToolLevel,
    ToolSource,
    create_failure_result,
)

logger = logging.getLogger(__name__)

# 工具参数 Schema（OpenAI Function Calling 格式）
hot_swap_schema: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "swap_plugin",
                "rollback_plugin",
                "save_config_version",
                "rollback_config",
                "list_versions",
            ],
            "description": "操作类型",
        },
        # swap_plugin 参数
        "plugin_name": {
            "type": "string",
            "description": "要替换的插件名称（swap_plugin 操作必填）",
        },
        "new_plugin_class": {
            "type": "string",
            "description": "新插件的完整类路径，如 'agent_os.plugins.input.my_plugin.MyPlugin'（swap_plugin 操作必填）",
        },
        "health_check": {
            "type": "boolean",
            "description": "是否执行健康检查（默认 true）",
            "default": True,
        },
        # rollback_plugin 参数
        "swap_id": {
            "type": "string",
            "description": "替换操作 ID（rollback_plugin 操作必填）",
        },
        # save_config_version / rollback_config 参数
        "config_id": {
            "type": "string",
            "description": "配置 ID（save_config_version / rollback_config / list_versions 操作必填）",
        },
        "config_data": {
            "type": "object",
            "description": "配置数据（save_config_version 操作必填）",
        },
        "description": {
            "type": "string",
            "description": "版本描述（save_config_version 操作可选）",
        },
        "version_id": {
            "type": "string",
            "description": "目标版本 ID（rollback_config 操作必填）",
        },
        "validator": {
            "type": "string",
            "description": "验证函数的完整路径（可选，用于 rollback_config 时验证）",
        },
    },
    "required": ["action"],
}

HOT_SWAP_DESCRIPTION = (
    "热替换与回滚工具。支持插件热替换（自动快照+健康检查+失败回滚）、"
    "插件回滚、配置版本管理和配置回滚。替换失败时自动恢复原状。"
)


def _check_action_params(action: str, params: dict[str, Any]) -> tuple[bool, str]:
    """按 action 校验必填参数（与 0.1 错误码面保持一致）。"""
    if action == "swap_plugin":
        if not params.get("plugin_name"):
            return False, "MISSING_PLUGIN_NAME"
        if not params.get("new_plugin_class"):
            return False, "MISSING_NEW_PLUGIN_CLASS"
        return True, ""
    if action == "rollback_plugin":
        if not params.get("swap_id"):
            return False, "MISSING_SWAP_ID"
        return True, ""
    if action in ("save_config_version", "list_versions"):
        if not params.get("config_id"):
            return False, "MISSING_CONFIG_ID"
        if action == "save_config_version" and not params.get("config_data"):
            return False, "MISSING_CONFIG_DATA"
        return True, ""
    if action == "rollback_config":
        if not params.get("version_id"):
            return False, "MISSING_VERSION_ID"
        return True, ""
    return True, ""


def _unavailable(action: str, params: dict[str, Any]) -> dict[str, Any]:
    """0.2 降级：热替换/配置回滚能力未接线 → 明确错误（不静默空转）。"""
    detail = (
        "0.2 插件/配置管理走内核 HTTP API（require_admin_role 鉴权面）与 "
        "plugin-loader 注册表，sidecar 未注入 HotSwapManager 等价能力，"
        "热替换/配置回滚暂不可用。"
    )
    logger.warning("[hot_swap] %s 降级不可用 | action=%s", detail, action)
    return {
        "success": False,
        "error": detail,
        "error_code": "HOT_SWAP_UNAVAILABLE",
    }


def hot_swap_func(params: dict[str, Any]) -> dict[str, Any]:
    """执行热替换与回滚操作（纯校验路径 + 0.2 降级）。

    Args:
        params: 工具参数，含 action 和对应操作的参数

    Returns:
        包含 success 和操作结果的字典
    """
    action = params.get("action")

    if not action:
        return {
            "success": False,
            "error": "必须提供 action 参数",
            "error_code": "MISSING_ACTION",
        }

    dispatchers = {
        "swap_plugin",
        "rollback_plugin",
        "save_config_version",
        "rollback_config",
        "list_versions",
    }
    if action not in dispatchers:
        return {
            "success": False,
            "error": f"不支持的操作: {action}",
            "error_code": "INVALID_ACTION",
        }

    ok, error_code = _check_action_params(action, params)
    if not ok:
        return {
            "success": False,
            "error": f"缺少必要参数: {error_code}",
            "error_code": error_code,
        }

    return _unavailable(action, params)


class HotSwapTool(BuiltinTool):
    """热替换与回滚工具（0.2 迁移：文档化降级实现）。"""

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="hot_swap",
            description=HOT_SWAP_DESCRIPTION,
            input_schema=hot_swap_schema,
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            level=ToolLevel.USER,
            tags=["hot_swap", "rollback", "config", "plugin"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行热替换与回滚操作。

        0.2 未注入热替换能力 → 参数校验通过后返回 HOT_SWAP_UNAVAILABLE
        （不静默空转、不假装成功）。
        """
        action = inputs.get("action")
        if not action:
            return create_failure_result(
                error="必须提供 action 参数",
                error_code="MISSING_ACTION",
            )
        if action not in {
            "swap_plugin",
            "rollback_plugin",
            "save_config_version",
            "rollback_config",
            "list_versions",
        }:
            return create_failure_result(
                error=f"不支持的操作: {action}",
                error_code="INVALID_ACTION",
            )

        ok, error_code = _check_action_params(action, inputs)
        if not ok:
            return create_failure_result(
                error=f"缺少必要参数: {error_code}",
                error_code=error_code,
            )

        detail = (
            "0.2 插件/配置管理走内核 HTTP API（require_admin_role 鉴权面）与 "
            "plugin-loader 注册表，sidecar 未注入 HotSwapManager 等价能力，"
            "热替换/配置回滚暂不可用。"
        )
        logger.warning("[hot_swap] 降级不可用 | action=%s", action)
        return create_failure_result(
            error=detail,
            error_code="HOT_SWAP_UNAVAILABLE",
        )
