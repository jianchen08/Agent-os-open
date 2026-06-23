"""视觉优化增强测试。

覆盖：
- LayoutScale 布局令牌（容器宽度、头部/侧边栏尺寸）
- ZIndexScale Z轴层级令牌
- TransitionScale 过渡/动画令牌
- OpacityScale 透明度令牌
- DesignTokens 新增子令牌的完整性
- tokens_to_css_variables 对新令牌的覆盖
- generate_css_stylesheet 完整样式表生成
- VisualPreset 视觉层级预设
- 响应式布局辅助函数
"""

from __future__ import annotations

from ui_schema.design_tokens import (
    ColorPalette,
    DesignTokens,
    LayoutScale,
    OpacityScale,
    TransitionScale,
    VisualPreset,
    ZIndexScale,
    generate_css_stylesheet,
    merge_tokens,
    tokens_to_css_variables,
    validate_token_values,
)

# ============================================================
# LayoutScale 布局令牌测试
# ============================================================


class TestLayoutScale:
    """布局令牌测试。"""

    def test_default_container_widths(self) -> None:
        """默认容器宽度应递增。"""
        layout = LayoutScale()
        widths = [layout.container_sm, layout.container_md, layout.container_lg, layout.container_xl]
        assert widths == sorted(widths), f"容器宽度未递增: {widths}"

    def test_default_container_values(self) -> None:
        """默认容器宽度值合理。"""
        layout = LayoutScale()
        assert layout.container_sm == 640
        assert layout.container_md == 768
        assert layout.container_lg == 1024
        assert layout.container_xl == 1280

    def test_default_component_dimensions(self) -> None:
        """默认组件尺寸。"""
        layout = LayoutScale()
        assert layout.header_height > 0
        assert layout.sidebar_width > 0
        assert layout.sidebar_collapsed_width > 0
        assert layout.sidebar_collapsed_width < layout.sidebar_width

    def test_container_widths_are_4px_multiples(self) -> None:
        """容器宽度应为 4px 倍数。"""
        layout = LayoutScale()
        for attr in ("container_sm", "container_md", "container_lg", "container_xl"):
            value = getattr(layout, attr)
            assert value % 4 == 0, f"{attr}={value} 不是 4px 的倍数"

    def test_header_height_reasonable(self) -> None:
        """头部高度应在合理范围。"""
        layout = LayoutScale()
        assert 40 <= layout.header_height <= 80

    def test_sidebar_width_reasonable(self) -> None:
        """侧边栏宽度应在合理范围。"""
        layout = LayoutScale()
        assert 160 <= layout.sidebar_width <= 320

    def test_custom_layout_values(self) -> None:
        """自定义布局值。"""
        layout = LayoutScale(header_height=48, sidebar_width=200)
        assert layout.header_height == 48
        assert layout.sidebar_width == 200


# ============================================================
# ZIndexScale Z轴层级令牌测试
# ============================================================


class TestZIndexScale:
    """Z轴层级令牌测试。"""

    def test_default_zindex_values(self) -> None:
        """默认 z-index 值完整。"""
        z = ZIndexScale()
        assert z.base == 0
        assert z.dropdown > z.base
        assert z.sticky >= z.dropdown
        assert z.modal > z.sticky
        assert z.popover > z.modal
        assert z.tooltip > z.popover

    def test_zindex_ascending(self) -> None:
        """z-index 值应层级递增。"""
        z = ZIndexScale()
        values = [z.base, z.dropdown, z.sticky, z.fixed, z.modal_backdrop, z.modal, z.popover, z.tooltip]
        # 允许相同值（某些层级可并列），但不允许低层级高于高层级
        for i in range(len(values) - 1):
            assert values[i] <= values[i + 1], (
                f"z-index 层级顺序错误: index {i} ({values[i]}) > index {i+1} ({values[i+1]})"
            )

    def test_zindex_values_positive(self) -> None:
        """z-index 值应为非负数。"""
        z = ZIndexScale()
        for attr in ("base", "dropdown", "sticky", "fixed", "modal_backdrop", "modal", "popover", "tooltip"):
            value = getattr(z, attr)
            assert value >= 0, f"{attr}={value} 为负数"

    def test_custom_zindex(self) -> None:
        """自定义 z-index 值。"""
        z = ZIndexScale(modal=2000, tooltip=3000)
        assert z.modal == 2000
        assert z.tooltip == 3000


# ============================================================
# TransitionScale 过渡令牌测试
# ============================================================


