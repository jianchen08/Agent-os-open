"""M6a Input 插件测试 — context_build, prompt_build, knowledge_inject, tool_schema。

验证四个 Agent 核心 Input 插件的独立功能。
"""

from __future__ import annotations

import pytest

from pipeline.plugin import PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys, create_initial_state
from plugins.input.context_build import ContextBuildPlugin
from plugins.input.knowledge_inject import KnowledgeInjectPlugin
from plugins.input.prompt_build import PromptBuildPlugin
from plugins.input.tool_schema import ToolSchemaPlugin


# ── Fixtures ──


@pytest.fixture
def base_state() -> dict:
    """创建基础测试状态。"""
    return create_initial_state(
        session_id="test-session",
        task_id="test-task",
        user_message="你好，请帮我写一段代码",
    )


@pytest.fixture
def ctx(base_state) -> PluginContext:
    """创建基础测试上下文。"""
    return PluginContext(state=base_state)


# ── ContextBuildPlugin Tests ──


class TestContextBuildPlugin:
    """上下文构建插件测试。"""

    def test_name_and_priority(self):
        """测试插件名称和优先级。"""
        plugin = ContextBuildPlugin()
        assert plugin.name == "context_build"
        assert plugin.priority == 10
        assert plugin.error_policy == ErrorPolicy.FALLBACK

    def test_custom_priority(self):
        """测试自定义优先级。"""
        plugin = ContextBuildPlugin({"priority": 15})
        assert plugin.priority == 15

    @pytest.mark.asyncio
    async def test_builds_basic_context(self, ctx, base_state):
        """测试构建基本上下文信息。"""
        plugin = ContextBuildPlugin({
            "system_prompt": "你是一个AI助手",
            "agent_name": "TestAgent",
            "agent_level": "l1_main",
        })
        result = await plugin.execute(ctx)

        assert isinstance(result, PluginResult)
        assert result.state_updates["context.system_prompt"] == "你是一个AI助手"
        assert result.state_updates["context.agent_name"] == "TestAgent"
        assert result.state_updates["context.agent_level"] == "l1_main"
        assert result.state_updates["context.session_id"] == "test-session"
        assert result.state_updates["context.task_id"] == "test-task"

    @pytest.mark.asyncio
    async def test_syncs_agent_level_to_state(self, ctx, base_state):
        """测试同步 agent_level 到框架字段。"""
        base_state[StateKeys.AGENT_LEVEL] = ""
        plugin = ContextBuildPlugin({"agent_level": "l2_subtask"})
        result = await plugin.execute(ctx)
        assert result.state_updates[StateKeys.AGENT_LEVEL] == "l2_subtask"

    @pytest.mark.asyncio
    async def test_extra_context(self, ctx):
        """测试额外上下文字段。"""
        plugin = ContextBuildPlugin({
            "extra_context": {"project": "Agent OS", "version": "1.0"},
        })
        result = await plugin.execute(ctx)
        assert result.state_updates["context.project"] == "Agent OS"
        assert result.state_updates["context.version"] == "1.0"

    @pytest.mark.asyncio
    async def test_tool_execution_flag(self, ctx, base_state):
        """测试工具执行标记。"""
        base_state[StateKeys.CORE_TYPE] = "tool_execute"
        plugin = ContextBuildPlugin()
        result = await plugin.execute(ctx)
        assert result.state_updates["context.is_tool_execution"] is True

    @pytest.mark.asyncio
    async def test_project_flag_for_l1(self, ctx):
        """测试 L1 层级的项目标记。"""
        plugin = ContextBuildPlugin({"agent_level": "l1_main"})
        result = await plugin.execute(ctx)
        assert result.state_updates["context.is_project"] is True

    @pytest.mark.asyncio
    async def test_fallback_state(self):
        """测试 FALLBACK 策略的默认状态。"""
        plugin = ContextBuildPlugin()
        assert "context.system_prompt" in plugin.fallback_state
        assert plugin.error_policy == ErrorPolicy.FALLBACK


# ── PromptBuildPlugin Tests ──


