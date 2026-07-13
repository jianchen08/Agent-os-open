"""外部工具异常体系测试。"""

from __future__ import annotations

import pytest

from core.exceptions import BaseAppException, DomainException
from tools.external.exceptions import (
    ConfigError,
    ConnectionError,
    ExecutionError,
    ExternalTimeoutError,
    ExternalToolException,
    SandboxError,
    SecretError,
)


# ════════════════════════════════════════════
# 异常层次结构
# ════════════════════════════════════════════


class TestExceptionHierarchy:
    """验证异常继承链完整。"""

    def test_base_external_tool_exception(self) -> None:
        """ExternalToolException 继承自 DomainException。"""
        assert issubclass(ExternalToolException, DomainException)
        assert issubclass(ExternalToolException, BaseAppException)
        assert issubclass(ExternalToolException, Exception)

    def test_connection_error_hierarchy(self) -> None:
        """ConnectionError 继承链正确。"""
        assert issubclass(ConnectionError, ExternalToolException)
        assert issubclass(ConnectionError, DomainException)

    def test_execution_error_hierarchy(self) -> None:
        """ExecutionError 继承链正确。"""
        assert issubclass(ExecutionError, ExternalToolException)

    def test_timeout_error_hierarchy(self) -> None:
        """ExternalTimeoutError 继承链正确。"""
        assert issubclass(ExternalTimeoutError, ExternalToolException)

    def test_config_error_hierarchy(self) -> None:
        """ConfigError 继承链正确。"""
        assert issubclass(ConfigError, ExternalToolException)

    def test_secret_error_hierarchy(self) -> None:
        """SecretError 继承链正确。"""
        assert issubclass(SecretError, ExternalToolException)

    def test_sandbox_error_hierarchy(self) -> None:
        """SandboxError 继承链正确。"""
        assert issubclass(SandboxError, ExternalToolException)


# ════════════════════════════════════════════
# ExternalToolException 基类
# ════════════════════════════════════════════


class TestExternalToolException:
    """基础外部工具异常测试。"""

    def test_default_code(self) -> None:
        """默认错误码。"""
        exc = ExternalToolException(message="test")
        assert exc.code == "EXT_TOOL_ERR"

    def test_custom_code(self) -> None:
        """自定义错误码。"""
        exc = ExternalToolException(message="test", code="CUSTOM_001")
        assert exc.code == "CUSTOM_001"

    def test_tool_name_in_details(self) -> None:
        """tool_name 应存入 details。"""
        exc = ExternalToolException(message="test", tool_name="my_tool")
        assert exc.details["tool_name"] == "my_tool"
        assert exc.tool_name == "my_tool"

    def test_with_cause(self) -> None:
        """原因异常正确传递。"""
        cause = ValueError("original")
        exc = ExternalToolException(message="wrapped", cause=cause)
        assert exc.cause is cause

    def test_str_representation(self) -> None:
        """字符串表示包含错误码和消息。"""
        exc = ExternalToolException(message="something failed")
        text = str(exc)
        assert "EXT_TOOL_ERR" in text
        assert "something failed" in text


# ════════════════════════════════════════════
# ConnectionError
# ════════════════════════════════════════════


class TestConnectionError:
    """连接异常测试。"""

    def test_default_code(self) -> None:
        assert ConnectionError.DEFAULT_CODE == "EXT_CONN_ERR"

    def test_default_message(self) -> None:
        """有默认中文消息。"""
        exc = ConnectionError()
        assert "连接失败" in exc.message

    def test_endpoint_in_details(self) -> None:
        """endpoint 存入 details。"""
        exc = ConnectionError(endpoint="http://localhost:8080")
        assert exc.details["endpoint"] == "http://localhost:8080"
        assert exc.endpoint == "http://localhost:8080"

    def test_with_all_params(self) -> None:
        """所有参数正确赋值。"""
        exc = ConnectionError(
            message="连接被拒绝",
            tool_name="my_tool",
            endpoint="ws://x:9090",
            cause=OSError("refused"),
        )
        assert exc.tool_name == "my_tool"
        assert exc.endpoint == "ws://x:9090"
        assert exc.cause is not None


# ════════════════════════════════════════════
# ExecutionError
# ════════════════════════════════════════════


