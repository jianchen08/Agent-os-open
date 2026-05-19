"""外部工具异常体系测试。"""

from __future__ import annotations

import pytest

from tools.external.exceptions import (
    ConfigError,
    ConnectionError,
    ExecutionError,
    ExternalTimeoutError,
    ExternalToolException,
    SandboxError,
    SecretError,
)


class TestExternalToolException:
    """基础异常测试。"""

    def test_basic_creation(self) -> None:
        exc = ExternalToolException(message="测试异常", tool_name="test_tool")
        assert "测试异常" in str(exc)
        assert exc.tool_name == "test_tool"
        assert exc.code == "EXT_TOOL_ERR"

    def test_with_cause(self) -> None:
        cause = ValueError("原始错误")
        exc = ExternalToolException(message="包装异常", cause=cause)
        assert exc.cause is cause

    def test_to_dict(self) -> None:
        exc = ExternalToolException(
            message="测试", tool_name="tool1", details={"key": "val"},
        )
        d = exc.to_dict()
        assert d["message"] == "测试"
        assert d["details"]["tool_name"] == "tool1"
        assert d["details"]["key"] == "val"


class TestConnectionError:
    def test_with_endpoint(self) -> None:
        exc = ConnectionError(
            message="连接失败",
            tool_name="test",
            endpoint="ws://localhost:8080",
        )
        assert exc.endpoint == "ws://localhost:8080"
        assert exc.details["endpoint"] == "ws://localhost:8080"
        assert exc.code == "EXT_CONN_ERR"

    def test_default_message(self) -> None:
        exc = ConnectionError(tool_name="test")
        assert "连接失败" in str(exc)


class TestExecutionError:
    def test_with_operation(self) -> None:
        exc = ExecutionError(
            message="执行失败",
            tool_name="test",
            operation="open_file",
        )
        assert exc.operation == "open_file"
        assert exc.details["operation"] == "open_file"

    def test_default_message(self) -> None:
        exc = ExecutionError(tool_name="test")
        assert "执行失败" in str(exc)


class TestExternalTimeoutError:
    def test_with_timeout_info(self) -> None:
        exc = ExternalTimeoutError(
            tool_name="test",
            timeout_seconds=30.0,
            operation="run_scene",
        )
        assert exc.timeout_seconds == 30.0
        assert exc.operation == "run_scene"
        assert "超时" in str(exc)


class TestConfigError:
    def test_with_config_key(self) -> None:
        exc = ConfigError(
            message="配置缺失",
            config_key="endpoint",
        )
        assert exc.config_key == "endpoint"
        assert exc.details["config_key"] == "endpoint"


class TestSecretError:
    def test_secret_key_masked(self) -> None:
        exc = SecretError(
            message="密钥错误",
            secret_key="my-secret-key-123",
        )
        assert exc.details["secret_key"] == "***"
        assert exc.secret_key == "my-secret-key-123"


class TestSandboxError:
    def test_with_sandbox_id(self) -> None:
        exc = SandboxError(
            message="沙箱崩溃",
            sandbox_id="ext_test_abc12345",
        )
        assert exc.sandbox_id == "ext_test_abc12345"
        assert exc.details["sandbox_id"] == "ext_test_abc12345"