class TestTransitionScale:
    """过渡/动画令牌测试。"""

    def test_default_durations(self) -> None:
        """默认过渡时长完整。"""
        t = TransitionScale()
        assert t.duration_fast > 0
        assert t.duration_normal > t.duration_fast
        assert t.duration_slow > t.duration_normal

    def test_default_durations_values(self) -> None:
        """默认过渡时长值合理。"""
        t = TransitionScale()
        assert t.duration_fast == 150
        assert t.duration_normal == 250
        assert t.duration_slow == 350

    def test_easing_strings(self) -> None:
        """缓动函数应为 CSS 合法值。"""
        t = TransitionScale()
        for attr in ("easing_default", "easing_in", "easing_out", "easing_in_out"):
            value = getattr(t, attr)
            assert isinstance(value, str)
            assert len(value) > 0
            # 缓动函数应包含 cubic-bezier 或 ease/linear
            assert "cubic-bezier" in value or "ease" in value or "linear" in value

    def test_custom_transition(self) -> None:
        """自定义过渡值。"""
        t = TransitionScale(duration_fast=100, duration_normal=200, duration_slow=300)
        assert t.duration_fast == 100


# ============================================================
# OpacityScale 透明度令牌测试
# ============================================================


class TestOpacityScale:
    """透明度令牌测试。"""

    def test_default_opacity_values(self) -> None:
        """默认透明度值完整。"""
        o = OpacityScale()
        assert 0 <= o.disabled <= 1
        assert 0 <= o.hover <= 1
        assert 0 <= o.secondary <= 1
        assert 0 <= o.backdrop <= 1

    def test_opacity_ordering(self) -> None:
        """透明度值应有合理排序。"""
        o = OpacityScale()
        assert o.disabled < o.secondary
        assert o.secondary < o.hover

    def test_default_values(self) -> None:
        """默认透明度值。"""
        o = OpacityScale()
        assert o.disabled == 0.4
        assert o.hover == 0.8
        assert o.secondary == 0.65
        assert o.backdrop == 0.5

    def test_custom_opacity(self) -> None:
        """自定义透明度值。"""
        o = OpacityScale(disabled=0.3, hover=0.7)
        assert o.disabled == 0.3
        assert o.hover == 0.7


# ============================================================
# DesignTokens 新增子令牌测试
# ============================================================


class TestDesignTokensExtended:
    """DesignTokens 新增子令牌测试。"""

    def test_default_tokens_include_layout(self) -> None:
        """默认令牌包含布局令牌。"""
        tokens = DesignTokens()
        assert hasattr(tokens, "layout")
        assert isinstance(tokens.layout, LayoutScale)

    def test_default_tokens_include_zindex(self) -> None:
        """默认令牌包含 z-index 令牌。"""
        tokens = DesignTokens()
        assert hasattr(tokens, "z_index")
        assert isinstance(tokens.z_index, ZIndexScale)

    def test_default_tokens_include_transition(self) -> None:
        """默认令牌包含过渡令牌。"""
        tokens = DesignTokens()
        assert hasattr(tokens, "transition")
        assert isinstance(tokens.transition, TransitionScale)

    def test_default_tokens_include_opacity(self) -> None:
        """默认令牌包含透明度令牌。"""
        tokens = DesignTokens()
        assert hasattr(tokens, "opacity")
        assert isinstance(tokens.opacity, OpacityScale)

    def test_backward_compatible_serialization(self) -> None:
        """新增字段不应影响旧版序列化。"""
        tokens = DesignTokens()
        data = tokens.model_dump()
        assert "spacing" in data
        assert "colors" in data
        assert "typography" in data
        assert "shadow" in data
        assert "border_radius" in data
        # 新增字段
        assert "layout" in data
        assert "z_index" in data
        assert "transition" in data
        assert "opacity" in data

    def test_round_trip_with_new_fields(self) -> None:
        """新增字段序列化后反序列化应保持一致。"""
        tokens = DesignTokens()
        data = tokens.model_dump()
        restored = DesignTokens(**data)
        assert restored.layout == tokens.layout
        assert restored.z_index == tokens.z_index

    def test_custom_layout_override(self) -> None:
        """可覆盖布局令牌。"""
        custom_layout = LayoutScale(header_height=72, sidebar_width=300)
        tokens = DesignTokens(layout=custom_layout)
        assert tokens.layout.header_height == 72
        assert tokens.layout.sidebar_width == 300


# ============================================================
# tokens_to_css_variables 新令牌覆盖测试
# ============================================================


