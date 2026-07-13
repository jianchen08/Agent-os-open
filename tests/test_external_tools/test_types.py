"""外部工具核心类型定义测试。"""

from __future__ import annotations

from dataclasses import fields

import pytest

from tools.external.types import (
    AuthConfig,
    AuthType,
    ExternalToolCapability,
    ExternalToolConfig,
    ExternalToolInfo,
    ExternalToolState,
    ProtocolType,
    RetryPolicy,
    SandboxResourceLimits,
)


# ════════════════════════════════════════════
# ExternalToolState 枚举
# ════════════════════════════════════════════


class TestExternalToolState:
    """外部工具状态枚举测试。"""

    def test_all_enum_values(self) -> None:
        """验证所有状态枚举值。"""
        expected = {
            "DISCONNECTED": "disconnected",
            "CONNECTING": "connecting",
            "CONNECTED": "connected",
            "RECONNECTING": "reconnecting",
            "ERROR": "error",
        }
        for attr, value in expected.items():
            assert ExternalToolState[attr].value == value

    def test_is_str_enum(self) -> None:
        """验证是 str 枚举，可直接比较。"""
        assert ExternalToolState.CONNECTED == "connected"
        assert isinstance(ExternalToolState.DISCONNECTED, str)

    def test_state_count(self) -> None:
        """验证状态总数。"""
        assert len(ExternalToolState) == 5


# ════════════════════════════════════════════
# AuthType 枚举
# ════════════════════════════════════════════


class TestAuthType:
    """认证类型枚举测试。"""

    def test_all_auth_types(self) -> None:
        """验证所有认证类型。"""
        expected = ["none", "api_key", "bearer", "basic", "oauth2", "custom"]
        values = [t.value for t in AuthType]
        assert set(values) == set(expected)

    def test_default_auth_type(self) -> None:
        """NONE 是默认认证类型。"""
        assert AuthType.NONE.value == "none"


# ════════════════════════════════════════════
# ProtocolType 枚举
# ════════════════════════════════════════════


class TestProtocolType:
    """协议类型枚举测试。"""

    def test_protocol_values(self) -> None:
        """验证 HTTP 和 WebSocket 协议。"""
        assert ProtocolType.HTTP.value == "http"
        assert ProtocolType.WEBSOCKET.value == "websocket"

    def test_protocol_count(self) -> None:
        """只有两种协议。"""
        assert len(ProtocolType) == 2


# ════════════════════════════════════════════
# RetryPolicy dataclass
# ════════════════════════════════════════════


class TestRetryPolicy:
    """重试策略配置测试。"""

    def test_default_values(self) -> None:
        """验证默认值。"""
        policy = RetryPolicy()
        assert policy.max_retries == 3
        assert policy.base_delay == 1.0
        assert policy.max_delay == 30.0
        assert policy.exponential_base == 2.0
        assert policy.jitter is True

    def test_custom_values(self) -> None:
        """自定义值正确赋值。"""
        policy = RetryPolicy(max_retries=5, base_delay=2.0, jitter=False)
        assert policy.max_retries == 5
        assert policy.base_delay == 2.0
        assert policy.jitter is False

    def test_field_count(self) -> None:
        """验证字段数量。"""
        assert len(fields(RetryPolicy)) == 5


# ════════════════════════════════════════════
# AuthConfig dataclass
# ════════════════════════════════════════════


class TestAuthConfig:
    """认证配置测试。"""

    def test_defaults(self) -> None:
        """默认无认证。"""
        cfg = AuthConfig()
        assert cfg.auth_type == AuthType.NONE
        assert cfg.secret_key is None
        assert cfg.headers == {}
        assert cfg.params == {}

    def test_api_key_config(self) -> None:
        """API Key 认证配置。"""
        cfg = AuthConfig(
            auth_type=AuthType.API_KEY,
            secret_key="my_api_key",
            headers={"X-Custom": "val"},
        )
        assert cfg.auth_type == AuthType.API_KEY
        assert cfg.secret_key == "my_api_key"
        assert cfg.headers == {"X-Custom": "val"}

    def test_headers_independent_between_instances(self) -> None:
        """不同实例的 headers 字段独立。"""
        a = AuthConfig()
        b = AuthConfig()
        a.headers["X"] = "1"
        assert "X" not in b.headers


# ════════════════════════════════════════════
# ExternalToolConfig dataclass
# ════════════════════════════════════════════


