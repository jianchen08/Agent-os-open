"""设计令牌（Design Tokens）系统。

提供统一的 UI 设计变量，包括间距、色彩、字体、阴影、圆角、布局、
Z轴层级、过渡动画、透明度等视觉规范。
所有设计令牌可通过 CSS 变量传递给前端，确保视觉一致性。

公共 API:
    SpacingScale: 间距梯度（4px 倍数体系）
    ColorPalette: 色彩系统（主色/语义色/中性色）
    TypographyScale: 字体梯度
    ShadowScale: 阴影层次
    BorderRadiusScale: 圆角规范
    LayoutScale: 布局令牌（容器/导航/侧边栏尺寸）
    ZIndexScale: Z轴层级令牌
    TransitionScale: 过渡/动画令牌
    OpacityScale: 透明度令牌
    VisualPreset: 视觉层级预设（卡片/模态框/按钮等）
    DesignTokens: 完整设计令牌集合
    tokens_to_css_variables: 令牌转 CSS 变量
    generate_css_stylesheet: 生成完整 CSS 样式表
    merge_tokens: 合并/覆盖令牌
    validate_token_values: 令牌值合法性验证
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field


# ---- 间距梯度 ----

class SpacingScale(BaseModel):
    """间距梯度。

    遵循 4px 基础单位倍数体系，提供从 xs 到 xxl 的间距梯度。
    所有间距值必须是 ``unit`` 的整数倍。

    Attributes:
        unit: 基础间距单位（px），默认 4。
        xs: 极小间距，默认 4px。
        sm: 小间距，默认 8px。
        md: 中等间距，默认 16px。
        lg: 大间距，默认 24px。
        xl: 超大间距，默认 32px。
        xxl: 极大间距，默认 48px。
    """

    unit: int = Field(default=4, description="基础间距单位（px）")
    xs: int = Field(default=4, description="极小间距")
    sm: int = Field(default=8, description="小间距")
    md: int = Field(default=16, description="中等间距")
    lg: int = Field(default=24, description="大间距")
    xl: int = Field(default=32, description="超大间距")
    xxl: int = Field(default=48, description="极大间距")


# ---- 色彩系统 ----

class ColorPalette(BaseModel):
    """色彩系统。

    包含主色、语义色（成功/警告/错误/信息）和中性色（背景/前景/边框/禁用）。

    Attributes:
        primary: 主色。
        primary_light: 主色浅色变体。
        primary_dark: 主色深色变体。
        success: 成功色。
        warning: 警告色。
        error: 错误色。
        info: 信息色。
        bg_primary: 主背景色。
        bg_secondary: 次背景色。
        fg_primary: 主前景色（文字）。
        fg_secondary: 次前景色（辅助文字）。
        border: 边框色。
        disabled: 禁用状态色。
    """

    primary: str = Field(default="#1677ff", description="主色")
    primary_light: str = Field(default="#4096ff", description="主色浅色变体")
    primary_dark: str = Field(default="#0958d9", description="主色深色变体")
    success: str = Field(default="#52c41a", description="成功色")
    warning: str = Field(default="#faad14", description="警告色")
    error: str = Field(default="#ff4d4f", description="错误色")
    info: str = Field(default="#1677ff", description="信息色")
    bg_primary: str = Field(default="#ffffff", description="主背景色")
    bg_secondary: str = Field(default="#f5f5f5", description="次背景色")
    fg_primary: str = Field(default="#1f1f1f", description="主前景色")
    fg_secondary: str = Field(default="#8c8c8c", description="次前景色")
    border: str = Field(default="#d9d9d9", description="边框色")
    disabled: str = Field(default="#bfbfbf", description="禁用状态色")


# ---- 字体梯度 ----

class TypographyScale(BaseModel):
    """字体梯度。

    定义字号、字体族、行高和字重的标准梯度。

    Attributes:
        font_family: 字体族。
        font_size_xs: 极小字号（12px）。
        font_size_sm: 小字号（13px）。
        font_size_md: 中等字号（14px）。
        font_size_lg: 大字号（16px）。
        font_size_xl: 超大字号（20px）。
        font_size_xxl: 极大字号（24px）。
        line_height_base: 基础行高。
        line_height_tight: 紧凑行高。
        line_height_loose: 宽松行高。
        font_weight_normal: 正常字重。
        font_weight_medium: 中等字重。
        font_weight_bold: 粗体字重。
    """

    font_family: str = Field(
        default="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
        description="字体族",
    )
    font_size_xs: int = Field(default=12, description="极小字号")
    font_size_sm: int = Field(default=13, description="小字号")
    font_size_md: int = Field(default=14, description="中等字号")
    font_size_lg: int = Field(default=16, description="大字号")
    font_size_xl: int = Field(default=20, description="超大字号")
    font_size_xxl: int = Field(default=24, description="极大字号")
    line_height_base: float = Field(default=1.5, description="基础行高")
    line_height_tight: float = Field(default=1.25, description="紧凑行高")
    line_height_loose: float = Field(default=1.75, description="宽松行高")
    font_weight_normal: int = Field(default=400, description="正常字重")
    font_weight_medium: int = Field(default=500, description="中等字重")
    font_weight_bold: int = Field(default=700, description="粗体字重")


# ---- 阴影层次 ----

class ShadowScale(BaseModel):
    """阴影层次。

    定义从 none 到 xl 的阴影梯度，用于区分视觉层次（elevation）。

    Attributes:
        none: 无阴影。
        sm: 小阴影（微弱层次感）。
        md: 中等阴影（卡片层次感）。
        lg: 大阴影（弹出层层次感）。
        xl: 超大阴影（对话框层次感）。
    """

    none: str = Field(default="none", description="无阴影")
    sm: str = Field(
        default="0 1px 2px 0 rgba(0, 0, 0, 0.03), 0 1px 6px -1px rgba(0, 0, 0, 0.02), 0 2px 4px 0 rgba(0, 0, 0, 0.02)",
        description="小阴影",
    )
    md: str = Field(
        default="0 3px 6px -4px rgba(0, 0, 0, 0.08), 0 6px 16px 0 rgba(0, 0, 0, 0.04), 0 9px 28px 8px rgba(0, 0, 0, 0.03)",
        description="中等阴影",
    )
    lg: str = Field(
        default="0 6px 16px -8px rgba(0, 0, 0, 0.08), 0 9px 28px 0 rgba(0, 0, 0, 0.05), 0 12px 48px 16px rgba(0, 0, 0, 0.03)",
        description="大阴影",
    )
    xl: str = Field(
        default="0 9px 28px 8px rgba(0, 0, 0, 0.05), 0 12px 48px 16px rgba(0, 0, 0, 0.05), 0 20px 64px 24px rgba(0, 0, 0, 0.03)",
        description="超大阴影",
    )


# ---- 圆角规范 ----

class BorderRadiusScale(BaseModel):
    """圆角规范。

    定义从 none 到 full 的圆角梯度，确保组件圆角一致性。

    Attributes:
        none: 无圆角（0）。
        sm: 小圆角（2px）。
        md: 中等圆角（6px）。
        lg: 大圆角（8px）。
        xl: 超大圆角（12px）。
        full: 完全圆角（50%）。
    """

    none: int = Field(default=0, description="无圆角")
    sm: int = Field(default=2, description="小圆角")
    md: int = Field(default=6, description="中等圆角")
    lg: int = Field(default=8, description="大圆角")
    xl: int = Field(default=12, description="超大圆角")
    full: str = Field(default="50%", description="完全圆角")


# ---- 布局令牌 ----

class LayoutScale(BaseModel):
    """布局令牌。

    定义容器宽度、头部高度、侧边栏宽度等布局尺寸规范。
    所有尺寸值应为 4px 的整数倍。

    Attributes:
        container_sm: 小容器最大宽度（640px）。
        container_md: 中容器最大宽度（768px）。
        container_lg: 大容器最大宽度（1024px）。
        container_xl: 超大容器最大宽度（1280px）。
        header_height: 头部导航高度（56px）。
        sidebar_width: 侧边栏展开宽度（240px）。
        sidebar_collapsed_width: 侧边栏折叠宽度（64px）。
        footer_height: 底部栏高度（48px）。
    """

    container_sm: int = Field(default=640, description="小容器最大宽度")
    container_md: int = Field(default=768, description="中容器最大宽度")
    container_lg: int = Field(default=1024, description="大容器最大宽度")
    container_xl: int = Field(default=1280, description="超大容器最大宽度")
    header_height: int = Field(default=56, description="头部导航高度")
    sidebar_width: int = Field(default=240, description="侧边栏展开宽度")
    sidebar_collapsed_width: int = Field(default=64, description="侧边栏折叠宽度")
    footer_height: int = Field(default=48, description="底部栏高度")


# ---- Z轴层级令牌 ----

class ZIndexScale(BaseModel):
    """Z轴层级令牌。

    定义组件的 z-index 层级梯度，确保层叠顺序统一。
    层级从低到高递增，高层级组件应覆盖低层级。

    Attributes:
        base: 基础层级（0）。
        dropdown: 下拉菜单层级（1000）。
        sticky: 粘性定位层级（1020）。
        fixed: 固定定位层级（1030）。
        modal_backdrop: 模态框遮罩层级（1040）。
        modal: 模态框层级（1050）。
        popover: 弹出框层级（1060）。
        tooltip: 工具提示层级（1070）。
    """

    base: int = Field(default=0, description="基础层级")
    dropdown: int = Field(default=1000, description="下拉菜单层级")
    sticky: int = Field(default=1020, description="粘性定位层级")
    fixed: int = Field(default=1030, description="固定定位层级")
    modal_backdrop: int = Field(default=1040, description="模态框遮罩层级")
    modal: int = Field(default=1050, description="模态框层级")
    popover: int = Field(default=1060, description="弹出框层级")
    tooltip: int = Field(default=1070, description="工具提示层级")


# ---- 过渡/动画令牌 ----

class TransitionScale(BaseModel):
    """过渡/动画令牌。

    定义组件过渡动画的时长和缓动函数。

    Attributes:
        duration_fast: 快速过渡时长（150ms）。
        duration_normal: 正常过渡时长（250ms）。
        duration_slow: 慢速过渡时长（350ms）。
        easing_default: 默认缓动函数。
        easing_in: 进入缓动函数。
        easing_out: 退出缓动函数。
        easing_in_out: 进入退出缓动函数。
    """

    duration_fast: int = Field(default=150, description="快速过渡时长（ms）")
    duration_normal: int = Field(default=250, description="正常过渡时长（ms）")
    duration_slow: int = Field(default=350, description="慢速过渡时长（ms）")
    easing_default: str = Field(
        default="cubic-bezier(0.4, 0, 0.2, 1)",
        description="默认缓动函数",
    )
    easing_in: str = Field(
        default="cubic-bezier(0.4, 0, 1, 1)",
        description="进入缓动函数",
    )
    easing_out: str = Field(
        default="cubic-bezier(0, 0, 0.2, 1)",
        description="退出缓动函数",
    )
    easing_in_out: str = Field(
        default="cubic-bezier(0.4, 0, 0.2, 1)",
        description="进入退出缓动函数",
    )


# ---- 透明度令牌 ----

class OpacityScale(BaseModel):
    """透明度令牌。

    定义组件在不同交互状态下的透明度值。
    值范围 0.0（完全透明）到 1.0（完全不透明）。

    Attributes:
        disabled: 禁用状态透明度（0.4）。
        hover: 悬停状态透明度（0.8）。
        secondary: 次要内容透明度（0.65）。
        backdrop: 遮罩层透明度（0.5）。
    """

    disabled: float = Field(default=0.4, description="禁用状态透明度")
    hover: float = Field(default=0.8, description="悬停状态透明度")
    secondary: float = Field(default=0.65, description="次要内容透明度")
    backdrop: float = Field(default=0.5, description="遮罩层透明度")


# ---- 视觉层级预设 ----

class VisualPreset:
    """视觉层级预设。

    提供常用组件的视觉样式预设，引用设计令牌变量。
    每个预设返回一组 CSS 属性值（引用 CSS 变量）。

    所有预设方法均为类方法，可直接调用而无需实例化。
    """

    @classmethod
    def card(cls) -> dict[str, str]:
        """卡片组件预设。

        Returns:
            卡片样式属性字典。
        """
        return {
            "shadow": "var(--shadow-md)",
            "border-radius": "var(--border-radius-lg)",
            "padding": "var(--spacing-md)",
            "background": "var(--color-bg-primary)",
            "border": "1px solid var(--color-border)",
        }

    @classmethod
    def modal(cls) -> dict[str, str]:
        """模态框组件预设。

        Returns:
            模态框样式属性字典。
        """
        return {
            "shadow": "var(--shadow-xl)",
            "z-index": "var(--z-index-modal)",
            "border-radius": "var(--border-radius-lg)",
            "padding": "var(--spacing-lg)",
            "background": "var(--color-bg-primary)",
        }

    @classmethod
    def button(cls) -> dict[str, str]:
        """按钮组件预设。

        Returns:
            按钮样式属性字典。
        """
        return {
            "padding": "var(--spacing-xs) var(--spacing-md)",
            "border-radius": "var(--border-radius-md)",
            "font-size": "var(--font-size-md)",
            "font-weight": "var(--font-weight-medium)",
            "transition": "all var(--transition-duration-fast) var(--transition-easing-default)",
        }

    @classmethod
    def input(cls) -> dict[str, str]:
        """输入框组件预设。

        Returns:
            输入框样式属性字典。
        """
        return {
            "padding": "var(--spacing-xs) var(--spacing-sm)",
            "border-radius": "var(--border-radius-md)",
            "border": "1px solid var(--color-border)",
            "font-size": "var(--font-size-md)",
            "line-height": "var(--line-height-base)",
        }

    @classmethod
    def section(cls) -> dict[str, str]:
        """区域/区块预设。

        Returns:
            区域样式属性字典。
        """
        return {
            "margin-bottom": "var(--spacing-lg)",
            "gap": "var(--spacing-md)",
            "padding": "var(--spacing-md)",
        }


# ---- 完整设计令牌 ----

class DesignTokens(BaseModel):
    """完整设计令牌集合。

    聚合所有子令牌系统（间距/色彩/字体/阴影/圆角/布局/层级/过渡/透明度），
    可序列化为 CSS 变量传递给前端。

    Attributes:
        spacing: 间距梯度。
        colors: 色彩系统。
        typography: 字体梯度。
        shadow: 阴影层次。
        border_radius: 圆角规范。
        layout: 布局令牌。
        z_index: Z轴层级令牌。
        transition: 过渡/动画令牌。
        opacity: 透明度令牌。
    """

    spacing: SpacingScale = Field(default_factory=SpacingScale, description="间距梯度")
    colors: ColorPalette = Field(default_factory=ColorPalette, description="色彩系统")
    typography: TypographyScale = Field(
        default_factory=TypographyScale, description="字体梯度"
    )
    shadow: ShadowScale = Field(default_factory=ShadowScale, description="阴影层次")
    border_radius: BorderRadiusScale = Field(
        default_factory=BorderRadiusScale, description="圆角规范"
    )
    layout: LayoutScale = Field(default_factory=LayoutScale, description="布局令牌")
    z_index: ZIndexScale = Field(default_factory=ZIndexScale, description="Z轴层级令牌")
    transition: TransitionScale = Field(
        default_factory=TransitionScale, description="过渡/动画令牌"
    )
    opacity: OpacityScale = Field(default_factory=OpacityScale, description="透明度令牌")


# ---- CSS 变量转换 ----

# 十六进制颜色校验正则
_HEX_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")


def tokens_to_css_variables(tokens: DesignTokens) -> dict[str, str]:
    """将设计令牌转换为 CSS 变量字典。

    生成 ``--前缀-名称: 值`` 格式的 CSS 变量，前端可直接使用。

    Args:
        tokens: 设计令牌集合。

    Returns:
        CSS 变量字典，key 为 ``--xxx`` 格式，value 为 CSS 合法值字符串。
    """
    variables: dict[str, str] = {}

    # 间距
    spacing = tokens.spacing
    variables["--spacing-unit"] = f"{spacing.unit}px"
    for attr in ("xs", "sm", "md", "lg", "xl", "xxl"):
        variables[f"--spacing-{attr}"] = f"{getattr(spacing, attr)}px"

    # 色彩
    colors = tokens.colors
    for attr in (
        "primary", "primary_light", "primary_dark",
        "success", "warning", "error", "info",
        "bg_primary", "bg_secondary",
        "fg_primary", "fg_secondary",
        "border", "disabled",
    ):
        variables[f"--color-{attr.replace('_', '-')}"] = getattr(colors, attr)

    # 字体
    typo = tokens.typography
    variables["--font-family"] = typo.font_family
    for attr in ("xs", "sm", "md", "lg", "xl", "xxl"):
        variables[f"--font-size-{attr}"] = f"{getattr(typo, f'font_size_{attr}')}px"
    variables["--line-height-base"] = str(typo.line_height_base)
    variables["--line-height-tight"] = str(typo.line_height_tight)
    variables["--line-height-loose"] = str(typo.line_height_loose)
    variables["--font-weight-normal"] = str(typo.font_weight_normal)
    variables["--font-weight-medium"] = str(typo.font_weight_medium)
    variables["--font-weight-bold"] = str(typo.font_weight_bold)

    # 阴影
    shadow = tokens.shadow
    for attr in ("none", "sm", "md", "lg", "xl"):
        variables[f"--shadow-{attr}"] = getattr(shadow, attr)

    # 圆角
    radius = tokens.border_radius
    for attr in ("none", "sm", "md", "lg", "xl"):
        variables[f"--border-radius-{attr}"] = f"{getattr(radius, attr)}px"
    variables["--border-radius-full"] = radius.full

    # 布局
    layout = tokens.layout
    for attr in ("container_sm", "container_md", "container_lg", "container_xl"):
        variables[f"--layout-{attr.replace('_', '-')}"] = f"{getattr(layout, attr)}px"
    variables["--layout-header-height"] = f"{layout.header_height}px"
    variables["--layout-sidebar-width"] = f"{layout.sidebar_width}px"
    variables["--layout-sidebar-collapsed-width"] = f"{layout.sidebar_collapsed_width}px"
    variables["--layout-footer-height"] = f"{layout.footer_height}px"

    # Z轴层级
    z = tokens.z_index
    for attr in (
        "base", "dropdown", "sticky", "fixed",
        "modal_backdrop", "modal", "popover", "tooltip",
    ):
        variables[f"--z-index-{attr.replace('_', '-')}"] = str(getattr(z, attr))

    # 过渡/动画
    trans = tokens.transition
    variables["--transition-duration-fast"] = f"{trans.duration_fast}ms"
    variables["--transition-duration-normal"] = f"{trans.duration_normal}ms"
    variables["--transition-duration-slow"] = f"{trans.duration_slow}ms"
    variables["--transition-easing-default"] = trans.easing_default
    variables["--transition-easing-in"] = trans.easing_in
    variables["--transition-easing-out"] = trans.easing_out
    variables["--transition-easing-in-out"] = trans.easing_in_out

    # 透明度
    opacity = tokens.opacity
    variables["--opacity-disabled"] = str(opacity.disabled)
    variables["--opacity-hover"] = str(opacity.hover)
    variables["--opacity-secondary"] = str(opacity.secondary)
    variables["--opacity-backdrop"] = str(opacity.backdrop)

    return variables


def generate_css_stylesheet(tokens: DesignTokens) -> str:
    """生成完整的 CSS 样式表。

    包含 :root CSS 变量声明和基础重置样式。
    前端可直接引入使用。

    Args:
        tokens: 设计令牌集合。

    Returns:
        完整的 CSS 样式表字符串。
    """
    css_vars = tokens_to_css_variables(tokens)

    # :root 变量块
    lines: list[str] = [":root {"]
    for key, value in sorted(css_vars.items()):
        lines.append(f"  {key}: {value};")
    lines.append("}")
    lines.append("")

    # 基础重置样式
    lines.append("*, *::before, *::after {")
    lines.append("  box-sizing: border-box;")
    lines.append("  margin: 0;")
    lines.append("  padding: 0;")
    lines.append("}")
    lines.append("")

    lines.append("body {")
    lines.append("  font-family: var(--font-family);")
    lines.append("  font-size: var(--font-size-md);")
    lines.append("  line-height: var(--line-height-base);")
    lines.append("  color: var(--color-fg-primary);")
    lines.append("  background-color: var(--color-bg-primary);")
    lines.append("  -webkit-font-smoothing: antialiased;")
    lines.append("  -moz-osx-font-smoothing: grayscale;")
    lines.append("}")
    lines.append("")

    # 通用过渡
    lines.append("button, input, select, textarea {")
    lines.append("  transition: all var(--transition-duration-fast) var(--transition-easing-default);")
    lines.append("}")

    return "\n".join(lines)


def merge_tokens(
    base: DesignTokens, overrides: dict[str, Any]
) -> DesignTokens:
    """合并设计令牌：用覆盖字典更新基础令牌。

    仅覆盖指定字段，未覆盖的保持原值。不修改原始 ``base`` 对象。

    Args:
        base: 基础设计令牌。
        overrides: 覆盖字典，结构与 DesignTokens 子字段对应。

    Returns:
        合并后的新 DesignTokens 实例。
    """
    base_data = base.model_dump()

    for section_key, section_overrides in overrides.items():
        if section_key in base_data and isinstance(section_overrides, dict):
            base_data[section_key].update(section_overrides)

    return DesignTokens(**base_data)


def validate_token_values(tokens: DesignTokens) -> list[str]:
    """验证设计令牌值的合法性。

    检查：
    - 间距值是否为基础单位的整数倍
    - 间距梯度是否递增
    - 色彩值是否为合法十六进制格式
    - 字号梯度是否递增
    - 布局容器宽度是否递增
    - 布局尺寸是否为正值
    - 过渡时长是否递增
    - 透明度值是否在 0-1 范围内

    Args:
        tokens: 待验证的设计令牌。

    Returns:
        错误列表，空列表表示全部通过。
    """
    errors: list[str] = []

    # 间距验证
    spacing = tokens.spacing
    spacing_values = [spacing.xs, spacing.sm, spacing.md, spacing.lg, spacing.xl, spacing.xxl]
    spacing_names = ["xs", "sm", "md", "lg", "xl", "xxl"]

    for name, value in zip(spacing_names, spacing_values):
        if spacing.unit > 0 and value % spacing.unit != 0:
            errors.append(
                f"spacing.{name}={value} 不是基础单位 {spacing.unit}px 的倍数"
            )

    if spacing_values != sorted(spacing_values):
        errors.append("间距梯度未递增排列")

    # 色彩验证
    colors = tokens.colors
    color_attrs = [
        "primary", "primary_light", "primary_dark",
        "success", "warning", "error", "info",
        "bg_primary", "bg_secondary",
        "fg_primary", "fg_secondary",
        "border", "disabled",
    ]
    for attr in color_attrs:
        value = getattr(colors, attr)
        if not _HEX_COLOR_PATTERN.match(value):
            errors.append(
                f"colors.{attr}='{value}' 不是合法十六进制颜色（需 #RRGGBB 格式）"
            )

    # 字号验证
    typo = tokens.typography
    font_sizes = [
        typo.font_size_xs, typo.font_size_sm, typo.font_size_md,
        typo.font_size_lg, typo.font_size_xl, typo.font_size_xxl,
    ]
    if font_sizes != sorted(font_sizes):
        errors.append("字号梯度未递增排列")

    # 布局验证
    layout = tokens.layout
    container_widths = [
        layout.container_sm, layout.container_md,
        layout.container_lg, layout.container_xl,
    ]
    if container_widths != sorted(container_widths):
        errors.append("布局容器宽度未递增排列")

    for attr in ("header_height", "sidebar_width", "sidebar_collapsed_width", "footer_height"):
        value = getattr(layout, attr)
        if value <= 0:
            errors.append(f"layout.{attr}={value} 必须为正数")

    # 过渡时长验证
    trans = tokens.transition
    durations = [trans.duration_fast, trans.duration_normal, trans.duration_slow]
    if durations != sorted(durations):
        errors.append("过渡时长未递增排列")

    # 透明度验证
    opacity = tokens.opacity
    for attr in ("disabled", "hover", "secondary", "backdrop"):
        value = getattr(opacity, attr)
        if value < 0 or value > 1:
            errors.append(f"opacity.{attr}={value} 不在合法范围 [0, 1] 内")

    return errors