class TestPromptBuildPlugin:
    """提示词构建插件测试。"""

    def test_name_and_priority(self):
        """测试插件名称和优先级。"""
        plugin = PromptBuildPlugin()
        assert plugin.name == "prompt_build"
        assert plugin.priority == 50
        assert plugin.error_policy == ErrorPolicy.ABORT

    @pytest.mark.asyncio
    async def test_builds_system_message(self, ctx, base_state):
        """测试构建系统消息字典。"""
        base_state["context.system_prompt"] = "你是一个代码助手"
        plugin = PromptBuildPlugin()
        result = await plugin.execute(ctx)

        assert isinstance(result, PluginResult)
        # PromptBuildPlugin 产出 system_message dict，而非 prompt.system 字符串
        assert "system_message" in result.state_updates
        assert result.state_updates["system_message"]["role"] == "system"
        assert "你是一个代码助手" in result.state_updates["system_message"]["content"]

    @pytest.mark.asyncio
    async def test_does_not_auto_inject_knowledge_context(self, ctx, base_state):
        """knowledge.context 不再自动拼入系统提示词（仅 static_vars opt-in）。"""
        base_state["context.system_prompt"] = "System"
        base_state["knowledge.context"] = "这是知识库内容"
        plugin = PromptBuildPlugin()
        result = await plugin.execute(ctx)

        system = result.state_updates["system_message"]["content"]
        assert "这是知识库内容" not in system

    @pytest.mark.asyncio
    async def test_includes_hard_constraints(self, ctx, base_state):
        """测试硬约束规则包含在系统提示词中。

        硬约束通过 context.system_prompt 传入，拼入 system_message。
        """
        base_state["context.system_prompt"] = "System\n不修改系统文件"
        plugin = PromptBuildPlugin()
        result = await plugin.execute(ctx)

        system = result.state_updates["system_message"]["content"]
        assert "不修改系统文件" in system

    @pytest.mark.asyncio
    async def test_does_not_auto_inject_memory_retrieved(self, ctx, base_state):
        """memory.retrieved 不再自动拼入系统提示词。"""
        base_state["context.system_prompt"] = "System"
        base_state["memory.retrieved"] = "用户之前问过关于Python的问题"
        plugin = PromptBuildPlugin()
        result = await plugin.execute(ctx)

        system = result.state_updates["system_message"]["content"]
        assert "用户之前问过关于Python的问题" not in system

    @pytest.mark.asyncio
    async def test_dynamic_vars(self, ctx, base_state):
        """测试动态变量生成。"""
        base_state["context.system_prompt"] = "System"
        base_state["context.agent_name"] = "TestAgent"
        plugin = PromptBuildPlugin()
        result = await plugin.execute(ctx)

        dynamic = result.state_updates.get("prompt.dynamic_vars", "")
        assert "日期" in dynamic["content"] or "时间" in dynamic["content"]

    @pytest.mark.asyncio
    async def test_tool_descriptions_in_system_message(self, ctx, base_state):
        """测试工具描述在开启开关时拼入系统消息。

        PromptBuildPlugin 不直接从 tool_registry 获取描述，
        而是从 state["prompt.tool_descriptions"] 读取（由 ToolSchemaPlugin 写入），
        且仅当 include_tools_description_in_prompt=True 时才拼入。
        """
        base_state["context.system_prompt"] = "System"
        base_state["prompt.tool_descriptions"] = "## 可用工具\n- read_file: 读取文件内容"

        # 开启工具描述开关
        plugin = PromptBuildPlugin({"include_tools_description_in_prompt": True})
        result = await plugin.execute(ctx)

        system = result.state_updates["system_message"]["content"]
        assert "read_file" in system


# ── KnowledgeInjectPlugin Tests ──


