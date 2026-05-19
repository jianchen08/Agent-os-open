"""记忆相关管道插件测试。

测试记忆模块相关的管道插件：
- KnowledgeInjectPlugin：知识注入 Input 插件
- MemoryReadPlugin：记忆读取 Input 插件

注意：MemoryWritePlugin 和 ContextCompressPlugin 在 memory/plugins/ 目录下
尚未实现（参见 src/memory/MEMORY.md），当前仅测试已实现的插件。
后续插件实现后应补充对应测试。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import ErrorPolicy


# ============================================================
# 辅助：创建 PluginContext
# ============================================================


def _make_context(
    state: dict[str, Any] | None = None,
    services: dict[str, Any] | None = None,
) -> PluginContext:
    """创建测试用 PluginContext。"""
    return PluginContext(
        state=state or {},
        config={},
        _services=services or {},
    )


# ============================================================
# 1. KnowledgeInjectPlugin 测试
# ============================================================


class TestKnowledgeInjectPlugin:
    """测试知识注入插件。"""

    def test_插件属性(self) -> None:
        """测试插件基本属性。"""
        from plugins.input.knowledge_inject import KnowledgeInjectPlugin

        plugin = KnowledgeInjectPlugin()
        assert plugin.name == "knowledge_inject"
        assert plugin.priority == 30
        assert plugin.error_policy == ErrorPolicy.FALLBACK

    def test_构造函数默认配置(self) -> None:
        """默认配置 mode 应为 disabled。"""
        from plugins.input.knowledge_inject import KnowledgeInjectPlugin

        plugin = KnowledgeInjectPlugin()
        assert plugin._mode == "disabled"
        assert plugin._top_k == 5
        assert plugin._max_tokens == 2000

    def test_构造函数自定义配置(self) -> None:
        """自定义配置应生效。"""
        from plugins.input.knowledge_inject import KnowledgeInjectPlugin

        plugin = KnowledgeInjectPlugin(config={
            "mode": "full",
            "top_k": 10,
            "max_tokens": 5000,
        })
        assert plugin._mode == "full"
        assert plugin._top_k == 10
        assert plugin._max_tokens == 5000

    @pytest.mark.asyncio
    async def test_disabled模式返回空(self) -> None:
        """disabled 模式应返回空知识。"""
        from plugins.input.knowledge_inject import KnowledgeInjectPlugin

        plugin = KnowledgeInjectPlugin(config={"mode": "disabled"})
        ctx = _make_context()
        result = await plugin.execute(ctx)
        assert result.state_updates.get("knowledge.context") == ""

    @pytest.mark.asyncio
    async def test_无用户消息返回空(self) -> None:
        """无 user_message 时应返回空知识。"""
        from plugins.input.knowledge_inject import KnowledgeInjectPlugin

        plugin = KnowledgeInjectPlugin(config={"mode": "full"})
        ctx = _make_context(state={})
        result = await plugin.execute(ctx)
        assert result.state_updates.get("knowledge.context") == ""

    @pytest.mark.asyncio
    async def test_无memory_service服务返回空(self) -> None:
        """无 memory_service 服务时应返回空知识。"""
        from plugins.input.knowledge_inject import KnowledgeInjectPlugin

        plugin = KnowledgeInjectPlugin(config={"mode": "full"})
        ctx = _make_context(
            state={"current_query": "查询", "user_id": "u1"},
            services={},  # 无 memory_service
        )
        result = await plugin.execute(ctx)
        assert result.state_updates.get("knowledge.context") == ""

    @pytest.mark.asyncio
    async def test_full模式注入知识(self) -> None:
        """full 模式应注入完整知识内容。"""
        from plugins.input.knowledge_inject import KnowledgeInjectPlugin

        mock_ms = AsyncMock()
        mock_ms.retrieve = AsyncMock(return_value=[])

        plugin = KnowledgeInjectPlugin(config={"mode": "full", "top_k": 5})
        ctx = _make_context(
            state={"current_query": "Python", "user_id": "u1"},
            services={"memory_service": mock_ms},
        )
        result = await plugin.execute(ctx)
        # 知识库为空时应返回空
        assert result.state_updates.get("knowledge.context") == ""

    @pytest.mark.asyncio
    async def test_hint模式格式化(self) -> None:
        """hint 模式应通过 MemoryService 检索并拼接结果。"""
        from plugins.input.knowledge_inject import KnowledgeInjectPlugin
        from memory.types import SearchResult, MemoryType

        mock_ms = AsyncMock()
        mock_ms.retrieve = AsyncMock(return_value=[
            SearchResult(id="1", content="Python 编程指南", score=1.0, memory_type=MemoryType.SEMANTIC),
            SearchResult(id="2", content="Flask Web 开发", score=0.9, memory_type=MemoryType.SEMANTIC),
        ])
        plugin = KnowledgeInjectPlugin(config={"mode": "hint"})
        ctx = _make_context(
            state={"current_query": "Python", "user_id": "u1"},
            services={"memory_service": mock_ms},
        )
        result = await plugin.execute(ctx)
        content = result.state_updates.get("knowledge.context", "")
        assert "Python 编程指南" in content
        assert "Flask Web 开发" in content

    @pytest.mark.asyncio
    async def test_compressed模式截断(self) -> None:
        """compressed 模式应通过 MemoryService 检索并拼接结果。"""
        from plugins.input.knowledge_inject import KnowledgeInjectPlugin
        from memory.types import SearchResult, MemoryType

        mock_ms = AsyncMock()
        mock_ms.retrieve = AsyncMock(return_value=[
            SearchResult(id="1", content="a" * 500, score=1.0, memory_type=MemoryType.SEMANTIC),
        ])
        plugin = KnowledgeInjectPlugin(config={"mode": "compressed"})
        ctx = _make_context(
            state={"current_query": "查询", "user_id": "u1"},
            services={"memory_service": mock_ms},
        )
        result = await plugin.execute(ctx)
        content = result.state_updates.get("knowledge.context", "")
        assert "a" in content

    @pytest.mark.asyncio
    async def test_异常时降级(self) -> None:
        """插件执行异常时应降级返回空。"""
        from plugins.input.knowledge_inject import KnowledgeInjectPlugin

        mock_ms = AsyncMock()
        mock_ms.retrieve = AsyncMock(side_effect=Exception("检索错误"))
        plugin = KnowledgeInjectPlugin(config={"mode": "full"})
        ctx = _make_context(
            state={"current_query": "查询", "user_id": "u1"},
            services={"memory_service": mock_ms},
        )
        result = await plugin.execute(ctx)
        assert result.state_updates.get("knowledge.context") == ""

    def test_format_full_限制token(self) -> None:
        """_format_full 应受 max_tokens 限制。"""
        from plugins.input.knowledge_inject import KnowledgeInjectPlugin

        plugin = KnowledgeInjectPlugin(config={"max_tokens": 50})
        items = [{"content": "a" * 100}, {"content": "b" * 100}]
        result = plugin._format_full(items)
        # 应截断以控制 token
        assert len(result) < 300  # 不超过总内容长度

    def test_format_hint_空列表(self) -> None:
        """_format_hint 空列表。"""
        from plugins.input.knowledge_inject import KnowledgeInjectPlugin

        plugin = KnowledgeInjectPlugin()
        result = plugin._format_hint([])
        assert "0" in result


# ============================================================
# 2. MemoryReadPlugin 测试
# ============================================================


class TestMemoryReadPlugin:
    """测试记忆读取插件。"""

    def test_插件属性(self) -> None:
        """测试插件基本属性。"""
        from plugins.input.memory_read import MemoryReadPlugin

        plugin = MemoryReadPlugin()
        assert plugin.name == "memory_read"
        assert plugin.priority == 35
        assert plugin.error_policy == ErrorPolicy.SKIP

    def test_构造函数默认配置(self) -> None:
        """默认配置 _config 为空字典，通过 get 取默认值。"""
        from plugins.input.memory_read import MemoryReadPlugin

        plugin = MemoryReadPlugin()
        # MemoryReadPlugin 的 _config 默认为空字典
        # top_k 和 memory_type 通过 self._config.get("top_k", 5) 取默认值
        assert plugin._config.get("top_k", 5) == 5
        assert plugin._config.get("memory_type", "semantic") == "semantic"

    def test_构造函数自定义配置(self) -> None:
        """自定义配置应生效。"""
        from plugins.input.memory_read import MemoryReadPlugin

        plugin = MemoryReadPlugin(config={"top_k": 10, "memory_type": "episode"})
        assert plugin._config["top_k"] == 10
        assert plugin._config["memory_type"] == "episode"

    @pytest.mark.asyncio
    async def test_无retriever服务返回空(self) -> None:
        """无 retriever 服务时应返回空结果。"""
        from plugins.input.memory_read import MemoryReadPlugin

        plugin = MemoryReadPlugin()
        ctx = _make_context(
            state={"user_message": "查询"},
            services={},
        )
        result = await plugin.execute(ctx)
        assert result.state_updates.get("memory.retrieved") == []

    @pytest.mark.asyncio
    async def test_无用户消息返回空(self) -> None:
        """无 user_message 时应返回空结果。"""
        from plugins.input.memory_read import MemoryReadPlugin

        plugin = MemoryReadPlugin()
        ctx = _make_context(state={}, services={})
        result = await plugin.execute(ctx)
        assert result.state_updates.get("memory.retrieved") == []

    @pytest.mark.asyncio
    async def test_正常检索(self) -> None:
        """正常检索应返回格式化结果。"""
        from plugins.input.memory_read import MemoryReadPlugin
        from memory.types import SearchResult, MemoryType

        mock_retriever = AsyncMock()
        mock_retriever.retrieve = AsyncMock(return_value=[
            SearchResult(id="1", content="结果1", score=0.9, memory_type=MemoryType.SEMANTIC),
        ])
        plugin = MemoryReadPlugin()
        ctx = _make_context(
            state={"user_message": "查询", "user_id": "u1"},
            services={"retriever": mock_retriever},
        )
        result = await plugin.execute(ctx)
        retrieved = result.state_updates.get("memory.retrieved", [])
        assert len(retrieved) == 1
        assert retrieved[0]["id"] == "1"

    @pytest.mark.asyncio
    async def test_检索失败降级(self) -> None:
        """检索失败时应降级返回空结果。"""
        from plugins.input.memory_read import MemoryReadPlugin

        mock_retriever = AsyncMock()
        mock_retriever.retrieve = AsyncMock(side_effect=Exception("检索错误"))
        plugin = MemoryReadPlugin()
        ctx = _make_context(
            state={"user_message": "查询", "user_id": "u1"},
            services={"retriever": mock_retriever},
        )
        result = await plugin.execute(ctx)
        assert result.state_updates.get("memory.retrieved") == []
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_Agent禁用插件(self) -> None:
        """Agent 通过 plugin_configs 禁用插件时应返回空结果。"""
        from plugins.input.memory_read import MemoryReadPlugin

        plugin = MemoryReadPlugin()
        ctx = _make_context(
            state={
                "user_message": "查询",
                "user_id": "u1",
                "plugin_configs": {"memory_read": {"enabled": False}},
            },
            services={"retriever": AsyncMock()},
        )
        result = await plugin.execute(ctx)
        assert result.state_updates.get("memory.retrieved") == []

    @pytest.mark.asyncio
    async def test_Agent覆盖top_k(self) -> None:
        """Agent 通过 plugin_configs 覆盖 top_k。"""
        from plugins.input.memory_read import MemoryReadPlugin

        mock_retriever = AsyncMock()
        mock_retriever.retrieve = AsyncMock(return_value=[])
        plugin = MemoryReadPlugin()
        ctx = _make_context(
            state={
                "user_message": "查询",
                "user_id": "u1",
                "plugin_configs": {"memory_read": {"top_k": 20}},
            },
            services={"retriever": mock_retriever},
        )
        await plugin.execute(ctx)
        assert plugin._top_k == 20
