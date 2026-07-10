"""端到端测试：context_window_guard → MemoryContextService → ContextCompressor。

验证整个压缩链路是否能正确触发并产出压缩结果。
"""

import asyncio
import json
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 确保 src 在路径中
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "src",
)))

logging.basicConfig(level=logging.DEBUG)


def _make_messages(count: int, chars_each: int = 2000) -> list[dict]:
    """生成指定数量的模拟消息。"""
    msgs = [{"role": "system", "content": "你是一个 AI 助手"}]
    for i in range(count):
        msgs.append({"role": "user", "content": f"消息 {i}: " + "x" * chars_each})
        msgs.append({"role": "assistant", "content": f"回复 {i}: " + "y" * chars_each})
    return msgs


def _fake_llm_response(l1: str = "压缩摘要内容", l2: str = "", keywords: list = None,
                       state_snapshot: dict = None, memory_items: dict = None) -> str:
    """构造 compressor 需要的假 LLM JSON 响应。"""
    return json.dumps({
        "l1": l1,
        "l2": l2 or "",
        "keywords": keywords or ["关键词1", "关键词2"],
        "state_snapshot": state_snapshot or {"current_state": "测试中", "pending_tasks": "无"},
        "memory_items": memory_items or {},
    }, ensure_ascii=False)


