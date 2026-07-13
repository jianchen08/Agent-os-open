"""能力适配器配置加载器。

从 config/capability_adapters.yaml 读取适配器注册信息，
提供统一的配置查询接口，供 ToolContextPlugin 和 ConnectorRegistry 使用。

与 tools/builtin/capability_adapters/_config.py 的区别：
- 本模块读取 config/capability_adapters.yaml（连接器注册配置，含四个适配器）
- _config.py 读取 config/tools/capability_adapters.yaml（MCP后端配置，含工具映射）

暴露接口：
- AdapterConfig：单个适配器配置数据类
- load_adapter_configs：加载所有适配器配置
- get_adapter_status：获取适配器在线状态摘要
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 配置文件路径：项目根目录下的 config/capability_adapters.yaml
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ADAPTER_CONFIG_PATH = _PROJECT_ROOT / "config" / "capability_adapters.yaml"


@dataclass(frozen=True)
class AdapterConfig:
    """单个适配器配置信息。

    与 config/capability_adapters.yaml 中每个适配器的字段一一对应。

    Attributes:
        name: 适配器名称（YAML key），如 "vscode"
        adapter_type: 适配器类别，如 "ide" / "creative" / "browser" / "desktop"
        priority: 优先级，数值越大越优先
        display_name: 显示名称，如 "Visual Studio Code"
        capabilities: 支持的操作类型列表
        available: 是否启用
        has_mcp: 是否为 MCP 连接器（mcp_config 非 null）
        connector_class: 连接器实现类路径（非 MCP 时有效）
    """

    name: str
    adapter_type: str = ""
    priority: int = 0
    display_name: str = ""
    capabilities: tuple[str, ...] = ()
    available: bool = True
    has_mcp: bool = False
    connector_class: str | None = None


def load_adapter_configs(
    config_path: str | Path | None = None,
) -> dict[str, AdapterConfig]:
    """从 YAML 文件加载所有适配器配置。

    Args:
        config_path: 配置文件路径，默认使用 config/capability_adapters.yaml

    Returns:
        适配器名称到配置的映射字典
    """
    path = Path(config_path) if config_path else _ADAPTER_CONFIG_PATH

    try:
        from config.config_center import get_config_center  # noqa: PLC0415

        rel = str(path).replace("\\", "/")
        if "config/" in rel:
            rel = rel[rel.index("config/") + len("config/") :]
        data = get_config_center().get(rel) or {}
    except Exception as exc:
        logger.error("[AdapterConfig] 配置加载失败: %s", exc)
        return {}

    adapters_raw = data.get("adapters", {})
    if not isinstance(adapters_raw, dict):
        logger.warning("[AdapterConfig] adapters 字段格式不正确")
        return {}

    result: dict[str, AdapterConfig] = {}
    for name, cfg in adapters_raw.items():
        if not isinstance(cfg, dict):
            continue
        mcp_config = cfg.get("mcp_config")
        result[name] = AdapterConfig(
            name=name,
            adapter_type=cfg.get("type", ""),
            priority=cfg.get("priority", 0),
            display_name=cfg.get("display_name", name),
            capabilities=tuple(cfg.get("capabilities") or []),
            available=cfg.get("available", True),
            has_mcp=mcp_config is not None,
            connector_class=cfg.get("connector_class"),
        )

    logger.debug(
        "[AdapterConfig] 已加载 %d 个适配器配置 | path=%s",
        len(result),
        path,
    )
    return result


def get_adapter_status_summary(
    configs: dict[str, AdapterConfig] | None = None,
) -> dict[str, dict[str, Any]]:
    """获取适配器状态摘要。

    供 ToolContextPlugin 写入 tool_context["adapter_status"] 使用。

    Args:
        configs: 适配器配置字典，为 None 时自动加载

    Returns:
        适配器名称到状态摘要的映射，每个摘要包含：
        - type: 适配器类别
        - available: 是否启用
        - capabilities: 支持的操作数量
        - has_mcp: 是否为 MCP 连接器
    """
    if configs is None:
        configs = load_adapter_configs()

    summary: dict[str, dict[str, Any]] = {}
    for name, cfg in configs.items():
        summary[name] = {
            "type": cfg.adapter_type,
            "available": cfg.available,
            "capabilities_count": len(cfg.capabilities),
            "capabilities": list(cfg.capabilities),
            "has_mcp": cfg.has_mcp,
            "display_name": cfg.display_name,
        }
    return summary