class TestKnowledgeInjectPlugin:
    """知识注入插件测试。"""

    def test_name_and_priority(self):
        """测试插件名称和优先级。"""
        plugin = KnowledgeInjectPlugin()
        assert plugin.name == "knowledge_inject"
        assert plugin.priority == 30
        assert plugin.error_policy == ErrorPolicy.FALLBACK

    @pytest.mark.asyncio
    async def test_disabled_mode(self, ctx):
        """测试禁用模式返回空内容。"""
        plugin = KnowledgeInjectPlugin({"mode": "disabled"})
        result = await plugin.execute(ctx)

        assert result.state_updates["knowledge.context"] == ""

    @pytest.mark.asyncio
    async def test_no_service_returns_empty(self, ctx):
        """测试无语义存储服务时返回空内容。"""
        plugin = KnowledgeInjectPlugin({"mode": "full"})
        result = await plugin.execute(ctx)

        assert result.state_updates["knowledge.context"] == ""

    @pytest.mark.asyncio
    async def test_no_query_returns_empty(self, ctx, base_state):
        """测试无查询内容时返回空。"""
        base_state["user_message"] = ""
        plugin = KnowledgeInjectPlugin({"mode": "full"})

        # Mock semantic_storage (M11b 版本)
        class MockSemanticStorage:
            async def find_by_user(self, user_id):
                return []

        ctx._services["semantic_storage"] = MockSemanticStorage()
        result = await plugin.execute(ctx)

        assert result.state_updates["knowledge.context"] == ""

    @pytest.mark.asyncio
    async def test_full_mode(self, ctx, base_state):
        """测试完整内容注入模式。"""
        base_state["user_message"] = "Python"

        # Mock semantic_storage 返回知识条目
        from memory.types import Knowledge
        mock_items = [
            Knowledge(user_id="", content="Python 是一种高级编程语言", source_type="test"),
            Knowledge(user_id="", content="支持面向对象和函数式编程", source_type="test"),
        ]

        class MockSemanticStorage:
            async def find_by_user(self, user_id):
                return mock_items

        ctx._services["semantic_storage"] = MockSemanticStorage()

        plugin = KnowledgeInjectPlugin({"mode": "full", "max_tokens": 10000})
        result = await plugin.execute(ctx)

        content = result.state_updates["knowledge.context"]
        assert "Python 是一种高级编程语言" in content
        assert "支持面向对象和函数式编程" in content

    @pytest.mark.asyncio
    async def test_compressed_mode(self, ctx, base_state):
        """测试压缩内容注入模式。"""
        base_state["user_message"] = "test"

        from memory.types import Knowledge
        mock_items = [
            Knowledge(user_id="", content="A" * 300, source_type="test"),
        ]

        class MockSemanticStorage:
            async def find_by_user(self, user_id):
                return mock_items

        ctx._services["semantic_storage"] = MockSemanticStorage()

        plugin = KnowledgeInjectPlugin({"mode": "compressed"})
        result = await plugin.execute(ctx)

        content = result.state_updates["knowledge.context"]
        assert len(content) < 400  # 应被压缩

    @pytest.mark.asyncio
    async def test_hint_mode(self, ctx, base_state):
        """测试检索提示模式。"""
        base_state["user_message"] = "test"

        from memory.types import Knowledge
        mock_items = [
            Knowledge(user_id="", content="Python 基础", source_type="test"),
            Knowledge(user_id="", content="Java 入门", source_type="test"),
        ]

        class MockSemanticStorage:
            async def find_by_user(self, user_id):
                return mock_items

        ctx._services["semantic_storage"] = MockSemanticStorage()

        plugin = KnowledgeInjectPlugin({"mode": "hint"})
        result = await plugin.execute(ctx)

        content = result.state_updates["knowledge.context"]
        assert "2 条相关内容" in content

    @pytest.mark.asyncio
    async def test_fallback_state(self):
        """测试 FALLBACK 策略的默认状态。"""
        plugin = KnowledgeInjectPlugin()
        assert "knowledge.context" in plugin.fallback_state


# ── ToolSchemaPlugin Tests ──


