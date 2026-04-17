"""提示词组装链路集成测试。

验证重构后的消息构建链路与旧代码 layer_order 完全一致：
    Input 插件链:
        message_inject → context_build → memory_read → tool_schema → prompt_build
    Core 插件:
        LLMCore._build_messages() 从 system_message + messages + dynamic_vars 组装

测试覆盖：
    1. prompt_build 只产出 state["system_message"]，不含历史和动态变量
    2. prompt_build 按 layer_order 顺序组装（system_prompt → static_vars → knowledge → memory → L3 → L2 → L1）
    3. prompt_build 默认不拼入 tools_description（走 function calling）
    4. tool_schema 默认不写 prompt.tool_descriptions
    5. LLMCore._build_messages 从三来源组装
    6. LLMCore._build_messages 动态变量追加在历史消息之后
    7. LLMCore 只将 assistant 回复追加到 state["messages"]（不包含 system/dynamic）
    8. 完整链路：message_inject → prompt_build → LLMCore._build_messages
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memory.types import ChunkData, SearchResult
from pipeline.plugin import PluginContext
from pipeline.types import StateKeys, create_initial_state
from plugins.core.llm_core import LLMCore
from plugins.input.prompt_build import PromptBuildPlugin
from plugins.input.tool_schema import ToolSchemaPlugin


# ── 测试辅助 ──


def make_ctx(state: dict, **services: Any) -> PluginContext:
    """创建带服务的插件上下文。"""
    ctx = PluginContext(state=state)
    for name, svc in services.items():
        ctx._services[name] = svc
    return ctx


def make_base_state(**overrides: Any) -> dict[str, Any]:
    """创建包含 system_prompt 的基础 state。"""
    state = create_initial_state(**overrides)
    state["context.system_prompt"] = "你是一个有用的 AI 助手。"
    state["context.session_id"] = "test-session-001"
    state["messages"] = []
    return state


# ══════════════════════════════════════════════════
# 1. prompt_build 基础：只产出 system_message
# ══════════════════════════════════════════════════


class TestPromptBuildBasic:
    """prompt_build 基础行为测试。"""

    @pytest.mark.asyncio
    async def test_output_only_system_message(self) -> None:
        """prompt_build 只产出 state['system_message']，不包含历史消息。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["messages"] = [{"role": "user", "content": "你好"}]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)

        assert "system_message" in result.state_updates
        assert result.state_updates["system_message"]["role"] == "system"
        assert "你是一个有用的 AI 助手。" in result.state_updates["system_message"]["content"]
        assert "你好" not in result.state_updates["system_message"]["content"]

    @pytest.mark.asyncio
    async def test_output_dynamic_vars_separately(self) -> None:
        """prompt_build 将 dynamic_vars 单独输出，不拼入 system_message。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)

        system_content = result.state_updates["system_message"]["content"]
        dynamic_vars = result.state_updates.get("prompt.dynamic_vars", "")

        assert "日期" in dynamic_vars
        assert "时间" in dynamic_vars
        assert dynamic_vars not in system_content

    @pytest.mark.asyncio
    async def test_no_messages_in_output(self) -> None:
        """prompt_build 产出不包含 messages 字段。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["messages"] = [{"role": "user", "content": "test"}]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)

        assert "messages" not in result.state_updates


# ══════════════════════════════════════════════════
# 2. prompt_build layer_order 顺序
# ══════════════════════════════════════════════════


