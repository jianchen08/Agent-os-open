"""外部工具核心类型定义测试。"""

from __future__ import annotations

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


class TestExternalToolState:
    """状态枚举测试。"""

    def test_all_states_exist(self) -> None:
        assert ExternalToolState.DISCONNECTED == "disconnected"
        assert ExternalToolState.CONNECTING == "connecting"
        assert ExternalToolState.CONNECTED == "connected"
        assert ExternalToolState.RECONNECTING == "reconnecting"
        assert ExternalToolState.ERROR == "error"

    def test_state_from_string(self) -> None:
        state = ExternalToolState("connected")
        assert state == ExternalToolState.CONNECTED

    def test_invalid_state_raises(self) -> None:
        with pytest.raises(ValueError):
            ExternalToolState("invalid_state")


class TestRetryPolicy:
    def test_defaults(self) -> None:
        policy = RetryPolicy()
        assert policy.max_retries == 3
        assert policy.base_delay == 1.0
        assert policy.max_delay == 30.0
        assert policy.exponential_base == 2.0
        assert policy.jitter is True

    def test_custom_values(self) -> None:
        policy = RetryPolicy(max_retries=5, base_delay=2.0, max_delay=60.0)
        assert policy.max_retries == 5
        assert policy.base_delay == 2.0


class TestAuthConfig:
    def test_defaults(self) -> None:
        auth = AuthConfig()
        assert auth.auth_type == AuthType.NONE
        assert auth.secret_key is None
        assert auth.headers == {}
        assert auth.params == {}

    def test_custom_auth(self) -> None:
        auth = AuthConfig(
            auth_type=AuthType.BEARER,
            secret_key="my_token_key",
            headers={"X-Custom": "value"},
        )
        assert auth.auth_type == AuthType.BEARER
        assert auth.secret_key == "my_token_key"
        assert auth.headers["X-Custom"] == "value"


class TestExternalToolConfig:
    def test_defaults(self) -> None:
        config = ExternalToolConfig(name="test")
        assert config.name == "test"
        assert config.protocol == ProtocolType.HTTP
        assert config.connect_timeout == 10.0
        assert config.execute_timeout == 60.0
        assert config.max_connections == 5
        assert config.enable_sandbox is False

    def test_full_config(self) -> None:
        config = ExternalToolConfig(
            name="vscode",
            display_name="VSCode",
            protocol=ProtocolType.WEBSOCKET,
            endpoint="ws://localhost:9741/ws",
            connect_timeout=5.0,
            execute_timeout=30.0,
            retry_policy=RetryPolicy(max_retries=5),
            auth=AuthConfig(auth_type=AuthType.API_KEY, secret_key="vscode_key"),
        )
        assert config.protocol == ProtocolType.WEBSOCKET
        assert config.retry_policy.max_retries == 5
        assert config.auth.auth_type == AuthType.API_KEY


class TestExternalToolCapability:
    def test_basic_capability(self) -> None:
        cap = ExternalToolCapability(
            name="open_file",
            description="打开文件",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        )
        assert cap.name == "open_file"
        assert cap.requires_sandbox is False
        assert cap.dangerous is False
        assert cap.timeout_override is None

    def test_dangerous_capability(self) -> None:
        cap = ExternalToolCapability(
            name="delete_resource",
            description="删除资源",
            dangerous=True,
        )
        assert cap.dangerous is True


class TestExternalToolInfo:
    def test_basic_info(self) -> None:
        info = ExternalToolInfo(
            name="vscode",
            version="1.0.0",
            display_name="VSCode",
            state=ExternalToolState.DISCONNECTED,
        )
        assert info.name == "vscode"
        assert info.state == ExternalToolState.DISCONNECTED
        assert info.capabilities == []

    def test_with_capabilities(self) -> None:
        cap = ExternalToolCapability(name="open_file")
        info = ExternalToolInfo(
            name="vscode",
            capabilities=[cap],
        )
        assert len(info.capabilities) == 1
        assert info.capabilities[0].name == "open_file"


class TestSandboxResourceLimits:
    def test_defaults(self) -> None:
        limits = SandboxResourceLimits()
        assert limits.cpu_limit == 1.0
        assert limits.memory_limit_mb == 512
        assert limits.disk_limit_mb == 1024
        assert limits.timeout_seconds == 60.0
        assert limits.network_whitelist == []

    def test_custom_limits(self) -> None:
        limits = SandboxResourceLimits(
            cpu_limit=2.0,
            memory_limit_mb=1024,
            network_whitelist=["localhost", "api.example.com"],
        )
        assert limits.cpu_limit == 2.0
        assert limits.memory_limit_mb == 1024
        assert len(limits.network_whitelist) == 2
