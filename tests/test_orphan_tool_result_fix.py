"""孤儿 tool result 修复回归测试。

根因分析测试：Minimax API 报 "tool result's tool id(...) not found (2013)"，
死循环触发。根因是 messages 历史里混入了孤儿 tool result（role=tool 但前面
没有匹配的 assistant(tool_calls)）。

三个叠加缺陷：
1. duplicate_check Level-2 拦截清空 RAW_TOOL_CALLS 但不移除末尾 assistant(tool_calls)
2. normalize 结果不写回 state，脏数据每轮重复扫描
3. 配对缓存跨管道共享，外部插件追加消息后缓存失效

验收标准：
- duplicate_check Level-2 拦截后 state["messages"] 末尾无 assistant(tool_calls)
- normalize 清理结果能写回 state（通过 LLMCore._writeback_cleaned_history）
- 不同 pipeline_id 的配对缓存互不污染
- 现有 normalize 行为不回归
"""

from __future__ import annotations

import hashlib

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys
from plugins.core.llm_core._message_normalizer import (
    _pairing_validated_len,
    normalize_messages_for_provider,
    reset_pairing_cache,
)
from plugins.output.duplicate_check import DuplicateCheckPlugin


def _tool_signature(name: str, args: dict) -> str:
    """复刻 duplicate_check 的工具签名算法，用于构造匹配的 last_tool_call。"""
    return hashlib.md5(f"{name}:{sorted(args.items())}".encode()).hexdigest()[:8]


@pytest.fixture(autouse=True)
def _clear_pairing_cache():
    """每个测试前后清理增量配对校验缓存，避免测试间相互污染。"""
    _pairing_validated_len.clear()
    yield
    _pairing_validated_len.clear()


def _make_ctx(state: dict) -> PluginContext:
    """构造最小可用的 PluginContext。"""
    return PluginContext(state=state, config={}, _services=None)


async def _run_plugin(plugin: DuplicateCheckPlugin, ctx: PluginContext) -> None:
    """执行插件并把 OutputResult.state_updates 合并回 state（模拟 PluginChain 行为）。

    duplicate_check 通过 ctx.state 直接写 messages（_inject_warning /
    _strip_trailing_tool_call_assistant），但 RAW_TOOL_CALLS 清空通过
    state_updates 返回，需要链式合并。测试中手动模拟该合并。
    """
    output = await plugin.execute(ctx)
    for key, value in (output.state_updates or {}).items():
        ctx.state[key] = value


# ── 改动 1：duplicate_check 拦截时移除末尾 assistant(tool_calls) ──