class TestPromptBuildLayerOrder:
    """prompt_build 按 layer_order 顺序组装各层内容。"""

    @pytest.mark.asyncio
    async def test_system_prompt_first(self) -> None:
        """system_prompt 是第一层。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert content.startswith("你是一个有用的 AI 助手。")

    @pytest.mark.asyncio
    async def test_tools_description_excluded_by_default(self) -> None:
        """默认不拼入 tools_description（走 function calling）。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["prompt.tool_descriptions"] = "## 可用工具\n- tool1: desc"
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "可用工具" not in content

    @pytest.mark.asyncio
    async def test_tools_description_included_when_enabled(self) -> None:
        """配置 include_tools_description_in_prompt=true 时拼入 tools_description。"""
        plugin = PromptBuildPlugin(config={"include_tools_description_in_prompt": True})
        state = make_base_state()
        state["prompt.tool_descriptions"] = "## 可用工具\n- tool1: desc"
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "可用工具" in content

    @pytest.mark.asyncio
    async def test_knowledge_context_included(self) -> None:
        """knowledge.context 被拼入 system_message（在 static_vars 之后）。"""
        plugin = PromptBuildPlugin(config={"include_static_vars": False, "include_compressed_layers": False})
        state = make_base_state()
        state["knowledge.context"] = "## 知识库\nPython 异常处理最佳实践"
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "知识库" in content
        assert "Python 异常处理最佳实践" in content

    @pytest.mark.asyncio
    async def test_memory_retrieved_included(self) -> None:
        """memory.retrieved 被拼入 system_message（在 knowledge 之后）。"""
        plugin = PromptBuildPlugin(config={"include_static_vars": False, "include_compressed_layers": False})
        state = make_base_state()
        state["knowledge.context"] = "知识内容"
        state["memory.retrieved"] = "## 记忆检索\n相关经验: 上次讨论了异常处理"
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        knowledge_pos = content.index("知识内容")
        memory_pos = content.index("记忆检索")
        assert memory_pos > knowledge_pos

    @pytest.mark.asyncio
    async def test_compressed_layers_order_l3_l2_l1(self) -> None:
        """压缩层按 L3 → L2 → L1 顺序排列。"""
        plugin = PromptBuildPlugin(config={"include_static_vars": False})
        state = make_base_state()

        chunk_service = MagicMock()
        chunk_service.find_by_session = AsyncMock(side_effect=lambda sid, layer: [
            ChunkData(content=f"{layer}摘要内容", keywords=[f"{layer}_kw1", f"{layer}_kw2"])
        ])
        ctx = make_ctx(state, chunk_service=chunk_service)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        l3_pos = content.index("关键词索引")
        l2_pos = content.index("三元组摘要")
        l1_pos = content.index("八段摘要")
        assert l3_pos < l2_pos < l1_pos


# ══════════════════════════════════════════════════
# 3. prompt_build static_vars
# ══════════════════════════════════════════════════


