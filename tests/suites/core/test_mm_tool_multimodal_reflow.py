"""MM-3+MM-5+MM-4b：工具多模态结果回流 LLM + 管道桥接 + WS 事件。

验证三个断点的打通：
- MM-3：image_generate / playwright_test 工具输出包含 multimodal_content
- MM-5：tool_core 检测 multimodal_content 后注入 messages 多模态块
- MM-4b：tool_core 通过 on_chunk 发射 tool_multimedia_result 事件
"""
from __future__ import annotations

import base64
import os
import tempfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys, create_initial_state
from plugins.core.tool_core import ToolCore


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _make_ctx(**overrides: Any) -> PluginContext:
    """构造 PluginContext，从 create_initial_state 开始叠加覆盖。"""
    state = create_initial_state(**overrides)
    return PluginContext(state=state)


def _make_tool_result_with_mm(
    tool_name: str = "image_generate",
    mm_blocks: list[dict] | None = None,
) -> Any:
    """构造一个带 multimodal_content metadata 的 ToolExecutionResult。"""
    from tools.types import create_success_result

    if mm_blocks is None:
        mm_blocks = [{
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
        }]

    return create_success_result(
        data={"file_path": "/tmp/test.png"},
        metadata={
            "action": tool_name,
            "multimodal_content": mm_blocks,
        },
    )


# ---------------------------------------------------------------------------
# MM-3：工具输出 multimodal_content
# ---------------------------------------------------------------------------

class TestImageGenerateMultimodalOutput:
    """验证 image_generate 工具输出包含 multimodal_content 字段。"""

    @pytest.mark.asyncio
    async def test_image_generate_includes_multimodal_content(self):
        """image_generate 成功时 metadata 包含 multimodal_content。"""
        from tools.builtin.image_generate.tool import ImageGenerateTool

        # 生成一个临时 PNG 文件
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
            tmp_path = f.name

        try:
            tool = ImageGenerateTool()

            # Mock MediaProviderRegistry 链
            mock_chain = AsyncMock()
            mock_chain.execute_generate = AsyncMock(return_value=MagicMock(
                file_path=tmp_path,
                media_type=MagicMock(value="image"),
                provider_name="test_provider",
                metadata={},
            ))

            mock_registry = MagicMock()
            mock_registry.get_chain_for_type.return_value = mock_chain
            tool._registry = mock_registry

            result = await tool.execute({"prompt": "a cat"})

            assert result.success
            assert "multimodal_content" in result.metadata
            mm = result.metadata["multimodal_content"]
            assert isinstance(mm, list)
            assert len(mm) == 1
            assert mm[0]["type"] == "image_url"
            assert "base64" in mm[0]["image_url"]["url"]
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


