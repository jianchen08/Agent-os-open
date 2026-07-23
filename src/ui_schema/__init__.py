"""UI Schema 模块。

提供后端 UI Schema 系统，包括：
- types: 与前端对齐的 Pydantic 类型定义
- parser: 从 YAML 配置解析 UI Schema
- validator: Schema 结构完整性验证

典型用法::

    from ui_schema import SchemaParser, SchemaValidator

    parser = SchemaParser()
    schemas = parser.load_directory("config/agents/")

    validator = SchemaValidator()
    errors = validator.validate(schemas[0])
"""

from ui_schema.parser import SchemaParser
from ui_schema.types import (
    AutoOpenConfig,
    CategoryType,
    ChatInteractionConfig,
    ChatInteractionType,
    ClientCapabilities,
    DockConfig,
    FallbackConfig,
    FullscreenConfig,
    LayoutConfig,
    ModuleAction,
    ModuleIdentity,
    ModuleRendering,
    ModuleUISchema,
    RenderingSpaceConfig,
    RenderingSpaceType,
)
from ui_schema.validator import VALID_WIDGET_TYPES, SchemaValidator

__all__ = [
    # 解析/验证
    "SchemaParser",
    "SchemaValidator",
    "VALID_WIDGET_TYPES",
    # 类型
    "AutoOpenConfig",
    "CategoryType",
    "ChatInteractionConfig",
    "ChatInteractionType",
    "ClientCapabilities",
    "DockConfig",
    "FallbackConfig",
    "FullscreenConfig",
    "LayoutConfig",
    "ModuleAction",
    "ModuleIdentity",
    "ModuleRendering",
    "ModuleUISchema",
    "RenderingSpaceConfig",
    "RenderingSpaceType",
]
