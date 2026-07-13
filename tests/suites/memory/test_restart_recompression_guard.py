"""回归测试：重启后不应重复压缩已有压缩块覆盖的旧消息。

覆盖两处修复：
1. `_estimate_effective_tokens` 策略 1 的重启守卫：
   tracked==0 且 current_non_sys>50 时跳过策略 1，避免 prev_input 误炸。
2. `execute` 主流程：裁剪在阈值估算之前执行，使压缩分支拿到裁剪后的 messages。
"""

import asyncio
import json
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "src",
)))

logging.basicConfig(level=logging.DEBUG)


def _make_messages(count: int, chars_each: int = 2000) -> list[dict]:
    """生成指定数量的模拟消息（1 条 system + count 轮 user/assistant）。"""
    msgs = [{"role": "system", "content": "你是一个 AI 助手"}]
    for i in range(count):
        msgs.append({"role": "user", "content": f"消息 {i}: " + "x" * chars_each})
        msgs.append({"role": "assistant", "content": f"回复 {i}: " + "y" * chars_each})
    return msgs


def _fake_llm_response() -> str:
    return json.dumps({
        "l1": "压缩摘要",
        "l2": "",
        "keywords": ["k1"],
        "state_snapshot": {"state": "测试"},
        "memory_items": {},
    }, ensure_ascii=False)


class _FakeChunk:
    """模拟 ChunkService 返回的 L1 压缩块。"""

    def __init__(self, sequence_end: int, content: str = "已有压缩块"):
        self.sequence_start = 1
        self.sequence_end = sequence_end
        self.content = content


class TestRestartRecompressionGuard:
    """重启后压缩块识别相关回归测试。"""

    @pytest.mark.asyncio
    async def test_strategy1_skipped_on_restart_signature(self):
        """重启特征（tracked=0 + current>50 + prev_input>0）下策略 1 必须被跳过。

        若不跳过，会得到 prev_input + 全量 delta 的爆炸值；
        跳过后落到策略 2（压缩块拼接），返回 L1 块 + recent 的合理值。
        """
        from plugins.input.context_window_guard.plugin import ContextWindowGuardPlugin

        plugin = ContextWindowGuardPlugin(config={"trigger_ratio": 0.5})

        # 60 条非 system 消息（>50 触发重启特征判定）
        messages = _make_messages(30, chars_each=200)

        # 模拟重启残留：prev_input 是上一进程某轮的大值
        ctx = MagicMock()
        ctx.state = {
            "context_window": 200000,
            "messages": messages,
            "llm_usage": {"input_tokens": 80000},  # 残留大值
            "track.llm_usage": {},
            "_tracked_msg_count": 0,                # 重启归零
            "pipeline_id": "test-pipeline",
        }

        def get_service(name):
            if name == "chunk_service":
                cs = MagicMock()
                # 已有 L1 块覆盖了前 30 条非 system 消息
                cs.find_by_pipeline = AsyncMock(return_value=[_FakeChunk(30)])
                return cs
            raise KeyError(name)
        ctx.get_service = get_service

        estimated = await plugin._estimate_effective_tokens(messages, ctx)

        # 策略 2 = L1 块 tokens + recent(system + 后 30 条非 system)
        # 60 条非 system 消息，max_end=30，recent 只算后 30 条
        # 期望值远小于 prev_input(80000) + 全量 delta 的爆炸值
        assert estimated < 80000, (
            f"重启场景应跳过策略 1 落到策略 2，但估算值 {estimated} 过大，"
            "可能误用了 prev_input + 全量 delta"
        )
        # 应该至少把后 30 条非 system 消息算上了（不是 0 或仅 system）
        assert estimated > 0

    @pytest.mark.asyncio
    async def test_trim_runs_before_threshold_check(self):
        """execute 主流程：裁剪必须在阈值估算之前执行。

        场景：重启后 messages 含已被压缩块覆盖的旧消息。
        期望：execute 调用 _trim_covered_messages 把旧消息裁掉，
        返回的 state_updates.messages 是裁剪后的列表。
        """
        from plugins.input.context_window_guard.plugin import ContextWindowGuardPlugin

        plugin = ContextWindowGuardPlugin(config={"trigger_ratio": 0.5})

        # 60 条非 system 消息，每条 100 字符（小，避免触发压缩）
        messages = _make_messages(30, chars_each=100)

        ctx = MagicMock()
        ctx.state = {
            "context_window": 200000,
            "messages": messages,
            "llm_usage": {"input_tokens": 0},
            "track.llm_usage": {},
            "_tracked_msg_count": 0,
            "pipeline_id": "test-pipeline",
        }

        def get_service(name):
            if name == "chunk_service":
                cs = MagicMock()
                # L1 块覆盖前 30 条非 system → 裁后应剩 30 条非 system
                cs.find_by_pipeline = AsyncMock(return_value=[_FakeChunk(30)])
                return cs
            if name == "context_service":
                svc = MagicMock()
                # clean_if_window_changed 返回 None（无窗口变更）
                svc.clean_if_window_changed = AsyncMock(return_value=None)
                svc.setup = MagicMock()
                return svc
            raise KeyError(name)
        ctx.get_service = get_service

        result = await plugin.execute(ctx)

        # state_updates 应包含裁剪后的 messages
        assert "messages" in result.state_updates, "execute 应输出裁剪后的 messages"
        trimmed = result.state_updates["messages"]
        non_sys_after = sum(1 for m in trimmed if m.get("role") != "system")
        # 原 60 条非 system，L1 覆盖 30 条 → 裁后剩 30 条
        assert non_sys_after == 30, (
            f"裁剪后非 system 消息应为 30，实际 {non_sys_after}"
        )

    @pytest.mark.asyncio
    async def test_no_trim_in_steady_iteration(self):
        """正常迭代（消息数未远超 tracked）不应触发裁剪。

        避免"过度裁剪"回归：稳态下 messages 应原样保留。
        """
        from plugins.input.context_window_guard.plugin import ContextWindowGuardPlugin

        plugin = ContextWindowGuardPlugin(config={"trigger_ratio": 0.5})
        # 模拟稳态：tracked 已经追踪到 10，messages 只有 12 条（增量 2，未超 +50）
        plugin._tracked_msg_count = 10

        messages = _make_messages(6, chars_each=100)  # 12 条非 system

        ctx = MagicMock()
        ctx.state = {
            "context_window": 200000,
            "messages": messages,
            "llm_usage": {"input_tokens": 1000},
            "track.llm_usage": {},
            "_tracked_msg_count": 10,
            "pipeline_id": "test-pipeline",
        }

        def get_service(name):
            if name == "chunk_service":
                cs = MagicMock()
                cs.find_by_pipeline = AsyncMock(return_value=[_FakeChunk(5)])
                return cs
            if name == "context_service":
                svc = MagicMock()
                svc.clean_if_window_changed = AsyncMock(return_value=None)
                svc.setup = MagicMock()
                return svc
            raise KeyError(name)
        ctx.get_service = get_service

        result = await plugin.execute(ctx)

        # 稳态下不应裁剪 → messages 不应被改写
        assert "messages" not in result.state_updates, (
            "稳态迭代不应触发裁剪，但 messages 被改写了"
        )
