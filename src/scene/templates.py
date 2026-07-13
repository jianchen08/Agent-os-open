"""预设场景模板库。

提供至少 3 个预设场景模板，用户可基于模板快速创建场景。

暴露接口：
- PRESET_TEMPLATES: 预设模板列表
- get_template: 按 ID 获取模板
- list_templates: 列出所有模板
"""

from __future__ import annotations

from .models import (
    SceneLayoutConfig,
    SceneLayoutType,
    SceneTemplate,
    SceneWidgetConfig,
)

# ---- 聊天工作台模板 ----
CHAT_WORKSPACE_TEMPLATE = SceneTemplate(
    id="chat_workspace",
    name="聊天工作台",
    description="左侧聊天面板 + 右侧工作区，适合对话驱动的任务场景",
    icon="💬",
    category="workspace",
    layout=SceneLayoutConfig(
        type=SceneLayoutType.SPLIT,
        direction="horizontal",
        ratio=[2, 3],
    ),
    widgets=[
        SceneWidgetConfig(
            widget_type="chat",
            props={"placeholder": "输入消息..."},
            position=0,
        ),
        SceneWidgetConfig(
            widget_type="workspace",
            props={},
            position=1,
        ),
    ],
)

# ---- 媒体展示模板 ----
MEDIA_GALLERY_TEMPLATE = SceneTemplate(
    id="media_gallery",
    name="媒体展示",
    description="图像画廊 + 音频播放器，适合展示多媒体内容",
    icon="🖼️",
    category="media",
    layout=SceneLayoutConfig(
        type=SceneLayoutType.STACK,
    ),
    widgets=[
        SceneWidgetConfig(
            widget_type="image_gallery",
            props={"columns": 3, "enableLightbox": True},
            position=0,
        ),
        SceneWidgetConfig(
            widget_type="audio_player",
            props={"autoplay": False, "showControls": True},
            position=1,
        ),
    ],
)

# ---- 仪表盘模板 ----
DASHBOARD_TEMPLATE = SceneTemplate(
    id="dashboard",
    name="仪表盘",
    description="多面板仪表盘布局，包含图表、表格和状态卡片",
    icon="📊",
    category="dashboard",
    layout=SceneLayoutConfig(
        type=SceneLayoutType.GRID,
        columns=3,
    ),
    widgets=[
        SceneWidgetConfig(
            widget_type="chart",
            props={"chartType": "line", "title": "趋势图"},
            position=0,
        ),
        SceneWidgetConfig(
            widget_type="table",
            props={"title": "数据列表", "pageSize": 10},
            position=1,
        ),
        SceneWidgetConfig(
            widget_type="status_card",
            props={"title": "系统状态", "showMetrics": True},
            position=2,
        ),
    ],
)

# ---- 预设模板列表 ----
PRESET_TEMPLATES: list[SceneTemplate] = [
    CHAT_WORKSPACE_TEMPLATE,
    MEDIA_GALLERY_TEMPLATE,
    DASHBOARD_TEMPLATE,
]

# 模板索引（按 ID 查找）
_TEMPLATE_MAP: dict[str, SceneTemplate] = {t.id: t for t in PRESET_TEMPLATES}


def get_template(template_id: str) -> SceneTemplate | None:
    """按 ID 获取模板。

    Args:
        template_id: 模板 ID

    Returns:
        模板对象，不存在则返回 None
    """
    return _TEMPLATE_MAP.get(template_id)


def list_templates() -> list[SceneTemplate]:
    """列出所有预设模板。

    Returns:
        模板列表
    """
    return list(PRESET_TEMPLATES)
