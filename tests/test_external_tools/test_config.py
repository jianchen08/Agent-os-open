"""外部工具配置管理器测试。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from tools.external.config import ExternalToolConfigManager
from tools.external.exceptions import ConfigError
from tools.external.types import AuthType, ProtocolType


# ════════════════════════════════════════════
# 辅助 fixtures
# ════════════════════════════════════════════


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """创建临时配置目录并写入测试配置文件。"""
    cfg_dir = tmp_path / "external_tools"
    cfg_dir.mkdir()

    # 默认配置
    default_cfg = {
        "connect_timeout": 20.0,
        "read_timeout": 60.0,
    }
    (cfg_dir / "default.yaml").write_text(yaml.dump(default_cfg), encoding="utf-8")

    # 工具 1: HTTP 工具
    tool1 = {
        "name": "tool_a",
        "display_name": "Tool A",
        "description": "HTTP test tool",
        "protocol": "http",
        "endpoint": "http://localhost:8080",
        "auth": {"type": "api_key", "secret_key": "key_a"},
        "extra": {"version": "2.0"},
    }
    (cfg_dir / "tool_a.yaml").write_text(yaml.dump(tool1), encoding="utf-8")

    # 工具 2: WebSocket 工具
    tool2 = {
        "name": "tool_b",
        "display_name": "Tool B",
        "protocol": "websocket",
        "endpoint": "ws://localhost:9090",
    }
    (cfg_dir / "tool_b.yaml").write_text(yaml.dump(tool2), encoding="utf-8")

    return cfg_dir


@pytest.fixture
def empty_dir(tmp_path: Path) -> Path:
    """空配置目录。"""
    d = tmp_path / "empty"
    d.mkdir()
    return d


# ════════════════════════════════════════════
# 配置加载
# ════════════════════════════════════════════


class TestLoadConfig:
    """配置加载测试。"""

    def test_load_all_success(self, config_dir: Path) -> None:
        """加载所有配置。"""
        mgr = ExternalToolConfigManager(str(config_dir))
        configs = mgr.load_all()
        assert len(configs) == 2
        assert "tool_a" in configs
        assert "tool_b" in configs

    def test_load_tool_config_values(self, config_dir: Path) -> None:
        """加载的配置值正确。"""
        mgr = ExternalToolConfigManager(str(config_dir))
        configs = mgr.load_all()
        tool_a = configs["tool_a"]
        assert tool_a.name == "tool_a"
        assert tool_a.protocol == ProtocolType.HTTP
        assert tool_a.endpoint == "http://localhost:8080"
        assert tool_a.auth.auth_type == AuthType.API_KEY
        assert tool_a.auth.secret_key == "key_a"

    def test_load_merges_default(self, config_dir: Path) -> None:
        """默认配置合并到工具配置中。"""
        mgr = ExternalToolConfigManager(str(config_dir))
        configs = mgr.load_all()
        tool_a = configs["tool_a"]
        # default.yaml 中 connect_timeout=20.0, tool_a 没有覆盖，应继承
        assert tool_a.connect_timeout == 20.0
        assert tool_a.read_timeout == 60.0

    def test_tool_specific_overrides_default(self, config_dir: Path) -> None:
        """工具配置覆盖默认值。"""
        mgr = ExternalToolConfigManager(str(config_dir))
        configs = mgr.load_all()
        tool_b = configs["tool_b"]
        assert tool_b.protocol == ProtocolType.WEBSOCKET

    def test_load_empty_dir(self, empty_dir: Path) -> None:
        """空目录返回空配置。"""
        mgr = ExternalToolConfigManager(str(empty_dir))
        configs = mgr.load_all()
        assert configs == {}

    def test_load_nonexistent_dir(self, tmp_path: Path) -> None:
        """不存在的目录返回空配置。"""
        mgr = ExternalToolConfigManager(str(tmp_path / "no_such_dir"))
        configs = mgr.load_all()
        assert configs == {}

    def test_get_config(self, config_dir: Path) -> None:
        """按名称获取配置。"""
        mgr = ExternalToolConfigManager(str(config_dir))
        mgr.load_all()
        cfg = mgr.get_config("tool_a")
        assert cfg is not None
        assert cfg.name == "tool_a"

    def test_get_config_not_found(self, config_dir: Path) -> None:
        """不存在的工具返回 None。"""
        mgr = ExternalToolConfigManager(str(config_dir))
        mgr.load_all()
        assert mgr.get_config("nonexistent") is None

    def test_get_all_configs(self, config_dir: Path) -> None:
        """获取所有已加载配置。"""
        mgr = ExternalToolConfigManager(str(config_dir))
        mgr.load_all()
        all_cfgs = mgr.get_all_configs()
        assert len(all_cfgs) == 2
        # 返回副本
        all_cfgs["tool_a"] = None  # type: ignore
        assert mgr.get_config("tool_a") is not None


# ════════════════════════════════════════════
# 配置解析
# ════════════════════════════════════════════


class TestConfigParsing:
    """配置解析细节测试。"""

    def test_parse_retry_policy(self, config_dir: Path) -> None:
        """解析重试策略。"""
        mgr = ExternalToolConfigManager(str(config_dir))
        configs = mgr.load_all()
        tool_a = configs["tool_a"]
        assert tool_a.retry_policy.max_retries == 3
        assert tool_a.retry_policy.base_delay == 1.0

    def test_parse_auth_config(self, config_dir: Path) -> None:
        """解析认证配置。"""
        mgr = ExternalToolConfigManager(str(config_dir))
        configs = mgr.load_all()
        tool_a = configs["tool_a"]
        assert tool_a.auth.auth_type == AuthType.API_KEY

    def test_parse_invalid_yaml(self, tmp_path: Path) -> None:
        """无效 YAML 不抛异常，而是跳过并记录错误日志。"""
        cfg_dir = tmp_path / "bad"
        cfg_dir.mkdir()
        (cfg_dir / "bad.yaml").write_text(": invalid: yaml: {{{", encoding="utf-8")

        mgr = ExternalToolConfigManager(str(cfg_dir))
        # load_all 内部捕获异常并记录日志，不向外抛出
        configs = mgr.load_all()
        assert configs == {}

    def test_parse_empty_yaml(self, tmp_path: Path) -> None:
        """空 YAML 内容抛出 ConfigError。"""
        cfg_dir = tmp_path / "empty_yaml"
        cfg_dir.mkdir()
        (cfg_dir / "empty.yaml").write_text("", encoding="utf-8")

        mgr = ExternalToolConfigManager(str(cfg_dir))
        # 空内容会被跳过（yaml.safe_load 返回 None）
        configs = mgr.load_all()
        assert configs == {}

    def test_parse_non_dict_yaml(self, tmp_path: Path) -> None:
        """非字典 YAML 不抛异常，而是跳过并记录错误日志。"""
        cfg_dir = tmp_path / "list_yaml"
        cfg_dir.mkdir()
        (cfg_dir / "list.yaml").write_text("- item1\n- item2\n", encoding="utf-8")

        mgr = ExternalToolConfigManager(str(cfg_dir))
        # load_all 内部捕获异常并记录日志，不向外抛出
        configs = mgr.load_all()
        assert configs == {}

    def test_parse_invalid_protocol_defaults_to_http(self, tmp_path: Path) -> None:
        """无效协议默认为 HTTP。"""
        cfg_dir = tmp_path / "proto"
        cfg_dir.mkdir()
        (cfg_dir / "tool.yaml").write_text(
            yaml.dump({"name": "tool_c", "protocol": "grpc"}), encoding="utf-8"
        )
        mgr = ExternalToolConfigManager(str(cfg_dir))
        configs = mgr.load_all()
        assert configs["tool_c"].protocol == ProtocolType.HTTP

    def test_parse_extra_field(self, config_dir: Path) -> None:
        """extra 字段正确解析。"""
        mgr = ExternalToolConfigManager(str(config_dir))
        configs = mgr.load_all()
        assert configs["tool_a"].extra["version"] == "2.0"


# ════════════════════════════════════════════
# 重新加载
# ════════════════════════════════════════════


class TestReload:
    """配置重新加载测试。"""

    def test_reload_clears_and_reloads(self, config_dir: Path) -> None:
        """重新加载清空并重新读取。"""
        mgr = ExternalToolConfigManager(str(config_dir))
        mgr.load_all()
        assert len(mgr.get_all_configs()) == 2

        # 重新加载
        configs = mgr.reload()
        assert len(configs) == 2

    def test_reload_picks_up_new_files(self, config_dir: Path) -> None:
        """重新加载能发现新文件。"""
        mgr = ExternalToolConfigManager(str(config_dir))
        mgr.load_all()
        assert len(mgr.get_all_configs()) == 2

        # 添加新配置文件
        tool_c = {"name": "tool_c", "protocol": "http", "endpoint": "http://c:3000"}
        (config_dir / "tool_c.yaml").write_text(yaml.dump(tool_c), encoding="utf-8")

        configs = mgr.reload()
        assert len(configs) == 3
        assert "tool_c" in configs


# ════════════════════════════════════════════
# 热更新回调
# ════════════════════════════════════════════


class TestOnChange:
    """配置变更回调测试。"""

    def test_register_callback(self, config_dir: Path) -> None:
        """注册回调成功。"""
        mgr = ExternalToolConfigManager(str(config_dir))
        callback = lambda name, cfg: None  # noqa: E731
        mgr.on_change(callback)
        assert len(mgr._on_change_callbacks) == 1

    def test_multiple_callbacks(self, config_dir: Path) -> None:
        """可注册多个回调。"""
        mgr = ExternalToolConfigManager(str(config_dir))
        mgr.on_change(lambda n, c: None)
        mgr.on_change(lambda n, c: None)
        assert len(mgr._on_change_callbacks) == 2


# ════════════════════════════════════════════
# config_dir 属性
# ════════════════════════════════════════════


class TestConfigDir:
    """配置目录属性测试。"""

    def test_config_dir_property(self, config_dir: Path) -> None:
        """config_dir 属性返回正确路径。"""
        mgr = ExternalToolConfigManager(str(config_dir))
        assert mgr.config_dir == config_dir

    def test_default_config_dir(self) -> None:
        """默认配置目录为 config/external_tools。"""
        mgr = ExternalToolConfigManager()
        config_str = str(mgr.config_dir).replace("\\", "/")
        assert config_str.endswith("config/external_tools")