class TestE2ECompression:
    """端到端压缩链路测试。"""

    @pytest.mark.asyncio
    async def test_full_chain_plugin_triggers_compression(self):
        """验证 plugin 阈值触发 → service 压缩 → compressor 调用 LLM 的完整链路。"""
        from memory.memory_context_service import MemoryContextService
        from plugins.input.context_window_guard.plugin import ContextWindowGuardPlugin

        context_window = 200000
        trigger_ratio = 0.5

        # 构造足够大的消息（超过触发线）
        messages = _make_messages(100, chars_each=2000)  # ~400K chars → ~200K tokens (len//2)
        assert len(messages) > 200

        # 1. 创建 plugin
        plugin = ContextWindowGuardPlugin(config={
            "trigger_ratio": trigger_ratio,
            "compression_model": None,
        })

        # 2. 创建 service
        service = MemoryContextService(
            config={"context_window": context_window, "compress_trigger_ratio": trigger_ratio},
        )

        # 3. Mock LLM 调用函数
        mock_llm_fn = AsyncMock(return_value=_fake_llm_response())
        service.set_llm_call_fn(mock_llm_fn)

        # 4. 不需要 chunk_service（跳过持久化）
        service.setup(
            pipeline_id="test-pipeline",
            session_id="test-session",
            context_window=context_window,
        )

        # 5. 直接调用 service.compress_messages
        result = await service.compress_messages(
            messages=messages,
            context_window=context_window,
            trigger_ratio=trigger_ratio,
        )

        # 验证：应该有压缩结果
        assert result is not None, "compress_messages 返回 None，压缩未生效"
        assert len(result) < len(messages), f"压缩后消息数未减少: {len(result)} >= {len(messages)}"
        print(f"压缩成功: {len(messages)} -> {len(result)} 条消息")

        # 验证 LLM 被调用了
        assert mock_llm_fn.called, "LLM 调用函数未被调用"
        print(f"LLM 被调用 {mock_llm_fn.call_count} 次")

    @pytest.mark.asyncio
    async def test_service_do_compress_round_directly(self):
        """直接测试 _do_compress_round 是否能正确切分和压缩。"""
        from memory.context_compressor import CompressionConfig
        from memory.memory_context_service import MemoryContextService

        context_window = 200000
        service = MemoryContextService(
            config={"context_window": context_window, "compress_trigger_ratio": 0.5},
        )

        # Mock LLM
        mock_llm_fn = AsyncMock(return_value=_fake_llm_response())
        service.set_llm_call_fn(mock_llm_fn)
        service.setup(pipeline_id="test", context_window=context_window)

        messages = _make_messages(50, chars_each=1000)
        config = CompressionConfig.from_yaml_config(context_window)
        budgets = config.get_budgets()

        print(f"budgets: {budgets}")
        print(f"messages: {len(messages)}, estimated tokens: {sum(service._estimate_msg_tokens(m) for m in messages)}")

        result = await service._do_compress_round(
            messages=messages,
            context_window=context_window,
            budgets=budgets,
            state_snapshot="",
            recent_process_blocks="",
            compression_window=1000000,
        )

        assert result is not None, "_do_compress_round 返回 None"
        assert len(result) < len(messages), f"压缩后消息数未减少: {len(result)} >= {len(messages)}"
        print(f"压缩成功: {len(messages)} -> {len(result)} 条消息")

    @pytest.mark.asyncio
    async def test_plugin_execute_with_mock_service(self):
        """测试 plugin 阈值估算是否正确判定超过触发线。"""
        from plugins.input.context_window_guard.plugin import ContextWindowGuardPlugin

        plugin = ContextWindowGuardPlugin(config={"trigger_ratio": 0.5})

        # 构造足够大的消息
        messages = _make_messages(100, chars_each=2000)

        # Mock context — 模拟首轮（prev_input=0）
        ctx = MagicMock()
        ctx.state = {
            "context_window": 200000,
            "messages": messages,
            "llm_usage": {"input_tokens": 0},
            "_tracked_msg_count": 0,
        }

        # 验证估算：len//2 算法，~400K chars → ~200K tokens > 100K trigger
        estimated = await plugin._estimate_effective_tokens(messages, ctx)
        trigger = int(200000 * 0.5)
        print(f"estimated={estimated}, trigger={trigger}, exceeds={estimated >= trigger}")
        assert estimated >= trigger, f"估算值 {estimated} 未超过触发线 {trigger}"

    @pytest.mark.asyncio
    async def test_budget_split_with_main_model_window(self):
        """验证用主模型窗口（200K）做预算切分，压缩模型窗口（1M）做分片大小。"""
        from memory.context_compressor import CompressionConfig
        from memory.memory_context_service import MemoryContextService

        context_window = 200000
        compression_window = 1000000

        service = MemoryContextService(
            config={"context_window": context_window},
        )
        service.set_llm_call_fn(AsyncMock(return_value=_fake_llm_response()))
        service.setup(pipeline_id="test", context_window=context_window)

        # 100 条消息，每条约 1000 chars → ~500 tokens each → ~50K total
        messages = _make_messages(50, chars_each=1000)

        config = CompressionConfig.from_yaml_config(context_window)
        budgets = config.get_budgets()

        # recent_budget = 200K * 0.1 = 20K tokens
        recent_budget = budgets["recent"]
        print(f"recent_budget={recent_budget}, context_window={context_window}")

        # 验证 split_idx > 0
        other_msgs = [m for m in messages if m.get("role") != "system"]
        split_idx = service._find_split_by_budget(other_msgs, recent_budget)
        print(f"split_idx={split_idx}, other_msgs={len(other_msgs)}")
        assert split_idx > 0, f"split_idx={split_idx}，所有消息都在 recent 预算内"

    @pytest.mark.asyncio
    async def test_no_compression_below_threshold(self):
        """验证消息量低于阈值时不触发压缩。"""
        from memory.memory_context_service import MemoryContextService

        context_window = 200000
        service = MemoryContextService(
            config={"context_window": context_window},
        )
        service.set_llm_call_fn(AsyncMock(return_value=_fake_llm_response()))

        # 很少的消息，远低于触发线
        messages = _make_messages(3, chars_each=100)
        trigger_ratio = 0.5
        trigger_tokens = int(context_window * trigger_ratio)

        total = sum(service._estimate_msg_tokens(m) for m in messages)
        print(f"total={total}, trigger={trigger_tokens}")
        assert total < trigger_tokens

        # compress_messages 应该直接返回 None（不触发）
        result = await service.compress_messages(
            messages=messages,
            context_window=context_window,
            trigger_ratio=trigger_ratio,
        )
        # 不触发时返回 None
        assert result is None, f"低消息量不应触发压缩，但返回了 {len(result)} 条消息"
        print("正确：低消息量未触发压缩")
