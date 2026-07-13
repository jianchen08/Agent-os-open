"""外部工具生命周期管理测试。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.external.adapter import ExternalToolAdapter
from tools.external.config import ExternalToolConfigManager
from tools.external.lifecycle import ExternalToolLifecycle
from tools.external.registry import ExternalToolRegistry
from tools.external.sandbox import ExternalToolSandbox
from tools.external.secrets import ExternalToolSecretManager
from tools.external.types import (
    ExternalToolCapability,
    ExternalToolConfig,
    ExternalToolState,
)


class StubAdapter(ExternalToolAdapter):
    """测试用 Stub 适配器。"""

    def define_schemas(self) -> list[ExternalToolCapability]:
        return [ExternalToolCapability(name="stub_op", description="Stub")]

    async def _do_execute(self, operation: str, inputs: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"success": True}


@pytest.fixture
def secret_manager() -> ExternalToolSecretManager:
    return ExternalToolSecretManager("test-lifecycle-key")


@pytest.fixture
def registry() -> ExternalToolRegistry:
    return ExternalToolRegistry()


@pytest.fixture
def sandbox() -> ExternalToolSandbox:
    return ExternalToolSandbox()


@pytest.fixture
def config_manager(tmp_path) -> ExternalToolConfigManager:
    import yaml
    config_dir = tmp_path / "external_tools"
    config_dir.mkdir()
    return ExternalToolConfigManager(config_dir=str(config_dir))


@pytest.fixture
def lifecycle(
    config_manager: ExternalToolConfigManager,
    secret_manager: ExternalToolSecretManager,
    registry: ExternalToolRegistry,
    sandbox: ExternalToolSandbox,
) -> ExternalToolLifecycle:
    return ExternalToolLifecycle(
        config_manager=config_manager,
        secret_manager=secret_manager,
        registry=registry,
        sandbox=sandbox,
    )


class TestExternalToolLifecycle:

    def test_initial_state(self, lifecycle: ExternalToolLifecycle) -> None:
        assert not lifecycle.is_running
        assert lifecycle.registry.count() == 0

    def test_register_adapter_type(self, lifecycle: ExternalToolLifecycle) -> None:
        lifecycle.register_adapter_type("stub", StubAdapter)
        assert "stub" in lifecycle._adapter_factory

    @pytest.mark.asyncio
    async def test_start_no_configs(self, lifecycle: ExternalToolLifecycle) -> None:
        """无配置时应正常启动但无工具。"""
        await lifecycle.start()
        assert lifecycle.is_running
        assert lifecycle.registry.count() == 0
        await lifecycle.stop()

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self, lifecycle: ExternalToolLifecycle) -> None:
        """未运行时停止不报错。"""
        await lifecycle.stop()
        assert not lifecycle.is_running

    @pytest.mark.asyncio
    async def test_start_idempotent(self, lifecycle: ExternalToolLifecycle) -> None:
        """重复启动不报错。"""
        await lifecycle.start()
        await lifecycle.start()  # 第二次应被忽略
        assert lifecycle.is_running
        await lifecycle.stop()

    @pytest.mark.asyncio
    async def test_get_status(self, lifecycle: ExternalToolLifecycle) -> None:
        status = lifecycle.get_status()
        assert status["running"] is False
        assert status["tool_count"] == 0
        assert status["tools"] == {}

    @pytest.mark.asyncio
    async def test_register_adapter_and_get_status(
        self,
        lifecycle: ExternalToolLifecycle,
        config_manager: ExternalToolConfigManager,
    ) -> None:
        """注册适配器类型后状态应反映。"""
        lifecycle.register_adapter_type("stub", StubAdapter)
        status = lifecycle.get_status()
        assert status["running"] is False

    @pytest.mark.asyncio
    async def test_start_tool_not_found(self, lifecycle: ExternalToolLifecycle) -> None:
        """启动不存在的工具应返回 False。"""
        result = await lifecycle.start_tool("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_stop_tool(self, lifecycle: ExternalToolLifecycle) -> None:
        """停止单个工具。"""
        config = ExternalToolConfig(name="test_tool", endpoint="http://localhost:8080")
        adapter = StubAdapter(config)
        lifecycle.registry.register_external_tool(adapter)
        assert lifecycle.registry.count() == 1

        result = await lifecycle.stop_tool("test_tool")
        assert result is True
        assert lifecycle.registry.count() == 0

    @pytest.mark.asyncio
    async def test_sandbox_accessible(self, lifecycle: ExternalToolLifecycle) -> None:
        assert lifecycle.sandbox is not None

    @pytest.mark.asyncio
    async def test_registry_accessible(self, lifecycle: ExternalToolLifecycle) -> None:
        assert lifecycle.registry is not None
