"""样式配置（Style Config）测试。

覆盖：
- ModuleStyleConfig 默认值与自定义
- 样式配置与设计令牌的关联
- 场景样式配置（SceneStyleConfig）
- 样式主题验证
- 样式配置序列化为前端兼容格式
- 响应式断点配置
"""

from __future__ import annotations

import pytest

from ui_schema.design_tokens import (
    BorderRadiusScale,
    ColorPalette,
    DesignTokens,
    ShadowScale,
    SpacingScale,
    TypographyScale,
)
from ui_schema.style_config import (
    BreakpointConfig,
    ModuleStyleConfig,
    SceneStyleConfig,
    ThemeName,
    validate_style_config,
)


# ============================================================
# ModuleStyleConfig 测试
# ============================================================


class TestModuleStyleConfig:
    """模块级样式配置测试。"""

    def test_default_values(self) -> None:
        """默认值应为空（继承全局主题）。"""
        config = ModuleStyleConfig()
        assert config.theme is None
        assert config.custom_spacing is None
        assert config.custom_colors is None
        assert config.border_radius is None
        assert config.elevation is None

    def test_with_theme(self) -> None:
        """指定主题名。"""
        config = ModuleStyleConfig(theme="dark")
        assert config.theme == "dark"

    def test_with_custom_colors(self) -> None:
        """自定义颜色覆盖。"""
        config = ModuleStyleConfig(custom_colors={"primary": "#ff0000"})
        assert config.custom_colors is not None
        assert config.custom_colors["primary"] == "#ff0000"

    def test_with_elevation(self) -> None:
        """指定阴影层次。"""
        config = ModuleStyleConfig(elevation="md")
        assert config.elevation == "md"

    def test_serialization_by_alias(self) -> None:
        """序列化为前端兼容格式。"""
        config = ModuleStyleConfig(
            theme="light",
            custom_colors={"primary": "#0066cc"},
            custom_spacing={"xs": 8, "sm": 12},
            border_radius="lg",
            elevation="sm",
        )
        data = config.model_dump(by_alias=True, exclude_none=True)
        assert "theme" in data
        assert "customColors" in data or "custom_colors" in data

    def test_round_trip(self) -> None:
        """序列化/反序列化往返一致。"""
        config = ModuleStyleConfig(
            theme="dark",
            custom_colors={"bg": "#1a1a1a"},
            elevation="lg",
        )
        data = config.model_dump()
        restored = ModuleStyleConfig(**data)
        assert restored == config


# ============================================================
# SceneStyleConfig 测试
# ============================================================


class TestSceneStyleConfig:
    """场景级样式配置测试。"""

    def test_default_values(self) -> None:
        """默认值。"""
        config = SceneStyleConfig()
        assert config.theme is None
        assert config.gap is None
        assert config.padding is None
        assert config.max_width is None
        assert config.background is None

    def test_with_layout_spacing(self) -> None:
        """布局间距配置。"""
        config = SceneStyleConfig(gap=16, padding=24)
        assert config.gap == 16
        assert config.padding == 24

    def test_with_theme(self) -> None:
        """指定主题。"""
        config = SceneStyleConfig(theme="dark")
        assert config.theme == "dark"

    def test_gap_is_spacing_multiple(self) -> None:
        """gap 应为间距系统的倍数。"""
        config = SceneStyleConfig(gap=16)
        assert config.gap % 4 == 0, "gap 不是 4px 倍数"

    def test_padding_is_spacing_multiple(self) -> None:
        """padding 应为间距系统的倍数。"""
        config = SceneStyleConfig(padding=24)
        assert config.padding % 4 == 0, "padding 不是 4px 倍数"

    def test_serialization(self) -> None:
        """序列化。"""
        config = SceneStyleConfig(
            theme="light",
            gap=16,
            padding=24,
            max_width=1200,
            background="#f5f5f5",
        )
        data = config.model_dump(exclude_none=True)
        assert data["gap"] == 16
        assert data["max_width"] == 1200


