"""设计令牌（Design Tokens）测试。

覆盖：
- DesignTokens 默认值完整性
- SpacingScale 间距梯度（4px 倍数）
- ColorPalette 色彩系统（主色/语义色/中性色）
- TypographyScale 字体梯度
- ShadowScale 阴影层次
- BorderRadiusScale 圆角规范
- 设计令牌序列化为 CSS 变量
- 自定义令牌覆盖与合并
- 令牌值合法性验证
"""

from __future__ import annotations


from ui_schema.design_tokens import (
    BorderRadiusScale,
    ColorPalette,
    DesignTokens,
    ShadowScale,
    SpacingScale,
    TypographyScale,
    merge_tokens,
    tokens_to_css_variables,
    validate_token_values,
)


# ============================================================
# SpacingScale 间距梯度测试
# ============================================================


class TestSpacingScale:
    """间距梯度测试。"""

    def test_default_spacing_values_are_4px_multiples(self) -> None:
        """默认间距值必须是 4px 的倍数。"""
        spacing = SpacingScale()
        for attr_name in ["xs", "sm", "md", "lg", "xl", "xxl"]:
            value = getattr(spacing, attr_name)
            assert value % 4 == 0, f"spacing.{attr_name}={value} 不是 4px 的倍数"

    def test_default_spacing_is_ascending(self) -> None:
        """默认间距值应递增排列。"""
        spacing = SpacingScale()
        values = [spacing.xs, spacing.sm, spacing.md, spacing.lg, spacing.xl, spacing.xxl]
        assert values == sorted(values), f"间距未递增: {values}"

    def test_default_values(self) -> None:
        """验证默认间距值。"""
        spacing = SpacingScale()
        assert spacing.unit == 4
        assert spacing.xs == 4
        assert spacing.sm == 8
        assert spacing.md == 16
        assert spacing.lg == 24
        assert spacing.xl == 32
        assert spacing.xxl == 48

    def test_custom_spacing_values(self) -> None:
        """自定义间距值。"""
        spacing = SpacingScale(xs=2, sm=4, md=8, lg=12, xl=16, xxl=24, unit=2)
        assert spacing.unit == 2
        assert spacing.xs == 2


# ============================================================
# ColorPalette 色彩系统测试
# ============================================================


class TestColorPalette:
    """色彩系统测试。"""

    def test_default_primary_colors(self) -> None:
        """默认主色系。"""
        colors = ColorPalette()
        assert colors.primary is not None
        assert colors.primary.startswith("#")

    def test_default_semantic_colors(self) -> None:
        """默认语义色（成功/警告/错误/信息）完整。"""
        colors = ColorPalette()
        assert colors.success is not None
        assert colors.warning is not None
        assert colors.error is not None
        assert colors.info is not None

    def test_default_neutral_colors(self) -> None:
        """默认中性色系完整（背景/前景/边框/禁用）。"""
        colors = ColorPalette()
        assert colors.bg_primary is not None
        assert colors.bg_secondary is not None
        assert colors.fg_primary is not None
        assert colors.fg_secondary is not None
        assert colors.border is not None
        assert colors.disabled is not None

    def test_custom_colors(self) -> None:
        """自定义颜色覆盖。"""
        colors = ColorPalette(primary="#ff0000", success="#00ff00")
        assert colors.primary == "#ff0000"
        assert colors.success == "#00ff00"

    def test_color_format_validation(self) -> None:
        """颜色值应以 # 开头且为合法十六进制。"""
        colors = ColorPalette()
        color_attrs = [
            colors.primary,
            colors.success,
            colors.warning,
            colors.error,
            colors.info,
        ]
        for color in color_attrs:
            assert color.startswith("#"), f"颜色值 {color} 不以 # 开头"
            assert len(color) == 7, f"颜色值 {color} 长度不为 7"


# ============================================================
# TypographyScale 字体梯度测试
# ============================================================


