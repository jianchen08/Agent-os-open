"""Godot 引擎插件连接器示例。

暴露接口：
- GodotConnector：通过 HTTP 连接 Godot 编辑器插件的外部工具适配器
"""

from __future__ import annotations

import logging
from typing import Any

from tools.external.adapter import ExternalToolAdapter
from tools.external.types import (
    ExternalToolCapability,
    ExternalToolConfig,
)

logger = logging.getLogger(__name__)

# ---- 操作 Schema 定义 ----

LIST_SCENES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "filter": {
            "type": "string",
            "description": "场景名称过滤（可选）",
        },
    },
}

OPEN_SCENE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scene_path": {
            "type": "string",
            "description": "场景文件路径（.tscn 或 .scn）",
        },
    },
    "required": ["scene_path"],
}

RUN_SCENE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scene_path": {
            "type": "string",
            "description": "要运行的场景路径（可选，不传则运行当前场景）",
        },
        "arguments": {
            "type": "array",
            "items": {"type": "string"},
            "description": "运行参数",
        },
    },
}

EXECUTE_SCRIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "script_content": {
            "type": "string",
            "description": "GDScript 脚本内容",
        },
        "script_path": {
            "type": "string",
            "description": "脚本文件路径（可选，如提供则从文件加载）",
        },
    },
}

MANAGE_RESOURCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "get", "create", "delete"],
            "description": "资源操作类型",
        },
        "resource_type": {
            "type": "string",
            "description": "资源类型（如 Texture, Mesh, Material 等）",
        },
        "resource_path": {
            "type": "string",
            "description": "资源路径（get/delete 时必填）",
        },
        "resource_data": {
            "type": "object",
            "description": "资源数据（create 时必填）",
        },
    },
    "required": ["action"],
}

GET_PROJECT_INFO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "include_settings": {
            "type": "boolean",
            "description": "是否包含项目设置",
            "default": False,
        },
    },
}

# 通用输出 Schema
OPERATION_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "data": {"type": "object"},
        "error": {"type": "string"},
    },
}


class GodotConnector(ExternalToolAdapter):
    """Godot 引擎插件连接器。

    通过 HTTP 连接 Godot 编辑器插件，支持：
    - list_scenes: 列出项目场景
    - open_scene: 打开场景
    - run_scene: 运行场景
    - execute_script: 执行 GDScript 脚本
    - manage_resource: 资源管理
    - get_project_info: 获取项目信息

    使用前需确保 Godot 编辑器已启动并安装了 HTTP 插件。
    """

    def __init__(self, config: ExternalToolConfig) -> None:
        """初始化 Godot 连接器。

        Args:
            config: 工具配置（protocol 应为 http）
        """
        super().__init__(config)

    def define_schemas(self) -> list[ExternalToolCapability]:
        """定义 Godot 支持的操作。"""
        return [
            ExternalToolCapability(
                name="list_scenes",
                description="列出 Godot 项目中的所有场景",
                input_schema=LIST_SCENES_SCHEMA,
                output_schema=OPERATION_RESULT_SCHEMA,
            ),
            ExternalToolCapability(
                name="open_scene",
                description="在 Godot 编辑器中打开场景",
                input_schema=OPEN_SCENE_SCHEMA,
                output_schema=OPERATION_RESULT_SCHEMA,
            ),
            ExternalToolCapability(
                name="run_scene",
                description="运行 Godot 场景（F5 效果）",
                input_schema=RUN_SCENE_SCHEMA,
                output_schema=OPERATION_RESULT_SCHEMA,
                timeout_override=120.0,
            ),
            ExternalToolCapability(
                name="execute_script",
                description="在 Godot 中执行 GDScript 脚本",
                input_schema=EXECUTE_SCRIPT_SCHEMA,
                output_schema=OPERATION_RESULT_SCHEMA,
                requires_sandbox=True,
                timeout_override=30.0,
            ),
            ExternalToolCapability(
                name="manage_resource",
                description="管理 Godot 项目资源（增删查）",
                input_schema=MANAGE_RESOURCE_SCHEMA,
                output_schema=OPERATION_RESULT_SCHEMA,
                dangerous=True,
            ),
            ExternalToolCapability(
                name="get_project_info",
                description="获取 Godot 项目信息",
                input_schema=GET_PROJECT_INFO_SCHEMA,
                output_schema=OPERATION_RESULT_SCHEMA,
            ),
        ]

    def validate_input(
        self,
        operation: str,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """验证输入参数（含场景/资源路径验证）。"""
        validated = super().validate_input(operation, inputs)

        # 验证场景文件路径格式
        if operation == "open_scene":
            scene_path = validated.get("scene_path", "")
            if scene_path and not scene_path.endswith((".tscn", ".scn")):
                raise ValueError(f"场景文件格式无效: {scene_path}，期望 .tscn 或 .scn")

        # 验证资源操作参数
        if operation == "manage_resource":
            action = validated.get("action", "")
            if action in ("get", "delete") and not validated.get("resource_path"):
                raise ValueError(f"{action} 操作需要提供 resource_path")

            if action == "create" and not validated.get("resource_data"):
                raise ValueError("create 操作需要提供 resource_data")

        # 验证脚本执行参数
        if operation == "execute_script":  # noqa: SIM102
            if not validated.get("script_content") and not validated.get("script_path"):
                raise ValueError("必须提供 script_content 或 script_path")

        return validated

    async def _do_execute(
        self,
        operation: str,
        inputs: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """执行 Godot 操作。

        Args:
            operation: 操作名称
            inputs: 已验证的输入参数
            context: 执行上下文

        Returns:
            操作结果
        """
        if self._connection is None:
            return {"success": False, "error": "Godot 连接未建立"}

        try:
            response = await self._connection.send_request(
                operation=operation,
                payload=inputs,
                timeout=self._config.execute_timeout,
            )
            return response

        except Exception as e:
            self._logger.error(
                "Godot 操作失败 | op=%s | error=%s",
                operation,
                e,
            )
            return {"success": False, "error": str(e), "operation": operation}
