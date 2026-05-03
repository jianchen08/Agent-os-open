"""UI Schema 模块。

提供后端 UI Schema 系统，包括：
- types: 与前端对齐的 Pydantic 类型定义
- parser: 从 YAML 配置解析 UI Schema
- validator: Schema 结构完整性验证
- design_tokens: 设计令牌（间距/色彩/字体/阴影/圆角/布局/层级/过渡/透明度）
- style_config: 模块/场景级样式配置

典型用法::

    from ui_schema import SchemaParser, SchemaValidator, DesignTokens

    parser = SchemaParser()
    schemas = parser.load_directory("config/agents/")

    validator = SchemaValidator()
    errors = validator.validate(schemas[0])

    tokens = DesignTokens()
    css_vars = tokens_to_css_variables(tokens)
    css_stylesheet = generate_css_stylesheet(tokens)
"""

from ui_schema.design_tokens import (
    BorderRadiusScale,
    ColorPalette,
    DesignTokens,
    LayoutScale,
    OpacityScale,
    ShadowScale,
    SpacingScale,
    TransitionScale,
    TypographyScale,
    VisualPreset,
    ZIndexScale,
    generate_css_stylesheet,
    merge_tokens,
    tokens_to_css_variables,
    validate_token_values,
)
from ui_schema.parser import SchemaParser
from ui_schema.style_config import (
    BreakpointConfig,
    ModuleStyleConfig,
    SceneStyleConfig,
    ThemeName,
    validate_style_config,
)
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
from ui_schema.validator import SchemaValidator, VALID_WIDGET_TYPES

__all__ = [
    # 解析/验证
    "SchemaParser",
    "SchemaValidator",
    "VALID_WIDGET_TYPES",
    # 设计令牌
    "DesignTokens",
    "SpacingScale",
    "ColorPalette",
    "TypographyScale",
    "ShadowScale",
    "BorderRadiusScale",
    "LayoutScale",
    "ZIndexScale",
    "TransitionScale",
    "OpacityScale",
    "VisualPreset",
    "tokens_to_css_variables",
    "generate_css_stylesheet",
    "merge_tokens",
    "validate_token_values",
    # 样式配置
    "ModuleStyleConfig",
    "SceneStyleConfig",
    "BreakpointConfig",
    "ThemeName",
    "validate_style_config",
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
