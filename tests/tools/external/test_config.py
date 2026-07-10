"""外部工具配置管理测试。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from tools.external.config import ExternalToolConfigManager
from tools.external.exceptions import ConfigError
from tools.external.types import AuthType, ExternalToolConfig, ProtocolType


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """创建临时配置目录和文件。"""
    ext_dir = tmp_path / "external_tools"
    ext_dir.mkdir()

    # 默认配置
    default_config = {
        "connect_timeout": 5.0,
        "retry_policy": {"max_retries": 5, "base_delay": 0.5},
    }
    (ext_dir / "default.yaml").write_text(
        yaml.dump(default_config), encoding="utf-8",
    )

    # 工具配置1
    tool1_config = {
        "name": "test_tool",
        "display_name": "Test Tool",
        "description": "A test tool",
        "protocol": "http",
        "endpoint": "http://localhost:8080",
        "execute_timeout": 120.0,
        "auth": {"type": "api_key", "secret_key": "test_key"},
        "extra": {"version": "2.0.0"},
    }
    (ext_dir / "test_tool.yaml").write_text(
        yaml.dump(tool1_config), encoding="utf-8",
    )

    # 工具配置2
    tool2_config = {
        "name": "ws_tool",
        "display_name": "WS Tool",
        "protocol": "websocket",
        "endpoint": "ws://localhost:9090",
    }
    (ext_dir / "ws_tool.yaml").write_text(
        yaml.dump(tool2_config), encoding="utf-8",
    )

    return ext_dir


@pytest.fixture
def manager(config_dir: Path) -> ExternalToolConfigManager:
    return ExternalToolConfigManager(config_dir=str(config_dir))


class TestExternalToolConfigManager:

    def test_load_all(self, manager: ExternalToolConfigManager) -> None:
        configs = manager.load_all()
        assert len(configs) == 2
        assert "test_tool" in configs
        assert "ws_tool" in configs

    def test_get_config(self, manager: ExternalToolConfigManager) -> None:
        manager.load_all()
        config = manager.get_config("test_tool")
        assert config is not None
        assert config.name == "test_tool"
        assert config.display_name == "Test Tool"
        assert config.protocol == ProtocolType.HTTP
        assert config.endpoint == "http://localhost:8080"

    def test_get_config_not_found(self, manager: ExternalToolConfigManager) -> None:
        manager.load_all()
        config = manager.get_config("nonexistent")
        assert config is None

    def test_default_config_merged(self, manager: ExternalToolConfigManager) -> None:
        manager.load_all()
        config = manager.get_config("test_tool")
        assert config is not None
        # 默认配置的 retry_policy.max_retries=5 被合并
        # 但工具配置未覆盖 retry_policy，所以使用默认值
        assert config.connect_timeout == 5.0  # 来自 default.yaml
        # execute_timeout 由工具配置覆盖
        assert config.execute_timeout == 120.0

    def test_auth_parsed(self, manager: ExternalToolConfigManager) -> None:
        manager.load_all()
        config = manager.get_config("test_tool")
        assert config is not None
        assert config.auth.auth_type == AuthType.API_KEY
        assert config.auth.secret_key == "test_key"

    def test_ws_config(self, manager: ExternalToolConfigManager) -> None:
        manager.load_all()
        config = manager.get_config("ws_tool")
        assert config is not None
        assert config.protocol == ProtocolType.WEBSOCKET
        assert config.endpoint == "ws://localhost:9090"

    def test_get_all_configs(self, manager: ExternalToolConfigManager) -> None:
        manager.load_all()
        all_configs = manager.get_all_configs()
        assert len(all_configs) == 2

    def test_reload(self, manager: ExternalToolConfigManager) -> None:
        configs1 = manager.load_all()
        configs2 = manager.reload()
        assert len(configs1) == len(configs2)

    def test_on_change_callback(self, manager: ExternalToolConfigManager) -> None:
        called = []
        manager.on_change(lambda *args: called.append(args))
        # on_change 只是注册，这里验证不报错
        assert len(manager._on_change_callbacks) == 1

    def test_empty_config_dir(self, tmp_path: Path) -> None:
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        mgr = ExternalToolConfigManager(config_dir=str(empty_dir))
        configs = mgr.load_all()
        assert configs == {}

    def test_nonexistent_config_dir(self) -> None:
        mgr = ExternalToolConfigManager(config_dir="/nonexistent/path")
        configs = mgr.load_all()
        assert configs == {}

    def test_extra_metadata(self, manager: ExternalToolConfigManager) -> None:
        manager.load_all()
        config = manager.get_config("test_tool")
        assert config is not None
        assert config.extra.get("version") == "2.0.0"

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        """测试无效 YAML 不影响其他配置加载。"""
        ext_dir = tmp_path / "external_tools"
        ext_dir.mkdir()

        (ext_dir / "default.yaml").write_text("", encoding="utf-8")

        # 有效配置
        (ext_dir / "good.yaml").write_text(
            yaml.dump({"name": "good", "endpoint": "http://localhost"}),
            encoding="utf-8",
        )

        # 无效配置
        (ext_dir / "bad.yaml").write_text("{{invalid yaml", encoding="utf-8")

        mgr = ExternalToolConfigManager(config_dir=str(ext_dir))
        configs = mgr.load_all()
        assert "good" in configs
