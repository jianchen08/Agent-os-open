"""Application 类单元测试。

验证 Application 类的服务构建和获取功能，
确保 start_server.py 和 CLI 通道能通过统一接口获取服务。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Application 类导入测试
# ---------------------------------------------------------------------------


class TestApplicationImport:
    """验证 Application 类可被正确导入。"""

    def test_import_application_class(self) -> None:
        """Application 类应可从 src.application 导入。"""
        from application import Application

        assert Application is not None

    def test_application_is_class(self) -> None:
        """Application 应该是一个类。"""
        from application import Application

        assert isinstance(Application, type)


# ---------------------------------------------------------------------------
# Application 实例化测试
# ---------------------------------------------------------------------------


class TestApplicationInit:
    """验证 Application 类的实例化和基本属性。"""

    def test_default_init(self) -> None:
        """默认初始化不应抛出异常。"""
        from application import Application

        app = Application()
        assert app is not None

    def test_services_property_empty(self) -> None:
        """未调用 build_services 时，services 应为空字典。"""
        from application import Application

        app = Application()
        assert app.services == {}

    def test_project_root_property(self) -> None:
        """project_root 应返回 Path 对象。"""
        from application import Application

        app = Application()
        assert isinstance(app.project_root, Path)


# ---------------------------------------------------------------------------
# Application.build_services 测试
# ---------------------------------------------------------------------------


class TestBuildServices:
    """验证 build_services 方法能正确构建服务字典。"""

    @patch("application._register_basic_tools")
    @patch("application.init_tool_auto_loader")
    @patch("application.ToolRegistry")
    def test_build_services_returns_dict(
        self,
        mock_tool_registry_cls: MagicMock,
        mock_auto_loader: MagicMock,
        mock_register_basic: MagicMock,
    ) -> None:
        """build_services 应返回字典。"""
        from application import Application

        app = Application()
        services = app.build_services()
        assert isinstance(services, dict)

    @patch("application._register_basic_tools")
    @patch("application.init_tool_auto_loader")
    @patch("application.ToolRegistry")
    @patch("application.JsonMemoryStore")
    def test_build_services_creates_tool_registry(
        self,
        mock_json_store: MagicMock,
        mock_tool_registry_cls: MagicMock,
        mock_auto_loader: MagicMock,
        mock_register_basic: MagicMock,
    ) -> None:
        """build_services 应创建 tool_registry 服务。"""
        from application import Application

        app = Application()
        services = app.build_services()
        assert "tool_registry" in services

    @patch("application._register_basic_tools")
    @patch("application.init_tool_auto_loader")
    @patch("application.ToolRegistry")
    @patch("application.JsonMemoryStore")
    def test_build_services_creates_memory_store(
        self,
        mock_json_store: MagicMock,
        mock_tool_registry_cls: MagicMock,
        mock_auto_loader: MagicMock,
        mock_register_basic: MagicMock,
    ) -> None:
        """build_services 应创建 memory_store 服务。"""
        from application import Application

        app = Application()
        services = app.build_services()
        assert "memory_store" in services

    @patch("application._register_basic_tools")
    @patch("application.init_tool_auto_loader")
    @patch("application.ToolRegistry")
    @patch("application.JsonMemoryStore")
    def test_build_services_creates_event_bus(
        self,
        mock_json_store: MagicMock,
        mock_tool_registry_cls: MagicMock,
        mock_auto_loader: MagicMock,
        mock_register_basic: MagicMock,
    ) -> None:
        """build_services 应创建 event_bus 服务。"""
        from application import Application

        app = Application()
        services = app.build_services()
        assert "event_bus" in services

    @patch("application._register_basic_tools")
    @patch("application.init_tool_auto_loader")
    @patch("application.ToolRegistry")
    @patch("application.JsonMemoryStore")
    def test_build_services_creates_task_service(
        self,
        mock_json_store: MagicMock,
        mock_tool_registry_cls: MagicMock,
        mock_auto_loader: MagicMock,
        mock_register_basic: MagicMock,
    ) -> None:
        """build_services 应创建 task_service 服务。"""
        from application import Application

        app = Application()
        services = app.build_services()
        assert "task_service" in services

    @patch("application._register_basic_tools")
    @patch("application.init_tool_auto_loader")
    @patch("application.ToolRegistry")
    @patch("application.JsonMemoryStore")
    def test_build_services_with_agent_registry(
        self,
        mock_json_store: MagicMock,
        mock_tool_registry_cls: MagicMock,
        mock_auto_loader: MagicMock,
        mock_register_basic: MagicMock,
    ) -> None:
        """build_services 接收 agent_registry 参数并注入到 services。"""
        from application import Application

        app = Application()
        mock_registry = MagicMock()
        services = app.build_services(agent_registry=mock_registry)
        assert "agent_registry" in services
        assert services["agent_registry"] is mock_registry

    @patch("application._register_basic_tools")
    @patch("application.init_tool_auto_loader")
    @patch("application.ToolRegistry")
    @patch("application.JsonMemoryStore")
    def test_build_services_stores_in_instance(
        self,
        mock_json_store: MagicMock,
        mock_tool_registry_cls: MagicMock,
        mock_auto_loader: MagicMock,
        mock_register_basic: MagicMock,
    ) -> None:
        """build_services 应将结果存储到实例的 services 属性中。"""
        from application import Application

        app = Application()
        services = app.build_services()
        assert app.services is services

    @patch("application._register_basic_tools")
    @patch("application.init_tool_auto_loader")
    @patch("application.ToolRegistry")
    @patch("application.JsonMemoryStore")
    def test_build_services_creates_checkpoint_manager(
        self,
        mock_json_store: MagicMock,
        mock_tool_registry_cls: MagicMock,
        mock_auto_loader: MagicMock,
        mock_register_basic: MagicMock,
    ) -> None:
        """build_services 应创建 checkpoint_manager 服务。"""
        from application import Application

        app = Application()
        services = app.build_services()
        assert "checkpoint_manager" in services


# ---------------------------------------------------------------------------
# Application.get_service 测试
# ---------------------------------------------------------------------------


class TestGetService:
    """验证 get_service 方法。"""

    def test_get_service_returns_registered_service(self) -> None:
        """get_service 应返回已注册的服务。"""
        from application import Application

        app = Application()
        mock_service = MagicMock()
        app._services = {"test_service": mock_service}
        result = app.get_service("test_service")
        assert result is mock_service

    def test_get_service_returns_none_for_missing(self) -> None:
        """get_service 对未注册的服务应返回 None。"""
        from application import Application

        app = Application()
        result = app.get_service("non_existent")
        assert result is None

    def test_get_service_with_default(self) -> None:
        """get_service 对未注册的服务应返回指定的默认值。"""
        from application import Application

        app = Application()
        result = app.get_service("non_existent", default="fallback")
        assert result == "fallback"


# ---------------------------------------------------------------------------
# start_server.py 改造验证测试
# ---------------------------------------------------------------------------


class TestStartServerRefactoring:
    """验证 start_server.py 的改造结果。"""

    def test_start_server_imports_application(self) -> None:
        """start_server.py 应从 src.application 导入 Application 类。"""
        import importlib

        spec = importlib.util.spec_from_file_location(
            "start_server",
            str(Path(__file__).resolve().parent.parent.parent / "start_server.py"),
        )
        assert spec is not None
        source = spec.loader.get_data("start_server.py").decode("utf-8")
        assert "from application import Application" in source or \
               "from src.application import Application" in source

    def test_start_server_no_build_services_function(self) -> None:
        """start_server.py 不应包含顶层 _build_services 函数定义。"""
        import importlib

        spec = importlib.util.spec_from_file_location(
            "start_server",
            str(Path(__file__).resolve().parent.parent.parent / "start_server.py"),
        )
        assert spec is not None
        source = spec.loader.get_data("start_server.py").decode("utf-8")
        # 不应有独立的 def _build_services 函数定义
        assert "\ndef _build_services(" not in source

    def test_start_server_has_main_function(self) -> None:
        """start_server.py 应保留 main 入口函数。"""
        import importlib

        spec = importlib.util.spec_from_file_location(
            "start_server",
            str(Path(__file__).resolve().parent.parent.parent / "start_server.py"),
        )
        assert spec is not None
        source = spec.loader.get_data("start_server.py").decode("utf-8")
        assert "def main()" in source

    def test_start_server_no_direct_pipeline_engine_instantiation(self) -> None:
        """start_server.py 不应直接实例化 PipelineEngine。"""
        import importlib

        spec = importlib.util.spec_from_file_location(
            "start_server",
            str(Path(__file__).resolve().parent.parent.parent / "start_server.py"),
        )
        assert spec is not None
        source = spec.loader.get_data("start_server.py").decode("utf-8")
        assert "PipelineEngine(" not in source

    def test_start_server_no_direct_task_worker_instantiation(self) -> None:
        """start_server.py 不应直接实例化 TaskWorker。"""
        import importlib

        spec = importlib.util.spec_from_file_location(
            "start_server",
            str(Path(__file__).resolve().parent.parent.parent / "start_server.py"),
        )
        assert spec is not None
        source = spec.loader.get_data("start_server.py").decode("utf-8")
        assert "TaskWorker(" not in source
