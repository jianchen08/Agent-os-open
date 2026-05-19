"""外部工具接口抽象方法验证测试。"""

from __future__ import annotations

import pytest

from tools.external.interfaces import (
    IExternalToolAdapter,
    IExternalToolConnection,
    IExternalToolSandbox,
    ISecretManager,
)


# ════════════════════════════════════════════
# IExternalToolConnection
# ════════════════════════════════════════════


class TestIExternalToolConnection:
    """连接管理接口测试。"""

    def test_cannot_instantiate(self) -> None:
        """接口不能直接实例化。"""
        with pytest.raises(TypeError):
            IExternalToolConnection()  # type: ignore[abstract]

    def test_abstract_methods(self) -> None:
        """验证所有抽象方法。"""
        abstracts = {
            "connect",
            "disconnect",
            "health_check",
            "get_state",
            "send_request",
        }
        actual = set(IExternalToolConnection.__abstractmethods__)
        assert actual == abstracts

    @pytest.mark.asyncio
    async def test_concrete_impl_works(self) -> None:
        """具体子类可以正常实例化和调用。"""
        from tools.external.types import ExternalToolState

        class _Concrete(IExternalToolConnection):
            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

            async def health_check(self) -> bool:
                return True

            def get_state(self) -> ExternalToolState:
                return ExternalToolState.CONNECTED

            async def send_request(
                self, operation: str, payload: dict, timeout: float | None = None
            ) -> dict:
                return {"ok": True}

        obj = _Concrete()
        assert await obj.health_check() is True
        assert obj.get_state() == ExternalToolState.CONNECTED


# ════════════════════════════════════════════
# IExternalToolAdapter
# ════════════════════════════════════════════


class TestIExternalToolAdapter:
    """工具适配接口测试。"""

    def test_cannot_instantiate(self) -> None:
        """接口不能直接实例化。"""
        with pytest.raises(TypeError):
            IExternalToolAdapter()  # type: ignore[abstract]

    def test_abstract_methods(self) -> None:
        """验证所有抽象方法。"""
        abstracts = {
            "define_schemas",
            "validate_input",
            "execute",
            "handle_error",
        }
        actual = set(IExternalToolAdapter.__abstractmethods__)
        assert actual == abstracts


# ════════════════════════════════════════════
# IExternalToolSandbox
# ════════════════════════════════════════════


class TestIExternalToolSandbox:
    """沙箱接口测试。"""

    def test_cannot_instantiate(self) -> None:
        """接口不能直接实例化。"""
        with pytest.raises(TypeError):
            IExternalToolSandbox()  # type: ignore[abstract]

    def test_abstract_methods(self) -> None:
        """验证所有抽象方法。"""
        abstracts = {
            "create_sandbox",
            "execute_in_sandbox",
            "destroy_sandbox",
        }
        actual = set(IExternalToolSandbox.__abstractmethods__)
        assert actual == abstracts


# ════════════════════════════════════════════
# ISecretManager
# ════════════════════════════════════════════


class TestISecretManager:
    """密钥管理接口测试。"""

    def test_cannot_instantiate(self) -> None:
        """接口不能直接实例化。"""
        with pytest.raises(TypeError):
            ISecretManager()  # type: ignore[abstract]

    def test_abstract_methods(self) -> None:
        """验证所有抽象方法。"""
        abstracts = {
            "store_secret",
            "get_secret",
            "rotate_secret",
            "delete_secret",
        }
        actual = set(ISecretManager.__abstractmethods__)
        assert actual == abstracts