class TestToolSchemaPlugin:
    """工具 Schema 注入插件测试。"""

    def test_name_and_priority(self):
        """测试插件名称和优先级。"""
        plugin = ToolSchemaPlugin()
        assert plugin.name == "tool_schema"
        assert plugin.priority == 50
        assert plugin.error_policy == ErrorPolicy.FALLBACK

    @pytest.mark.asyncio
    async def test_disabled_returns_empty(self, ctx):
        """测试禁用时返回空。"""
        plugin = ToolSchemaPlugin({"enabled": False})
        result = await plugin.execute(ctx)

        assert result.state_updates["tool_schemas"] == []
        # 禁用时不产出 prompt.tool_descriptions
        assert "prompt.tool_descriptions" not in result.state_updates

    @pytest.mark.asyncio
    async def test_no_registry_returns_empty(self, ctx):
        """测试无工具注册表时返回空。"""
        plugin = ToolSchemaPlugin()
        result = await plugin.execute(ctx)

        assert result.state_updates["tool_schemas"] == []

    @pytest.mark.asyncio
    async def test_injects_tool_schemas(self, ctx):
        """测试注入工具 Schema。

        ToolSchemaPlugin 使用 tool_registry.list_all() 和 tool.to_llm_format()。
        """
        # Mock tool_registry（使用 list_all 和 to_llm_format）
        class MockTool:
            name = "read_file"
            description = "读取文件内容"
            input_schema = {"type": "object", "properties": {"path": {"type": "string"}}}

            def to_llm_format(self):
                """返回 LLM function calling 格式。"""
                return {
                    "type": "function",
                    "function": {
                        "name": self.name,
                        "description": self.description,
                        "parameters": self.input_schema,
                    },
                }

        class MockRegistry:
            def list_all(self):
                return [MockTool()]
            def get(self, name):
                return MockTool()

        ctx._services["tool_registry"] = MockRegistry()

        plugin = ToolSchemaPlugin()
        result = await plugin.execute(ctx)

        schemas = result.state_updates["tool_schemas"]
        assert len(schemas) == 1
        assert schemas[0]["type"] == "function"
        assert schemas[0]["function"]["name"] == "read_file"
        # 默认不产出 prompt.tool_descriptions（include_tools_description_in_prompt=False）
        assert "prompt.tool_descriptions" not in result.state_updates

    @pytest.mark.asyncio
    async def test_specific_tool_ids(self, ctx):
        """测试指定工具 ID 列表。"""
        class MockTool1:
            name = "read_file"
            description = "读取文件"
            input_schema = {"type": "object", "properties": {}}

            def to_llm_format(self):
                """返回 LLM function calling 格式。"""
                return {
                    "type": "function",
                    "function": {
                        "name": self.name,
                        "description": self.description,
                        "parameters": self.input_schema,
                    },
                }

        class MockTool2:
            name = "write_file"
            description = "写入文件"
            input_schema = {"type": "object", "properties": {}}

            def to_llm_format(self):
                """返回 LLM function calling 格式。"""
                return {
                    "type": "function",
                    "function": {
                        "name": self.name,
                        "description": self.description,
                        "parameters": self.input_schema,
                    },
                }

        class MockRegistry:
            def list_all(self):
                return [MockTool1(), MockTool2()]
            def get(self, name):
                if name == "read_file":
                    return MockTool1()
                raise KeyError(name)

        ctx._services["tool_registry"] = MockRegistry()

        plugin = ToolSchemaPlugin({"tool_ids": ["read_file"]})
        result = await plugin.execute(ctx)

        schemas = result.state_updates["tool_schemas"]
        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "read_file"

    @pytest.mark.asyncio
    async def test_empty_registry(self, ctx):
        """测试空工具注册表。"""
        class MockRegistry:
            def list_all(self):
                return []

        ctx._services["tool_registry"] = MockRegistry()

        plugin = ToolSchemaPlugin()
        result = await plugin.execute(ctx)

        assert result.state_updates["tool_schemas"] == []

    @pytest.mark.asyncio
    async def test_tool_descriptions_with_config_enabled(self, ctx):
        """测试开启 include_tools_description_in_prompt 时产出工具描述。"""
        class MockTool:
            name = "read_file"
            description = "读取文件内容"
            input_schema = {"type": "object", "properties": {}}

            def to_llm_format(self):
                """返回 LLM function calling 格式。"""
                return {
                    "type": "function",
                    "function": {
                        "name": self.name,
                        "description": self.description,
                        "parameters": self.input_schema,
                    },
                }

        class MockRegistry:
            def list_all(self):
                return [MockTool()]

        ctx._services["tool_registry"] = MockRegistry()

        plugin = ToolSchemaPlugin({"include_tools_description_in_prompt": True})
        result = await plugin.execute(ctx)

        assert "可用工具" in result.state_updates["prompt.tool_descriptions"]
        assert "read_file" in result.state_updates["prompt.tool_descriptions"]


# ── Mock Helpers ──


class MockResult:
    """模拟语义记忆检索结果。"""

    def __init__(self, content: str, score: float = 0.85, summary: str | None = None):
        self.content = content
        self.score = score
        self.summary = summary


class MockSemanticMemory:
    """模拟语义记忆服务。"""

    def __init__(self, results: list[MockResult]):
        self._results = results

    async def retrieve(self, query: str, top_k: int = 5, score_threshold: float = 0.7):
        return self._results[:top_k]