class TestTypographyScale:
    """字体梯度测试。"""

    def test_default_font_sizes(self) -> None:
        """默认字号梯度（xs/sm/md/lg/xl/xxl）。"""
        typo = TypographyScale()
        assert typo.font_size_xs is not None
        assert typo.font_size_sm is not None
        assert typo.font_size_md is not None
        assert typo.font_size_lg is not None
        assert typo.font_size_xl is not None
        assert typo.font_size_xxl is not None

    def test_default_font_sizes_ascending(self) -> None:
        """字号梯度应递增。"""
        typo = TypographyScale()
        sizes = [
            typo.font_size_xs,
            typo.font_size_sm,
            typo.font_size_md,
            typo.font_size_lg,
            typo.font_size_xl,
            typo.font_size_xxl,
        ]
        assert sizes == sorted(sizes), f"字号未递增: {sizes}"

    def test_default_font_family(self) -> None:
        """默认字体族。"""
        typo = TypographyScale()
        assert typo.font_family is not None
        assert isinstance(typo.font_family, str)
        assert len(typo.font_family) > 0

    def test_default_line_height(self) -> None:
        """默认行高。"""
        typo = TypographyScale()
        assert typo.line_height_base > 0

    def test_default_font_weights(self) -> None:
        """默认字重梯度。"""
        typo = TypographyScale()
        assert typo.font_weight_normal > 0
        assert typo.font_weight_medium > typo.font_weight_normal
        assert typo.font_weight_bold > typo.font_weight_medium


# ============================================================
# ShadowScale 阴影层次测试
# ============================================================


class TestShadowScale:
    """阴影层次测试。"""

    def test_default_shadows(self) -> None:
        """默认阴影层次（none/sm/md/lg/xl）。"""
        shadow = ShadowScale()
        assert shadow.none == "none"
        assert shadow.sm is not None
        assert shadow.md is not None
        assert shadow.lg is not None
        assert shadow.xl is not None

    def test_shadow_strings_format(self) -> None:
        """阴影值应为标准 CSS box-shadow 格式。"""
        shadow = ShadowScale()
        for attr_name in ["sm", "md", "lg", "xl"]:
            value = getattr(shadow, attr_name)
            # CSS box-shadow 通常包含数字和 px
            assert "px" in value or "none" in value, (
                f"shadow.{attr_name}={value} 不是合法 box-shadow"
            )


# ============================================================
# BorderRadiusScale 圆角测试
# ============================================================


class TestBorderRadiusScale:
    """圆角规范测试。"""

    def test_default_values(self) -> None:
        """默认圆角值。"""
        radius = BorderRadiusScale()
        assert radius.none == 0
        assert radius.sm > 0
        assert radius.md > radius.sm
        assert radius.lg > radius.md
        assert radius.xl > radius.lg
        assert radius.full == "50%"

    def test_radius_values_are_consistent(self) -> None:
        """圆角值应一致递增。"""
        radius = BorderRadiusScale()
        numeric_values = [radius.none, radius.sm, radius.md, radius.lg, radius.xl]
        assert numeric_values == sorted(numeric_values)


# ============================================================
# DesignTokens 综合测试
# ============================================================


class TestDesignTokens:
    """DesignTokens 综合测试。"""

    def test_default_tokens_complete(self) -> None:
        """默认令牌包含所有子令牌系统。"""
        tokens = DesignTokens()
        assert tokens.spacing is not None
        assert tokens.colors is not None
        assert tokens.typography is not None
        assert tokens.shadow is not None
        assert tokens.border_radius is not None

    def test_default_tokens_sub_types(self) -> None:
        """子令牌系统类型正确。"""
        tokens = DesignTokens()
        assert isinstance(tokens.spacing, SpacingScale)
        assert isinstance(tokens.colors, ColorPalette)
        assert isinstance(tokens.typography, TypographyScale)
        assert isinstance(tokens.shadow, ShadowScale)
        assert isinstance(tokens.border_radius, BorderRadiusScale)

    def test_serialization_round_trip(self) -> None:
        """令牌序列化后反序列化应保持一致。"""
        tokens = DesignTokens()
        data = tokens.model_dump()
        restored = DesignTokens(**data)
        assert restored == tokens

    def test_custom_sub_token_override(self) -> None:
        """可覆盖子令牌系统。"""
        custom_spacing = SpacingScale(xs=8, sm=12, md=20, lg=28, xl=36, xxl=52)
        tokens = DesignTokens(spacing=custom_spacing)
        assert tokens.spacing.xs == 8
        assert tokens.spacing.sm == 12

    def test_json_serialization(self) -> None:
        """应能序列化为 JSON。"""
        tokens = DesignTokens()
        json_str = tokens.model_dump_json()
        assert "spacing" in json_str
        assert "colors" in json_str
        assert "typography" in json_str


# ============================================================
# tokens_to_css_variables 测试
# ============================================================


