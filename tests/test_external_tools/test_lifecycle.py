"""外部工具生命周期管理器测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from tools.external.config import ExternalToolConfigManager
from tools.external.lifecycle import ExternalToolLifecycle
from tools.external.sandbox import ExternalToolSandbox
from tools.external.secrets import ExternalToolSecretManager
from tools.external.types import (
    ExternalToolConfig,
    ExternalToolState,
    ProtocolType,
)
from tests.test_external_tools.conftest import _StubAdapter


# ── 辅助 fixtures ──


@pytest.fixture
def secret_mgr() -> ExternalToolSecretManager:
    return ExternalToolSecretManager("test_lifecycle_key")


@pytest.fixture
def mock_config_mgr() -> MagicMock:
    """模拟配置管理器。"""
    mgr = MagicMock(spec=ExternalToolConfigManager)
    config = ExternalToolConfig(
        name="test_tool",
        display_name="Test Tool",
        protocol=ProtocolType.HTTP,
        endpoint="http://localhost:8080",
        extra={"type": "stub"},
    )
    mgr.load_all.return_value = {"test_tool": config}
    mgr.get_config.return_value = config
    return mgr


@pytest.fixture
def lifecycle(
    mock_config_mgr: MagicMock, secret_mgr: ExternalToolSecretManager
) -> ExternalToolLifecycle:
    return ExternalToolLifecycle(
        config_manager=mock_config_mgr,
        secret_manager=secret_mgr,
    )


# ════════════════════════════════════════════
# 初始化
# ════════════════════════════════════════════


class TestLifecycleInit:
    """初始化测试。"""

    def test_default_components(
        self, mock_config_mgr: MagicMock, secret_mgr: ExternalToolSecretManager
    ) -> None:
        """默认创建注册表和沙箱。"""
        lc = ExternalToolLifecycle(mock_config_mgr, secret_mgr)
        assert lc.registry is not None
        assert lc.sandbox is not None

    def test_custom_components(
        self, mock_config_mgr: MagicMock, secret_mgr: ExternalToolSecretManager
    ) -> None:
        """注入自定义注册表和沙箱。"""
        registry = MagicMock()
        sandbox = MagicMock(spec=ExternalToolSandbox)
        lc = ExternalToolLifecycle(mock_config_mgr, secret_mgr, registry, sandbox)
        assert lc.registry is registry
        assert lc.sandbox is sandbox

    def test_initial_state_not_running(self, lifecycle: ExternalToolLifecycle) -> None:
        """初始未运行。"""
        assert lifecycle.is_running is False


# ════════════════════════════════════════════
# 适配器类型注册
# ════════════════════════════════════════════


class TestRegisterAdapterType:
    """适配器类型注册测试。"""

    def test_register_adapter_type(self, lifecycle: ExternalToolLifecycle) -> None:
        """注册适配器类型。"""
        lifecycle.register_adapter_type("stub", _StubAdapter)
        assert "stub" in lifecycle._adapter_factory
        assert lifecycle._adapter_factory["stub"] is _StubAdapter


# ════════════════════════════════════════════
# 启动
# ════════════════════════════════════════════


class TestStart:
    """启动流程测试。"""

    @pytest.mark.asyncio
    async def test_start_no_configs(
        self, secret_mgr: ExternalToolSecretManager
    ) -> None:
        """无配置时启动仍标记为 running。"""
        empty_mgr = MagicMock(spec=ExternalToolConfigManager)
        empty_mgr.load_all.return_value = {}
        lc = ExternalToolLifecycle(empty_mgr, secret_mgr)

        await lc.start()
        assert lc.is_running is True

    @pytest.mark.asyncio
    async def test_start_already_running(self, lifecycle: ExternalToolLifecycle) -> None:
        """重复启动不报错。"""
        lifecycle._running = True
        await lc_start_safe(lifecycle)  # 不应抛异常

    @pytest.mark.asyncio
    async def test_start_registers_adapter(
        self, lifecycle: ExternalToolLifecycle, mock_config_mgr: MagicMock
    ) -> None:
        """启动时根据配置创建适配器。"""
        lifecycle.register_adapter_type("stub", _StubAdapter)
        with patch("tools.external.lifecycle.ExternalToolConnection") as mock_conn_cls:
            mock_conn = AsyncMock()
            mock_conn.connect = AsyncMock()
            mock_conn.get_state.return_value = ExternalToolState.CONNECTED
            mock_conn_cls.return_value = mock_conn

            await lifecycle.start()
            assert lifecycle.registry.count() == 1

    @pytest.mark.asyncio
    async def test_start_unknown_adapter_type(
        self, lifecycle: ExternalToolLifecycle, mock_config_mgr: MagicMock
    ) -> None:
        """未知适配器类型跳过该工具。"""
        # 不注册任何适配器类型
        await lifecycle.start()
        assert lifecycle.registry.count() == 0

    @pytest.mark.asyncio
    async def test_start_connection_failure_not_blocking(
        self, lifecycle: ExternalToolLifecycle, mock_config_mgr: MagicMock
    ) -> None:
        """连接失败不阻塞启动流程。"""
        lifecycle.register_adapter_type("stub", _StubAdapter)
        with patch("tools.external.lifecycle.ExternalToolConnection") as mock_conn_cls:
            mock_conn = AsyncMock()
            mock_conn.connect = AsyncMock(side_effect=Exception("conn refused"))
            mock_conn_cls.return_value = mock_conn

            await lifecycle.start()
            assert lifecycle.is_running


# ════════════════════════════════════════════
# 停止
# ════════════════════════════════════════════


class TestStop:
    """停止流程测试。"""

    @pytest.mark.asyncio
    async def test_stop_sets_not_running(self, lifecycle: ExternalToolLifecycle) -> None:
        """停止后 is_running 为 False。"""
        lifecycle._running = True
        await lifecycle.stop()
        assert lifecycle.is_running is False

    @pytest.mark.asyncio
    async def test_stop_not_running_no_error(self, lifecycle: ExternalToolLifecycle) -> None:
        """未运行时停止不报错。"""
        lifecycle._running = False
        await lifecycle.stop()  # 不应抛异常

    @pytest.mark.asyncio
    async def test_stop_disconnects_connections(
        self, lifecycle: ExternalToolLifecycle
    ) -> None:
        """停止时断开所有连接。"""
        mock_conn = AsyncMock()
        mock_adapter = _StubAdapter(
            ExternalToolConfig(name="tool_x", protocol=ProtocolType.HTTP)
        )
        lifecycle._registry.register_external_tool(mock_adapter, mock_conn)

        lifecycle._running = True
        await lifecycle.stop()
        mock_conn.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_destroys_sandboxes(self, lifecycle: ExternalToolLifecycle) -> None:
        """停止时销毁所有沙箱。"""
        mock_sandbox = AsyncMock(spec=ExternalToolSandbox)
        lifecycle._sandbox = mock_sandbox
        lifecycle._running = True
        await lifecycle.stop()
        mock_sandbox.destroy_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_cancels_monitor(self, lifecycle: ExternalToolLifecycle) -> None:
        """停止时取消监控任务。"""
        lifecycle._running = True
        lifecycle._monitor_task = asyncio.create_task(asyncio.sleep(100))

        await lifecycle.stop()
        assert lifecycle._monitor_task is None


# ════════════════════════════════════════════
# 重载
# ════════════════════════════════════════════


class TestReload:
    """重载测试。"""

    @pytest.mark.asyncio
    async def test_reload_calls_stop_then_start(self, lifecycle: ExternalToolLifecycle) -> None:
        """重载先停止再启动。"""
        lifecycle.register_adapter_type("stub", _StubAdapter)
        with patch.object(lifecycle, "stop", new_callable=AsyncMock) as mock_stop, \
             patch.object(lifecycle, "start", new_callable=AsyncMock) as mock_start:
            await lifecycle.reload()
            mock_stop.assert_called_once()
            mock_start.assert_called_once()


# ════════════════════════════════════════════
# 单工具启停
# ════════════════════════════════════════════


class TestSingleToolControl:
    """单工具启停测试。"""

    @pytest.mark.asyncio
    async def test_start_tool_no_config(
        self, lifecycle: ExternalToolLifecycle, mock_config_mgr: MagicMock
    ) -> None:
        """配置不存在时返回 False。"""
        mock_config_mgr.get_config.return_value = None
        result = await lifecycle.start_tool("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_start_tool_success(
        self, lifecycle: ExternalToolLifecycle, mock_config_mgr: MagicMock
    ) -> None:
        """启动单个工具成功。"""
        lifecycle.register_adapter_type("stub", _StubAdapter)
        with patch("tools.external.lifecycle.ExternalToolConnection") as mock_conn_cls:
            mock_conn = AsyncMock()
            mock_conn.connect = AsyncMock()
            mock_conn_cls.return_value = mock_conn
            result = await lifecycle.start_tool("test_tool")
            assert result is True

    @pytest.mark.asyncio
    async def test_stop_tool_success(self, lifecycle: ExternalToolLifecycle) -> None:
        """ 停止单个工具成功。"""
        mock_conn = AsyncMock()
        mock_adapter = _StubAdapter(
            ExternalToolConfig(name="t", protocol=ProtocolType.HTTP)
        )
        lifecycle._registry.register_external_tool(mock_adapter, mock_conn)
        result = await lifecycle.stop_tool("t")
        assert result is True
        mock_conn.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_tool_no_connection(self, lifecycle: ExternalToolLifecycle) -> None:
        """无连接时停止仍成功。"""
        mock_adapter = _StubAdapter(
            ExternalToolConfig(name="t", protocol=ProtocolType.HTTP)
        )
        lifecycle._registry.register_external_tool(mock_adapter)
        result = await lifecycle.stop_tool("t")
        assert result is True


# ════════════════════════════════════════════
# 状态查询
# ════════════════════════════════════════════


class TestGetStatus:
    """状态查询测试。"""

    def test_get_status_initial(self, lifecycle: ExternalToolLifecycle) -> None:
        """初始状态。"""
        status = lifecycle.get_status()
        assert status["running"] is False
        assert status["tool_count"] == 0
        assert status["tools"] == {}

    def test_get_status_with_tools(self, lifecycle: ExternalToolLifecycle) -> None:
        """有工具时的状态。"""
        adapter = _StubAdapter(
            ExternalToolConfig(name="tool_x", protocol=ProtocolType.HTTP)
        )
        lifecycle._registry.register_external_tool(adapter)
        status = lifecycle.get_status()
        assert status["tool_count"] == 1
        assert "tool_x" in status["tools"]
        assert status["tools"]["tool_x"]["state"] == "disconnected"


# ── 辅助函数 ──


async def lc_start_safe(lc: ExternalToolLifecycle) -> None:
    """安全调用 start（忽略已运行警告）。"""
    await lc.start()
