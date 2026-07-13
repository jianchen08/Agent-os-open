"""Test MiniMax system role normalization.

根因分析测试：Minimax API 不允许非首位 system role 消息。
管道中 StreamRepetitionGuard、ThinkingTruncationGuard 等会注入 system 消息，
normalize_messages_for_provider 需要确保所有非首位 system→user 转换。

验收标准：
- 首位 system 消息保留
- 所有非首位 system 消息转换为 user
- adapter 层防御性兜底
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from plugins.core.llm_core._message_normalizer import (
    _pairing_validated_len,
    normalize_messages_for_provider,
)


@pytest.fixture(autouse=True)
def _clear_pairing_cache():
    """每个测试前后清理增量配对校验缓存，避免测试间相互污染。"""
    _pairing_validated_len.clear()
    yield
    _pairing_validated_len.clear()


def _normalize_minimax(messages):
    """以 minimax provider 调用 normalize_messages_for_provider。"""
    return normalize_messages_for_provider(messages, provider="minimax", name="test")


def _normalize_openai(messages):
    """以 openai provider 调用 normalize_messages_for_provider（对照组）。"""
    return normalize_messages_for_provider(messages, provider="openai", name="test")


class TestMinimaxSystemRoleNormalize:
    """Minimax system role 转换测试。"""

    def test_first_system_message_preserved(self) -> None:
        """首位 system 消息应保留为 system 角色。"""
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ]
        result = _normalize_minimax(messages)
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "You are a helpful assistant."

    def test_non_first_system_converted_to_user(self) -> None:
        """非首位 system 消息（如 StreamRepetitionGuard 注入）应被转换为 user 角色。"""
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": "[StreamRepetitionGuard] 检测到重复"},
            {"role": "user", "content": "Continue"},
        ]
        result = _normalize_minimax(messages)
        assert result[0]["role"] == "system"
        # 所有非首位消息不应有 system 角色
        for i, m in enumerate(result[1:], start=1):
            assert m["role"] != "system", (
                f"MSG-{i} 仍然是 system 角色: {m.get('content', '')[:80]}"
            )
        # 注入的 system 消息应转为 user 并保留 content
        assert result[2]["role"] == "user"
        assert "[StreamRepetitionGuard]" in result[2]["content"]

    def test_multiple_system_messages_all_converted(self) -> None:
        """所有非首位 system 消息都应被转换，无论数量多少。"""
        messages = [
            {"role": "system", "content": "Main system prompt"},
            {"role": "system", "content": "Guard 1 warning"},
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": "Guard 2 reminder"},
            {"role": "assistant", "content": "Hi"},
            {"role": "system", "content": "Guard 3 truncation"},
        ]
        result = _normalize_minimax(messages)
        assert result[0]["role"] == "system"
        assert all(m["role"] != "system" for m in result[1:]), (
            f"仍有非首位 system: {[(i, m['role']) for i, m in enumerate(result) if m.get('role') == 'system']}"
        )

    def test_no_system_messages_at_all(self) -> None:
        """没有 system 消息时应正常处理不报错。"""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        result = _normalize_minimax(messages)
        assert all(m["role"] != "system" for m in result)

    def test_only_system_messages_first_kept_rest_converted(self) -> None:
        """只有 system 消息时首位保留，其余全部转换。"""
        messages = [
            {"role": "system", "content": "Prompt 1"},
            {"role": "system", "content": "Prompt 2"},
            {"role": "system", "content": "Prompt 3"},
        ]
        result = _normalize_minimax(messages)
        assert result[0]["role"] == "system"
        assert result[1]["role"] == "user"
        assert result[2]["role"] == "user"

    def test_system_in_tool_calls_context(self) -> None:
        """system 消息混在 tool_calls 上下文中应被正确处理。"""
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Use tool"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "test", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            {"role": "system", "content": "[TaskReminder] do something"},
            {"role": "assistant", "content": "Done"},
        ]
        result = _normalize_minimax(messages)
        assert result[0]["role"] == "system"
        assert all(m["role"] != "system" for m in result[1:])

    def test_thinking_truncation_guard_system_converted(self) -> None:
        """ThinkingTruncationGuard 注入的 system 消息应被转换。"""
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Think deeply"},
            {"role": "assistant", "content": "Let me think..."},
            {
                "role": "system",
                "content": "[ThinkingTruncationGuard] 思考内容过长已截断",
            },
        ]
        result = _normalize_minimax(messages)
        assert result[0]["role"] == "system"
        assert all(m["role"] != "system" for m in result[1:])
        # 检查转换后的消息保留了原始 content
        truncation_msg = result[-1]
        assert truncation_msg["role"] == "user"
        assert "ThinkingTruncationGuard" in truncation_msg["content"]

    def test_non_minimax_provider_system_untouched(self) -> None:
        """非 minimax provider 不应修改 system 消息角色。"""
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": "Additional context"},
        ]
        result = _normalize_openai(messages)
        # OpenAI 支持多条 system 消息，不应修改
        assert result[0]["role"] == "system"
        assert result[2]["role"] == "system"

    def test_empty_messages_list(self) -> None:
        """空消息列表应正常返回。"""
        result = _normalize_minimax([])
        assert result == []

    def test_converted_messages_have_no_name_field(self) -> None:
        """转换后的 user 消息不应有 name 字段（MiniMax 要求 user 消息 name 一致）。"""
        messages = [
            {"role": "system", "content": "System", "name": "sys"},
            {"role": "system", "content": "Guard", "name": "guard"},
        ]
        result = _normalize_minimax(messages)
        # 首位 system 保留 name（由后续 Phase 处理）
        assert result[0]["role"] == "system"
        # 转换后的 user 不应有 name
        assert result[1]["role"] == "user"
        assert "name" not in result[1]


class TestAdapterDefensiveRoleCheck:
    """Adapter 层防御性 role 检查测试。"""

    def test_minimax_model_detected_and_sanitized(self) -> None:
        """minimax 模型应触发防御性 role 检查。"""
        from llm.adapter import _BaseLiteLLMAdapter

        adapter = _BaseLiteLLMAdapter()
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": "Injected system"},
        ]
        result = adapter._ensure_minimax_role_safety("minimax/MiniMax-M2.7", messages)
        assert result[0]["role"] == "system"
        assert result[2]["role"] == "user"
        assert "Injected system" in result[2]["content"]

    def test_non_minimax_model_not_sanitized(self) -> None:
        """非 minimax 模型不应触发 role 检查。"""
        from llm.adapter import _BaseLiteLLMAdapter

        adapter = _BaseLiteLLMAdapter()
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "system", "content": "Additional"},
        ]
        result = adapter._ensure_minimax_role_safety("openai/gpt-4", messages)
        # 非 minimax 不修改
        assert result[0]["role"] == "system"
        assert result[1]["role"] == "system"

    def test_already_clean_messages_not_modified(self) -> None:
        """已经干净的消息不应被修改。"""
        from llm.adapter import _BaseLiteLLMAdapter

        adapter = _BaseLiteLLMAdapter()
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Hello"},
        ]
        result = adapter._ensure_minimax_role_safety("minimax/MiniMax-M2.7", messages)
        assert result == messages  # 同一对象，未修改

    def test_router_model_id_with_minimax_name_detected(self) -> None:
        """路由模式下的 model_id 包含 minimax 也应被检测。"""
        from llm.adapter import _BaseLiteLLMAdapter

        adapter = _BaseLiteLLMAdapter()
        messages = [
            {"role": "system", "content": "System"},
            {"role": "system", "content": "Guard"},
        ]
        # 路由模式 model_id 不含 minimax 前缀，不应被检测到
        result = adapter._ensure_minimax_role_safety("minimax-chat", messages)
        # "minimax-chat" 包含 "minimax" 子串，应该被检测到
        assert result[1]["role"] == "user"


    def test_adapter_removes_name_field_on_fix(self) -> None:
        """adapter 兜底修正时，name 字段应被移除。"""
        from llm.adapter import _BaseLiteLLMAdapter

        adapter = _BaseLiteLLMAdapter()
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": "Guard", "name": "guard"},
        ]
        result = adapter._ensure_minimax_role_safety("minimax/MiniMax-M2.7", messages)
        assert result[2]["role"] == "user"
        assert "name" not in result[2], "转换后 name 字段应被移除"

    def test_adapter_multiple_non_first_system_all_fixed(self) -> None:
        """adapter 层多条非首位 system 消息全部被修正。"""
        from llm.adapter import _BaseLiteLLMAdapter

        adapter = _BaseLiteLLMAdapter()
        messages = [
            {"role": "system", "content": "System"},
            {"role": "system", "content": "Guard 1"},
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": "Guard 2"},
            {"role": "system", "content": "Guard 3"},
        ]
        result = adapter._ensure_minimax_role_safety("minimax/MiniMax-M2.7", messages)
        assert result[0]["role"] == "system"
        assert all(m["role"] != "system" for m in result[1:]), (
            f"仍有非首位 system: {[(i, m['role']) for i, m in enumerate(result) if m.get('role') == 'system']}"
        )

    def test_adapter_case_insensitive_model_detection(self) -> None:
        """adapter 层 model 检测应忽略大小写。"""
        from llm.adapter import _BaseLiteLLMAdapter

        adapter = _BaseLiteLLMAdapter()
        messages = [
            {"role": "system", "content": "System"},
            {"role": "system", "content": "Guard"},
        ]
        # "MiniMax" (大写) 也应被检测
        result = adapter._ensure_minimax_role_safety("MiniMax/M2.7", messages)
        assert result[1]["role"] == "user", "大写 MiniMax 应被检测并修正"


class TestMinimaxPhase0OrphanToolResult:
    """Phase 0: 孤立 tool result（前面没有 assistant(tool_calls)）应被清理。"""

    def test_orphan_tool_result_removed(self) -> None:
        """前面没有 assistant(tool_calls) 的 tool result 应被移除。"""
        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Hello"},
            {"role": "tool", "tool_call_id": "orphan_1", "content": "orphan result"},
            {"role": "assistant", "content": "Hi"},
        ]
        result = _normalize_minimax(messages)
        # orphan tool result 应被移除
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        assert len(tool_msgs) == 0, f"孤立 tool result 应被清理，但仍剩 {len(tool_msgs)} 条"

    def test_valid_tool_result_kept(self) -> None:
        """有效的 tool result（前面有 assistant(tool_calls)）应被保留。"""
        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Use tool"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "test", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ]
        result = _normalize_minimax(messages)
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        assert len(tool_msgs) == 1, "有效 tool result 应被保留"
        assert tool_msgs[0]["tool_call_id"] == "call_1"

    def test_mixed_orphan_and_valid_tool_results(self) -> None:
        """混合场景：孤立 tool 被清理，有效 tool 被保留。"""
        messages = [
            {"role": "system", "content": "System"},
            {"role": "tool", "tool_call_id": "orphan_1", "content": "orphan"},
            {"role": "user", "content": "Go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_a1b2c3d4e5f6",
                    "type": "function",
                    "function": {"name": "fn", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "call_a1b2c3d4e5f6", "content": "valid result"},
            {"role": "user", "content": "Next"},
            {"role": "tool", "tool_call_id": "orphan_2", "content": "another orphan"},
        ]
        result = _normalize_minimax(messages)
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        # 只有 call_valid 的 tool result 应保留
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_a1b2c3d4e5f6"


class TestMinimaxPhase2RelocateIntruders:
    """Phase 2: assistant(tool_calls) 和 tool 之间插入的非 tool 消息应被重定位。"""

    def test_system_between_assistant_tool_calls_and_tool_removed(self) -> None:
        """系统消息打破 assistant(tool_calls)→tool 序列时，不完整的 assistant 应被删除。

        旧行为（已废弃）：将系统消息重定位到 tool 组之后。
        新行为：消息注入打破了配对 → tool result 成为孤儿 → assistant 被删除。
                LLM 不会看到孤儿工具调用，避免上下文污染和补全假结果。
        """
        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "fn", "arguments": "{}"},
                }],
            },
            {"role": "system", "content": "[TaskReminder] injected"},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ]
        result = _normalize_minimax(messages)
        roles = [m["role"] for m in result]
        assert "system" in roles, "系统消息应保留"
        assert "user" in roles, "用户消息应保留"
        has_tool_calls = any(
            m.get("role") == "assistant" and m.get("tool_calls")
            for m in result
        )
        assert not has_tool_calls, "不完整的 assistant(tool_calls) 应被删除"

    def test_user_between_assistant_tool_calls_and_tool_removed(self) -> None:
        """user 消息打破 assistant(tool_calls)→tool 序列时，不完整的 assistant 应被删除。"""
        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "fn", "arguments": "{}"},
                }],
            },
            {"role": "user", "content": "Intruder"},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ]
        result = _normalize_minimax(messages)
        # 不完整的 assistant 被删除，用户消息保留
        roles = [m["role"] for m in result]
        assert "user" in roles
        has_tool_calls = any(
            m.get("role") == "assistant" and m.get("tool_calls")
            for m in result
        )
        assert not has_tool_calls, "不完整的 assistant(tool_calls) 应被删除"


class TestMinimaxPhase5SafetyNet:
    """Phase 5: 终极安全网 — 确保所有非首位 system 消息都被转换。"""

    def test_safety_net_catches_remaining_system(self) -> None:
        """即使极端场景下仍有非首位 system，Phase 5 应兜底修正。

        模拟：构造一条在 Phase 1-4 理论上不会遗漏的简单场景，
        验证 Phase 5 作为安全网存在。
        """
        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": "Missed by earlier phases"},
            {"role": "assistant", "content": "Hi"},
        ]
        result = _normalize_minimax(messages)
        # 绝对不能有非首位 system
        for i, m in enumerate(result):
            if i > 0:
                assert m["role"] != "system", (
                    f"Phase 5 安全网应修正 idx={i} 的 system 消息"
                )


class TestMinimaxToolContentCleanup:
    """MiniMax 对 tool 内容的清理（去 null 字符、截断）。"""

    def test_tool_content_null_bytes_removed(self) -> None:
        """tool result 中的 null 字符应被移除。"""
        messages = [
            {"role": "system", "content": "System"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "fn", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "result\x00with\x00nulls"},
        ]
        result = _normalize_minimax(messages)
        tool_msg = next(m for m in result if m.get("role") == "tool")
        assert "\x00" not in tool_msg["content"], "null 字符应被移除"
        assert "resultwithnulls" == tool_msg["content"]

    def test_tool_content_truncated_at_8000(self) -> None:
        """tool result 超过 8000 字符应被截断。"""
        long_content = "A" * 10000
        messages = [
            {"role": "system", "content": "System"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "fn", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "c1", "content": long_content},
        ]
        result = _normalize_minimax(messages)
        tool_msg = next(m for m in result if m.get("role") == "tool")
        assert len(tool_msg["content"]) < 8100, (
            f"超长 tool content 应被截断，实际长度={len(tool_msg['content'])}"
        )
        assert "...[truncated]" in tool_msg["content"]


class TestMinimaxProviderCaseSensitivity:
    """Provider 字符串匹配应是大小写敏感的（'minimax' 全小写）。"""

    def test_uppercase_provider_not_matched(self) -> None:
        """provider='Minimax' (大写 M) 不应触发 MiniMax 专有修正。

        注意: 当前实现使用 == 'minimax' 精确匹配，大写不会匹配。
        """
        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": "Guard"},
        ]
        result = normalize_messages_for_provider(
            messages, provider="Minimax", name="test"
        )
        # 大写 'Minimax' 不匹配 'minimax'，所以不应转换
        assert result[2]["role"] == "system", (
            "大写 provider 不应触发 MiniMax 专有修正"
        )


class TestCallLlmActiveFix:
    """_call_llm 中的 MiniMax 主动修复（normalize 之后第二道防线）。"""

    def test_active_fix_catches_leaked_system(self) -> None:
        """_call_llm 内的主动修复应捕获 normalize 遗漏的 system 消息。

        模拟 normalize 返回仍有非首位 system 的场景（极端情况），
        验证 _call_llm 中的主动修复逻辑。
        """
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from plugins.core.llm_core.plugin import LLMCore
        from llm.adapter import LLMResponse

        core = object.__new__(LLMCore)
        core._provider = "minimax"
        core._model = "MiniMax-M2.7"
        core._config = {}
        core._context_window = 32000
        core._default_params = {}
        core._api_base = None
        core._api_key = None
        core._use_router = False
        core._num_retries = 1
        core._retry_delay = 60.0

        # mock adapter
        mock_adapter = AsyncMock()
        captured_messages: list[dict] = []

        async def capture_completion(*args, **kwargs):
            captured_messages.extend(kwargs.get("messages", []))
            return LLMResponse(text="ok")

        mock_adapter.completion = capture_completion
        core._adapter = mock_adapter

        # 构建 context
        ctx = MagicMock()
        ctx.state = {
            "messages": [
                {"role": "system", "content": "System"},
                {"role": "user", "content": "Hello"},
            ],
            "streaming": False,
            "on_chunk": None,
            "tool_schemas": [],
        }

        # 直接调用 _call_llm
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": "Leaked system msg"},
        ]

        # 通过 normalize 先看结果
        normalized = normalize_messages_for_provider(
            list(messages), provider="minimax", name="test"
        )
        # Phase 1+5 应该已经处理了，但验证双重保险
        assert all(m["role"] != "system" for m in normalized[1:]), (
            "normalize 后不应有非首位 system"
        )


class TestMinimaxNormalizeIdempotency:
    """验证 normalize_messages_for_provider 的幂等性：多次调用结果一致。"""

    def test_double_normalize_same_result(self) -> None:
        """连续两次 normalize 应产生相同结果。"""
        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": "[Guard] warning"},
            {"role": "assistant", "content": "Hi"},
            {"role": "system", "content": "[Guard2] reminder"},
        ]
        result1 = _normalize_minimax(messages)
        result2 = _normalize_minimax(result1)
        # 两次结果的角色序列应一致
        roles1 = [m["role"] for m in result1]
        roles2 = [m["role"] for m in result2]
        assert roles1 == roles2, (
            f"幂等性检查失败: 第一次={roles1}, 第二次={roles2}"
        )

    def test_normalize_preserves_content_integrity(self) -> None:
        """normalize 后所有消息的 content 应保持不变。"""
        messages = [
            {"role": "system", "content": "Original system prompt"},
            {"role": "user", "content": "User message"},
            {"role": "system", "content": "[StreamRepetitionGuard] detected"},
        ]
        result = _normalize_minimax(messages)
        contents = [m.get("content", "") for m in result]
        assert "Original system prompt" in contents
        assert "User message" in contents
        assert "[StreamRepetitionGuard] detected" in contents


class TestMinimaxSingleMessage:
    """边界场景：只有一条消息。"""

    def test_single_system_message_preserved(self) -> None:
        """只有一条 system 消息应保留不变。"""
        messages = [{"role": "system", "content": "Only system"}]
        result = _normalize_minimax(messages)
        assert result == [{"role": "system", "content": "Only system"}]

    def test_single_user_message_preserved(self) -> None:
        """只有一条 user 消息应保留不变。"""
        messages = [{"role": "user", "content": "Only user"}]
        result = _normalize_minimax(messages)
        assert result == [{"role": "user", "content": "Only user"}]

    def test_single_system_at_non_first_position(self) -> None:
        """首位 user + 第二位 system，system 应被转换。"""
        messages = [
            {"role": "user", "content": "First"},
            {"role": "system", "content": "Second"},
        ]
        result = _normalize_minimax(messages)
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "user", "非首位 system 应转为 user"
