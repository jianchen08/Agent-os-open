"""样式配置（Style Config）系统。

提供模块级和场景级的样式配置，支持主题切换、自定义覆盖和响应式断点。
与 DesignTokens 结合使用，确保样式继承和覆盖的一致性。

公共 API:
    ModuleStyleConfig: 模块级样式配置
    SceneStyleConfig: 场景级样式配置
    BreakpointConfig: 响应式断点配置
    ThemeName: 合法主题名称类型
    validate_style_config: 样式配置验证
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

# 合法主题名称
ThemeName = Literal["light", "dark"]
"""合法主题名称：light（浅色）、dark（深色）。"""

# 合法阴影层次值
_VALID_ELEVATIONS: set[str] = {"none", "sm", "md", "lg", "xl"}

# 合法圆角值
_VALID_BORDER_RADIUS: set[str] = {"none", "sm", "md", "lg", "xl", "full"}

# 十六进制颜色校验正则
_HEX_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")


class ModuleStyleConfig(BaseModel):
    """模块级样式配置。

    允许单个模块覆盖全局设计令牌的特定值，如颜色、间距、圆角等。
    未指定的字段继承全局主题默认值。

    Attributes:
        theme: 主题名称（light/dark），None 表示继承全局主题。
        custom_spacing: 自定义间距覆盖，键为梯度名（xs/sm/md/lg/xl/xxl）。
        custom_colors: 自定义颜色覆盖，键为颜色名。
        border_radius: 圆角预设名（none/sm/md/lg/xl/full）。
        elevation: 阴影层次（none/sm/md/lg/xl）。
    """

    theme: str | None = Field(default=None, description="主题名称")
    custom_spacing: dict[str, int] | None = Field(
        default=None, alias="customSpacing", description="自定义间距覆盖"
    )
    custom_colors: dict[str, str] | None = Field(
        default=None, alias="customColors", description="自定义颜色覆盖"
    )
    border_radius: str | None = Field(
        default=None, alias="borderRadius", description="圆角预设名"
    )
    elevation: str | None = Field(
        default=None, description="阴影层次"
    )

    model_config = {"populate_by_name": True}


class SceneStyleConfig(BaseModel):
    """场景级样式配置。

    定义场景整体布局的间距、背景、最大宽度等视觉属性。
    所有间距值应遵循 4px 倍数体系。

    Attributes:
        theme: 主题名称（light/dark），None 表示继承全局主题。
        gap: 组件之间的间距（px），应为 4 的倍数。
        padding: 场景内边距（px），应为 4 的倍数。
        max_width: 场景最大宽度（px）。
        background: 场景背景色。
    """

    theme: str | None = Field(default=None, description="主题名称")
    gap: int | None = Field(default=None, description="组件间距（px）")
    padding: int | None = Field(default=None, description="内边距（px）")
    max_width: int | None = Field(
        default=None, alias="maxWidth", description="最大宽度（px）"
    )
    background: str | None = Field(
        default=None, description="背景色"
    )

    model_config = {"populate_by_name": True}


class BreakpointConfig(BaseModel):
    """响应式断点配置。

    定义不同屏幕尺寸的断点值，前端据此调整布局。

    Attributes:
        sm: 小屏幕断点（640px）。
        md: 中等屏幕断点（768px）。
        lg: 大屏幕断点（1024px）。
        xl: 超大屏幕断点（1280px）。
    """

    sm: int = Field(default=640, description="小屏幕断点")
    md: int = Field(default=768, description="中等屏幕断点")
    lg: int = Field(default=1024, description="大屏幕断点")
    xl: int = Field(default=1280, description="超大屏幕断点")


def validate_style_config(config: ModuleStyleConfig) -> list[str]:
    """验证模块样式配置的合法性。

    检查：
    - theme 是否为合法主题名
    - elevation 是否为合法值
    - border_radius 是否为合法值
    - custom_colors 中的颜色值格式
    - custom_spacing 中的间距是否为 4 的倍数

    Args:
        config: 待验证的模块样式配置。

    Returns:
        错误列表，空列表表示全部通过。
    """
    errors: list[str] = []

    # 主题验证
    valid_themes: set[str] = {"light", "dark"}
    if config.theme is not None and config.theme not in valid_themes:
        errors.append(
            f"theme='{config.theme}' 不是合法主题名，合法值: {sorted(valid_themes)}"
        )

    # 阴影层次验证
    if config.elevation is not None and config.elevation not in _VALID_ELEVATIONS:
        errors.append(
            f"elevation='{config.elevation}' 不是合法值，"
            f"合法值: {sorted(_VALID_ELEVATIONS)}"
        )

    # 圆角验证
    if config.border_radius is not None and config.border_radius not in _VALID_BORDER_RADIUS:
        errors.append(
            f"border_radius='{config.border_radius}' 不是合法值，"
            f"合法值: {sorted(_VALID_BORDER_RADIUS)}"
        )

    # 自定义颜色格式验证
    if config.custom_colors is not None:
        for key, value in config.custom_colors.items():
            if not _HEX_COLOR_PATTERN.match(value):
                errors.append(
                    f"custom_colors.{key}='{value}' 不是合法十六进制颜色"
                )

    # 自定义间距验证
    if config.custom_spacing is not None:
        spacing_unit = 4
        for key, value in config.custom_spacing.items():
            if value % spacing_unit != 0:
                errors.append(
                    f"custom_spacing.{key}={value} 不是 {spacing_unit}px 的倍数"
                )

    return errors