class TestTokensToCssVariables:
    """令牌转 CSS 变量测试。"""

    def test_generates_css_variable_format(self) -> None:
        """输出应为 CSS 变量格式 --xxx: yyy。"""
        tokens = DesignTokens()
        css_vars = tokens_to_css_variables(tokens)
        assert isinstance(css_vars, dict)
        assert len(css_vars) > 0
        for key, value in css_vars.items():
            assert key.startswith("--"), f"CSS 变量名 {key} 不以 -- 开头"
            assert isinstance(value, str)

    def test_includes_all_categories(self) -> None:
        """CSS 变量应覆盖所有类别。"""
        tokens = DesignTokens()
        css_vars = tokens_to_css_variables(tokens)
        keys = set(css_vars.keys())
        # 间距相关
        spacing_keys = [k for k in keys if "spacing" in k]
        assert len(spacing_keys) > 0, "缺少 spacing 类别的 CSS 变量"
        # 色彩相关
        color_keys = [k for k in keys if "color" in k]
        assert len(color_keys) > 0, "缺少 color 类别的 CSS 变量"
        # 字体相关
        typo_keys = [k for k in keys if "font" in k or "line-height" in k]
        assert len(typo_keys) > 0, "缺少 typography 类别的 CSS 变量"

    def test_css_variable_values_are_valid(self) -> None:
        """CSS 变量值应为合法 CSS 值。"""
        tokens = DesignTokens()
        css_vars = tokens_to_css_variables(tokens)
        for key, value in css_vars.items():
            # 不包含空白以外的非法字符
            assert len(value.strip()) > 0, f"CSS 变量 {key} 的值为空"


# ============================================================
# merge_tokens 测试
# ============================================================


class TestMergeTokens:
    """自定义令牌覆盖与合并测试。"""

    def test_merge_overrides_spacing(self) -> None:
        """合并覆盖间距值。"""
        base = DesignTokens()
        override = {"spacing": {"xs": 8, "sm": 12}}
        merged = merge_tokens(base, override)
        assert merged.spacing.xs == 8
        assert merged.spacing.sm == 12
        # 未覆盖的值保持原样
        assert merged.spacing.md == base.spacing.md

    def test_merge_overrides_colors(self) -> None:
        """合并覆盖颜色值。"""
        base = DesignTokens()
        override = {"colors": {"primary": "#ff0000"}}
        merged = merge_tokens(base, override)
        assert merged.colors.primary == "#ff0000"
        # 未覆盖的颜色保持原样
        assert merged.colors.success == base.colors.success

    def test_merge_preserves_base(self) -> None:
        """合并不应修改原始令牌。"""
        base = DesignTokens()
        original_xs = base.spacing.xs
        override = {"spacing": {"xs": 100}}
        merged = merge_tokens(base, override)
        assert base.spacing.xs == original_xs
        assert merged.spacing.xs == 100

    def test_merge_empty_override_returns_copy(self) -> None:
        """空覆盖返回原令牌的副本。"""
        base = DesignTokens()
        merged = merge_tokens(base, {})
        assert merged.spacing.xs == base.spacing.xs
        assert merged.colors.primary == base.colors.primary


# ============================================================
# validate_token_values 测试
# ============================================================


class TestValidateTokenValues:
    """令牌值合法性验证测试。"""

    def test_valid_default_tokens_no_errors(self) -> None:
        """默认令牌应通过验证。"""
        tokens = DesignTokens()
        errors = validate_token_values(tokens)
        assert errors == [], f"默认令牌验证失败: {errors}"

    def test_invalid_spacing_not_multiples_of_base(self) -> None:
        """间距值不符合基础单位时应报告错误。"""
        tokens = DesignTokens(
            spacing=SpacingScale(xs=3, sm=7, md=15, lg=23, xl=31, xxl=47, unit=4)
        )
        errors = validate_token_values(tokens)
        assert len(errors) > 0, "间距非 4px 倍数未报错"

    def test_invalid_color_format(self) -> None:
        """颜色值格式不合法时应报告错误。"""
        tokens = DesignTokens(
            colors=ColorPalette(primary="red", success="#00ff00")
        )
        errors = validate_token_values(tokens)
        assert any("primary" in e for e in errors), "颜色格式不合法未报错"

    def test_spacing_not_ascending(self) -> None:
        """间距非递增时应报告错误。"""
        tokens = DesignTokens(
            spacing=SpacingScale(xs=32, sm=16, md=8, lg=4, xl=2, xxl=1)
        )
        errors = validate_token_values(tokens)
        assert any("spacing" in e.lower() or "递增" in e for e in errors)

    def test_font_sizes_not_ascending(self) -> None:
        """字号非递增时应报告错误。"""
        typo = TypographyScale(
            font_size_xs=24,
            font_size_sm=20,
            font_size_md=16,
            font_size_lg=14,
            font_size_xl=12,
            font_size_xxl=10,
        )
        tokens = DesignTokens(typography=typo)
        errors = validate_token_values(tokens)
        assert any("font" in e.lower() or "递增" in e for e in errors)