# ============================================================
# BreakpointConfig 测试
# ============================================================


class TestBreakpointConfig:
    """响应式断点配置测试。"""

    def test_default_breakpoints(self) -> None:
        """默认断点值。"""
        bp = BreakpointConfig()
        assert bp.sm > 0
        assert bp.md > bp.sm
        assert bp.lg > bp.md
        assert bp.xl > bp.lg

    def test_default_values_reasonable(self) -> None:
        """默认断点值应在合理范围。"""
        bp = BreakpointConfig()
        assert bp.sm == 640
        assert bp.md == 768
        assert bp.lg == 1024
        assert bp.xl == 1280

    def test_custom_breakpoints(self) -> None:
        """自定义断点。"""
        bp = BreakpointConfig(sm=480, md=768, lg=1024, xl=1440)
        assert bp.sm == 480
        assert bp.xl == 1440


# ============================================================
# ThemeName 测试
# ============================================================


class TestThemeName:
    """主题名称测试。"""

    def test_valid_theme_names(self) -> None:
        """合法主题名。"""
        valid_names = ["light", "dark"]
        for name in valid_names:
            config = ModuleStyleConfig(theme=name)
            assert config.theme == name

    def test_theme_optional(self) -> None:
        """主题名为可选。"""
        config = ModuleStyleConfig()
        assert config.theme is None


# ============================================================
# validate_style_config 测试
# ============================================================


class TestValidateStyleConfig:
    """样式配置验证测试。"""

    def test_valid_config_no_errors(self) -> None:
        """有效配置不应报错。"""
        config = ModuleStyleConfig(
            theme="light",
            custom_colors={"primary": "#0066cc"},
            elevation="md",
        )
        errors = validate_style_config(config)
        assert errors == []

    def test_invalid_elevation_value(self) -> None:
        """无效的阴影层次值应报错。"""
        config = ModuleStyleConfig(elevation="super_high")
        errors = validate_style_config(config)
        assert any("elevation" in e.lower() for e in errors)

    def test_invalid_theme_name(self) -> None:
        """无效的主题名应报错。"""
        config = ModuleStyleConfig(theme="neon")
        errors = validate_style_config(config)
        assert any("theme" in e.lower() for e in errors)

    def test_custom_colors_invalid_hex(self) -> None:
        """自定义颜色值格式不合法应报错。"""
        config = ModuleStyleConfig(custom_colors={"primary": "not-a-color"})
        errors = validate_style_config(config)
        assert any("color" in e.lower() for e in errors)

    def test_custom_spacing_not_multiple(self) -> None:
        """自定义间距不是基础单位倍数应报错。"""
        config = ModuleStyleConfig(custom_spacing={"xs": 5, "sm": 9})
        errors = validate_style_config(config)
        assert any("spacing" in e.lower() for e in errors)

    def test_border_radius_invalid(self) -> None:
        """无效圆角值应报错。"""
        config = ModuleStyleConfig(border_radius="huge")
        errors = validate_style_config(config)
        assert any("border" in e.lower() or "radius" in e.lower() for e in errors)

    def test_valid_elevation_values(self) -> None:
        """所有合法的阴影层次值应通过。"""
        for elev in ("none", "sm", "md", "lg", "xl"):
            config = ModuleStyleConfig(elevation=elev)
            errors = validate_style_config(config)
            elev_errors = [e for e in errors if "elevation" in e.lower()]
            assert elev_errors == [], f"elevation={elev} 不应报错: {elev_errors}"

    def test_valid_border_radius_values(self) -> None:
        """所有合法圆角值应通过。"""
        for radius in ("none", "sm", "md", "lg", "xl", "full"):
            config = ModuleStyleConfig(border_radius=radius)
            errors = validate_style_config(config)
            radius_errors = [e for e in errors if "radius" in e.lower()]
            assert radius_errors == [], f"border_radius={radius} 不应报错: {radius_errors}"
