"""消息框架重构第二轮修复 — 验证测试。

验证范围:
1. pipeline.message_bus: send_pipeline_message 转发 _inject_request 后行为不变
2. app_factory: import 提升后无循环导入
3. pipeline.message_types: TYPE_CHECKING 类型标注不影响运行时
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1. send_pipeline_message → _inject_request 转发行为验证
# ---------------------------------------------------------------------------

class TestSendPipelineMessageForwarding:
    """验证 send_pipeline_message 转发 _inject_request 后行为不变。"""

    @pytest.mark.asyncio
    async def test_empty_pipeline_id_returns_failure(self):
        """空 pipeline_id 应返回失败 InjectResult。"""
        from pipeline.message_bus import send_pipeline_message

        result = await send_pipeline_message("", "hello")
        assert not result.success
        assert "不能为空" in result.error
        assert result.method == "failed"

    @pytest.mark.asyncio
    async def test_whitespace_only_message_rejected(self):
        """仅空白字符的消息应被拒绝。"""
        from pipeline.message_bus import send_pipeline_message

        for ws in ["   ", "\n", "\t", "  \n  \t  "]:
            result = await send_pipeline_message("test-pipeline", ws)
            assert not result.success, f"应拒绝空白消息: '{ws}'"
            assert "不能仅包含空白" in result.error

    @pytest.mark.asyncio
    async def test_send_pipeline_message_calls_inject_request(self):
        """send_pipeline_message 内部调用 _inject_request，且传递正确参数。"""
        from pipeline.message_bus import send_pipeline_message, InjectResult

        mock_result = InjectResult(success=True, method="notification", pipeline_id="p1")
        with patch(
            "pipeline.message_bus._inject_request",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_inject:
            result = await send_pipeline_message(
                "p1", "hello world",
                metadata={"source": "user"},
                thread_id="t1",
            )

            assert result.success
            assert result.method == "notification"
            mock_inject.assert_awaited_once()
            # 验证传入的是 PipelineRequest
            request_arg = mock_inject.call_args.args[0]
            assert request_arg.message.content == "hello world"
            assert request_arg.message.pipeline_id == "p1"
            assert request_arg.message.thread_id == "t1"

    @pytest.mark.asyncio
    async def test_send_pipeline_message_passes_optional_fields(self):
        """send_pipeline_message 正确传递可选字段（workspace、task_id、streaming）。"""
        from pipeline.message_bus import send_pipeline_message, InjectResult

        mock_result = InjectResult(success=True, method="start", pipeline_id="p2")
        with patch(
            "pipeline.message_bus._inject_request",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_inject:
            await send_pipeline_message(
                "p2", "test",
                workspace="/tmp/ws",
                task_id="task-123",
                streaming=False,
            )

            request_arg = mock_inject.call_args.args[0]
            assert request_arg.workspace == "/tmp/ws"
            assert request_arg.task_id == "task-123"
            assert request_arg.streaming is False

    @pytest.mark.asyncio
    async def test_handle_incoming_message_also_uses_inject_request(self):
        """handle_incoming_message 也走 _inject_request，与 send_pipeline_message 共享逻辑。"""
        from pipeline.message_bus import handle_incoming_message, InjectResult
        from pipeline.message_types import PipelineMessage, MessageType

        mock_result = InjectResult(success=True, method="notification", pipeline_id="p3")
        with patch(
            "pipeline.message_bus._inject_request",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_inject:
            msg = PipelineMessage(
                type=MessageType.CHAT,
                content="hello",
                pipeline_id="p3",
                thread_id="t3",
            )
            result = await handle_incoming_message(msg)

            assert result.success
            mock_inject.assert_awaited_once()
            request_arg = mock_inject.call_args.args[0]
            assert request_arg.message is msg
            assert request_arg.streaming is True

    @pytest.mark.asyncio
    async def test_send_pipeline_message_returns_inject_result_type(self):
        """send_pipeline_message 返回值类型为 InjectResult。"""
        from pipeline.message_bus import send_pipeline_message, InjectResult

        result = await send_pipeline_message("", "x")
        assert isinstance(result, InjectResult)


# ---------------------------------------------------------------------------
# 2. app_factory import 完整性验证（无循环导入）
# ---------------------------------------------------------------------------

class TestAppFactoryImports:
    """验证 app_factory.py import 提升后无循环导入。"""

    def test_app_factory_imports_cleanly(self):
        """app_factory 模块可以无错误导入。"""
        import importlib
        mod = importlib.import_module("app_factory")
        assert mod is not None

    def test_app_factory_key_symbols_exist(self):
        """app_factory 导出的关键符号存在。"""
        import app_factory

        # 验证被 import 提升的模块
        assert hasattr(app_factory, "PipelineContext")
        assert hasattr(app_factory, "parse_frontend_message")
        assert hasattr(app_factory, "handle_incoming_message")
        assert hasattr(app_factory, "MessageParseError")


# ---------------------------------------------------------------------------
# 3. message_types TYPE_CHECKING 运行时安全验证
# ---------------------------------------------------------------------------

class TestMessageTypeRuntimeSafety:
    """验证 message_types.py TYPE_CHECKING 不影响运行时。"""

    def test_pipeline_message_types_imports_cleanly(self):
        """pipeline.message_types 模块可无错误导入。"""
        from pipeline import message_types
        assert message_types is not None

    def test_pipeline_message_types_runtime_symbols(self):
        """运行时可用的符号（非 TYPE_CHECKING）正常。"""
        from pipeline.message_types import (
            MessageSource,
            MessageType,
            PipelineMessage,
            PipelineRequest,
        )

        # 枚举值正常
        assert MessageType.CHAT == "chat"
        assert MessageType.CONTROL == "control"
        assert MessageType.INTERACTION_RESPONSE == "interaction_response"
        assert MessageSource.USER == "user"

        # dataclass 可正常实例化
        msg = PipelineMessage(type=MessageType.CHAT, content="test")
        assert msg.content == "test"
        assert msg.source == MessageSource.USER

        # PipelineRequest 可正常实例化（agent_config 默认 None）
        req = PipelineRequest(message=msg)
        assert req.agent_config is None
        assert req.streaming is True

    def test_api_websocket_message_types_imports_cleanly(self):
        """src.api.websocket.message_types 模块可无错误导入。"""
        from src.api.websocket import message_types
        assert message_types is not None

    def test_api_websocket_message_types_factory_functions(self):
        """消息工厂函数返回正确结构。"""
        from src.api.websocket.message_types import (
            create_interaction_request_message,
            create_interaction_cancelled_message,
        )

        # 交互请求消息
        msg = create_interaction_request_message(
            thread_id="t1",
            request_id="r1",
            interaction_type="approval",
            mode="inline",
            title="请确认",
        )
        assert msg["type"] == "interaction_request"
        assert msg["data"]["thread_id"] == "t1"
        assert msg["data"]["request_id"] == "r1"
        assert msg["data"]["title"] == "请确认"

        # 交互取消消息
        cancel_msg = create_interaction_cancelled_message(
            thread_id="t1",
            request_id="r1",
            reason="timeout",
        )
        assert cancel_msg["type"] == "interaction_cancelled"
        assert cancel_msg["data"]["reason"] == "timeout"

    def test_api_websocket_message_bus_runtime_safe(self):
        """src.api.websocket.message_bus 模块运行时安全（无 TYPE_CHECKING 依赖）。"""
        from src.api.websocket.message_bus import SourceType, MessageBus, get_message_bus

        # SourceType 枚举正常
        assert SourceType.SYSTEM == "system"
        assert SourceType.AGENT == "agent"

        # 单例正常返回
        bus = get_message_bus()
        assert isinstance(bus, MessageBus)


# ---------------------------------------------------------------------------
# 4. 消息流转端到端验证（send_pipeline_message 与 _inject_request 行为等价性）
# ---------------------------------------------------------------------------

class TestMessageFlowEquivalence:
    """验证 send_pipeline_message 和 handle_incoming_message 行为等价。"""

    @pytest.mark.asyncio
    async def test_both_paths_reject_empty_pipeline_id(self):
        """两条入口都拒绝空 pipeline_id。"""
        from pipeline.message_bus import send_pipeline_message, handle_incoming_message
        from pipeline.message_types import PipelineMessage, MessageType

        # send_pipeline_message 路径
        r1 = await send_pipeline_message("", "hello")
        assert not r1.success
        assert "不能为空" in r1.error

        # handle_incoming_message 路径
        msg = PipelineMessage(type=MessageType.CHAT, content="hello", pipeline_id="")
        r2 = await handle_incoming_message(msg)
        assert not r2.success
        assert "不能为空" in r2.error

    @pytest.mark.asyncio
    async def test_both_paths_reject_whitespace_message(self):
        """两条入口都拒绝仅空白的消息。"""
        from pipeline.message_bus import send_pipeline_message, handle_incoming_message
        from pipeline.message_types import PipelineMessage, MessageType

        # send_pipeline_message 路径
        r1 = await send_pipeline_message("p1", "   ")
        assert not r1.success
        assert "不能仅包含空白" in r1.error

        # handle_incoming_message 路径
        msg = PipelineMessage(type=MessageType.CHAT, content="   ", pipeline_id="p1")
        r2 = await handle_incoming_message(msg)
        assert not r2.success
        assert "不能仅包含空白" in r2.error

    @pytest.mark.asyncio
    async def test_empty_content_allowed_for_wake(self):
        """空字符串（len=0）允许通过验证（用于唤醒场景）。"""
        from pipeline.message_bus import send_pipeline_message

        # 空字符串（len=0）不触发空白检查
        result = await send_pipeline_message("nonexistent-pipeline", "")
        # 会走到 revive 路径，但不是因为消息为空被拒绝
        assert "不能仅包含空白" not in result.error