class TestPromptBuildStaticVars:
    """prompt_build 静态变量加载测试。"""

    @pytest.mark.asyncio
    async def test_timestamp_type(self) -> None:
        """timestamp 类型静态变量生成当前时间。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["context.static_vars"] = [
            {"type": "timestamp", "name": "当前时间", "format": "%Y-%m-%d"}
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "当前时间" in content
        assert "静态变量" in content

    @pytest.mark.asyncio
    async def test_content_type(self) -> None:
        """content 类型静态变量直接使用文本值。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["context.static_vars"] = [
            {"type": "content", "name": "项目说明", "value": "这是一个 Agent OS 项目"}
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "项目说明" in content
        assert "Agent OS" in content

    @pytest.mark.asyncio
    async def test_disabled_var_skipped(self) -> None:
        """enabled=false 的静态变量被跳过。"""
        plugin = PromptBuildPlugin()
        state = make_base_state()
        state["context.static_vars"] = [
            {"type": "content", "name": "跳过", "value": "不应出现", "enabled": False}
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "不应出现" not in content

    @pytest.mark.asyncio
    async def test_static_vars_disabled_by_config(self) -> None:
        """include_static_vars=false 时跳过所有静态变量。"""
        plugin = PromptBuildPlugin(config={"include_static_vars": False})
        state = make_base_state()
        state["context.static_vars"] = [
            {"type": "content", "name": "项目说明", "value": "不应出现"}
        ]
        ctx = make_ctx(state)

        result = await plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        assert "不应出现" not in content


# ══════════════════════════════════════════════════
# 4. tool_schema 默认行为
# ══════════════════════════════════════════════════


class TestToolSchemaDefault:
    """tool_schema 默认不写 prompt.tool_descriptions。"""

    @pytest.mark.asyncio
    async def test_no_tool_descriptions_by_default(self) -> None:
        """默认不生成 prompt.tool_descriptions。"""
        plugin = ToolSchemaPlugin()
        state = make_base_state()

        mock_registry = MagicMock()
        mock_tool = MagicMock()
        mock_tool.name = "echo"
        mock_tool.description = "回显工具"
        mock_tool.to_llm_format.return_value = {"type": "function", "function": {"name": "echo"}}
        mock_registry.list_all.return_value = [mock_tool]

        ctx = make_ctx(state, tool_registry=mock_registry)
        result = await plugin.execute(ctx)

        assert "tool_schemas" in result.state_updates
        assert "prompt.tool_descriptions" not in result.state_updates

    @pytest.mark.asyncio
    async def test_tool_descriptions_when_enabled(self) -> None:
        """include_tools_description_in_prompt=true 时生成 prompt.tool_descriptions。"""
        plugin = ToolSchemaPlugin(config={"include_tools_description_in_prompt": True})
        state = make_base_state()

        mock_registry = MagicMock()
        mock_tool = MagicMock()
        mock_tool.name = "echo"
        mock_tool.description = "回显工具"
        mock_tool.to_llm_format.return_value = {"type": "function", "function": {"name": "echo"}}
        mock_registry.list_all.return_value = [mock_tool]

        ctx = make_ctx(state, tool_registry=mock_registry)
        result = await plugin.execute(ctx)

        assert "tool_schemas" in result.state_updates
        assert "prompt.tool_descriptions" in result.state_updates
        assert "可用工具" in result.state_updates["prompt.tool_descriptions"]

    @pytest.mark.asyncio
    async def test_tool_schemas_always_written(self) -> None:
        """tool_schemas 始终写入（不受 include_tools_description_in_prompt 影响）。"""
        plugin_default = ToolSchemaPlugin()
        plugin_enabled = ToolSchemaPlugin(config={"include_tools_description_in_prompt": True})
        state = make_base_state()

        mock_registry = MagicMock()
        mock_tool = MagicMock()
        mock_tool.to_llm_format.return_value = {"type": "function", "function": {"name": "echo"}}
        mock_registry.list_all.return_value = [mock_tool]

        ctx1 = make_ctx(dict(state), tool_registry=mock_registry)
        ctx2 = make_ctx(dict(state), tool_registry=mock_registry)

        result_default = await plugin_default.execute(ctx1)
        result_enabled = await plugin_enabled.execute(ctx2)

        assert len(result_default.state_updates["tool_schemas"]) == 1
        assert len(result_enabled.state_updates["tool_schemas"]) == 1


# ══════════════════════════════════════════════════
# 5. LLMCore._build_messages 三来源组装
# ══════════════════════════════════════════════════


class TestLLMCoreBuildMessages:
    """LLMCore._build_messages 从三来源组装 messages。"""

    def test_system_message_first(self) -> None:
        """system_message 是 messages 的第一条。"""
        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = {
            "system_message": {"role": "system", "content": "你是一个助手。"},
            "messages": [{"role": "user", "content": "你好"}],
        }

        messages = core._build_messages(state)

        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "你是一个助手。"

    def test_history_in_middle(self) -> None:
        """历史消息在 system_message 之后、dynamic_vars 之前。"""
        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = {
            "system_message": {"role": "system", "content": "系统提示词"},
            "messages": [
                {"role": "user", "content": "问题1"},
                {"role": "assistant", "content": "回答1"},
                {"role": "user", "content": "问题2"},
            ],
            "prompt.dynamic_vars": "动态变量内容",
        }

        messages = core._build_messages(state)

        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert messages[2]["role"] == "assistant"
        assert messages[3]["role"] == "user"

    def test_dynamic_vars_last(self) -> None:
        """动态变量追加在历史消息之后（作为第二条 SystemMessage）。"""
        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = {
            "system_message": {"role": "system", "content": "系统提示词"},
            "messages": [{"role": "user", "content": "你好"}],
            "prompt.dynamic_vars": "- 日期: 2025-01-01\n- 时间: 12:00:00",
        }

        messages = core._build_messages(state)

        assert len(messages) == 3
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert messages[2]["role"] == "system"
        assert "日期" in messages[2]["content"]

    def test_no_dynamic_vars(self) -> None:
        """没有 dynamic_vars 时不追加额外的 SystemMessage。"""
        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = {
            "system_message": {"role": "system", "content": "系统提示词"},
            "messages": [{"role": "user", "content": "你好"}],
        }

        messages = core._build_messages(state)

        assert len(messages) == 2
        assert messages[-1]["role"] == "user"

    def test_empty_state(self) -> None:
        """state 为空时返回空列表。"""
        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        messages = core._build_messages({})
        assert messages == []

    def test_only_system_message(self) -> None:
        """只有 system_message 时返回单元素列表。"""
        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = {
            "system_message": {"role": "system", "content": "提示词"},
        }

        messages = core._build_messages(state)

        assert len(messages) == 1
        assert messages[0]["role"] == "system"


# ══════════════════════════════════════════════════
# 6. LLMCore messages 不累积 system/dynamic
# ══════════════════════════════════════════════════


class TestLLMCoreNoAccumulation:
    """验证 LLMCore 只将 assistant 回复追加到 state['messages']，不包含 system/dynamic。"""

    @pytest.mark.asyncio
    async def test_assistant_reply_appended_only(self) -> None:
        """LLM 普通文本回复只追加 assistant 消息到 messages。"""
        from llm.adapter import LLMResponse

        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = make_base_state()
        state["system_message"] = {"role": "system", "content": "提示词"}
        state["prompt.dynamic_vars"] = "动态变量"
        state["messages"] = [{"role": "user", "content": "你好"}]

        core._call_llm = AsyncMock(return_value=LLMResponse(
            text="你好！有什么可以帮你？", tool_calls=[], thinking_text=None
        ))

        ctx = make_ctx(state)
        result = await core.execute(ctx)

        updated_messages = result["messages"]
        assert len(updated_messages) == 2
        assert updated_messages[0]["role"] == "user"
        assert updated_messages[1]["role"] == "assistant"
        assert updated_messages[1]["content"] == "你好！有什么可以帮你？"

    @pytest.mark.asyncio
    async def test_tool_calls_appended_only(self) -> None:
        """LLM 工具调用只追加 assistant 消息（含 tool_calls）到 messages。"""
        from llm.adapter import LLMResponse

        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = make_base_state()
        state["system_message"] = {"role": "system", "content": "提示词"}
        state["messages"] = [{"role": "user", "content": "搜索天气"}]

        tool_calls = [{"id": "call_123", "name": "search", "args": '{"query": "天气"}'}]
        core._call_llm = AsyncMock(return_value=LLMResponse(
            text="", tool_calls=tool_calls, thinking_text=None
        ))

        ctx = make_ctx(state)
        result = await core.execute(ctx)

        updated_messages = result["messages"]
        assert len(updated_messages) == 2
        assert updated_messages[0]["role"] == "user"
        assert updated_messages[1]["role"] == "assistant"
        assert "tool_calls" in updated_messages[1]
        assert updated_messages[1]["tool_calls"][0]["function"]["name"] == "search"

    @pytest.mark.asyncio
    async def test_no_system_message_in_messages_after_llm(self) -> None:
        """LLM 执行后 state['messages'] 不包含 system_message。"""
        from llm.adapter import LLMResponse

        core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})
        state = make_base_state()
        state["system_message"] = {"role": "system", "content": "提示词"}
        state["prompt.dynamic_vars"] = "动态变量"
        state["messages"] = [{"role": "user", "content": "你好"}]

        core._call_llm = AsyncMock(return_value=LLMResponse(
            text="回复", tool_calls=[], thinking_text=None
        ))

        ctx = make_ctx(state)
        result = await core.execute(ctx)

        for msg in result["messages"]:
            assert msg["role"] != "system", "state['messages'] 不应包含 system 消息"