class TestPlaywrightScreenshotMultimodalOutput:
    """验证 playwright_test 截图输出包含 multimodal_content 字段。"""

    @pytest.mark.asyncio
    async def test_screenshot_includes_multimodal_content(self):
        """截图成功时 metadata 包含 multimodal_content。"""
        from tools.builtin.playwright_test.tool import PlaywrightTestTool

        tool = PlaywrightTestTool()

        # Mock ScreenshotManager.capture_full_page 返回带 base64 的结果
        mock_result = {
            "success": True,
            "path": "/tmp/screenshot.png",
            "base64_data": "iVBORw0KGgo=",
            "mime_type": "image/png",
            "message": "全页面截图已保存",
        }

        with patch(
            "tools.builtin.playwright_test.tool.ScreenshotManager.capture_full_page",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            # Mock session/page validation
            mock_page = MagicMock()
            with patch.object(
                tool, "_validate_session_page",
                return_value=("session-1", mock_page),
            ):
                result = await tool.execute({
                    "action": "screenshot_compare",
                    "session_id": "session-1",
                    "screenshot_action": "full_page",
                })

        assert result.success
        assert "multimodal_content" in result.metadata
        mm = result.metadata["multimodal_content"]
        assert isinstance(mm, list)
        assert len(mm) == 1
        assert mm[0]["type"] == "image_url"
        assert "data:image/png;base64,iVBORw0KGgo=" in mm[0]["image_url"]["url"]


# ---------------------------------------------------------------------------
# MM-5：tool_core 检测 multimodal_content 并注入 messages
# ---------------------------------------------------------------------------

class TestToolCoreMultimodalInjection:
    """验证 tool_core 检测 multimodal_content 后注入多模态 messages。"""

    @pytest.mark.asyncio
    async def test_mm_content_injected_for_vision_model(self):
        """支持视觉的模型 → multimodal_content 注入为多模态 user 消息。"""
        core = ToolCore()
        core.register_tool(
            "image_generate",
            lambda args: _make_tool_result_with_mm(),
        )

        ctx = _make_ctx(
            raw_tool_calls=[{"name": "image_generate", "args": {}}],
            llm_model="glm-5.2",  # 支持视觉
        )

        with patch(
            "multimodal.capabilities.ModelCapabilityRegistry.is_multimodal_supported",
            return_value=True,
        ):
            result = await core.execute(ctx)

        messages = result["messages"]
        img_msgs = [m for m in messages if m.get("name") == "tool_images"]
        assert len(img_msgs) == 1
        assert isinstance(img_msgs[0]["content"], list)
        assert any(c["type"] == "image_url" for c in img_msgs[0]["content"])

    @pytest.mark.asyncio
    async def test_mm_content_text_fallback_for_non_vision(self):
        """不支持视觉的模型 → 注入文本提示。"""
        core = ToolCore()
        core.register_tool(
            "image_generate",
            lambda args: _make_tool_result_with_mm(),
        )

        ctx = _make_ctx(
            raw_tool_calls=[{"name": "image_generate", "args": {}}],
            llm_model="deepseek-chat",
        )

        with patch(
            "multimodal.capabilities.ModelCapabilityRegistry.is_multimodal_supported",
            return_value=False,
        ):
            result = await core.execute(ctx)

        messages = result["messages"]
        img_msgs = [m for m in messages if m.get("name") == "tool_images"]
        assert len(img_msgs) == 1
        assert isinstance(img_msgs[0]["content"], str)

    @pytest.mark.asyncio
    async def test_no_mm_content_no_injection(self):
        """工具返回无 multimodal_content 时 → 不注入额外消息。"""
        from tools.types import create_success_result

        core = ToolCore()
        core.register_tool(
            "bash_execute",
            lambda args: create_success_result(data={"stdout": "done"}),
        )

        ctx = _make_ctx(
            raw_tool_calls=[{"name": "bash_execute", "args": {"command": "ls"}}],
            llm_model="glm-5.2",
        )

        result = await core.execute(ctx)
        messages = result["messages"]
        img_msgs = [m for m in messages if m.get("name") == "tool_images"]
        assert len(img_msgs) == 0


# ---------------------------------------------------------------------------
# MM-4b：tool_multimedia_result WS 事件
# ---------------------------------------------------------------------------

class TestToolMultimediaResultEvent:
    """验证 tool_multimedia_result WS 事件被正确发射。"""

    @pytest.mark.asyncio
    async def test_event_fired_when_mm_content_present(self):
        """工具有多模态结果 → on_chunk 收到 tool_multimedia_result 事件。"""
        chunks: list[dict] = []

        core = ToolCore()
        core.register_tool(
            "image_generate",
            lambda args: _make_tool_result_with_mm(),
        )

        ctx = _make_ctx(
            raw_tool_calls=[{"name": "image_generate", "args": {}}],
            llm_model="glm-5.2",
            on_chunk=lambda chunk: chunks.append(chunk),
        )

        with patch(
            "multimodal.capabilities.ModelCapabilityRegistry.is_multimodal_supported",
            return_value=True,
        ):
            await core.execute(ctx)

        mm_events = [c for c in chunks if c["type"] == "tool_multimedia_result"]
        assert len(mm_events) == 1
        assert mm_events[0]["count"] >= 1
        assert "multimedia" in mm_events[0]

    @pytest.mark.asyncio
    async def test_no_event_without_mm_content(self):
        """工具无多模态结果 → 不发射 tool_multimedia_result 事件。"""
        from tools.types import create_success_result

        chunks: list[dict] = []

        core = ToolCore()
        core.register_tool(
            "bash_execute",
            lambda args: create_success_result(data={"stdout": "done"}),
        )

        ctx = _make_ctx(
            raw_tool_calls=[{"name": "bash_execute", "args": {"command": "ls"}}],
            llm_model="glm-5.2",
            on_chunk=lambda chunk: chunks.append(chunk),
        )

        await core.execute(ctx)

        mm_events = [c for c in chunks if c["type"] == "tool_multimedia_result"]
        assert len(mm_events) == 0


# ---------------------------------------------------------------------------
# Bridge 事件格式化：tool_multimedia_result
# ---------------------------------------------------------------------------

class TestBridgeMultimediaEvent:
    """验证 PipelineStreamBridge 能格式化 tool_multimedia_result 事件。"""

    @pytest.mark.asyncio
    async def test_bridge_formats_multimedia_event(self):
        """bridge._handle_chunk 对 tool_multimedia_result 格式化并发送。"""
        from pipeline.bridge_core import BridgeCore
        from pipeline.bridge_events import BridgeEventsMixin

        sink_events: list[dict] = []

        class MockSink:
            async def send_event(self, event: dict) -> bool:
                sink_events.append(event)
                return True

        class TestBridge(BridgeEventsMixin, BridgeCore):
            pass

        bridge = TestBridge.__new__(TestBridge)
        bridge.output_sink = MockSink()
        bridge.pipeline_id = "test-pipeline-001"
        bridge.message_id = "msg-001"
        bridge._thinking_active = False
        bridge._stream_started = True
        bridge._sent_tool_starts = set()
        bridge._llm_seen_call_ids = set()
        bridge._part_seq = 0
        bridge._container_task_id = ""

        await bridge._handle_chunk({
            "type": "tool_multimedia_result",
            "count": 2,
            "multimedia": [
                {"mime_type": "image/png", "path": "/tmp/a.png"},
                {"mime_type": "image/png", "path": "/tmp/b.png"},
            ],
        })

        assert len(sink_events) == 1
        event = sink_events[0]
        assert event["type"] == "tool_multimedia_result"
        assert event["data"]["count"] == 2
        assert len(event["data"]["multimedia"]) == 2


# ---------------------------------------------------------------------------
# slim 序列化排除 multimodal_content
# ---------------------------------------------------------------------------

class TestSlimSerializationExclusion:
    """验证 slim 模式排除 multimodal_content（防止 base64 污染 LLM 文本）。"""

    def test_slim_excludes_multimodal_content(self):
        """slim 序列化不含 multimodal_content 字段。"""
        from core.results.tool import ToolExecutionResult

        result = ToolExecutionResult.create_completed(
            output={"file_path": "/tmp/test.png"},
            metadata={
                "action": "image_generate",
                "multimodal_content": [{"type": "image_url", "image_url": {"url": "data:..."}}],
            },
        )

        slim = result.to_dict(slim=True)
        assert "multimodal_content" not in slim.get("metadata", {})

    def test_non_slim_includes_multimodal_content(self):
        """非 slim 序列化包含 multimodal_content 字段。"""
        from core.results.tool import ToolExecutionResult

        result = ToolExecutionResult.create_completed(
            output={"file_path": "/tmp/test.png"},
            metadata={
                "action": "image_generate",
                "multimodal_content": [{"type": "image_url"}],
            },
        )

        full = result.to_dict(slim=False)
        assert "multimodal_content" in full.get("metadata", {})
