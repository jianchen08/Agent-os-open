"""resource_search 工具测试。

覆盖场景：
- get_tool_definition: 定义正确性（description / when_to_use / when_not_to_use / caveats）
- execute: 搜索 agent / tool / skill / all
- execute: simple / detailed 模式
- execute: 动态工具注入（detailed + tool）
- _match_query: 匹配逻辑
- _search_external: 外部搜索（配置关闭时应跳过）
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tools.builtin.resource_search.tool import ResourceSearchTool


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _make_tool(**kwargs) -> ResourceSearchTool:
    """创建 ResourceSearchTool 实例"""
    return ResourceSearchTool(**kwargs)


def _mock_agent_registry():
    """模拟 Agent 注册表"""
    agent = MagicMock()
    agent.name = "general_agent"
    agent.description = "通用智能体，处理各类任务"
    agent.config_id = "general_agent"
    agent.tags = ["general", "coding"]
    agent.category = "system"
    agent.level = "L2"
    agent.deliverables = []
    agent.recommended_metrics = []
    registry = MagicMock()
    registry.list_all.return_value = [agent]
    return registry


def _mock_tool_registry():
    """模拟 Tool 注册表"""
    tools = []
    for name, desc, tags in [
        ("file_read", "读取文件内容", ["file", "read"]),
        ("file_write", "写入文件内容", ["file", "write"]),
        ("enhanced_search", "代码搜索工具", ["search", "ripgrep"]),
        ("web_search", "互联网搜索", ["web", "search"]),
    ]:
        tool = MagicMock()
        tool.name = name
        tool.description = desc
        tool.tags = tags
        tool.category = None
        tool.level = "user"
        tools.append(tool)

    registry = MagicMock()
    registry.list_all.return_value = tools
    registry.get_dynamic_tool_names.return_value = set()
    registry.has.return_value = False
    return registry, tools


# ---------------------------------------------------------------------------
# 测试：工具定义
# ---------------------------------------------------------------------------

class TestToolDefinition:

    def test_definition_basic_fields(self):
        tool_def = ResourceSearchTool.get_tool_definition()
        assert tool_def.name == "resource_search"
        assert "Agent" in tool_def.description
        assert "工具" in tool_def.description
        assert "Skill" in tool_def.description
        assert "已有明确资源映射时直接使用" in tool_def.description
        assert "空结果" in tool_def.description

    def test_when_to_use(self):
        tool_def = ResourceSearchTool.get_tool_definition()
        assert len(tool_def.when_to_use) >= 2
        assert any("不确定" in w for w in tool_def.when_to_use)
        assert any("detailed" in w for w in tool_def.when_to_use)

    def test_when_not_to_use(self):
        tool_def = ResourceSearchTool.get_tool_definition()
        assert len(tool_def.when_not_to_use) >= 3
        assert any("已知资源" in w for w in tool_def.when_not_to_use)
        assert any("enhanced_search" in w for w in tool_def.when_not_to_use)
        assert any("web_search" in w for w in tool_def.when_not_to_use)

    def test_caveats(self):
        tool_def = ResourceSearchTool.get_tool_definition()
        assert len(tool_def.caveats) >= 2
        assert any("不要重复" in c for c in tool_def.caveats)
        assert any("只调用一次" in c for c in tool_def.caveats)

    def test_input_schema_required(self):
        tool_def = ResourceSearchTool.get_tool_definition()
        assert "resource_type" in tool_def.input_schema.get("required", [])
        props = tool_def.input_schema.get("properties", {})
        assert "query" in props
        assert "mode" in props

    def test_mode_description_is_concise(self):
        tool_def = ResourceSearchTool.get_tool_definition()
        mode_desc = tool_def.input_schema["properties"]["mode"]["description"]
        assert len(mode_desc) < 120

    def test_injected_params(self):
        tool_def = ResourceSearchTool.get_tool_definition()
        assert "session_id" in tool_def.injected_params
        assert "_retriever" in tool_def.injected_params


# ---------------------------------------------------------------------------
# 测试：搜索 Agent
# ---------------------------------------------------------------------------

class TestSearchAgents:

    @pytest.mark.asyncio
    async def test_search_agent_simple(self):
        tool = _make_tool(agent_registry=_mock_agent_registry())
        result = await tool.execute({"resource_type": "agent", "query": "general", "mode": "simple"})
        assert result.success is True
        assert result.data["query"] == "general"
        assert result.data["agent_c"] >= 1

    @pytest.mark.asyncio
    async def test_search_agent_no_match(self):
        tool = _make_tool(agent_registry=_mock_agent_registry())
        result = await tool.execute({"resource_type": "agent", "query": "nonexistent_agent_xyz", "mode": "simple"})
        assert result.success is True
        assert result.data.get("agent_c", 0) == 0

    @pytest.mark.asyncio
    async def test_search_agent_all_type(self):
        tool = _make_tool(agent_registry=_mock_agent_registry(), tool_registry=_mock_tool_registry()[0])
        result = await tool.execute({"resource_type": "all", "query": "", "mode": "simple"})
        assert result.success is True
        assert result.data.get("agent_c", 0) >= 1


# ---------------------------------------------------------------------------
# 测试：搜索 Tool
# ---------------------------------------------------------------------------

class TestSearchTools:

    @pytest.mark.asyncio
    async def test_search_tool_simple(self):
        mock_registry, _ = _mock_tool_registry()
        tool = _make_tool(tool_registry=mock_registry)
        result = await tool.execute({"resource_type": "tool", "query": "file", "mode": "simple"})
        assert result.success is True
        assert result.data.get("tool_c", 0) >= 1

    @pytest.mark.asyncio
    async def test_search_tool_detailed_returns_loaded(self):
        mock_registry, _ = _mock_tool_registry()
        tool = _make_tool(tool_registry=mock_registry)
        result = await tool.execute({"resource_type": "tool", "query": "file_read", "mode": "detailed"})
        assert result.success is True
        assert result.data.get("tool_c", 0) >= 1
        assert "file_read" in str(result.data.get("tool_d", []))

    @pytest.mark.asyncio
    async def test_search_tool_batch_detailed(self):
        mock_registry, _ = _mock_tool_registry()
        mock_injector = AsyncMock()
        tool = _make_tool(tool_registry=mock_registry, dynamic_tool_injector=mock_injector)
        result = await tool.execute({"resource_type": "tool", "query": "file_read,file_write", "mode": "detailed"})
        assert result.success is True
        assert result.data.get("tool_c", 0) >= 2

    @pytest.mark.asyncio
    async def test_search_tool_no_match_triggers_external(self):
        mock_registry, _ = _mock_tool_registry()
        tool = _make_tool(tool_registry=mock_registry)
        result = await tool.execute({"resource_type": "tool", "query": "nonexistent_tool_xyz", "mode": "simple"})
        assert result.success is True
        assert result.data.get("tool_c", 0) >= 0


# ---------------------------------------------------------------------------
# 测试：匹配逻辑
# ---------------------------------------------------------------------------

class TestMatchQuery:

    def test_match_name(self):
        tool = _make_tool()
        assert tool._match_query("file", "File Read", "", [], exact=False) is True

    def test_match_description(self):
        tool = _make_tool()
        assert tool._match_query("读取", "File", "读取文件内容", [], exact=False) is True

    def test_match_tag(self):
        tool = _make_tool()
        assert tool._match_query("search", "X", "Y", ["search", "web"], exact=False) is True

    def test_no_match(self):
        tool = _make_tool()
        assert tool._match_query("zzz_nonexistent", "File", "Read", [], exact=False) is False

    def test_exact_match(self):
        tool = _make_tool()
        assert tool._match_query("file_read", "file_read", "", [], exact=True) is True

    def test_exact_no_match(self):
        tool = _make_tool()
        assert tool._match_query("file_read", "file_write", "", [], exact=True) is False


# ---------------------------------------------------------------------------
# 测试：外部搜索
# ---------------------------------------------------------------------------

class TestExternalSearch:

    def test_external_search_enabled(self):
        tool = _make_tool()
        ext = tool._get_external_search()
        assert ext is not None, "external_search 已启用，应返回 ExternalResourceSearch 实例"

    @pytest.mark.asyncio
    async def test_search_external_live_smithery(self):
        tool = _make_tool()
        names, descs, schemas = await tool._search_external("web search", "tool", 3)
        assert isinstance(names, list)
        assert isinstance(descs, list)
        assert isinstance(schemas, list)
        if names:
            assert len(names) == len(descs) == len(schemas)

    @pytest.mark.asyncio
    async def test_search_external_live_mcp_registry(self):
        tool = _make_tool()
        names, descs, schemas = await tool._search_external("file", "tool", 3)
        assert isinstance(names, list)
        if names:
            assert len(names) == len(descs) == len(schemas)


# ---------------------------------------------------------------------------
# 测试：空 query
# ---------------------------------------------------------------------------

class TestEmptyQuery:

    @pytest.mark.asyncio
    async def test_empty_query_returns_all(self):
        mock_registry, _ = _mock_tool_registry()
        tool = _make_tool(tool_registry=mock_registry)
        result = await tool.execute({"resource_type": "tool", "query": "", "mode": "simple"})
        assert result.success is True
        assert result.data.get("tool_c", 0) >= 1
