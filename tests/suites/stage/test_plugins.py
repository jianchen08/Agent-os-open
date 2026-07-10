"""M6 全量插件单元测试。

覆盖 Input 插件（7 个，排除已测试的 pending_tools）和 Output 插件（8 个）。
每个插件至少 2 个测试：正常路径 + 边界/异常路径。

测试使用 Mock 的 PluginContext，不依赖真实 LLM 调用。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock


from pipeline.plugin import PluginContext, PluginResult
from pipeline.types import ErrorPolicy, StateKeys


# ──────────────────────────────────────────────
# 辅助工具
# ──────────────────────────────────────────────

def make_ctx(
    state: dict[str, Any] | None = None,
    services: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> PluginContext:
    """创建测试用 PluginContext。"""
    return PluginContext(
        state=state or {},
        config=config or {},
        _services=services or {},
    )


def run(coro: Any) -> Any:
    """运行异步协程（兼容已有事件循环场景）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        # 在已有事件循环中（如 pytest-asyncio），用新线程
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


# ══════════════════════════════════════════════
# Input 插件测试
# ══════════════════════════════════════════════

# ── ContextBuildPlugin ──────────────────────

class TestContextBuildPlugin:
    """上下文构建插件测试。"""

    def test_normal_build(self) -> None:
        """正常构建上下文 — 配置完整时所有字段正确写入 state。"""
        from plugins.input.context_build import ContextBuildPlugin

        plugin = ContextBuildPlugin(config={
            "system_prompt": "你是助手",
            "agent_name": "test_agent",
            "agent_level": "l2_subtask",
            "extra_context": {"project": "AgentOS"},
        })
        ctx = make_ctx(state={
            StateKeys.SESSION_ID: "sess-001",
            StateKeys.TASK_ID: "task-001",
            StateKeys.ITERATION: 3,
            StateKeys.CORE_TYPE: "llm_call",
        })
        result = run(plugin.execute(ctx))

        assert isinstance(result, PluginResult)
        su = result.state_updates
        assert su["context.system_prompt"] == "你是助手"
        assert su["context.agent_name"] == "test_agent"
        assert su["context.agent_level"] == "l2_subtask"
        assert su["context.session_id"] == "sess-001"
        assert su["context.task_id"] == "task-001"
        assert su["context.iteration"] == 3
        assert su["context.is_tool_execution"] is False
        assert su["context.is_project"] is False  # l2_subtask ≠ l1_main
        assert su["context.project"] == "AgentOS"

    def test_empty_config_defaults(self) -> None:
        """边界 — 无配置时使用默认值。"""
        from plugins.input.context_build import ContextBuildPlugin

        plugin = ContextBuildPlugin()
        ctx = make_ctx(state={
            StateKeys.CORE_TYPE: "tool_execute",
        })
        result = run(plugin.execute(ctx))
        su = result.state_updates

        assert su["context.system_prompt"] == ""
        assert su["context.agent_name"] == ""
        assert su["context.agent_level"] == "l1_main"
        assert su["context.is_tool_execution"] is True
        assert su["context.is_project"] is True

    def test_fallback_state_and_error_policy(self) -> None:
        """验证 error_policy 和 fallback_state 属性。"""
        from plugins.input.context_build import ContextBuildPlugin

        plugin = ContextBuildPlugin()
        assert plugin.error_policy == ErrorPolicy.FALLBACK
        assert "context.system_prompt" in plugin.fallback_state


# ── KnowledgeInjectPlugin ───────────────────