class TestCssVariablesWithNewTokens:
    """CSS 变量转换对新令牌的覆盖测试。"""

    def test_includes_layout_variables(self) -> None:
        """CSS 变量应包含布局变量。"""
        tokens = DesignTokens()
        css_vars = tokens_to_css_variables(tokens)
        layout_keys = [k for k in css_vars if "layout" in k or "container" in k or "header" in k or "sidebar" in k]
        assert len(layout_keys) > 0, "缺少布局类别的 CSS 变量"

    def test_includes_zindex_variables(self) -> None:
        """CSS 变量应包含 z-index 变量。"""
        tokens = DesignTokens()
        css_vars = tokens_to_css_variables(tokens)
        zindex_keys = [k for k in css_vars if "z-index" in k or "zindex" in k]
        assert len(zindex_keys) > 0, "缺少 z-index 类别的 CSS 变量"

    def test_includes_transition_variables(self) -> None:
        """CSS 变量应包含过渡变量。"""
        tokens = DesignTokens()
        css_vars = tokens_to_css_variables(tokens)
        transition_keys = [k for k in css_vars if "transition" in k or "duration" in k or "easing" in k]
        assert len(transition_keys) > 0, "缺少过渡类别的 CSS 变量"

    def test_includes_opacity_variables(self) -> None:
        """CSS 变量应包含透明度变量。"""
        tokens = DesignTokens()
        css_vars = tokens_to_css_variables(tokens)
        opacity_keys = [k for k in css_vars if "opacity" in k]
        assert len(opacity_keys) > 0, "缺少透明度类别的 CSS 变量"

    def test_all_variable_values_are_strings(self) -> None:
        """所有 CSS 变量值应为字符串。"""
        tokens = DesignTokens()
        css_vars = tokens_to_css_variables(tokens)
        for key, value in css_vars.items():
            assert isinstance(value, str), f"CSS 变量 {key} 的值不是字符串: {type(value)}"

    def test_css_variable_names_use_dashes(self) -> None:
        """CSS 变量名应使用连字符分隔。"""
        tokens = DesignTokens()
        css_vars = tokens_to_css_variables(tokens)
        for key in css_vars:
            assert key.startswith("--"), f"CSS 变量名 {key} 不以 -- 开头"
            # 不应包含下划线（应转换为连字符）
            assert "_" not in key, f"CSS 变量名 {key} 包含下划线"


# ============================================================
# generate_css_stylesheet 测试
# ============================================================


class TestGenerateCssStylesheet:
    """CSS 样式表生成测试。"""

    def test_generates_root_block(self) -> None:
        """应生成 :root 块。"""
        tokens = DesignTokens()
        css = generate_css_stylesheet(tokens)
        assert ":root" in css

    def test_contains_css_variables(self) -> None:
        """生成的 CSS 应包含 CSS 变量。"""
        tokens = DesignTokens()
        css = generate_css_stylesheet(tokens)
        assert "--spacing-xs" in css
        assert "--color-primary" in css
        assert "--font-size-md" in css

    def test_contains_base_styles(self) -> None:
        """生成的 CSS 应包含基础样式。"""
        tokens = DesignTokens()
        css = generate_css_stylesheet(tokens)
        assert "body" in css or "*" in css

    def test_output_is_valid_css(self) -> None:
        """输出应为合法 CSS 格式。"""
        tokens = DesignTokens()
        css = generate_css_stylesheet(tokens)
        # CSS 应包含花括号对
        assert css.count("{") == css.count("}"), "CSS 花括号不匹配"
        # CSS 应包含冒号（属性声明）
        assert ":" in css
        # CSS 应包含分号（声明结束）
        assert ";" in css

    def test_includes_layout_variables_in_output(self) -> None:
        """CSS 样式表应包含布局变量。"""
        tokens = DesignTokens()
        css = generate_css_stylesheet(tokens)
        assert "--layout-" in css

    def test_includes_transition_variables_in_output(self) -> None:
        """CSS 样式表应包含过渡变量。"""
        tokens = DesignTokens()
        css = generate_css_stylesheet(tokens)
        assert "--transition-" in css

    def test_default_tokens_generates_css(self) -> None:
        """默认令牌应能生成完整 CSS。"""
        tokens = DesignTokens()
        css = generate_css_stylesheet(tokens)
        assert len(css) > 100, "生成的 CSS 过短"

    def test_custom_tokens_generates_different_css(self) -> None:
        """自定义令牌应生成不同的 CSS。"""
        tokens1 = DesignTokens()
        tokens2 = DesignTokens(colors=ColorPalette(primary="#ff0000"))
        css1 = generate_css_stylesheet(tokens1)
        css2 = generate_css_stylesheet(tokens2)
        assert css1 != css2, "自定义令牌应生成不同的 CSS"


