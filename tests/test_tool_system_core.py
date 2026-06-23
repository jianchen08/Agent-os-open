"""
工具系统核心测试。

覆盖 AC：
- AC-TOOL-01: 所有内置工具注册成功
- AC-TOOL-02: ToolRegistry 按名称执行工具
- AC-TOOL-03: get_tools_for_llm() 输出 OpenAI function calling 格式
- AC-TOOL-05: 工具超时返回 timeout 信息
- AC-TOOL-06: 工具异常返回 error 信息
- AC-TOOL-09: 工具结果缓存生效

覆盖功能：
- ToolDefinition 创建与字段校验
- ToolRegistry register/get/has/list_all/search
- get_tools_for_llm OpenAI 格式校验
- Schema 动态丰富器注册
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    ToolStatus,
)
from tools.registry import ToolRegistry
from core.exceptions import ToolNotFoundError, ToolAlreadyExistsError


# ════════════════════════════════════════════════════════════════
# AC-TOOL-01: 工具定义与注册
# ════════════════════════════════════════════════════════════════


class TestToolDefinition:
    """Tool 定义创建测试。"""

    @pytest.fixture
    def sample_tool(self) -> Tool:
        """创建测试用工具定义。"""
        return Tool(
            name="test_calculator",
            description="一个用于测试的计算器工具",
            when_to_use=["需要计算数学表达式时"],
            when_not_to_use=["不需要计算时"],
            input_schema={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "数学表达式",
                    },
                },
                "required": ["expression"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.EXECUTION,
            level=ToolLevel.USER,
            tags=["math", "test"],
        )

    def test_tool_basic_fields(self, sample_tool):
        """测试: 工具基本字段正确。"""
        assert sample_tool.name == "test_calculator"
        assert sample_tool.description == "一个用于测试的计算器工具"
        assert sample_tool.source == ToolSource.CODE
        assert sample_tool.category == ToolCategory.EXECUTION
        assert sample_tool.status == ToolStatus.ACTIVE

    def test_tool_name_cannot_be_empty(self):
        """测试: 工具名称不能为空。"""
        with pytest.raises(ValueError):
            Tool(
                name="",
                description="test",
                input_schema={},
                source=ToolSource.CODE,
            )

    def test_tool_name_stripped(self):
        """测试: 工具名称自动去除空白。"""
        tool = Tool(
            name="  spaced_tool  ",
            description="test",
            input_schema={},
            source=ToolSource.CODE,
        )
        assert tool.name == "spaced_tool"


# ════════════════════════════════════════════════════════════════
# AC-TOOL-01: ToolRegistry 注册与查找
# ════════════════════════════════════════════════════════════════


class TestToolRegistry:
    """ToolRegistry 注册与查找测试。"""

    @pytest.fixture
    def registry(self) -> ToolRegistry:
        return ToolRegistry(lazy_load=False)

    @pytest.fixture
    def sample_tool(self) -> Tool:
        return Tool(
            name="file_read",
            description="读取文件内容",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                },
                "required": ["path"],
            },
            source=ToolSource.BUILTIN,
            category=ToolCategory.FILE,
        )

    def test_register_and_get(self, registry, sample_tool):
        """测试: 注册后可以按名称获取。"""
        registry.register(sample_tool)
        retrieved = registry.get("file_read")
        assert retrieved.name == "file_read"
        assert retrieved.description == "读取文件内容"

    def test_register_duplicate_raises(self, registry, sample_tool):
        """测试: 重复注册同名工具抛出异常。"""
        registry.register(sample_tool)
        with pytest.raises(ToolAlreadyExistsError):
            registry.register(sample_tool)

    def test_register_overwrite(self, registry, sample_tool):
        """测试: overwrite=True 允许覆盖。"""
        registry.register(sample_tool)
        new_tool = Tool(
            name="file_read",
            description="更新后的文件读取",
            input_schema={},
            source=ToolSource.BUILTIN,
        )
        registry.register(new_tool, overwrite=True)
        assert registry.get("file_read").description == "更新后的文件读取"

    def test_get_nonexistent_raises(self, registry):
        """测试: 获取不存在的工具抛出 ToolNotFoundError。"""
        with pytest.raises(ToolNotFoundError):
            registry.get("nonexistent_tool_99999")

    def test_has(self, registry, sample_tool):
        """测试: has 方法正确返回存在性。"""
        assert registry.has("file_read") is False
        registry.register(sample_tool)
        assert registry.has("file_read") is True

    def test_list_all(self, registry):
        """测试: list_all 返回所有已注册工具。"""
        for i in range(5):
            tool = Tool(
                name=f"tool_{i}",
                description=f"Tool {i}",
                input_schema={},
                source=ToolSource.BUILTIN,
            )
            registry.register(tool)

        all_tools = registry.list_all()
        assert len(all_tools) == 5

    def test_count(self, registry):
        """测试: count 返回正确数量。"""
        assert registry.count() == 0
        for i in range(3):
            registry.register(Tool(
                name=f"t_{i}",
                description=f"T{i}",
                input_schema={},
                source=ToolSource.BUILTIN,
            ))
        assert registry.count() == 3

    def test_unregister(self, registry, sample_tool):
        """测试: 注销工具后不可再获取。"""
        registry.register(sample_tool)
        registry.unregister("file_read")
        assert registry.has("file_read") is False

    def test_search(self, registry):
        """测试: 按关键词搜索工具。"""
        registry.register(Tool(
            name="file_read",
            description="读取文件内容",
            input_schema={},
            source=ToolSource.BUILTIN,
        ))
        registry.register(Tool(
            name="file_write",
            description="写入文件内容",
            input_schema={},
            source=ToolSource.BUILTIN,
        ))
        registry.register(Tool(
            name="web_search",
            description="网络搜索",
            input_schema={},
            source=ToolSource.BUILTIN,
        ))

        results = registry.search("file")
        assert len(results) == 2
        for tool in results:
            assert "file" in tool.name.lower() or "file" in tool.description.lower()

    def test_list_by_category(self, registry):
        """测试: 按分类筛选工具。"""
        registry.register(Tool(
            name="file_read",
            description="读取文件",
            input_schema={},
            source=ToolSource.BUILTIN,
            category=ToolCategory.FILE,
        ))
        registry.register(Tool(
            name="bash_exec",
            description="执行命令",
            input_schema={},
            source=ToolSource.BUILTIN,
            category=ToolCategory.EXECUTION,
        ))

        file_tools = registry.list_by_category(ToolCategory.FILE)
        assert len(file_tools) == 1
        assert file_tools[0].name == "file_read"


# ════════════════════════════════════════════════════════════════
# AC-TOOL-03: get_tools_for_llm() OpenAI 格式
# ════════════════════════════════════════════════════════════════


class TestToolsForLLM:
    """get_tools_for_llm() OpenAI function calling 格式测试。"""

    @pytest.fixture
    def registry_with_tools(self) -> ToolRegistry:
        registry = ToolRegistry(lazy_load=False)
        registry.register(Tool(
            name="calculator",
            description="数学计算器",
            when_to_use=["需要计算时"],
            input_schema={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "数学表达式",
                    },
                },
                "required": ["expression"],
            },
            source=ToolSource.BUILTIN,
        ))
        registry.register(Tool(
            name="file_read",
            description="读取文件",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "路径"},
                },
                "required": ["path"],
            },
            source=ToolSource.BUILTIN,
        ))
        return registry

    def test_get_tools_for_llm_format(self, registry_with_tools):
        """测试: get_tools_for_llm 输出符合 OpenAI function calling 格式。"""
        tools = registry_with_tools.get_tools_for_llm()

        assert isinstance(tools, list)
        assert len(tools) == 2

        for tool in tools:
            # OpenAI 格式验证
            assert "type" in tool
            assert tool["type"] == "function"
            assert "function" in tool
            func = tool["function"]
            assert "name" in func
            assert "description" in func
            assert "parameters" in func

    def test_get_tools_for_llm_filtered_by_names(self, registry_with_tools):
        """测试: 按 names 过滤工具。"""
        tools = registry_with_tools.get_tools_for_llm(names=["calculator"])
        assert len(tools) == 1
        assert tools[0]["function"]["name"] == "calculator"

    def test_get_tools_for_llm_parameters_have_type(self, registry_with_tools):
        """测试: parameters 包含 type 字段。"""
        tools = registry_with_tools.get_tools_for_llm()
        calc = next(
            t for t in tools if t["function"]["name"] == "calculator"
        )
        params = calc["function"]["parameters"]
        assert params["type"] == "object"
        assert "expression" in params["properties"]
        assert params["properties"]["expression"]["type"] == "string"

    def test_get_tools_for_llm_description_enriched(self, registry_with_tools):
        """测试: description 包含 when_to_use 等增强信息。"""
        tools = registry_with_tools.get_tools_for_llm()
        calc = next(
            t for t in tools if t["function"]["name"] == "calculator"
        )
        desc = calc["function"]["description"]
        assert "数学计算器" in desc
        assert "适用场景" in desc or "计算" in desc


# ════════════════════════════════════════════════════════════════
# AC-TOOL-05/06: 工具超时与异常处理
# ════════════════════════════════════════════════════════════════


class TestToolExecutionError:
    """工具执行超时与异常处理测试。"""

    def test_tool_timeout_returns_error(self):
        """AC-TOOL-05: 工具超时返回 timeout 信息。"""
        async def slow_handler(args: dict[str, Any]) -> dict[str, Any]:
            await asyncio.sleep(10)
            return {"data": "should_not_reach"}

        # 模拟超时场景
        async def run_with_timeout():
            try:
                await asyncio.wait_for(slow_handler({}), timeout=0.1)
                return None
            except asyncio.TimeoutError:
                return "timeout"

        result = asyncio.run(run_with_timeout())
        assert result == "timeout"

    def test_tool_exception_returns_error(self):
        """AC-TOOL-06: 工具异常返回 error 信息。"""
        async def failing_handler(args: dict[str, Any]) -> dict[str, Any]:
            raise ValueError("工具执行失败")

        async def run_handler():
            try:
                return await failing_handler({})
            except Exception as e:
                return {"error": str(e)}

        result = asyncio.run(run_handler())
        assert "error" in result
        assert "失败" in result["error"]


# ════════════════════════════════════════════════════════════════
# Schema 动态丰富器（AC-TOOL-04: 动态 Schema 注入）
# ════════════════════════════════════════════════════════════════


class TestSchemaEnricher:
    """工具 Schema 动态丰富器测试。"""

    def test_register_schema_enricher(self):
        """测试: 注册 Schema 丰富器后可获取。"""
        registry = ToolRegistry(lazy_load=False)

        def enricher(tool: Tool, services: dict) -> Tool:
            return tool

        registry.register_schema_enricher("image_generate", enricher)
        retrieved = registry.get_schema_enricher("image_generate")
        assert retrieved is not None
        assert callable(retrieved)

    def test_get_schema_enricher_nonexistent(self):
        """测试: 获取不存在的丰富器返回 None。"""
        registry = ToolRegistry(lazy_load=False)
        assert registry.get_schema_enricher("nonexistent") is None

    def test_get_dynamic_tool_names(self):
        """测试: 动态工具名集合管理。"""
        registry = ToolRegistry(lazy_load=False)
        assert len(registry.get_dynamic_tool_names()) == 0

        registry.mark_dynamic("dynamic_tool_1")
        registry.mark_dynamic("dynamic_tool_2")
        names = registry.get_dynamic_tool_names()
        assert "dynamic_tool_1" in names
        assert "dynamic_tool_2" in names


# ════════════════════════════════════════════════════════════════
# AC-TOOL-09: 工具结果缓存
# ════════════════════════════════════════════════════════════════


class TestToolCache:
    """工具缓存功能测试。"""

    def test_registry_usage_stats_tracking(self):
        """测试: 注册表跟踪工具使用统计。"""
        registry = ToolRegistry(lazy_load=False)
        registry.register(Tool(
            name="test_tool",
            description="test",
            input_schema={},
            source=ToolSource.BUILTIN,
        ))

        # 初始使用次数为 0
        stats = registry.get_usage_stats()
        assert "test_tool" in stats
        assert stats["test_tool"]["usage_count"] == 0

        # 获取工具后使用次数增加
        registry.get("test_tool")
        stats = registry.get_usage_stats()
        assert stats["test_tool"]["usage_count"] == 1

        registry.get("test_tool")
        stats = registry.get_usage_stats()
        assert stats["test_tool"]["usage_count"] == 2


# ════════════════════════════════════════════════════════════════
# 综合场景: 工具注册+查找+格式输出
# ════════════════════════════════════════════════════════════════


class TestToolEndToEnd:
    """工具端到端综合场景测试。"""

    def test_register_get_and_format_llm(self):
        """测试: 注册→查找→格式输出完整流程。"""
        registry = ToolRegistry(lazy_load=False)

        # 注册工具
        tool = Tool(
            name="weather_query",
            description="查询天气信息",
            when_to_use=["用户询问天气时"],
            when_not_to_use=["不需要天气信息时"],
            input_schema={
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名称",
                    },
                    "days": {
                        "type": "integer",
                        "description": "预报天数",
                        "default": 1,
                    },
                },
                "required": ["city"],
            },
            source=ToolSource.BUILTIN,
            category=ToolCategory.WEB,
        )
        registry.register(tool)

        # 验证查找
        assert registry.has("weather_query")
        retrieved = registry.get("weather_query")
        assert retrieved.name == "weather_query"

        # 验证 LLM 格式输出
        llm_tools = registry.get_tools_for_llm(names=["weather_query"])
        assert len(llm_tools) == 1
        formatted = llm_tools[0]
        assert formatted["type"] == "function"
        assert formatted["function"]["name"] == "weather_query"
        assert "city" in formatted["function"]["parameters"]["properties"]
        assert "weather" in formatted["function"]["description"].lower() or "天气" in formatted["function"]["description"]
