"""tool_core 多模态图片处理测试 - 验证双保险路径"""

from __future__ import annotations

from typing import Any

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys
from plugins.core.tool_core import ToolCore


def _make_ctx(**overrides: Any) -> PluginContext:
    from pipeline.types import create_initial_state
    state = create_initial_state(**overrides)
    return PluginContext(state=state)


class TestMultimodalImageHandling:
    """tool_core 双保险图片处理"""

    @pytest.mark.asyncio
    async def test_vision_model_gets_multimodal_message(self):
        """支持视觉的模型 → 注入多模态 user 消息"""
        core = ToolCore()
        core.register_tool("screenshot", lambda args: {
            "success": True,
            "path": "/tmp/test.png",
            "base64_data": "iVBORw0KGgo=",
            "mime_type": "image/png",
        })

        ctx = _make_ctx(
            raw_tool_calls=[{"name": "screenshot", "args": {}}],
            llm_model="glm-5.2",  # 支持视觉
        )
        # 隔离配置加载：mock 视觉能力为 True，专注测插件分支逻辑
        from unittest.mock import patch
        with patch(
            "multimodal.capabilities.ModelCapabilityRegistry.is_multimodal_supported",
            return_value=True,
        ):
            result = await core.execute(ctx)
        messages = result["messages"]

        # 找到 tool_images 消息
        img_msgs = [m for m in messages if m.get("name") == "tool_images"]
        assert len(img_msgs) == 1

        msg = img_msgs[0]
        assert msg["role"] == "user"
        assert isinstance(msg["content"], list)
        # 第一个是文本，后续是 image_url
        assert msg["content"][0]["type"] == "text"
        assert any(c["type"] == "image_url" for c in msg["content"])

    @pytest.mark.asyncio
    async def test_non_vision_model_gets_text_prompt(self):
        """不支持视觉的模型 → 注入文本提示引导 MCP 分析"""
        core = ToolCore()
        core.register_tool("screenshot", lambda args: {
            "success": True,
            "path": "/tmp/test.png",
            "base64_data": "iVBORw0KGgo=",
            "mime_type": "image/png",
        })

        ctx = _make_ctx(
            raw_tool_calls=[{"name": "screenshot", "args": {}}],
            llm_model="deepseek-chat",  # 不支持视觉
        )
        result = await core.execute(ctx)
        messages = result["messages"]

        img_msgs = [m for m in messages if m.get("name") == "tool_images"]
        assert len(img_msgs) == 1

        msg = img_msgs[0]
        assert msg["role"] == "user"
        assert isinstance(msg["content"], str)
        assert "mcp__4_5v_mcp__analyze_image" in msg["content"]
        assert "/tmp/test.png" in msg["content"]

    @pytest.mark.asyncio
    async def test_no_image_data_no_extra_message(self):
        """工具返回无图片数据时 → 不注入额外消息"""
        core = ToolCore()
        core.register_tool("echo", lambda args: {
            "success": True,
            "data": "just text",
        })

        ctx = _make_ctx(
            raw_tool_calls=[{"name": "echo", "args": {}}],
            llm_model="glm-5.2",
        )
        result = await core.execute(ctx)
        messages = result["messages"]

        img_msgs = [m for m in messages if m.get("name") == "tool_images"]
        assert len(img_msgs) == 0

    @pytest.mark.asyncio
    async def test_multiple_images_in_one_result(self):
        """工具返回多张图片（images 列表）→ 全部包含在消息中"""
        core = ToolCore()
        core.register_tool("multi_shot", lambda args: {
            "success": True,
            "images": [
                {"base64": "img1base64", "mime_type": "image/png", "path": "/tmp/1.png"},
                {"base64": "img2base64", "mime_type": "image/png", "path": "/tmp/2.png"},
            ],
        })

        ctx = _make_ctx(
            raw_tool_calls=[{"name": "multi_shot", "args": {}}],
            llm_model="glm-5.2",
        )
        from unittest.mock import patch
        with patch(
            "multimodal.capabilities.ModelCapabilityRegistry.is_multimodal_supported",
            return_value=True,
        ):
            result = await core.execute(ctx)
        messages = result["messages"]

        img_msgs = [m for m in messages if m.get("name") == "tool_images"]
        assert len(img_msgs) == 1
        msg = img_msgs[0]
        # 1 text + 2 image_url = 3 content blocks
        image_blocks = [c for c in msg["content"] if c["type"] == "image_url"]
        assert len(image_blocks) == 2

    @pytest.mark.asyncio
    async def test_unknown_model_defaults_to_text_prompt(self):
        """未知模型（不在注册表中）→ 默认走文本提示路径"""
        core = ToolCore()
        core.register_tool("screenshot", lambda args: {
            "success": True,
            "path": "/tmp/test.png",
            "base64_data": "abc123",
            "mime_type": "image/png",
        })

        ctx = _make_ctx(
            raw_tool_calls=[{"name": "screenshot", "args": {}}],
            llm_model="unknown-model-xyz",
        )
        result = await core.execute(ctx)
        messages = result["messages"]

        img_msgs = [m for m in messages if m.get("name") == "tool_images"]
        assert len(img_msgs) == 1
        # 未知模型默认不支持视觉
        assert isinstance(img_msgs[0]["content"], str)

    @pytest.mark.asyncio
    async def test_tool_result_still_has_original_data(self):
        """图片处理不影响原始工具结果"""
        core = ToolCore()
        core.register_tool("screenshot", lambda args: {
            "success": True,
            "path": "/tmp/test.png",
            "base64_data": "abc123",
            "mime_type": "image/png",
            "message": "截图成功",
        })

        ctx = _make_ctx(
            raw_tool_calls=[{"name": "screenshot", "args": {}}],
            llm_model="glm-5.2",
        )
        result = await core.execute(ctx)

        tool_results = result[StateKeys.TOOL_RESULTS]
        assert len(tool_results) == 1
        assert tool_results[0]["success"] is True
        assert tool_results[0]["data"]["message"] == "截图成功"