class TestExternalToolConfig:
    """外部工具连接配置测试。"""

    def test_defaults(self) -> None:
        """验证所有默认值。"""
        cfg = ExternalToolConfig()
        assert cfg.name == ""
        assert cfg.display_name == ""
        assert cfg.protocol == ProtocolType.HTTP
        assert cfg.endpoint == ""
        assert cfg.connect_timeout == 10.0
        assert cfg.read_timeout == 30.0
        assert cfg.execute_timeout == 60.0
        assert isinstance(cfg.retry_policy, RetryPolicy)
        assert isinstance(cfg.auth, AuthConfig)
        assert cfg.max_connections == 5
        assert cfg.idle_timeout == 300.0
        assert cfg.heartbeat_interval == 30.0
        assert cfg.enable_sandbox is False
        assert cfg.sandbox_image == "python:3.11-slim"
        assert cfg.extra == {}

    def test_custom_config(self) -> None:
        """自定义配置正确赋值。"""
        cfg = ExternalToolConfig(
            name="my_tool",
            protocol=ProtocolType.WEBSOCKET,
            endpoint="ws://localhost:9090",
            connect_timeout=5.0,
        )
        assert cfg.name == "my_tool"
        assert cfg.protocol == ProtocolType.WEBSOCKET
        assert cfg.endpoint == "ws://localhost:9090"
        assert cfg.connect_timeout == 5.0

    def test_nested_retry_policy(self) -> None:
        """嵌套重试策略正确配置。"""
        cfg = ExternalToolConfig(
            retry_policy=RetryPolicy(max_retries=10, base_delay=0.5)
        )
        assert cfg.retry_policy.max_retries == 10
        assert cfg.retry_policy.base_delay == 0.5

    def test_extra_arbitrary_data(self) -> None:
        """extra 字段可存储任意数据。"""
        cfg = ExternalToolConfig(extra={"version": "2.0", "tags": ["a", "b"]})
        assert cfg.extra["version"] == "2.0"
        assert len(cfg.extra["tags"]) == 2


# ════════════════════════════════════════════
# ExternalToolCapability dataclass
# ════════════════════════════════════════════


class TestExternalToolCapability:
    """工具能力描述测试。"""

    def test_defaults(self) -> None:
        """验证默认值。"""
        cap = ExternalToolCapability()
        assert cap.name == ""
        assert cap.description == ""
        assert cap.input_schema == {}
        assert cap.output_schema == {}
        assert cap.requires_sandbox is False
        assert cap.timeout_override is None
        assert cap.dangerous is False

    def test_full_capability(self) -> None:
        """完整能力描述。"""
        cap = ExternalToolCapability(
            name="exec",
            description="执行代码",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            requires_sandbox=True,
            timeout_override=120.0,
            dangerous=True,
        )
        assert cap.name == "exec"
        assert cap.requires_sandbox is True
        assert cap.timeout_override == 120.0
        assert cap.dangerous is True


# ════════════════════════════════════════════
# ExternalToolInfo dataclass
# ════════════════════════════════════════════


class TestExternalToolInfo:
    """工具元信息测试。"""

    def test_defaults(self) -> None:
        """验证默认值。"""
        info = ExternalToolInfo()
        assert info.name == ""
        assert info.version == "1.0.0"
        assert info.state == ExternalToolState.DISCONNECTED
        assert info.capabilities == []
        assert info.config is None
        assert info.metadata == {}

    def test_with_capabilities(self) -> None:
        """携带能力列表。"""
        caps = [
            ExternalToolCapability(name="op1"),
            ExternalToolCapability(name="op2"),
        ]
        info = ExternalToolInfo(name="tool", capabilities=caps, state=ExternalToolState.CONNECTED)
        assert info.name == "tool"
        assert len(info.capabilities) == 2
        assert info.state == ExternalToolState.CONNECTED


# ════════════════════════════════════════════
# SandboxResourceLimits dataclass
# ════════════════════════════════════════════


class TestSandboxResourceLimits:
    """沙箱资源限制测试。"""

    def test_defaults(self) -> None:
        """验证默认值。"""
        limits = SandboxResourceLimits()
        assert limits.cpu_limit == 1.0
        assert limits.memory_limit_mb == 512
        assert limits.disk_limit_mb == 1024
        assert limits.network_whitelist == []
        assert limits.max_processes == 10
        assert limits.timeout_seconds == 60.0

    def test_custom_limits(self) -> None:
        """自定义资源限制。"""
        limits = SandboxResourceLimits(
            cpu_limit=2.0,
            memory_limit_mb=1024,
            network_whitelist=["localhost"],
            timeout_seconds=30.0,
        )
        assert limits.cpu_limit == 2.0
        assert limits.memory_limit_mb == 1024
        assert limits.network_whitelist == ["localhost"]
        assert limits.timeout_seconds == 30.0
