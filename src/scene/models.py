"""场景管理数据模型。

定义场景管理系统的核心数据结构，包括场景、场景状态、场景模板等。

暴露接口：
- Scene: 场景数据模型
- SceneState: 场景状态快照
- SceneTemplate: 场景模板
- SceneLayoutType: 布局类型枚举
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SceneLayoutType(str, Enum):
    """场景布局类型。

    Attributes:
        GRID: 网格布局
        SPLIT: 分割布局（水平/垂直）
        STACK: 堆叠布局
        TAB: 标签页布局
    """

    GRID = "grid"
    SPLIT = "split"
    STACK = "stack"
    TAB = "tab"


class SceneWidgetConfig(BaseModel):
    """场景内组件配置。

    Attributes:
        widget_type: 组件类型（如 chat、gallery、audio_player 等）
        props: 组件属性
        data_source: 数据源引用
        position: 在布局中的位置索引
    """

    widget_type: str = Field(..., description="组件类型")
    props: dict[str, Any] = Field(default_factory=dict, description="组件属性")
    data_source: str | None = Field(default=None, description="数据源引用")
    position: int = Field(default=0, ge=0, description="布局位置索引")


class SceneLayoutConfig(BaseModel):
    """场景布局配置。

    Attributes:
        type: 布局类型
        direction: 分割方向（仅 split 布局有效）
        columns: 网格列数（仅 grid 布局有效）
        ratio: 分割比例（仅 split 布局有效）
        default_tab: 默认标签页（仅 tab 布局有效）
    """

    type: SceneLayoutType = Field(default=SceneLayoutType.SPLIT, description="布局类型")
    direction: str | None = Field(
        default="horizontal",
        description="分割方向: horizontal/vertical",
    )
    columns: int | None = Field(default=2, description="网格列数")
    ratio: list[float] | None = Field(default=None, description="分割比例")
    default_tab: int | None = Field(default=0, description="默认标签页索引")


class SceneState(BaseModel):
    """场景状态快照。

    保存场景切换前的完整状态，用于恢复。

    Attributes:
        active_widget_id: 当前活跃的组件 ID
        scroll_position: 滚动位置
        widget_states: 各组件的状态数据
        custom_data: 自定义状态数据
    """

    active_widget_id: str | None = Field(default=None, description="活跃组件ID")
    scroll_position: dict[str, int] = Field(
        default_factory=lambda: {"x": 0, "y": 0},
        description="滚动位置",
    )
    widget_states: dict[str, Any] = Field(default_factory=dict, description="各组件状态")
    custom_data: dict[str, Any] = Field(default_factory=dict, description="自定义状态数据")


class Scene(BaseModel):
    """场景数据模型。

    代表一个完整的场景实例，包含布局配置和组件列表。

    Attributes:
        id: 场景唯一标识（UUID）
        name: 场景名称
        description: 场景描述
        template_id: 基于创建的模板 ID（可选）
        layout: 布局配置
        widgets: 场景内的组件列表
        state: 场景当前状态
        is_active: 是否为当前活跃场景
        created_at: 创建时间
        updated_at: 最后更新时间
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="场景ID")
    name: str = Field(..., description="场景名称")
    description: str = Field(default="", description="场景描述")
    template_id: str | None = Field(default=None, description="模板ID")
    layout: SceneLayoutConfig = Field(default_factory=SceneLayoutConfig, description="布局配置")
    widgets: list[SceneWidgetConfig] = Field(default_factory=list, description="组件列表")
    state: SceneState = Field(default_factory=SceneState, description="场景状态")
    is_active: bool = Field(default=False, description="是否活跃")
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat(), description="创建时间")
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat(), description="更新时间")


class SceneTemplate(BaseModel):
    """场景模板。

    预定义的场景配置，用户可基于模板快速创建场景。

    Attributes:
        id: 模板唯一标识
        name: 模板名称
        description: 模板描述
        icon: 模板图标
        layout: 预定义布局配置
        widgets: 预定义组件列表
        category: 模板分类
    """

    id: str = Field(..., description="模板ID")
    name: str = Field(..., description="模板名称")
    description: str = Field(default="", description="模板描述")
    icon: str = Field(default="📋", description="模板图标")
    layout: SceneLayoutConfig = Field(default_factory=SceneLayoutConfig, description="布局配置")
    widgets: list[SceneWidgetConfig] = Field(default_factory=list, description="组件列表")
    category: str = Field(default="general", description="模板分类")


class SceneCreateRequest(BaseModel):
    """创建场景请求。

    Attributes:
        name: 场景名称
        description: 场景描述
        template_id: 模板 ID（可选，基于模板创建时提供）
        layout: 布局配置（可选，覆盖模板默认布局）
        widgets: 组件列表（可选，覆盖模板默认组件）
    """

    name: str = Field(..., min_length=1, max_length=100, description="场景名称")
    description: str = Field(default="", max_length=500, description="场景描述")
    template_id: str | None = Field(default=None, description="模板ID")
    layout: SceneLayoutConfig | None = Field(default=None, description="布局配置")
    widgets: list[SceneWidgetConfig] | None = Field(default=None, description="组件列表")


class SceneUpdateRequest(BaseModel):
    """更新场景请求。

    Attributes:
        name: 新名称（可选）
        description: 新描述（可选）
        layout: 新布局配置（可选）
        widgets: 新组件列表（可选）
        state: 新场景状态（可选）
    """

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    layout: SceneLayoutConfig | None = None
    widgets: list[SceneWidgetConfig] | None = None
    state: SceneState | None = None
