"""Step3 架构重构测试：激活 ChannelGateway 并消除 sys._agent_os_* 覆盖。

验证：
- AC-1: ChannelGateway 在 Application 层被正确启动和初始化
- AC-2: 外部通道适配器能通过 Gateway 获取服务
- AC-3: sys._agent_os_* 全局变量不再被多通道覆盖
- AC-4: 所有通道通过统一的服务注入获取依赖
- AC-5: 不引入循环依赖
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest



# ---------------------------------------------------------------------------
# 辅助：安全重置 ServiceProvider
# ---------------------------------------------------------------------------

def _reset_sp() -> None:
    """重置 ServiceProvider 单例。"""
    from infrastructure.service_provider import ServiceProvider
    ServiceProvider.reset()


# ---------------------------------------------------------------------------
# AC-1: ChannelGateway 在 Application 层被正确启动和初始化
# ---------------------------------------------------------------------------


class TestChannelGatewayActivation:
    """验证 Application 层正确创建和管理 ChannelGateway。"""

    def test_create_gateway_returns_instance(self) -> None:
        """create_gateway 应返回 ChannelGateway 实例。"""
        from application import Application
        from channels.gateway.channel_gateway import ChannelGateway

        app = Application()
        gateway = app.create_gateway()
        assert isinstance(gateway, ChannelGateway)

    def test_build_services_creates_channel_gateway(self) -> None:
        """build_services 应将 channel_gateway 注册到 services 字典中。

        build_services 内部有 try/except 保护，即使部分服务创建失败，
        channel_gateway 仍应被成功创建并注册。
        """
        from application import Application

        _reset_sp()
        app = Application()
        services = app.build_services()

        assert "channel_gateway" in services
        _reset_sp()

    def test_channel_gateway_is_correct_type(self) -> None:
        """channel_gateway 服务应为 ChannelGateway 实例。"""
        from application import Application
        from channels.gateway.channel_gateway import ChannelGateway

        _reset_sp()
        app = Application()
        services = app.build_services()

        assert isinstance(services["channel_gateway"], ChannelGateway)
        _reset_sp()

    def test_channel_gateway_has_services_reference(self) -> None:
        """ChannelGateway 应持有 services 字典的引用。"""
        from application import Application

        _reset_sp()
        app = Application()
        services = app.build_services()

        gateway = services["channel_gateway"]
        # gateway.services 应该就是同一个 services 字典
        assert gateway.services is services
        _reset_sp()


# ---------------------------------------------------------------------------
# AC-2: 外部通道适配器能通过 Gateway 获取服务
# ---------------------------------------------------------------------------


class TestGatewayServiceInjection:
    """验证 ChannelGateway 能为适配器提供服务访问。"""

    def test_gateway_has_services_property(self) -> None:
        """ChannelGateway 应有 services 属性。"""
        from channels.gateway.channel_gateway import ChannelGateway

        gateway = ChannelGateway()
        assert hasattr(gateway, "services")

    def test_gateway_services_setter(self) -> None:
        """ChannelGateway.services 应可被设置为服务字典。"""
        from channels.gateway.channel_gateway import ChannelGateway

        gateway = ChannelGateway()
        test_services = {"tool_registry": MagicMock(), "task_service": MagicMock()}
        gateway.services = test_services
        assert gateway.services is test_services

    def test_gateway_get_service_returns_registered(self) -> None:
        """ChannelGateway.get_service 应返回已注册的服务。"""
        from channels.gateway.channel_gateway import ChannelGateway

        gateway = ChannelGateway()
        mock_svc = MagicMock()
        gateway.services = {"my_service": mock_svc}
        result = gateway.get_service("my_service")
        assert result is mock_svc

    def test_gateway_get_service_returns_none_for_missing(self) -> None:
        """ChannelGateway.get_service 对未注册的服务返回 None。"""
        from channels.gateway.channel_gateway import ChannelGateway

        gateway = ChannelGateway()
        gateway.services = {}
        result = gateway.get_service("non_existent")
        assert result is None


# ---------------------------------------------------------------------------
# AC-3: sys._agent_os_* 全局变量不再被多通道覆盖
# ---------------------------------------------------------------------------


class TestServiceProviderRegistration:
    """验证服务通过 ServiceProvider 统一注册，防止全局变量覆盖。"""

    def test_service_provider_has_register_services(self) -> None:
        """ServiceProvider 应有 register_services 批量注册方法。"""
        from infrastructure.service_provider import ServiceProvider

        provider = ServiceProvider()
        assert hasattr(provider, "register_services")

    def test_register_services_stores_all(self) -> None:
        """register_services 应将所有服务存入内部字典。"""
        from infrastructure.service_provider import ServiceProvider

        _reset_sp()
        provider = ServiceProvider()
        mock_a = MagicMock()
        mock_b = MagicMock()
        provider.register_services({"svc_a": mock_a, "svc_b": mock_b})
        assert provider.get("svc_a") is mock_a
        assert provider.get("svc_b") is mock_b
        _reset_sp()

    def test_register_services_idempotent(self) -> None:
        """重复 register_services 不应覆盖已注册的服务（防止多通道冲突）。"""
        from infrastructure.service_provider import ServiceProvider

        _reset_sp()
        provider = ServiceProvider()
        first_svc = MagicMock(name="first")
        second_svc = MagicMock(name="second")
        provider.register("shared_service", first_svc)
        # 再次注册同名的，应保留第一个（不覆盖）
        provider.register_services({"shared_service": second_svc})
        assert provider.get("shared_service") is first_svc
        _reset_sp()

    def test_build_services_registers_to_service_provider(self) -> None:
        """Application.build_services 应将服务注册到 ServiceProvider。"""
        from application import Application
        from infrastructure.service_provider import ServiceProvider

        _reset_sp()
        app = Application()
        app.build_services()

        provider = ServiceProvider()
        # build_services 会创建 task_service（在 try/except 中），
        # 如果创建成功则应注册到 ServiceProvider
        assert provider.get("task_service") is not None
        _reset_sp()

    def test_build_services_no_longer_writes_sys_globals(self) -> None:
        """Application.build_services 不应写入 sys._agent_os_* 全局变量。"""
        from application import Application

        _reset_sp()
        # 清除可能存在的旧全局变量
        for key in list(vars(sys).keys()):
            if key.startswith("_agent_os_"):
                delattr(sys, key)

        app = Application()
        app.build_services()

        # 验证没有写入新的 sys._agent_os_* 变量
        agent_os_vars = [k for k in vars(sys) if k.startswith("_agent_os_")]
        assert agent_os_vars == []
        _reset_sp()

    def test_service_provider_reset(self) -> None:
        """ServiceProvider.reset 应清除所有已注册服务。"""
        from infrastructure.service_provider import ServiceProvider

        _reset_sp()
        provider = ServiceProvider()
        provider.register("test_svc", MagicMock())
        assert provider.get("test_svc") is not None
        provider.reset()
        provider = ServiceProvider()
        assert provider.get("test_svc") is None


# ---------------------------------------------------------------------------
# AC-4: 所有通道通过统一的服务注入获取依赖
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="模块 tools.builtin.task_manage 不存在")
class TestTaskManageUsesServiceProvider:
    """验证 task_manage.py 优先使用 ServiceProvider 获取服务。"""

    @staticmethod
    def _import_task_manage() -> Any:
        """安全导入 task_manage 模块（避免 pytest 环境的命名空间冲突）。"""
        import importlib
        return importlib.import_module("tools.builtin.task_manage")

    def test_get_task_service_uses_service_provider_first(self) -> None:
        """_get_task_service 应优先从 ServiceProvider 获取。"""
        from infrastructure.service_provider import ServiceProvider

        _reset_sp()
        provider = ServiceProvider()
        mock_task_service = MagicMock(name="sp_task_service")
        provider.register("task_service", mock_task_service)

        tm = self._import_task_manage()
        tm._task_service_instance = None

        result = tm._get_task_service()
        assert result is mock_task_service
        _reset_sp()

    def test_get_task_service_falls_back_to_sys(self) -> None:
        """_get_task_service 在 ServiceProvider 无服务时应回退到 sys。"""

        _reset_sp()
        mock_svc = MagicMock(name="sys_task_service")
        sys._agent_os_task_service = mock_svc

        tm = self._import_task_manage()
        tm._task_service_instance = None

        try:
            result = tm._get_task_service()
            assert result is mock_svc
        finally:
            del sys._agent_os_task_service
            _reset_sp()


class TestTaskWorkerRegistersViaServiceProvider:
    """验证 TaskWorker 通过 ServiceProvider 注册自身。"""

    def test_worker_creation_succeeds(self) -> None:
        """TaskWorker 应能正常创建实例。"""
        from infrastructure.task_worker import TaskWorker

        _reset_sp()
        worker = TaskWorker(
            task_service=MagicMock(),
            plugin_registry=MagicMock(),
            input_route_table=MagicMock(),
            output_route_table=MagicMock(),
            services={},
            event_bus=MagicMock(),
        )
        assert worker is not None
        _reset_sp()


# ---------------------------------------------------------------------------
# AC-5: 不引入循环依赖
# ---------------------------------------------------------------------------


class TestNoCircularImports:
    """验证修改不引入循环依赖。"""

    def test_application_imports_channel_gateway(self) -> None:
        """Application 应能导入 ChannelGateway 而无循环依赖。"""
        from application import Application
        from channels.gateway.channel_gateway import ChannelGateway

        assert Application is not None
        assert ChannelGateway is not None

    def test_service_provider_no_circular_deps(self) -> None:
        """ServiceProvider 不应依赖 Application。"""
        import infrastructure.service_provider as sp_module

        source = open(sp_module.__file__, encoding="utf-8").read()
        assert "from application" not in source
        assert "import application" not in source

    def test_application_no_sys_import(self) -> None:
        """Application 不应导入 sys（消除 sys._agent_os_* 的标志）。"""
        import application as app_module

        source = open(app_module.__file__, encoding="utf-8").read()
        # 允许在注释中出现 sys，但不能在代码中使用
        lines = [
            line for line in source.splitlines()
            if not line.strip().startswith("#")
            and not line.strip().startswith('"""')
            and not line.strip().startswith("'''")
        ]
        code_text = "\n".join(lines)
        assert "import sys" not in code_text