# ══════════════════════════════════════════════════
# 7. 完整链路集成测试
# ══════════════════════════════════════════════════


class TestFullAssemblyPipeline:
    """完整链路：prompt_build → LLMCore._build_messages。"""

    @pytest.mark.asyncio
    async def test_full_pipeline_system_memory_history_dynamic(self) -> None:
        """完整链路：system → knowledge → memory → history → dynamic_vars。"""
        prompt_plugin = PromptBuildPlugin(config={"include_static_vars": False, "include_compressed_layers": False})
        llm_core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})

        state = make_base_state()
        state["knowledge.context"] = "## 知识库\n项目使用 Python 开发"
        state["memory.retrieved"] = "## 记忆检索\n上次讨论了架构设计"
        state["messages"] = [
            {"role": "user", "content": "帮我设计架构"},
            {"role": "assistant", "content": "好的，让我分析一下..."},
        ]

        # Step 1: prompt_build 产出 system_message + dynamic_vars
        ctx = make_ctx(state)
        prompt_result = await prompt_plugin.execute(ctx)
        state.update(prompt_result.state_updates)

        # Step 2: LLMCore._build_messages 组装最终 messages
        final_messages = llm_core._build_messages(state)

        # 验证顺序: system → history → dynamic
        assert final_messages[0]["role"] == "system"
        assert "你是一个有用的 AI 助手" in final_messages[0]["content"]
        assert "知识库" in final_messages[0]["content"]
        assert "记忆检索" in final_messages[0]["content"]

        assert final_messages[1]["role"] == "user"
        assert final_messages[2]["role"] == "assistant"

        assert final_messages[3]["role"] == "system"
        assert "日期" in final_messages[3]["content"]

    @pytest.mark.asyncio
    async def test_full_pipeline_with_static_vars_and_compressed(self) -> None:
        """完整链路包含 static_vars 和压缩层。"""
        prompt_plugin = PromptBuildPlugin()
        llm_core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})

        state = make_base_state()
        state["context.static_vars"] = [
            {"type": "content", "name": "项目名", "value": "Agent OS"}
        ]

        chunk_service = MagicMock()
        chunk_service.find_by_session = AsyncMock(side_effect=lambda sid, layer: [
            ChunkData(content=f"{layer}内容", keywords=[f"kw_{layer}"])
        ])

        ctx = make_ctx(state, chunk_service=chunk_service)
        prompt_result = await prompt_plugin.execute(ctx)
        state.update(prompt_result.state_updates)

        final_messages = llm_core._build_messages(state)
        system_content = final_messages[0]["content"]

        assert "Agent OS" in system_content
        assert "关键词索引" in system_content
        assert "三元组摘要" in system_content
        assert "八段摘要" in system_content

    @pytest.mark.asyncio
    async def test_full_pipeline_tools_in_function_calling_mode(self) -> None:
        """完整链路：tools 走 function calling，不拼入 system_message。"""
        prompt_plugin = PromptBuildPlugin()
        tool_schema_plugin = ToolSchemaPlugin()
        llm_core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})

        state = make_base_state()
        state["messages"] = [{"role": "user", "content": "搜索天气"}]

        mock_registry = MagicMock()
        mock_tool = MagicMock()
        mock_tool.name = "search"
        mock_tool.description = "搜索工具"
        mock_tool.to_llm_format.return_value = {
            "type": "function",
            "function": {"name": "search", "parameters": {}},
        }
        mock_registry.list_all.return_value = [mock_tool]

        ctx = make_ctx(state, tool_registry=mock_registry)

        # tool_schema 写入 tool_schemas
        schema_result = await tool_schema_plugin.execute(ctx)
        state.update(schema_result.state_updates)

        # prompt_build 产出 system_message
        prompt_result = await prompt_plugin.execute(ctx)
        state.update(prompt_result.state_updates)

        # LLMCore._build_messages 组装
        final_messages = llm_core._build_messages(state)

        # system_message 不包含工具描述
        assert "可用工具" not in final_messages[0]["content"]
        assert "搜索工具" not in final_messages[0]["content"]

        # tool_schemas 在 state 中（供 LLM API 的 tools 参数使用）
        assert "tool_schemas" in state
        assert len(state["tool_schemas"]) == 1

    @pytest.mark.asyncio
    async def test_full_pipeline_tools_in_prompt_mode(self) -> None:
        """完整链路：配置开启时 tools 拼入 system_message。"""
        prompt_plugin = PromptBuildPlugin(config={"include_tools_description_in_prompt": True})
        tool_schema_plugin = ToolSchemaPlugin(config={"include_tools_description_in_prompt": True})
        llm_core = LLMCore(config={"provider": "openai", "model_name": "gpt-4"})

        state = make_base_state()

        mock_registry = MagicMock()
        mock_tool = MagicMock()
        mock_tool.name = "search"
        mock_tool.description = "搜索工具"
        mock_tool.to_llm_format.return_value = {
            "type": "function",
            "function": {"name": "search", "parameters": {}},
        }
        mock_registry.list_all.return_value = [mock_tool]

        ctx = make_ctx(state, tool_registry=mock_registry)

        schema_result = await tool_schema_plugin.execute(ctx)
        state.update(schema_result.state_updates)

        prompt_result = await prompt_plugin.execute(ctx)
        state.update(prompt_result.state_updates)

        final_messages = llm_core._build_messages(state)

        # system_message 包含工具描述
        assert "可用工具" in final_messages[0]["content"]
        assert "搜索工具" in final_messages[0]["content"]

    @pytest.mark.asyncio
    async def test_layer_order_matches_old_code(self) -> None:
        """验证 layer_order 与旧代码完全一致。"""
        prompt_plugin = PromptBuildPlugin(config={
            "include_tools_description_in_prompt": True,
            "include_static_vars": True,
            "include_compressed_layers": True,
        })
        state = make_base_state()
        state["prompt.tool_descriptions"] = "## 工具\n- search: 搜索"
        state["context.static_vars"] = [{"type": "content", "name": "项目", "value": "AgentOS"}]
        state["knowledge.context"] = "知识内容"
        state["memory.retrieved"] = "记忆内容"

        chunk_service = MagicMock()
        chunk_service.find_by_session = AsyncMock(side_effect=lambda sid, layer: [
            ChunkData(content=f"{layer}摘要", keywords=[f"kw_{layer}"])
        ])
        ctx = make_ctx(state, chunk_service=chunk_service)

        result = await prompt_plugin.execute(ctx)
        content = result.state_updates["system_message"]["content"]

        positions = {
            "system_prompt": content.index("AI 助手"),
            "tools": content.index("工具"),
            "static_vars": content.index("静态变量"),
            "knowledge": content.index("知识内容"),
            "memory": content.index("记忆内容"),
            "l3": content.index("关键词索引"),
            "l2": content.index("三元组摘要"),
            "l1": content.index("八段摘要"),
        }

        order = list(positions.values())
        assert order == sorted(order), (
            f"layer_order 不一致: {list(positions.keys())} -> {[positions[k] for k in positions]}"
        )