class TestExecutionError:
    """执行异常测试。"""

    def test_default_code(self) -> None:
        assert ExecutionError.DEFAULT_CODE == "EXT_EXEC_ERR"

    def test_default_message(self) -> None:
        exc = ExecutionError()
        assert "执行失败" in exc.message

    def test_operation_in_details(self) -> None:
        """operation 存入 details。"""
        exc = ExecutionError(operation="run_code")
        assert exc.details["operation"] == "run_code"
        assert exc.operation == "run_code"


# ════════════════════════════════════════════
# ExternalTimeoutError
# ════════════════════════════════════════════


class TestExternalTimeoutError:
    """超时异常测试。"""

    def test_default_code(self) -> None:
        assert ExternalTimeoutError.DEFAULT_CODE == "EXT_TIMEOUT_ERR"

    def test_default_message(self) -> None:
        exc = ExternalTimeoutError()
        assert "超时" in exc.message

    def test_timeout_seconds_in_details(self) -> None:
        """timeout_seconds 存入 details。"""
        exc = ExternalTimeoutError(timeout_seconds=30.0)
        assert exc.details["timeout_seconds"] == 30.0
        assert exc.timeout_seconds == 30.0

    def test_operation_in_details(self) -> None:
        """operation 也存入 details。"""
        exc = ExternalTimeoutError(operation="run_code")
        assert exc.details["operation"] == "run_code"
        assert exc.operation == "run_code"


# ════════════════════════════════════════════
# ConfigError
# ════════════════════════════════════════════


class TestConfigError:
    """配置异常测试。"""

    def test_default_code(self) -> None:
        assert ConfigError.DEFAULT_CODE == "EXT_CONFIG_ERR"

    def test_default_message(self) -> None:
        exc = ConfigError()
        assert "配置错误" in exc.message

    def test_config_key_in_details(self) -> None:
        exc = ConfigError(config_key="endpoint")
        assert exc.details["config_key"] == "endpoint"
        assert exc.config_key == "endpoint"


# ════════════════════════════════════════════
# SecretError
# ════════════════════════════════════════════


class TestSecretError:
    """密钥异常测试。"""

    def test_default_code(self) -> None:
        assert SecretError.DEFAULT_CODE == "EXT_SECRET_ERR"

    def test_default_message(self) -> None:
        exc = SecretError()
        assert "密钥操作失败" in exc.message

    def test_secret_key_masked_in_details(self) -> None:
        """密钥在 details 中应被脱敏为 ***。"""
        exc = SecretError(secret_key="super_secret_key")
        assert exc.details["secret_key"] == "***"
        assert exc.secret_key == "super_secret_key"


# ════════════════════════════════════════════
# SandboxError
# ════════════════════════════════════════════


class TestSandboxError:
    """沙箱异常测试。"""

    def test_default_code(self) -> None:
        assert SandboxError.DEFAULT_CODE == "EXT_SANDBOX_ERR"

    def test_default_message(self) -> None:
        exc = SandboxError()
        assert "沙箱操作失败" in exc.message

    def test_sandbox_id_in_details(self) -> None:
        exc = SandboxError(sandbox_id="ext_tool_abc123")
        assert exc.details["sandbox_id"] == "ext_tool_abc123"
        assert exc.sandbox_id == "ext_tool_abc123"


# ════════════════════════════════════════════
# 错误传播和 to_dict
# ════════════════════════════════════════════


class TestErrorPropagation:
    """错误传播测试。"""

    def test_to_dict(self) -> None:
        """异常可序列化为字典。"""
        exc = ConnectionError(
            message="conn failed",
            tool_name="tool1",
            endpoint="http://x",
        )
        d = exc.to_dict()
        assert d["code"] == "EXT_CONN_ERR"
        assert d["message"] == "conn failed"
        assert d["type"] == "ConnectionError"
        assert d["details"]["tool_name"] == "tool1"

    def test_catch_by_base_class(self) -> None:
        """可以通过基类捕获子类异常。"""
        with pytest.raises(ExternalToolException):
            raise ConnectionError(message="test")

        with pytest.raises(ExternalToolException):
            raise ExecutionError(message="test")

        with pytest.raises(ExternalToolException):
            raise ExternalTimeoutError(message="test")

    def test_catch_by_domain_exception(self) -> None:
        """可以通过 DomainException 捕获。"""
        with pytest.raises(DomainException):
            raise SandboxError(message="test")

    def test_details_not_shared_between_instances(self) -> None:
        """details 字典不会被实例间共享。"""
        e1 = ConnectionError(details={"key": "val1"})
        e2 = ConnectionError(details={"key": "val2"})
        assert e1.details["key"] == "val1"
        assert e2.details["key"] == "val2"
