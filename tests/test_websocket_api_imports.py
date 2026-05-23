"""src.api.websocket 包导入验证和接口签名匹配测试。

验证：
1. 所有消费者导入不再报错
2. 模块接口签名与消费者调用匹配
3. _init_pipeline_context() 中 build_services() 调用链相关导入正常
"""
import inspect
import pytest


class TestConsumerImports:
    """验证所有消费者能正常导入 src.api.websocket 包中的符号。"""

    def test_import_source_type_and_get_message_bus(self):
        """消费者 websocket_notifier.py 导入 SourceType, get_message_bus。"""
        from src.api.websocket.message_bus import SourceType, get_message_bus
        assert SourceType is not None
        assert callable(get_message_bus)

    def test_import_create_interaction_messages(self):
        """消费者 websocket_notifier.py 导入消息工厂函数。"""
        from src.api.websocket.message_types import (
            create_interaction_request_message,
            create_interaction_cancelled_message,
        )
        assert callable(create_interaction_request_message)
        assert callable(create_interaction_cancelled_message)

    def test_import_get_event_service(self):
        """消费者导入 get_event_service。"""
        from src.api.websocket.service import get_event_service
        assert callable(get_event_service)

    def test_import_connection_manager(self):
        """消费者 task_submit/tool.py 导入 connection_manager。"""
        from src.api.websocket.handler import connection_manager
        assert connection_manager is not None

    def test_import_from_package_init(self):
        """通过包级别 __init__.py 导入所有公开符号。"""
        from src.api.websocket import (
            SourceType,
            connection_manager,
            create_interaction_cancelled_message,
            create_interaction_request_message,
            get_event_service,
            get_message_bus,
        )
        assert SourceType is not None
        assert connection_manager is not None
        assert callable(create_interaction_cancelled_message)
        assert callable(create_interaction_request_message)
        assert callable(get_event_service)
        assert callable(get_message_bus)


class TestInterfaceSignatureMatch:
    """验证模块接口签名与消费者调用方式匹配。"""

    def test_send_execution_start_signature(self):
        """验证 EventService.send_execution_start 签名与消费者调用匹配。"""
        from src.api.websocket.service import EventService
        sig = inspect.signature(EventService.send_execution_start)
        params = list(sig.parameters.keys())
        for p in ("user_id", "execution_id", "execution_type",
                   "name", "description", "parent_id", "input_data", "metadata"):
            assert p in params, f"send_execution_start 缺少参数: {p}"

    def test_send_execution_done_signature(self):
        """验证 EventService.send_execution_done 签名与消费者调用匹配。"""
        from src.api.websocket.service import EventService
        sig = inspect.signature(EventService.send_execution_done)
        params = list(sig.parameters.keys())
        for p in ("user_id", "execution_id", "success",
                   "output", "error", "duration_ms", "summary"):
            assert p in params, f"send_execution_done 缺少参数: {p}"

    def test_connection_manager_has_broadcast(self):
        """验证 ConnectionManager.broadcast 接受 dict 参数。"""
        from src.api.websocket.handler import ConnectionManager
        assert hasattr(ConnectionManager, "broadcast")
        sig = inspect.signature(ConnectionManager.broadcast)
        assert "message" in sig.parameters

    def test_message_bus_emit_signature(self):
        """验证 MessageBus.emit 签名与 websocket_notifier 调用匹配。"""
        from src.api.websocket.message_bus import MessageBus
        sig = inspect.signature(MessageBus.emit)
        params = list(sig.parameters.keys())
        for p in ("thread_id", "message", "source_type", "source_id"):
            assert p in params, f"MessageBus.emit 缺少参数: {p}"

    def test_create_interaction_request_message_signature(self):
        """验证 create_interaction_request_message 签名与 websocket_notifier 调用匹配。"""
        from src.api.websocket.message_types import create_interaction_request_message
        sig = inspect.signature(create_interaction_request_message)
        params = list(sig.parameters.keys())
        for p in ("thread_id", "request_id", "interaction_type",
                   "mode", "title", "description", "priority",
                   "timeout", "approval_options", "context",
                   "conversation_context", "agent_id"):
            assert p in params, f"create_interaction_request_message 缺少参数: {p}"

    def test_create_interaction_cancelled_message_signature(self):
        """验证 create_interaction_cancelled_message 签名匹配。"""
        from src.api.websocket.message_types import create_interaction_cancelled_message
        sig = inspect.signature(create_interaction_cancelled_message)
        params = list(sig.parameters.keys())
        for p in ("thread_id", "request_id", "reason"):
            assert p in params, f"create_interaction_cancelled_message 缺少参数: {p}"


class TestPipelineContextImports:
    """验证 stream_handler._init_pipeline_context 涉及的关键导入正常。"""

    def test_build_services_import_chain(self):
        """验证 build_services 调用链中的关键导入。"""
        from application import Application
        assert hasattr(Application, "build_services")
        sig = inspect.signature(Application.build_services)
        assert "agent_registry" in sig.parameters

    def test_task_submit_tool_import_chain(self):
        """验证 task_submit/tool.py 中延迟导入路径有效。"""
        from src.api.websocket.handler import connection_manager as cm
        assert cm is not None
        assert hasattr(cm, "broadcast")


class TestModuleLevelObjects:
    """验证模块级单例和对象的基本行为。"""

    def test_get_message_bus_returns_same_instance(self):
        """验证 get_message_bus() 返回单例。"""
        from src.api.websocket.message_bus import get_message_bus
        assert get_message_bus() is get_message_bus()

    def test_get_event_service_returns_same_instance(self):
        """验证 get_event_service() 返回单例。"""
        from src.api.websocket.service import get_event_service
        assert get_event_service() is get_event_service()

    def test_connection_manager_is_module_singleton(self):
        """验证 connection_manager 是模块级单例。"""
        from src.api.websocket.handler import connection_manager as cm1
        from src.api.websocket.handler import connection_manager as cm2
        assert cm1 is cm2

    def test_source_type_enum_values(self):
        """验证 SourceType 枚举包含预期值。"""
        from src.api.websocket.message_bus import SourceType
        assert SourceType.SYSTEM.value == "system"
        assert SourceType.AGENT.value == "agent"
        assert SourceType.USER.value == "user"
        assert SourceType.TOOL.value == "tool"

    def test_create_interaction_request_returns_dict(self):
        """验证 create_interaction_request_message 返回正确结构。"""
        from src.api.websocket.message_types import create_interaction_request_message
        msg = create_interaction_request_message(
            thread_id="t1", request_id="r1",
            interaction_type="approval", mode="sync", title="Test",
        )
        assert isinstance(msg, dict)
        assert msg["type"] == "interaction_request"
        assert msg["data"]["thread_id"] == "t1"
        assert msg["data"]["request_id"] == "r1"

    def test_create_interaction_cancelled_returns_dict(self):
        """验证 create_interaction_cancelled_message 返回正确结构。"""
        from src.api.websocket.message_types import create_interaction_cancelled_message
        msg = create_interaction_cancelled_message(
            thread_id="t1", request_id="r1", reason="timeout",
        )
        assert isinstance(msg, dict)
        assert msg["type"] == "interaction_cancelled"
        assert msg["data"]["reason"] == "timeout"