class TestDuplicateCheckStripsAssistant:
    """duplicate_check Level-2 拦截必须移除末尾未配对 assistant(tool_calls)。"""

    @pytest.mark.asyncio
    async def test_level2_intercept_removes_trailing_assistant_tool_calls(self) -> None:
        """Level-2 拦截后末尾不应残留 assistant(tool_calls)（避免孤儿 tool result）。"""
        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 2})
        # 模拟 llm_core 已 append 的 assistant(tool_calls)，正是被拦截的重复调用
        tool_args = {}
        state = {
            "messages": [
                {"role": "user", "content": "do task"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_aaaaaaaaaaaaaaaaaaaaaaaa",
                            "type": "function",
                            "function": {"name": "task_submit", "arguments": "{}"},
                        }
                    ],
                },
            ],
            StateKeys.RAW_TOOL_CALLS: [
                {"id": "call_aaaaaaaaaaaaaaaaaaaaaaaa", "name": "task_submit", "args": tool_args}
            ],
            # last_tool_call 匹配当前调用 + count=1 → 本轮匹配后 count=2 触发 Level-2
            "router.last_tool_call": _tool_signature("task_submit", tool_args),
            "router.duplicate_count": 1,
            "router.duplicate_intercepts": 0,
            StateKeys.AGENT_LEVEL: "L3",
        }
        ctx = _make_ctx(state)
        await _run_plugin(plugin, ctx)

        messages = state["messages"]
        # 末尾不应有 assistant(tool_calls) —— 拦截撤销了工具调用意图
        assert messages[-1]["role"] != "assistant" or not messages[-1].get("tool_calls"), (
            f"末尾仍残留 assistant(tool_calls): {messages[-1]}"
        )
        # 原始 user 消息应保留
        assert messages[0]["role"] == "user"
        # 警告应被注入
        assert any(
            "[DuplicateCheck]" in m.get("content", "") for m in messages
        ), "未注入拦截警告"
        # RAW_TOOL_CALLS 应被清空
        assert state[StateKeys.RAW_TOOL_CALLS] == []

    @pytest.mark.asyncio
    async def test_level2_keeps_plain_assistant_text(self) -> None:
        """Level-2 拦截不应移除普通 assistant 文本消息（无 tool_calls）。"""
        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 2})
        tool_args = {}
        state = {
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "你好"},  # 普通文本，应保留
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_bbbbbbbbbbbbbbbbbbbbbbbb",
                            "type": "function",
                            "function": {"name": "task_submit", "arguments": "{}"},
                        }
                    ],
                },
            ],
            StateKeys.RAW_TOOL_CALLS: [
                {"id": "call_bbbbbbbbbbbbbbbbbbbbbbbb", "name": "task_submit", "args": tool_args}
            ],
            "router.last_tool_call": _tool_signature("task_submit", tool_args),
            "router.duplicate_count": 1,
            "router.duplicate_intercepts": 0,
            StateKeys.AGENT_LEVEL: "L3",
        }
        ctx = _make_ctx(state)
        await _run_plugin(plugin, ctx)

        messages = state["messages"]
        # 普通 assistant 文本应保留
        assert any(
            m["role"] == "assistant" and not m.get("tool_calls")
            and m.get("content") == "你好"
            for m in messages
        ), "普通 assistant 文本消息被误删"
        # assistant(tool_calls) 应被移除
        assert not any(
            m["role"] == "assistant" and m.get("tool_calls") for m in messages
        ), "assistant(tool_calls) 未被移除"

    @pytest.mark.asyncio
    async def test_level1_hint_does_not_strip(self) -> None:
        """Level-1 软提示不应移除 assistant(tool_calls)（工具仍执行）。"""
        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 2})
        tool_args = {}
        state = {
            "messages": [
                {"role": "user", "content": "do task"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_ccccccccccccccccccccccccc",
                            "type": "function",
                            "function": {"name": "task_submit", "arguments": "{}"},
                        }
                    ],
                },
            ],
            StateKeys.RAW_TOOL_CALLS: [
                {"id": "call_ccccccccccccccccccccccccc", "name": "task_submit", "args": tool_args}
            ],
            # last_tool_call 不匹配 → count 重置为 0 → Level-1 不触发（无 strip）
            # 这里用匹配的 last_tool_call + count=0 → 本轮 count=1 < 2 → Level-1
            "router.last_tool_call": _tool_signature("task_submit", tool_args),
            "router.duplicate_count": 0,
            "router.duplicate_intercepts": 0,
            StateKeys.AGENT_LEVEL: "L3",
        }
        ctx = _make_ctx(state)
        await _run_plugin(plugin, ctx)

        messages = state["messages"]
        # Level-1 工具仍执行，assistant(tool_calls) 应保留
        assert any(
            m["role"] == "assistant" and m.get("tool_calls") for m in messages
        ), "Level-1 误删了 assistant(tool_calls)"


# ── 改动 2：normalize 清理结果写回 state（通过 LLMCore._writeback_cleaned_history）──


class TestNormalizeWriteback:
    """normalize 清理孤儿后，LLMCore 应把干净历史写回 state。"""

    def test_writeback_cleaned_history_strips_orphan(self) -> None:
        """_writeback_cleaned_history 应把清理后的历史段写回 state['messages']。"""
        from plugins.core.llm_core.plugin import LLMCore

        # raw 含孤儿 tool result（前面无匹配 assistant）
        # 结构：[system] + [history: user, tool(孤儿)] + [dynamic_vars]
        raw_messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "call_deadbeefdeadbeefdeadbeef", "content": "orphan"},
            {"role": "user", "name": "dynamic_context", "content": "ts"},
        ]
        # cleaned 移除了孤儿 tool result
        cleaned_messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "user", "name": "dynamic_context", "content": "ts"},
        ]
        state = {
            "system_message": {"role": "system", "content": "sys"},
            "compression_messages": [],
            "prompt.dynamic_vars": {"content": "ts"},
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "tool", "tool_call_id": "call_deadbeefdeadbeefdeadbeef", "content": "orphan"},
            ],
        }

        # 直接调用 helper（不依赖 LLM 初始化）
        plugin = LLMCore.__new__(LLMCore)
        plugin._writeback_cleaned_history(state, raw_messages, cleaned_messages)

        # state['messages'] 应只含清理后的历史段（不含 system/dynamic_vars）
        assert state["messages"] == [{"role": "user", "content": "hi"}], (
            f"写回历史段错误: {state['messages']}"
        )

    def test_normalize_drops_orphan_tool_result(self) -> None:
        """normalize 应清理孤儿 tool result（前面无匹配 assistant(tool_calls)）。"""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            # 孤儿 tool result：没有 preceding assistant(tool_calls)
            {"role": "tool", "tool_call_id": "call_08c1ef1979b0441aa019b488", "content": "error: MISSING_GOAL"},
            {"role": "user", "content": "[DuplicateCheck] 你仍然在重复调用"},
        ]
        result = normalize_messages_for_provider(
            messages, provider="minimax", name="test", pipeline_id="pipe-A",
        )
        # 孤儿 tool result 应被移除
        assert not any(
            m.get("role") == "tool" for m in result
        ), f"孤儿 tool result 未被清理: {result}"
        # 其余消息保留
        assert any("[DuplicateCheck]" in m.get("content", "") for m in result)


