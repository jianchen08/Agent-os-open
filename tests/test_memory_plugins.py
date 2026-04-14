"""记忆模块管道插件测试。

测试 4 个插件的 execute 方法，使用 Mock 底层服务。
插件通过 ctx.get_service() 获取依赖，测试中通过 _services 注入 Mock。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory.plugins.memory_read import MemoryReadPlugin
from memory.plugins.memory_write import MemoryWritePlugin
from memory.plugins.knowledge_inject import KnowledgeInjectPlugin
from memory.plugins.context_compress import ContextCompressPlugin
from memory.types import SearchResult, MemoryType
from pipeline.plugin import PluginContext
from pipeline.types import StateKeys


def make_context(services: dict[str, Any] | None = None, **overrides: Any) -> PluginContext:
    """创建测试用 PluginContext。"""
    state = {
        StateKeys.SESSION_ID: "test-session",
        StateKeys.ITERATION: 0,
        StateKeys.RAW_RESULT: "LLM回复内容",
        StateKeys.TOOL_RESULTS: [],
        "user_id": "user-1",
        "user_message": "测试消息",
    }
    state.update(overrides)
    return PluginContext(state=state, config={}, _services=services or {})


class TestMemoryReadPlugin:
    """MemoryReadPlugin 测试。"""

    @pytest.mark.asyncio
    async def test_execute_with_results(self) -> None:
        """测试正常检索。"""
        mock_retriever = AsyncMock()
        mock_retriever.retrieve.return_value = [
            SearchResult(id="1", content="相关内容1", score=0.9, memory_type=MemoryType.SEMANTIC),
            SearchResult(id="2", content="相关内容2", score=0.7, memory_type=MemoryType.SEMANTIC),
        ]

        plugin = MemoryReadPlugin()
        ctx = make_context(services={"retriever": mock_retriever})

        result = await plugin.execute(ctx)

        assert result.state_updates["memory.context"]
        assert len(result.state_updates["memory.context"]) == 2
        mock_retriever.retrieve.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_empty_query(self) -> None:
        """测试空查询。"""
        mock_retriever = AsyncMock()
        plugin = MemoryReadPlugin()
        ctx = make_context(services={"retriever": mock_retriever}, user_message="")

        result = await plugin.execute(ctx)

        assert result.state_updates["memory.context"] == []
        mock_retriever.retrieve.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_retriever_error(self) -> None:
        """测试检索器异常。"""
        mock_retriever = AsyncMock()
        mock_retriever.retrieve.side_effect = RuntimeError("检索失败")

        plugin = MemoryReadPlugin()
        ctx = make_context(services={"retriever": mock_retriever})

        result = await plugin.execute(ctx)

        assert result.error is not None
        assert result.state_updates["memory.context"] == []

    @pytest.mark.asyncio
    async def test_execute_no_retriever_service(self) -> None:
        """测试无 retriever 服务时静默跳过。"""
        plugin = MemoryReadPlugin()
        ctx = make_context(services={})

        result = await plugin.execute(ctx)

        assert result.state_updates["memory.context"] == []

    def test_name_and_priority(self) -> None:
        """测试插件名称和优先级。"""
        plugin = MemoryReadPlugin()
        assert plugin.name == "memory_read"
        assert plugin.priority == 35


class TestMemoryWritePlugin:
    """MemoryWritePlugin 测试。"""

    @pytest.mark.asyncio
    async def test_execute_write_user_and_llm(self) -> None:
        """测试写入用户消息和 LLM 回复。"""
        mock_store = AsyncMock()
        mock_store.save.return_value = "saved-id"

        plugin = MemoryWritePlugin()
        ctx = make_context(services={"memory_store": mock_store})

        result = await plugin.execute(ctx)

        assert result.state_updates["memory.written"]["success"] is True
        assert result.state_updates["memory.written"]["items"] == 2  # user + llm
        assert mock_store.save.call_count == 2

    @pytest.mark.asyncio
    async def test_execute_disabled_user(self) -> None:
        """测试禁用用户消息写入。"""
        mock_store = AsyncMock()
        mock_store.save.return_value = "saved-id"

        plugin = MemoryWritePlugin(config={"write_user_messages": False})
        ctx = make_context(services={"memory_store": mock_store})

        result = await plugin.execute(ctx)

        assert result.state_updates["memory.written"]["success"] is True
        assert result.state_updates["memory.written"]["items"] == 1  # only llm

    @pytest.mark.asyncio
    async def test_execute_write_error(self) -> None:
        """测试写入失败。"""
        mock_store = AsyncMock()
        mock_store.save.side_effect = RuntimeError("写入失败")

        plugin = MemoryWritePlugin()
        ctx = make_context(services={"memory_store": mock_store})

        result = await plugin.execute(ctx)

        assert result.state_updates["memory.written"]["success"] is False
        assert "写入失败" in result.state_updates["memory.written"]["reason"]

    @pytest.mark.asyncio
    async def test_execute_with_tool_results(self) -> None:
        """测试写入工具结果。"""
        mock_store = AsyncMock()
        mock_store.save.return_value = "saved-id"

        ctx = make_context(
            services={"memory_store": mock_store},
            **{
                StateKeys.TOOL_RESULTS: [
                    {"name": "tool1", "result": "结果1"},
                    {"name": "tool2", "result": "结果2"},
                ],
            },
        )

        plugin = MemoryWritePlugin(config={"write_tool_results": True})
        result = await plugin.execute(ctx)

        assert result.state_updates["memory.written"]["success"] is True
        assert result.state_updates["memory.written"]["items"] == 4  # user + llm + 2 tools

    @pytest.mark.asyncio
    async def test_execute_no_memory_store_service(self) -> None:
        """测试无 memory_store 服务时静默跳过。"""
        plugin = MemoryWritePlugin()
        ctx = make_context(services={})

        result = await plugin.execute(ctx)

        # 无服务时返回空 OutputResult（默认值）
        assert result.state_updates == {}

    def test_name_and_priority(self) -> None:
        """测试插件名称和优先级。"""
        plugin = MemoryWritePlugin()
        assert plugin.name == "memory_write"
        assert plugin.priority == 26


class TestKnowledgeInjectPlugin:
    """KnowledgeInjectPlugin 测试。"""

    @pytest.mark.asyncio
    async def test_execute_disabled(self) -> None:
        """测试禁用模式。"""
        plugin = KnowledgeInjectPlugin(config={"mode": "disabled"})
        ctx = make_context()

        result = await plugin.execute(ctx)

        assert result.state_updates["knowledge.context"] == ""

    @pytest.mark.asyncio
    async def test_execute_full_mode_no_storage(self) -> None:
        """测试完整注入模式（无存储服务）。"""
        plugin = KnowledgeInjectPlugin(config={"mode": "full", "top_k": 3})
        ctx = make_context(services={})

        result = await plugin.execute(ctx)

        # 无存储服务，降级为空
        assert result.state_updates["knowledge.context"] == ""

    @pytest.mark.asyncio
    async def test_execute_no_query(self) -> None:
        """测试无查询。"""
        plugin = KnowledgeInjectPlugin(config={"mode": "full"})
        ctx = make_context(user_message="")

        result = await plugin.execute(ctx)

        assert result.state_updates["knowledge.context"] == ""

    @pytest.mark.asyncio
    async def test_execute_no_semantic_storage_service(self) -> None:
        """测试无 semantic_storage 服务时静默跳过。"""
        plugin = KnowledgeInjectPlugin(config={"mode": "full"})
        ctx = make_context(services={})

        result = await plugin.execute(ctx)

        assert result.state_updates["knowledge.context"] == ""

    def test_name_and_priority(self) -> None:
        """测试插件名称和优先级。"""
        plugin = KnowledgeInjectPlugin()
        assert plugin.name == "knowledge_inject"
        assert plugin.priority == 30


class TestContextCompressPlugin:
    """ContextCompressPlugin 测试。"""

    @pytest.mark.asyncio
    async def test_execute_no_session(self) -> None:
        """测试无会话 ID。"""
        plugin = ContextCompressPlugin()
        ctx = make_context(**{StateKeys.SESSION_ID: ""})

        result = await plugin.execute(ctx)

        assert result.state_updates["memory.compressed"]["triggered"] is False
        assert result.state_updates["memory.compressed"]["reason"] == "no session"

    @pytest.mark.asyncio
    async def test_execute_with_session(self) -> None:
        """测试正常执行。"""
        plugin = ContextCompressPlugin()
        ctx = make_context()

        result = await plugin.execute(ctx)

        assert result.state_updates["memory.compressed"]["triggered"] is True
        assert "total_tokens" in result.state_updates["memory.compressed"]

    @pytest.mark.asyncio
    async def test_execute_with_custom_config(self) -> None:
        """测试自定义配置。"""
        plugin = ContextCompressPlugin(
            config={"context_window": 8000, "compress_trigger_ratio": 0.3},
        )
        ctx = make_context()

        result = await plugin.execute(ctx)

        assert result.state_updates["memory.compressed"]["triggered"] is True

    def test_name_and_priority(self) -> None:
        """测试插件名称和优先级。"""
        plugin = ContextCompressPlugin()
        assert plugin.name == "context_compress"
        assert plugin.priority == 24