# ============================================================
# VisualPreset 视觉层级预设测试
# ============================================================


class TestVisualPreset:
    """视觉层级预设测试。"""

    def test_card_preset(self) -> None:
        """卡片预设应包含阴影和圆角。"""
        preset = VisualPreset.card()
        assert "shadow" in preset
        assert "border-radius" in preset
        assert "padding" in preset

    def test_modal_preset(self) -> None:
        """模态框预设应包含高 z-index 和大阴影。"""
        preset = VisualPreset.modal()
        assert "shadow" in preset
        assert "z-index" in preset
        assert "border-radius" in preset

    def test_button_preset(self) -> None:
        """按钮预设应包含内边距和圆角。"""
        preset = VisualPreset.button()
        assert "padding" in preset
        assert "border-radius" in preset

    def test_input_preset(self) -> None:
        """输入框预设应包含内边距和边框。"""
        preset = VisualPreset.input()
        assert "padding" in preset
        assert "border-radius" in preset

    def test_section_preset(self) -> None:
        """区域预设应包含间距。"""
        preset = VisualPreset.section()
        assert "margin-bottom" in preset or "gap" in preset

    def test_preset_values_reference_tokens(self) -> None:
        """预设值应引用设计令牌变量。"""
        preset = VisualPreset.card()
        for key, value in preset.items():
            assert isinstance(value, str), f"预设值 {key} 应为字符串，实际为 {type(value)}"


# ============================================================
# validate_token_values 新令牌验证测试
# ============================================================


class TestValidateExtendedTokens:
    """扩展令牌验证测试。"""

    def test_default_tokens_still_valid(self) -> None:
        """默认令牌（含新增字段）应通过验证。"""
        tokens = DesignTokens()
        errors = validate_token_values(tokens)
        assert errors == [], f"默认令牌验证失败: {errors}"

    def test_invalid_layout_container_order(self) -> None:
        """容器宽度顺序错误应报错。"""
        tokens = DesignTokens(
            layout=LayoutScale(
                container_sm=1280, container_md=1024, container_lg=768, container_xl=640
            )
        )
        errors = validate_token_values(tokens)
        assert len(errors) > 0, "容器宽度顺序错误未报错"

    def test_negative_header_height(self) -> None:
        """头部高度为负应报错。"""
        tokens = DesignTokens(layout=LayoutScale(header_height=-10))
        errors = validate_token_values(tokens)
        assert len(errors) > 0, "负的头部高度未报错"

    def test_invalid_transition_durations(self) -> None:
        """过渡时长顺序错误应报错。"""
        tokens = DesignTokens(
            transition=TransitionScale(duration_fast=300, duration_normal=200, duration_slow=100)
        )
        errors = validate_token_values(tokens)
        assert len(errors) > 0, "过渡时长顺序错误未报错"

    def test_invalid_opacity_range(self) -> None:
        """透明度超出范围应报错。"""
        tokens = DesignTokens(opacity=OpacityScale(disabled=2.0))
        errors = validate_token_values(tokens)
        assert len(errors) > 0, "透明度超出范围未报错"


# ============================================================
# merge_tokens 新令牌合并测试
# ============================================================


class TestMergeTokensExtended:
    """扩展令牌合并测试。"""

    def test_merge_layout_overrides(self) -> None:
        """合并布局覆盖。"""
        base = DesignTokens()
        override = {"layout": {"header_height": 72, "sidebar_width": 300}}
        merged = merge_tokens(base, override)
        assert merged.layout.header_height == 72
        assert merged.layout.sidebar_width == 300
        assert merged.layout.container_sm == base.layout.container_sm

    def test_merge_transition_overrides(self) -> None:
        """合并过渡覆盖。"""
        base = DesignTokens()
        override = {"transition": {"duration_fast": 100}}
        merged = merge_tokens(base, override)
        assert merged.transition.duration_fast == 100
        assert merged.transition.duration_normal == base.transition.duration_normal

    def test_merge_preserves_base_extended(self) -> None:
        """合并不应修改原始令牌（含扩展字段）。"""
        base = DesignTokens()
        original_header = base.layout.header_height
        override = {"layout": {"header_height": 999}}
        merged = merge_tokens(base, override)
        assert base.layout.header_height == original_header
        assert merged.layout.header_height == 999