# ── 改动 3：配对缓存按 pipeline_id 隔离 ──


class TestPairingCachePipelineIsolation:
    """配对缓存必须按 pipeline_id 隔离，避免并发管道互相污染。"""

    def test_different_pipeline_ids_do_not_share_cache(self) -> None:
        """两个 pipeline 共享 provider:name 但 ID 不同，缓存互不影响。"""
        base_messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
        ]

        # pipeline A：完整配对（建立缓存）
        msgs_a = base_messages + [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_aaaa11112222333344445555",
                    "type": "function",
                    "function": {"name": "t", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "call_aaaa11112222333344445555", "content": "ok"},
        ]
        normalize_messages_for_provider(
            msgs_a, provider="minimax", name="test", pipeline_id="pipe-A",
        )
        cache_a_key = "minimax:test:pipe-A"
        assert cache_a_key in _pairing_validated_len

        # pipeline B：含孤儿 tool result
        msgs_b = base_messages + [
            {"role": "tool", "tool_call_id": "call_bbbb00001111222233334444", "content": "orphan"},
        ]
        result_b = normalize_messages_for_provider(
            msgs_b, provider="minimax", name="test", pipeline_id="pipe-B",
        )
        # pipeline B 的孤儿应被清理（缓存未命中，全量扫描）
        assert not any(m.get("role") == "tool" for m in result_b), (
            f"pipeline B 孤儿未清理（缓存被 A 污染）: {result_b}"
        )

    def test_reset_pairing_cache_with_pipeline_id_is_precise(self) -> None:
        """reset_pairing_cache 带 pipeline_id 只清当前管道缓存。"""
        # 建立两个管道的缓存
        normalize_messages_for_provider(
            [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
            provider="minimax", name="test", pipeline_id="pipe-X",
        )
        normalize_messages_for_provider(
            [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
            provider="minimax", name="test", pipeline_id="pipe-Y",
        )
        assert "minimax:test:pipe-X" in _pairing_validated_len
        assert "minimax:test:pipe-Y" in _pairing_validated_len

        # 精确清 pipe-X
        reset_pairing_cache("minimax", "test", pipeline_id="pipe-X")
        assert "minimax:test:pipe-X" not in _pairing_validated_len
        assert "minimax:test:pipe-Y" in _pairing_validated_len, "误清了其他管道缓存"

    def test_reset_pairing_cache_without_pipeline_id_clears_all_pipelines(self) -> None:
        """reset_pairing_cache 不带 pipeline_id 清该 provider:name 下所有管道（向后兼容）。"""
        normalize_messages_for_provider(
            [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
            provider="minimax", name="test", pipeline_id="pipe-1",
        )
        normalize_messages_for_provider(
            [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
            provider="minimax", name="test", pipeline_id="pipe-2",
        )
        reset_pairing_cache("minimax", "test")
        # 该 provider:name 下所有管道缓存都应被清空
        remaining = [k for k in _pairing_validated_len if k.startswith("minimax:test:")]
        assert remaining == [], f"残留缓存: {remaining}"


# ── 端到端：模拟日志中的死循环场景 ──


class TestOrphanReproductionScenario:
    """复现日志中的死循环场景并验证修复。"""

    def test_orphan_between_user_messages_is_cleaned(self) -> None:
        """复现日志 MSG-9/10/11: user → tool(孤儿) → user 应被清理。"""
        # 完全复刻日志中的消息序列（简化）
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "进行任务继承测试"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_ec507dd1c37f453aa20026d9",
                    "type": "function",
                    "function": {"name": "task_submit", "arguments": "{}"},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call_ec507dd1c37f453aa20026d9",
                "content": "error: MISSING_GOAL",
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_a78fee044051412099f02b13",
                    "type": "function",
                    "function": {"name": "task_submit", "arguments": "{}"},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call_a78fee044051412099f02b13",
                "content": "error: MISSING_GOAL",
            },
            {"role": "user", "content": "[DuplicateCheck] 你已经连续 1 次..."},
            # 孤儿 tool result：它的 id 从未作为 tool_call 出现
            {
                "role": "tool",
                "tool_call_id": "call_08c1ef1979b0441aa019b488",
                "content": "error: MISSING_GOAL",
            },
            {"role": "user", "content": "[DuplicateCheck] 你仍然在重复调用..."},
        ]

        result = normalize_messages_for_provider(
            messages, provider="minimax", name="llm_core", pipeline_id="pipe-log",
        )

        # 关键断言：孤儿 tool_call_id 不应在结果中出现
        tool_ids = [
            m.get("tool_call_id") for m in result if m.get("role") == "tool"
        ]
        assert "call_08c1ef1979b0441aa019b488" not in tool_ids, (
            f"孤儿 tool result 未被清理，残留 tool ids: {tool_ids}"
        )
        # 配对的 tool result 应保留
        assert "call_ec507dd1c37f453aa20026d9" in tool_ids
        assert "call_a78fee044051412099f02b13" in tool_ids
