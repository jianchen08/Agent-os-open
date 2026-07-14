"""SDK 类型定义。

定义工具、资源、生命周期事件等核心数据结构。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class LifecycleEvent(StrEnum):
    """生命周期事件类型。"""

    ON_LOAD = "on_load"
    ON_UNLOAD = "on_unload"
    ON_CONFIG_CHANGE = "on_config_change"
    ON_PIPELINE_START = "on_pipeline_start"
    ON_PIPELINE_END = "on_pipeline_end"
    ON_ERROR = "on_error"


@dataclass
class ToolDef:
    """工具定义。

    Attributes:
        name: 工具名称。
        schema: JSON Schema 描述输入参数。
        handler: 处理函数（async 或 sync）。
        description: 工具描述。
        output_schema: 输出 JSON Schema（可选）。
    """

    name: str
    schema: dict[str, Any]
    handler: Callable[..., Any]
    description: str = ""
    output_schema: dict[str, Any] | None = None


@dataclass
class ResourceDef:
    """资源定义。

    Attributes:
        uri: 资源 URI。
        handler: 资源读取函数。
        name: 资源名称。
        description: 资源描述。
        mime_type: MIME 类型。
    """

    uri: str
    handler: Callable[..., Any]
    name: str = ""
    description: str = ""
    mime_type: str = "application/json"


@dataclass
class CapabilityInjection:
    """内核在 initialize 时注入的能力信息。

    Attributes:
        capabilities: 能力名称到句柄信息的映射。
        config: 内核传入的插件配置。
    """

    capabilities: dict[str, dict[str, Any]] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
