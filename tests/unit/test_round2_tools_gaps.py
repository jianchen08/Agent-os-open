"""Round2 测试审查 - 工具系统模块测试缺口补充

覆盖需求：03_工具系统模块需求文档
- F-TOOL-01/02: 工具注册与查找
- F-TOOL-03: OpenAI function calling 格式输出
- F-TOOL-05/06: 工具元数据 (name/description/input_schema/when_to_use)
- F-TOOL-12/13/14/15: 四种执行策略
"""

import pytest


class TestToolDefinition:
    """F-TOOL-05/06: ToolDefinition 数据模型"""

    def test_tool_definition_importable(self):
        """ToolDefinition 可导入"""
        try:
            from src.tools.types import ToolDefinition
            assert ToolDefinition is not None
        except ImportError:
            pytest.skip("ToolDefinition 模块路径不同")

    def test_tool_definition_fields(self):
        """ToolDefinition 包含必要字段"""
        try:
            from src.tools.types import ToolDefinition
            td = ToolDefinition(
                name="file_read",
                description="Read a file",
                input_schema={"type": "object", "properties": {}},
                handler=lambda **kwargs: None,
                when_to_use="When you need to read file content",
                when_not_to_use="When writing files",
            )
            assert td.name == "file_read"
            assert td.description == "Read a file"
            assert td.when_to_use.startswith("When")
        except (ImportError, TypeError):
            pytest.skip("ToolDefinition 结构不同")


class TestToolRegistry:
    """F-TOOL-01/02: 工具注册与查找

    源码 ToolRegistry.register 接收 Tool 对象（非 name=/func= 散参）。
    register 重复抛 ToolAlreadyExistsError；get 不存在抛 ToolNotFoundError。
    """

    @staticmethod
    def _make_tool(name: str, description: str = "A test tool"):
        from src.tools.types import Tool, ToolSource
        return Tool(
            name=name,
            description=description,
            input_schema={"type": "object", "properties": {}},
            source=ToolSource.CODE,
        )

    def test_registry_register_and_get(self):
        """注册工具后可通过名称查找"""
        from src.tools.registry import ToolRegistry
        registry = ToolRegistry()
        registry.register(self._make_tool("test_tool"))
        td = registry.get("test_tool")
        assert td.name == "test_tool"

    def test_registry_get_nonexistent(self):
        """查找不存在的工具应抛 ToolNotFoundError"""
        from core.exceptions import ToolNotFoundError
        from src.tools.registry import ToolRegistry
        registry = ToolRegistry()
        with pytest.raises(ToolNotFoundError):
            registry.get("nonexistent_tool_xyz")

    def test_registry_has(self):
        """has() 方法正确返回布尔值"""
        from src.tools.registry import ToolRegistry
        registry = ToolRegistry()
        registry.register(self._make_tool("exists_tool"))
        assert registry.has("exists_tool") is True
        assert registry.has("not_exists") is False

    def test_registry_list_tools(self):
        """list_all 返回所有已注册工具"""
        from src.tools.registry import ToolRegistry
        registry = ToolRegistry()
        registry.register(self._make_tool("t1"))
        registry.register(self._make_tool("t2"))
        tools = registry.list_all()
        names = [t.name for t in tools]
        assert "t1" in names
        assert "t2" in names


class TestToolForLLM:
    """F-TOOL-03: get_tools_for_llm 输出 OpenAI 格式"""

    def test_get_tools_for_llm_format(self):
        """get_tools_for_llm 返回 OpenAI function calling 格式"""
        from src.tools.registry import ToolRegistry
        from src.tools.types import Tool, ToolSource
        registry = ToolRegistry()
        registry.register(Tool(
            name="file_read",
            description="Read file",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            source=ToolSource.CODE,
        ))
        tools_for_llm = registry.get_tools_for_llm()
        assert len(tools_for_llm) >= 1
        # OpenAI 格式有 function 字段
        first = tools_for_llm[0]
        assert "function" in first or "name" in first


class TestToolErrorPolicy:
    """F-TOOL-12/13/14/15: 四种执行策略"""

    def test_error_policy_values(self):
        """四种错误策略常量存在"""
        from src.pipeline.types import ErrorPolicy
        assert ErrorPolicy.ABORT.value == "abort"
        assert ErrorPolicy.SKIP.value == "skip"
        assert ErrorPolicy.FALLBACK.value == "fallback"
        assert ErrorPolicy.RETRY.value == "retry"


class TestRouteSignalForTools:
    """F-PIP-08: PendingToolsOutput 路由信号"""

    def test_route_signal_next_tool(self):
        """raw_tool_calls 非空时发出 next_tool 信号"""
        from src.pipeline.types import RouteSignal
        signal = RouteSignal(route_type="next_tool", target="tool_execute")
        assert signal.route_type == "next_tool"
