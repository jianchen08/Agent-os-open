"""外部工具配置管理。

暴露接口：
- ExternalToolConfigManager：从 YAML 加载配置，支持热更新
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from tools.external.exceptions import ConfigError
from tools.external.types import (
    AuthConfig,
    AuthType,
    ExternalToolConfig,
    ProtocolType,
    RetryPolicy,
)

logger = logging.getLogger(__name__)

# 默认配置目录
DEFAULT_CONFIG_DIR = "config/external_tools"


class ExternalToolConfigManager:
    """外部工具配置管理器。

    职责：
    - 从 config/external_tools/*.yaml 加载配置
    - 支持配置合并策略（default.yaml + 工具特定配置）
    - 支持配置热更新（通过回调通知）
    """

    def __init__(self, config_dir: str | None = None) -> None:
        """初始化配置管理器。

        Args:
            config_dir: 配置目录路径，默认 config/external_tools
        """
        self._config_dir = Path(config_dir or DEFAULT_CONFIG_DIR)
        self._configs: dict[str, ExternalToolConfig] = {}
        self._default_config: dict[str, Any] = {}
        self._on_change_callbacks: list[Any] = []
        self._logger = logging.getLogger(f"{__name__}")

    @property
    def config_dir(self) -> Path:
        """获取配置目录。"""
        return self._config_dir

    def load_all(self) -> dict[str, ExternalToolConfig]:
        """加载所有外部工具配置。

        Returns:
            配置字典（工具名 → 配置）
        """
        self._configs.clear()

        # 1. 加载默认配置
        self._default_config = self._load_default_config()

        # 2. 加载各工具配置
        if not self._config_dir.exists():
            self._logger.warning(
                "配置目录不存在 | dir=%s",
                self._config_dir,
            )
            return self._configs

        for yaml_file in self._config_dir.glob("*.yaml"):
            if yaml_file.name == "default.yaml":
                continue

            try:
                config = self._load_tool_config(yaml_file)
                if config:
                    self._configs[config.name] = config
                    self._logger.info(
                        "配置已加载 | tool=%s | file=%s",
                        config.name,
                        yaml_file.name,
                    )
            except Exception as e:
                self._logger.error(
                    "配置加载失败 | file=%s | error=%s",
                    yaml_file.name,
                    e,
                )

        return self._configs

    def get_config(self, tool_name: str) -> ExternalToolConfig | None:
        """获取指定工具的配置。

        Args:
            tool_name: 工具名称

        Returns:
            工具配置，不存在返回 None
        """
        return self._configs.get(tool_name)

    def get_all_configs(self) -> dict[str, ExternalToolConfig]:
        """获取所有已加载的配置。"""
        return dict(self._configs)

    def reload(self) -> dict[str, ExternalToolConfig]:
        """重新加载所有配置。"""
        self._logger.info("重新加载配置 | dir=%s", self._config_dir)
        return self.load_all()

    def on_change(self, callback: Any) -> None:
        """注册配置变更回调。

        Args:
            callback: 回调函数，签名 (tool_name: str, config: ExternalToolConfig) -> None
        """
        self._on_change_callbacks.append(callback)

    def _load_default_config(self) -> dict[str, Any]:
        """加载默认配置模板。"""
        default_file = self._config_dir / "default.yaml"
        if not default_file.exists():
            return {}

        try:
            with open(default_file, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            self._logger.warning("默认配置加载失败: %s", e)
            return {}

    def _load_tool_config(self, yaml_file: Path) -> ExternalToolConfig | None:
        """加载单个工具的 YAML 配置。

        Args:
            yaml_file: YAML 文件路径

        Returns:
            工具配置

        Raises:
            ConfigError: 配置格式错误
        """
        try:
            with open(yaml_file, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigError(
                message=f"YAML 解析失败: {yaml_file.name}",
                config_key=yaml_file.name,
                cause=e,
            ) from e

        if not raw or not isinstance(raw, dict):
            raise ConfigError(
                message=f"配置格式无效: {yaml_file.name}",
                config_key=yaml_file.name,
            )

        # 合并默认配置
        merged = {**self._default_config, **raw}

        return self._parse_config(merged)

    def _parse_config(self, raw: dict[str, Any]) -> ExternalToolConfig:
        """解析原始配置字典为 ExternalToolConfig。

        Args:
            raw: 原始配置字典

        Returns:
            解析后的配置
        """
        # 解析重试策略
        retry_raw = raw.get("retry_policy", {})
        retry_policy = RetryPolicy(
            max_retries=retry_raw.get("max_retries", 3),
            base_delay=retry_raw.get("base_delay", 1.0),
            max_delay=retry_raw.get("max_delay", 30.0),
            exponential_base=retry_raw.get("exponential_base", 2.0),
            jitter=retry_raw.get("jitter", True),
        )

        # 解析认证配置
        auth_raw = raw.get("auth", {})
        auth_config = AuthConfig(
            auth_type=AuthType(auth_raw.get("type", "none")),
            secret_key=auth_raw.get("secret_key"),
            headers=auth_raw.get("headers", {}),
            params=auth_raw.get("params", {}),
        )

        # 解析协议
        protocol_str = raw.get("protocol", "http")
        try:
            protocol = ProtocolType(protocol_str)
        except ValueError:
            protocol = ProtocolType.HTTP

        return ExternalToolConfig(
            name=raw.get("name", ""),
            display_name=raw.get("display_name", raw.get("name", "")),
            description=raw.get("description", ""),
            protocol=protocol,
            endpoint=raw.get("endpoint", ""),
            connect_timeout=raw.get("connect_timeout", 10.0),
            read_timeout=raw.get("read_timeout", 30.0),
            execute_timeout=raw.get("execute_timeout", 60.0),
            retry_policy=retry_policy,
            auth=auth_config,
            max_connections=raw.get("max_connections", 5),
            idle_timeout=raw.get("idle_timeout", 300.0),
            heartbeat_interval=raw.get("heartbeat_interval", 30.0),
            enable_sandbox=raw.get("enable_sandbox", False),
            sandbox_image=raw.get("sandbox_image", "python:3.11-slim"),
            extra=raw.get("extra", {}),
        )
