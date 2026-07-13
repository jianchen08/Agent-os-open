"""Schema 验证器。

验证 UI Schema 结构完整性，包括：
- 必填字段检查（identity.id, identity.name 等）
- API 端点格式验证
- Widget 类型白名单验证
"""

from __future__ import annotations

import logging
import re

from ui_schema.types import ModuleAction, ModuleUISchema

logger = logging.getLogger(__name__)

# 合法的 widget 类型白名单
VALID_WIDGET_TYPES: set[str] = {
    # 基础组件
    "text",
    "button",
    "form",
    "input",
    "select",
    "textarea",
    "toggle",
    "slider",
    # 数据展示
    "table",
    "chart",
    "gallery",
    "list",
    "tree",
    "kanban",
    "card",
    "status_card",
    "metric",
    # 内容
    "code_block",
    "markdown",
    "image",
    "video",
    "audio",
    "progress",
    # 布局
    "panel",
    "tabs",
    "split",
    "grid",
    "stack",
    # 交互
    "decision",
    "confirm",
    "dialog",
    "drawer",
    "tooltip",
    # 专用组件
    "filter_bar",
    "comfyui_panel",
    "comfyui_card",
    "terminal",
    "editor",
    "preview",
    "map",
    "calendar",
    "timeline",
    "gantt",
    "dashboard",
}

# API 端点正则：以 /api/ 开头，路径段由小写字母/数字/下划线/连字符组成
_API_ENDPOINT_PATTERN = re.compile(r"^/api(/[a-z0-9_-]+)+$")


class SchemaValidator:
    """UI Schema 验证器。

    验证 Schema 的结构完整性和字段合法性。

    检查项：
    - identity.id 非空
    - identity.name 非空
    - actions 中的 api 端点格式（若存在）
    - rendering.spaces 中的 widget 类型是否在白名单内
    """

    def validate(self, schema: ModuleUISchema) -> list[str]:
        """验证 UI Schema 的完整性。

        Args:
            schema: 待验证的 ModuleUISchema 对象。

        Returns:
            错误列表，空列表表示验证通过。
        """
        errors: list[str] = []

        # 1. identity 必填字段
        self._validate_identity(schema, errors)

        # 2. actions API 端点格式
        self._validate_actions(schema, errors)

        # 3. rendering widget 白名单
        self._validate_rendering(schema, errors)

        return errors

    def validate_all(self, schemas: list[ModuleUISchema]) -> dict[str, list[str]]:
        """批量验证多个 Schema。

        Args:
            schemas: 待验证的 Schema 列表。

        Returns:
            字典，key 为 module id，value 为错误列表。
        """
        results: dict[str, list[str]] = {}
        for schema in schemas:
            errors = self.validate(schema)
            if errors:
                results[schema.identity.id] = errors
        return results

    def _validate_identity(self, schema: ModuleUISchema, errors: list[str]) -> None:
        """验证 identity 部分。

        Args:
            schema: 待验证的 Schema。
            errors: 错误列表，会就地追加。
        """
        identity = schema.identity

        if not identity.id or not identity.id.strip():
            errors.append("identity.id 不能为空")

        if not identity.name or not identity.name.strip():
            errors.append("identity.name 不能为空")

        # id 格式：只允许小写字母、数字、下划线、连字符
        if identity.id and not re.match(r"^[a-z0-9_-]+$", identity.id):
            errors.append(f"identity.id 格式不合法: '{identity.id}'，仅允许小写字母、数字、下划线、连字符")

    def _validate_actions(self, schema: ModuleUISchema, errors: list[str]) -> None:
        """验证 actions 部分的 API 端点格式。

        Args:
            schema: 待验证的 Schema。
            errors: 错误列表，会就地追加。
        """
        for action in schema.actions:
            if action.api is not None and not _API_ENDPOINT_PATTERN.match(action.api):
                errors.append(
                    f"action '{action.id}' 的 API 端点格式不合法: '{action.api}'，应以 /api/ 开头且路径段合法"
                )
            self._validate_action_id(action, errors)

    def _validate_action_id(self, action: ModuleAction, errors: list[str]) -> None:
        """验证 action ID 格式。

        Args:
            action: 待验证的 action。
            errors: 错误列表，会就地追加。
        """
        if not action.id or not action.id.strip():
            errors.append("action.id 不能为空")

    def _validate_rendering(self, schema: ModuleUISchema, errors: list[str]) -> None:
        """验证 rendering 部分的 widget 类型。

        Args:
            schema: 待验证的 Schema。
            errors: 错误列表，会就地追加。
        """
        for space_config in schema.rendering.spaces:
            widget = space_config.widget
            if widget and widget not in VALID_WIDGET_TYPES:
                errors.append(
                    f"rendering.spaces 中 widget '{widget}' 不在白名单内，合法值: {sorted(VALID_WIDGET_TYPES)[:10]}..."
                )
