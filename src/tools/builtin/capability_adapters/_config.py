"""
能力适配器配置加载器

暴露接口：
- BackendConfig：后端配置数据类
- CapabilityAdapterConfig：配置加载（每次调用时读文件，支持热更新）
"""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.mcp_loader import MCPServerConfig

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parent.parent.parent.parent.parent / "config" / "tools"
_DEFAULT_CONFIG_PATH = _CONFIG_DIR / "capability_adapters.yaml"

_ENV_PATTERN = re.compile(r"\$\{(\w+)\}")

_empty_cache_sentinel = object()
_parse_cache: dict[str, list["BackendConfig"]] | object = (
    _empty_cache_sentinel  # "配置不存在"时缓存空字典避免反复读文件
)


@dataclass
class BackendConfig:
    """单个后端配置"""

    name: str
    priority: int = 1
    server: MCPServerConfig | None = None
    tool_mapping: dict[str, str] = field(default_factory=dict)
    timeout: float = 120.0
    overall_timeout: float = 180.0
    available: bool = True


def _interpolate_env(value: str) -> str:
    """替换 ${VAR_NAME} 为环境变量值"""
    return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)


def _parse_server_config(name: str, raw: dict[str, Any]) -> MCPServerConfig:
    """从 YAML 字典解析 MCPServerConfig"""
    env = raw.get("env", {})
    interpolated_env = {k: _interpolate_env(str(v)) for k, v in env.items()}

    return MCPServerConfig(
        name=name,
        command=raw.get("command", ""),
        args=raw.get("args", []),
        env=interpolated_env,
        disabled=False,
    )


def _parse_yaml(path: Path) -> dict[str, list[BackendConfig]]:
    """解析 YAML 配置文件为后端配置字典（通过 ConfigCenter 统一缓存）"""
    from config.config_center import get_config_center  # noqa: PLC0415

    rel = str(path).replace("\\", "/")
    if "config/" in rel:
        rel = rel[rel.index("config/") + len("config/") :]
    data = get_config_center().get(rel) or {}

    adapters_raw = data.get("adapters", {})
    result: dict[str, list[BackendConfig]] = {}

    for adapter_name, adapter_config in adapters_raw.items():
        if not isinstance(adapter_config, dict):
            continue

        backends_raw = adapter_config.get("backends", [])
        backends: list[BackendConfig] = []

        for backend_raw in backends_raw:
            if not isinstance(backend_raw, dict):
                continue

            server_raw = backend_raw.get("server", {})
            backend_name = backend_raw.get("name", "unknown")
            server_config = _parse_server_config(backend_name, server_raw)

            backends.append(
                BackendConfig(
                    name=backend_name,
                    priority=backend_raw.get("priority", 1),
                    server=server_config,
                    tool_mapping=backend_raw.get("tool_mapping", {}),
                    timeout=backend_raw.get("timeout", 120.0),
                    overall_timeout=backend_raw.get("overall_timeout", 180.0),
                    available=backend_raw.get("available", True),
                )
            )

        backends.sort(key=lambda b: b.priority)
        result[adapter_name] = backends

    return result


class CapabilityAdapterConfig:
    """能力适配器配置管理

    每次 load() 时读取 YAML 文件，修改配置后无需重启即生效。
    仅在配置文件不存在时缓存空结果（避免反复读不存在的文件）。
    """

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> dict[str, list[BackendConfig]]:
        """加载配置（每次读取文件，支持热更新）"""
        global _parse_cache  # noqa: PLW0603

        path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH

        if not path.exists():
            if _parse_cache is not _empty_cache_sentinel and _parse_cache == {}:
                return {}
            logger.warning("[CapabilityAdapter] 配置文件不存在: %s", path)
            _parse_cache = {}
            return {}

        try:
            result = _parse_yaml(path)
            logger.debug(
                "[CapabilityAdapter] 配置热加载 | adapters=%d",
                len(result),
            )
            return result
        except Exception as e:
            logger.error("[CapabilityAdapter] 配置加载失败: %s", e)
            return {}