class TestKnowledgeInjectPlugin:
    """知识注入插件测试（M11b 真实版）。"""

    def test_disabled_mode_returns_empty(self) -> None:
        """disabled 模式 — 返回空知识内容。"""
        from plugins.input.knowledge_inject import KnowledgeInjectPlugin

        plugin = KnowledgeInjectPlugin(config={"mode": "disabled"})
        ctx = make_ctx(state={"user_message": "测试"})
        result = run(plugin.execute(ctx))

        assert result.state_updates["knowledge.context"] == ""

    def test_no_semantic_storage_service(self) -> None:
        """边界 — 无语义存储服务时降级返回空。"""
        from plugins.input.knowledge_inject import KnowledgeInjectPlugin

        plugin = KnowledgeInjectPlugin(config={"mode": "full"})
        ctx = make_ctx(state={"user_message": "测试"}, services={})
        result = run(plugin.execute(ctx))

        assert result.state_updates["knowledge.context"] == ""

    def test_full_mode_with_mock_service(self) -> None:
        """full 模式 — Mock 语义存储服务返回结果。"""
        from plugins.input.knowledge_inject import KnowledgeInjectPlugin
        from memory.types import Knowledge

        mock_items = [
            Knowledge(user_id="", content="知识内容A", source_type="test"),
        ]
        mock_storage = AsyncMock()
        mock_storage.find_by_user = AsyncMock(return_value=mock_items)

        plugin = KnowledgeInjectPlugin(config={"mode": "full", "max_tokens": 5000})
        ctx = make_ctx(
            state={"user_message": "查询", "user_id": "user-1"},
            services={"semantic_storage": mock_storage},
        )
        result = run(plugin.execute(ctx))
        content = result.state_updates["knowledge.context"]

        assert "知识内容A" in content
        mock_storage.find_by_user.assert_awaited_once()

    def test_no_user_message_skips_retrieval(self) -> None:
        """边界 — 无用户消息时跳过检索。"""
        from plugins.input.knowledge_inject import KnowledgeInjectPlugin

        mock_storage = AsyncMock()
        plugin = KnowledgeInjectPlugin(config={"mode": "full"})
        ctx = make_ctx(state={}, services={"semantic_storage": mock_storage})
        result = run(plugin.execute(ctx))

        assert result.state_updates["knowledge.context"] == ""
        mock_storage.find_by_user.assert_not_awaited()


# ── ParamInjectPlugin ───────────────────────

class TestParamInjectPlugin:
    """参数注入插件测试。"""

    def test_inject_for_tool_execute(self) -> None:
        """正常 — 工具执行时注入 session_id 和 timestamp。"""
        from plugins.input.param_inject import ParamInjectPlugin

        plugin = ParamInjectPlugin()
        ctx = make_ctx(state={
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [
                {"name": "search", "args": {"query": "test"}},
            ],
            StateKeys.SESSION_ID: "sess-123",
            "user_id": "user-456",
        })
        result = run(plugin.execute(ctx))
        su = result.state_updates

        assert su["tool.params_injected"] is True
        injected_tc = su[StateKeys.RAW_TOOL_CALLS][0]
        assert injected_tc["args"]["session_id"] == "sess-123"
        assert injected_tc["args"]["user_id"] == "user-456"
        assert "timestamp" in injected_tc["args"]

    def test_skip_for_llm_call(self) -> None:
        """边界 — LLM 调用时标记不注入。"""
        from plugins.input.param_inject import ParamInjectPlugin

        plugin = ParamInjectPlugin()
        ctx = make_ctx(state={StateKeys.CORE_TYPE: "llm_call"})
        result = run(plugin.execute(ctx))

        assert result.state_updates["tool.params_injected"] is False

    def test_default_params_inject(self) -> None:
        """边界 — 工具默认参数注入（仅当参数不存在时）。"""
        from plugins.input.param_inject import ParamInjectPlugin

        plugin = ParamInjectPlugin(config={
            "default_params": {
                "search": {"limit": 10, "offset": 0},
            },
            "inject_session_id": False,
            "inject_user_id": False,
            "inject_timestamp": False,
        })
        ctx = make_ctx(state={
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [
                {"name": "search", "args": {"query": "test", "limit": 5}},
            ],
        })
        result = run(plugin.execute(ctx))
        injected_tc = result.state_updates[StateKeys.RAW_TOOL_CALLS][0]

        assert injected_tc["args"]["limit"] == 5  # 已有参数不被覆盖
        assert injected_tc["args"]["offset"] == 0  # 新增默认参数


# ── PromptBuildPlugin ───────────────────────

