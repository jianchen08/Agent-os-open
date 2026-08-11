"""UI Schema 类型定义。

使用 Pydantic 定义与前端 ``frontend/src/types/schema.ts`` 对应的后端类型。
所有类型均支持 YAML 解析友好的字段别名，并可通过 ``model_dump()`` 序列化为 JSON。

Schema 分为四部分：
- identity: 模块身份信息
- actions: 模块操作定义
- rendering: 渲染配置（聊天交互 + 渲染空间）
- clients: 客户端能力要求
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ---- 枚举类型 ----

CategoryType = Literal["builtin", "extension", "custom"]
"""模块分类：builtin=内置、extension=扩展、custom=自定义。"""

ActionType = Literal["command", "query", "event", "stream"]
"""操作类型。"""

ChatInteractionType = Literal[
    "form",
    "chart",
    "gallery",
    "table",
    "progress",
    "code_block",
    "status_card",
    "decision",
]
"""聊天交互模板类型，共 8 种。"""

RenderingSpaceType = Literal["chat", "workspace", "floating", "dock", "fullscreen"]
"""渲染空间类型。"""

PositionType = Literal[
    "auto",
    "center",
    "top-left",
    "top-right",
    "bottom-left",
    "bottom-right",
]
"""布局位置类型。"""

IndicatorType = Literal["none", "dot", "badge"]
"""Dock 状态指示灯类型。"""


# ---- 基础模型 ----


class ModuleIdentity(BaseModel):
    """模块身份信息。

    对应前端 ``ModuleIdentity`` 接口。

    Attributes:
        id: 模块唯一标识。
        name: 模块名称。
        version: 模块版本。
        category: 模块分类（builtin/extension/custom）。
        description: 模块描述。
        icon: 模块图标（emoji 或图标名）。
        author: 模块作者。
        tags: 模块标签列表。
    """

    id: str
    name: str
    version: str = "1.0.0"
    category: CategoryType = "custom"
    description: str | None = None
    icon: str | None = None
    author: str | None = None
    tags: list[str] | None = None


class ModuleAction(BaseModel):
    """模块操作定义。

    对应前端 ``ModuleAction`` 接口。

    Attributes:
        id: 操作 ID。
        name: 操作名称。
        type: 操作类型（command/query/event/stream）。
        description: 操作描述。
        input_schema: 输入参数 Schema。
        output_schema: 输出参数 Schema。
        requires_confirmation: 是否需要用户确认。
        is_dangerous: 是否为危险操作。
        api: API 端点路径。
        params: 参数列表。
        label: 操作显示标签。
    """

    id: str
    name: str
    type: ActionType = "command"
    description: str | None = Field(default=None, alias="description")
    input_schema: dict[str, Any] | None = Field(default=None, alias="inputSchema")
    output_schema: dict[str, Any] | None = Field(default=None, alias="outputSchema")
    requires_confirmation: bool = Field(default=False, alias="requiresConfirmation")
    is_dangerous: bool = Field(default=False, alias="isDangerous")
    api: str | None = None
    params: list[str] | None = None
    label: str | None = None

    model_config = {"populate_by_name": True}


class ChatInteractionConfig(BaseModel):
    """聊天交互组件配置。

    对应前端 ``ChatInteractionConfig`` 接口。

    Attributes:
        type: 交互类型（8 种之一）。
        props: 组件配置属性。
        data_source: 数据源引用。
        refresh_interval: 自动刷新间隔（毫秒）。
    """

    type: ChatInteractionType
    props: dict[str, Any] | None = None
    data_source: str | None = Field(default=None, alias="dataSource")
    refresh_interval: int | None = Field(default=None, alias="refreshInterval")

    model_config = {"populate_by_name": True}


class LayoutConfig(BaseModel):
    """布局配置。

    Attributes:
        width: 宽度（数值或 CSS 值）。
        height: 高度（数值或 CSS 值）。
        min_width: 最小宽度。
        min_height: 最小高度。
        resizable: 是否可调整大小。
        draggable: 是否可拖拽。
        position: 布局位置。
    """

    width: int | str | None = None
    height: int | str | None = None
    min_width: int | None = Field(default=None, alias="minWidth")
    min_height: int | None = Field(default=None, alias="minHeight")
    resizable: bool | None = None
    draggable: bool | None = None
    position: PositionType | None = None

    model_config = {"populate_by_name": True}


class AutoOpenConfig(BaseModel):
    """自动弹出条件配置。

    Attributes:
        event: 触发事件名称。
        delay: 延迟（毫秒）。
    """

    event: str | None = None
    delay: int | None = None


class RenderingSpaceConfig(BaseModel):
    """渲染空间配置。

    对应前端 ``RenderingSpaceConfig`` 接口。

    Attributes:
        space: 渲染空间类型。
        widget: 组件类型。
        props: 组件属性。
        data_source: 数据源引用。
        layout: 布局配置。
        auto_open: 自动弹出条件。
    """

    space: RenderingSpaceType = "workspace"
    widget: str = ""
    props: dict[str, Any] | None = None
    data_source: str | None = Field(default=None, alias="dataSource")
    layout: dict[str, Any] | None = None
    auto_open: dict[str, Any] | None = Field(default=None, alias="autoOpen")

    model_config = {"populate_by_name": True}


class DockConfig(BaseModel):
    """Dock 栏配置。

    Attributes:
        icon: Dock 图标。
        label: Dock 标签。
        indicator: 状态指示灯类型。
        indicator_color: 指示灯颜色。
    """

    icon: str | None = None
    label: str | None = None
    indicator: IndicatorType | None = "none"
    indicator_color: str | None = Field(default=None, alias="indicatorColor")

    model_config = {"populate_by_name": True}


class FullscreenConfig(BaseModel):
    """全屏触发配置。

    Attributes:
        trigger_event: 触发事件名称。
        auto_enter: 是否自动进入全屏。
    """

    trigger_event: str | None = Field(default=None, alias="triggerEvent")
    auto_enter: bool | None = Field(default=None, alias="autoEnter")

    model_config = {"populate_by_name": True}


class ModuleRendering(BaseModel):
    """模块渲染配置。

    对应前端 ``ModuleRendering`` 接口。

    Attributes:
        chat: 聊天交互模板列表。
        spaces: 渲染空间列表。
        dock: Dock 栏配置。
        fullscreen: 全屏触发配置。
    """

    chat: list[ChatInteractionConfig] = Field(default_factory=list)
    spaces: list[RenderingSpaceConfig] = Field(default_factory=list)
    dock: DockConfig | None = None
    fullscreen: FullscreenConfig | None = None


class FallbackConfig(BaseModel):
    """降级方案配置。

    Attributes:
        widget: 降级到的交互组件。
        space: 降级到的渲染空间。
    """

    widget: str = "status_card"
    space: RenderingSpaceType = "chat"


class ClientCapabilities(BaseModel):
    """客户端能力要求。

    对应前端 ``ClientCapabilities`` 接口。

    Attributes:
        required_spaces: 要求的渲染空间列表。
        required_widgets: 要求的交互组件列表。
        min_client_version: 最低客户端版本。
        fallback: 降级方案。
    """

    required_spaces: list[RenderingSpaceType] = Field(default_factory=list, alias="requiredSpaces")
    required_widgets: list[str] = Field(default_factory=list, alias="requiredWidgets")
    min_client_version: str | None = Field(default=None, alias="minClientVersion")
    fallback: dict[str, Any] | None = None

    model_config = {"populate_by_name": True}


class ModuleUISchema(BaseModel):
    """完整的模块 UI Schema。

    对应前端 ``ModuleUISchema`` 接口。
    包含 identity/actions/rendering/clients 四个部分。

    Attributes:
        identity: 模块身份信息。
        actions: 模块操作列表。
        rendering: 渲染配置。
        clients: 客户端能力要求。
    """

    identity: ModuleIdentity
    actions: list[ModuleAction] = Field(default_factory=list)
    rendering: ModuleRendering = Field(default_factory=ModuleRendering)
    clients: ClientCapabilities = Field(default_factory=ClientCapabilities)