class TestPromptBuildPlugin:
    """提示词构建插件测试。"""

    def test_build_system_and_messages(self) -> None:
        """正常 — 构建系统消息。"""
        from plugins.input.prompt_build import PromptBuildPlugin

        plugin = PromptBuildPlugin()
        ctx = make_ctx(state={
            "context.system_prompt": "你是测试助手",
            "user_message": "你好",
            "history": [{"role": "user", "content": "历史消息"}],
        })
        result = run(plugin.execute(ctx))
        su = result.state_updates

        assert "system_message" in su
        assert su["system_message"]["role"] == "system"
        assert "你是测试助手" in su["system_message"]["content"]

    def test_empty_state_no_crash(self) -> None:
        """边界 — 空 state 不崩溃。"""
        from plugins.input.prompt_build import PromptBuildPlugin

        plugin = PromptBuildPlugin()
        ctx = make_ctx(state={})
        result = run(plugin.execute(ctx))
        su = result.state_updates

        assert "system_message" in su
        assert su["system_message"]["role"] == "system"
        # 空 state 时 system_message 的 content 为空字符串
        assert "prompt.dynamic_vars" in su

    def test_constraints_included(self) -> None:
        """约束规则 — 硬约束和软约束通过 system_prompt 传入。"""
        from plugins.input.prompt_build import PromptBuildPlugin

        plugin = PromptBuildPlugin()
        ctx = make_ctx(state={"context.system_prompt": "助手\n禁止执行危险操作\n尽量简洁"})
        result = run(plugin.execute(ctx))
        system_msg = result.state_updates["system_message"]

        assert "禁止执行危险操作" in system_msg["content"]
        assert "尽量简洁" in system_msg["content"]


# ── ReasoningCheckPlugin ────────────────────

class TestReasoningCheckPlugin:
    """推理检查插件测试。"""

    def test_passed_when_no_issues(self) -> None:
        """正常 — 无推理风险时通过。"""
        from plugins.input.reasoning_check import ReasoningCheckPlugin

        plugin = ReasoningCheckPlugin()
        ctx = make_ctx(state={
            StateKeys.CORE_TYPE: "llm_call",
            StateKeys.RAW_RESULT: "这是一个简单的回复。",
        })
        result = run(plugin.execute(ctx))
        check = result.state_updates["reasoning.check_result"]

        assert check["passed"] is True

    def test_too_many_steps(self) -> None:
        """边界 — 推理步数超限。"""
        from plugins.input.reasoning_check import ReasoningCheckPlugin

        # 构造超过阈值的推理文本
        steps = " ".join(f"步骤{i}" for i in range(25))
        plugin = ReasoningCheckPlugin(config={"max_reasoning_steps": 5})
        ctx = make_ctx(state={
            StateKeys.CORE_TYPE: "llm_call",
            StateKeys.RAW_RESULT: steps,
        })
        result = run(plugin.execute(ctx))
        check = result.state_updates["reasoning.check_result"]

        assert check["passed"] is False
        assert "Too many reasoning steps" in check["reason"]

    def test_disabled_bypasses_check(self) -> None:
        """禁用时直接通过。"""
        from plugins.input.reasoning_check import ReasoningCheckPlugin

        plugin = ReasoningCheckPlugin(config={"enabled": False})
        ctx = make_ctx(state={
            StateKeys.CORE_TYPE: "llm_call",
            StateKeys.RAW_RESULT: "步骤1 步骤2 步骤3",
        })
        result = run(plugin.execute(ctx))
        check = result.state_updates["reasoning.check_result"]

        assert check["passed"] is True
        assert check["reason"] == "disabled"


# ── SecurityCheckPlugin ─────────────────────

class TestSecurityCheckPlugin:
    """安全检查插件测试。"""

    def test_allow_llm_call(self) -> None:
        """正常 — LLM 调用不需要安全检查。"""
        from plugins.input.security_check import SecurityCheckPlugin

        plugin = SecurityCheckPlugin()
        ctx = make_ctx(state={StateKeys.CORE_TYPE: "llm_call"})
        result = run(plugin.execute(ctx))
        decision = result.state_updates["security.decision"]

        assert decision["allowed"] is True

    def test_block_dangerous_command(self) -> None:
        """边界 — 拦截危险命令。"""
        from plugins.input.security_check import SecurityCheckPlugin

        plugin = SecurityCheckPlugin()
        ctx = make_ctx(state={
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [
                {"name": "shell", "args": {"command": "rm -rf /"}},
            ],
        })
        result = run(plugin.execute(ctx))
        decision = result.state_updates["security.decision"]

        assert decision["allowed"] is False
        assert "dangerous" in decision["reason"].lower()

    def test_block_protected_path(self) -> None:
        """边界 — 拦截受保护路径访问。"""
        from plugins.input.security_check import SecurityCheckPlugin

        plugin = SecurityCheckPlugin()
        ctx = make_ctx(state={
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [
                {"name": "file_read", "args": {"path": "/etc/passwd"}},
            ],
        })
        result = run(plugin.execute(ctx))
        decision = result.state_updates["security.decision"]

        assert decision["allowed"] is False
        assert "protected_paths" in decision["reason"]

    def test_disabled_allows_all(self) -> None:
        """禁用时允许所有操作。"""
        from plugins.input.security_check import SecurityCheckPlugin

        plugin = SecurityCheckPlugin(config={"enabled": False})
        ctx = make_ctx(state={
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.RAW_TOOL_CALLS: [
                {"name": "shell", "args": {"command": "rm -rf /"}},
            ],
        })
        result = run(plugin.execute(ctx))
        decision = result.state_updates["security.decision"]

        assert decision["allowed"] is True


# ── ToolSchemaPlugin ────────────────────────

class TestToolSchemaPlugin:
    """工具 Schema 注入插件测试。"""

    def test_inject_schemas_from_registry(self) -> None:
        """正常 — 从工具注册表注入 Schema。"""
        from plugins.input.tool_schema import ToolSchemaPlugin

        mock_tool = MagicMock()
        mock_tool.name = "search"
        mock_tool.description = "搜索工具"
        mock_tool.input_schema = {"type": "object", "properties": {"q": {"type": "string"}}}
        mock_tool.to_llm_format.return_value = {
            "type": "function",
            "function": {
                "name": "search",
                "description": "搜索工具",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }

        mock_registry = MagicMock()
        mock_registry.list_all.return_value = [mock_tool]

        plugin = ToolSchemaPlugin()
        ctx = make_ctx(
            state={},
            services={"tool_registry": mock_registry},
        )
        result = run(plugin.execute(ctx))
        su = result.state_updates

        assert len(su["tool_schemas"]) == 1
        assert su["tool_schemas"][0]["type"] == "function"
        assert su["tool_schemas"][0]["function"]["name"] == "search"

    def test_no_registry_returns_empty(self) -> None:
        """边界 — 无工具注册表时返回空。"""
        from plugins.input.tool_schema import ToolSchemaPlugin

        plugin = ToolSchemaPlugin()
        ctx = make_ctx(state={}, services={})
        result = run(plugin.execute(ctx))

        assert result.state_updates["tool_schemas"] == []
        # prompt.tool_descriptions 不写入（默认 include_tools_description_in_prompt=False）
        assert "prompt.tool_descriptions" not in result.state_updates

    def test_disabled_returns_empty(self) -> None:
        """禁用时返回空 Schema。"""
        from plugins.input.tool_schema import ToolSchemaPlugin

        plugin = ToolSchemaPlugin(config={"enabled": False})
        ctx = make_ctx(state={})
        result = run(plugin.execute(ctx))

        assert result.state_updates["tool_schemas"] == []


# ══════════════════════════════════════════════
# Output 插件测试
# ══════════════════════════════════════════════

# ── DuplicateCheckPlugin ────────────────────

class TestDuplicateCheckPlugin:
    """重复检查插件测试。"""

    def test_no_duplicate_no_signal(self) -> None:
        """正常 — 无重复时不产出路由信号。"""
        from plugins.output.duplicate_check import DuplicateCheckPlugin

        plugin = DuplicateCheckPlugin()
        ctx = make_ctx(state={
            StateKeys.RAW_TOOL_CALLS: [{"name": "search", "args": {"q": "test"}}],
            StateKeys.RAW_RESULT: "搜索结果A",
        })
        result = run(plugin.execute(ctx))

        assert result.route_signal is None
        assert result.state_updates["router.duplicate_count"] == 0

    def test_duplicate_calls_exceed_limit(self) -> None:
        """边界 — 工具调用重复超限产出 end 信号。

        逻辑：duplicate_count 在每次检测到重复时 +1，当 count > max 时触发 end。
        max=1 → count=2 时触发（第二次重复）。
        """
        from plugins.output.duplicate_check import DuplicateCheckPlugin

        plugin = DuplicateCheckPlugin(config={"max_duplicate_calls": 1})
        tool_call = {"name": "search", "args": {"q": "test"}}

        # 第一次执行 — 无上次签名，count=0（不同则重置）
        ctx1 = make_ctx(state={
            StateKeys.RAW_TOOL_CALLS: [tool_call],
            StateKeys.RAW_RESULT: "结果",
        })
        result1 = run(plugin.execute(ctx1))

        # 第二次执行 — 相同签名，count=1
        ctx2 = make_ctx(state={
            StateKeys.RAW_TOOL_CALLS: [tool_call],
            StateKeys.RAW_RESULT: "结果",
            **result1.state_updates,
        })
        result2 = run(plugin.execute(ctx2))

        # 第三次执行 — 相同签名，count=2 > max(1) → end
        ctx3 = make_ctx(state={
            StateKeys.RAW_TOOL_CALLS: [tool_call],
            StateKeys.RAW_RESULT: "结果",
            **result2.state_updates,
        })
        result3 = run(plugin.execute(ctx3))

        assert result3.route_signal is not None
        assert result3.route_signal.route_type == "end"

    def test_similarity_check(self) -> None:
        """边界 — 高相似度输出计入重复。"""
        from plugins.output.duplicate_check import DuplicateCheckPlugin

        plugin = DuplicateCheckPlugin(config={
            "max_repetitive_output": 1,
            "similarity_threshold": 0.5,
        })

        # 两次非常相似的输出
        text1 = "The quick brown fox jumps over the lazy dog and runs away"
        text2 = "The quick brown fox jumps over the lazy dog and runs fast"

        ctx1 = make_ctx(state={StateKeys.RAW_RESULT: text1})
        result1 = run(plugin.execute(ctx1))

        ctx2 = make_ctx(state={
            StateKeys.RAW_RESULT: text2,
            **result1.state_updates,
        })
        result2 = run(plugin.execute(ctx2))

        # 相似度应该触发重复计数
        assert result2.state_updates["router.repetitive_count"] >= 1


# ── ErrorCheckPlugin ────────────────────────

class TestErrorCheckPlugin:
    """错误检查插件测试。"""

    def test_no_error_success(self) -> None:
        """正常 — 无错误时返回成功状态。"""
        from plugins.output.error_check import ErrorCheckPlugin

        plugin = ErrorCheckPlugin()
        ctx = make_ctx(state={
            StateKeys.RAW_RESULT: "正常回复",
            StateKeys.RAW_ERROR: None,
        })
        result = run(plugin.execute(ctx))

        assert result.route_signal is None
        assert result.state_updates[StateKeys.EXECUTION_STATUS] == "success"

    def test_retryable_error_next_llm(self) -> None:
        """可重试错误 — 产出 next_llm 信号。"""
        from plugins.output.error_check import ErrorCheckPlugin

        plugin = ErrorCheckPlugin(config={"max_retries": 3})
        ctx = make_ctx(state={
            StateKeys.RAW_ERROR: "Connection timeout",
            "retry.count": 0,
        })
        result = run(plugin.execute(ctx))

        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"
        assert result.state_updates["retry.count"] == 1

    def test_non_retryable_error_end(self) -> None:
        """不可重试错误 — 产出 end 信号。"""
        from plugins.output.error_check import ErrorCheckPlugin

        plugin = ErrorCheckPlugin()
        ctx = make_ctx(state={
            StateKeys.RAW_ERROR: "Invalid API key provided",
            "retry.count": 0,
        })
        result = run(plugin.execute(ctx))

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"

    def test_empty_response_retry(self) -> None:
        """空响应 — 触发重试。"""
        from plugins.output.error_check import ErrorCheckPlugin

        plugin = ErrorCheckPlugin(config={"max_retries": 2})
        ctx = make_ctx(state={
            StateKeys.RAW_RESULT: "",
            StateKeys.RAW_ERROR: None,
            "retry.count": 0,
        })
        result = run(plugin.execute(ctx))

        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"

    def test_max_retries_exhausted(self) -> None:
        """边界 — 重试用尽后产出 end 信号。"""
        from plugins.output.error_check import ErrorCheckPlugin

        plugin = ErrorCheckPlugin(config={"max_retries": 2})
        ctx = make_ctx(state={
            StateKeys.RAW_ERROR: "Connection timeout",
            "retry.count": 2,
        })
        result = run(plugin.execute(ctx))

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"


# ── PersistPlugin 已移除（功能合并到 TrackPlugin） ──
# ── MemoryWritePlugin 已废弃移除 ──

# ── ResultFormatPlugin ──────────────────────

class TestResultFormatPlugin:
    """结果格式化插件测试。"""

    def test_format_tool_success_result(self) -> None:
        """正常 — 格式化工具成功结果。"""
        from plugins.output.result_format import ResultFormatPlugin

        plugin = ResultFormatPlugin()
        ctx = make_ctx(state={
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.TOOL_RESULTS: [
                {"name": "search", "success": True, "result": "搜索结果"},
            ],
        })
        result = run(plugin.execute(ctx))
        formatted = result.state_updates["tool.formatted_results"]

        assert len(formatted) == 1
        assert formatted[0]["role"] == "tool"
        assert "[search]" in formatted[0]["content"]
        assert "搜索结果" in formatted[0]["content"]

    def test_format_tool_error_result(self) -> None:
        """边界 — 格式化工具错误结果。"""
        from plugins.output.result_format import ResultFormatPlugin

        plugin = ResultFormatPlugin()
        ctx = make_ctx(state={
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.TOOL_RESULTS: [
                {"name": "search", "success": False, "error": "超时"},
            ],
        })
        result = run(plugin.execute(ctx))
        formatted = result.state_updates["tool.formatted_results"]

        assert "[search]" in formatted[0]["content"]
        assert "Error" in formatted[0]["content"]
        assert "超时" in formatted[0]["content"]

    def test_skip_for_llm_call(self) -> None:
        """LLM 调用时跳过格式化。"""
        from plugins.output.result_format import ResultFormatPlugin

        plugin = ResultFormatPlugin()
        ctx = make_ctx(state={StateKeys.CORE_TYPE: "llm_call"})
        result = run(plugin.execute(ctx))

        assert result.state_updates == {}

    def test_truncation(self) -> None:
        """边界 — 长结果截断。"""
        from plugins.output.result_format import ResultFormatPlugin

        plugin = ResultFormatPlugin(config={"max_result_length": 50})
        long_result = "A" * 200
        ctx = make_ctx(state={
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.TOOL_RESULTS: [
                {"name": "tool", "success": True, "result": long_result},
            ],
        })
        result = run(plugin.execute(ctx))
        content = result.state_updates["tool.formatted_results"][0]["content"]

        # 截断后的内容应小于原始长度
        assert len(content) < 200 + len("[tool] ")


# ── StopCheckPlugin ─────────────────────────

class TestStopCheckPlugin:
    """停止检查插件测试。"""

    def test_should_stop_by_user(self) -> None:
        """用户请求停止 — 产出 end 信号。"""
        from plugins.output.stop_check import StopCheckPlugin

        plugin = StopCheckPlugin()
        ctx = make_ctx(state={StateKeys.SHOULD_STOP: True})
        result = run(plugin.execute(ctx))

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        assert result.state_updates["router.stop_reason"] == "user_requested"

    def test_max_iterations_exceeded(self) -> None:
        """边界 — 迭代超限产出 end 信号。"""
        from plugins.output.stop_check import StopCheckPlugin

        plugin = StopCheckPlugin(config={"max_iterations": 5})
        ctx = make_ctx(state={StateKeys.ITERATION: 6})
        result = run(plugin.execute(ctx))

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        assert result.state_updates["router.stop_reason"] == "max_iterations"

    def test_no_stop_condition(self) -> None:
        """正常 — 无停止条件时继续。"""
        from plugins.output.stop_check import StopCheckPlugin

        plugin = StopCheckPlugin(config={"max_iterations": 20, "max_duration_seconds": 600})
        ctx = make_ctx(state={
            StateKeys.SHOULD_STOP: False,
            StateKeys.ITERATION: 5,
        })
        result = run(plugin.execute(ctx))

        assert result.route_signal is None
        assert result.state_updates["router.stop_reason"] == ""

    def test_task_canceled(self) -> None:
        """边界 — 任务被取消产出 end 信号。"""
        from plugins.output.stop_check import StopCheckPlugin

        plugin = StopCheckPlugin()
        ctx = make_ctx(state={
            StateKeys.SHOULD_STOP: False,
            StateKeys.ITERATION: 0,
            "task_status": "canceled",
        })
        result = run(plugin.execute(ctx))

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"


# ── TaskEvaluationPlugin ────────────────────

class TestTaskEvaluationPlugin:
    """任务评估插件测试。"""

    def test_completion_indicator_detected(self) -> None:
        """正常 — 检测到完成指示词产出 end 信号。"""
        from plugins.output.task_evaluation import TaskEvaluationPlugin

        plugin = TaskEvaluationPlugin()
        ctx = make_ctx(state={
            StateKeys.RAW_RESULT: "任务完成，所有步骤已执行。",
        })
        result = run(plugin.execute(ctx))

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        assert result.state_updates["evaluation.triggered"] is True

    def test_no_completion_no_signal(self) -> None:
        """正常 — 无完成指示时不产出信号。"""
        from plugins.output.task_evaluation import TaskEvaluationPlugin

        plugin = TaskEvaluationPlugin()
        ctx = make_ctx(state={
            StateKeys.RAW_RESULT: "还在处理中...",
        })
        result = run(plugin.execute(ctx))

        assert result.route_signal is None
        assert result.state_updates["evaluation.triggered"] is False

    def test_disabled_no_evaluation(self) -> None:
        """禁用时不触发评估。"""
        from plugins.output.task_evaluation import TaskEvaluationPlugin

        plugin = TaskEvaluationPlugin(config={"enabled": False})
        ctx = make_ctx(state={
            StateKeys.RAW_RESULT: "任务完成",
        })
        result = run(plugin.execute(ctx))

        assert result.state_updates["evaluation.triggered"] is False

    def test_metrics_passed(self) -> None:
        """边界 — 评估指标通过产出 end 信号。"""
        from plugins.output.task_evaluation import TaskEvaluationPlugin

        plugin = TaskEvaluationPlugin(config={
            "evaluation_metrics": ["accuracy"],
            "auto_evaluate": True,
        })
        ctx = make_ctx(state={
            StateKeys.RAW_RESULT: "中间结果",
            "evaluation.result": {"passed": True},
        })
        result = run(plugin.execute(ctx))

        assert result.route_signal is not None
        assert result.state_updates["evaluation.reason"] == "metrics_passed"


# ── TrackPlugin ─────────────────────────────

class TestTrackPlugin:
    """追踪统计插件测试。"""

    def test_collect_usage_and_stats(self) -> None:
        """正常 — 收集 token 用量和执行统计。"""
        from plugins.output.track import TrackPlugin

        plugin = TrackPlugin()
        ctx = make_ctx(state={
            StateKeys.ITERATION: 3,
            StateKeys.CORE_TYPE: "llm_call",
            StateKeys.EXECUTION_STATUS: "success",
            "llm_usage": {"input_tokens": 100, "output_tokens": 50},
        })
        result = run(plugin.execute(ctx))
        su = result.state_updates

        assert su["track.llm_usage"]["last_input_tokens"] == 100
        assert su["track.llm_usage"]["last_output_tokens"] == 50
        assert su["track.execution_stats"]["iteration"] == 3

    def test_accumulated_token_usage(self) -> None:
        """边界 — 跨迭代累加 token 用量。"""
        from plugins.output.track import TrackPlugin

        plugin = TrackPlugin()
        ctx = make_ctx(state={
            StateKeys.ITERATION: 2,
            StateKeys.CORE_TYPE: "llm_call",
            StateKeys.EXECUTION_STATUS: "success",
            "llm_usage": {"input_tokens": 50, "output_tokens": 25},
            "track.llm_usage": {
                "total_input_tokens": 100,
                "total_output_tokens": 50,
            },
        })
        result = run(plugin.execute(ctx))
        usage = result.state_updates["track.llm_usage"]

        assert usage["total_input_tokens"] == 150
        assert usage["total_output_tokens"] == 75

    def test_disabled_returns_empty(self) -> None:
        """禁用时返回空更新。"""
        from plugins.output.track import TrackPlugin

        plugin = TrackPlugin(config={"enabled": False})
        ctx = make_ctx(state={})
        result = run(plugin.execute(ctx))

        assert result.state_updates == {}
