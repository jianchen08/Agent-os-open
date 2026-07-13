"""回归测试：压缩写回前 tool_calls 标准化（P0 修复）。

复现路径：raw 格式 tool_calls（缺 type / 扁平，来自执行记录/state 恢复）
→ compress_messages 原样保留进 recent 段 → 写回 state。
修复前：raw 格式滞留 → 后续发上游 400「工具类型不能为空」。
修复后：context_window_guard 在写回前标准化，输出必为标准格式。

本测试验证：
1. raw 输入 → 经 plugin.execute 写回的 messages 中无 raw 格式 tool_calls
2. 配对的 tool result 的 tool_call_id 与 assistant 同步（不破坏配对）
"""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "src",
)))


def _fake_compress_response() -> str:
    return json.dumps({
        "l1": "过程摘要", "l2": "", "keywords": ["k"],
        "state_snapshot": {"current_state": "测试"}, "memory_items": {},
    }, ensure_ascii=False)


def _raw_tc(tc_id: str, name: str = "read_file", path: str = "a.txt") -> dict:
    """raw 格式：缺 type、扁平结构（模拟执行记录恢复的脏数据）。"""
    return {"id": tc_id, "name": name, "arguments": json.dumps({"path": path})}


def _build_raw_dialog(rounds: int, chars: int = 1800) -> list[dict]:
    """构造 assistant.tool_calls 全是 raw 格式的大对话。"""
    msgs = [{"role": "system", "content": "你是助手"}]
    for i in range(rounds):
        tcid = f"call_{i:024x}"[:29]
        msgs.append({"role": "user", "content": f"第{i}轮读文件 " + "x" * chars})
        msgs.append({"role": "assistant", "content": "", "tool_calls": [_raw_tc(tcid, path=f"f{i}.txt")]})
        msgs.append({"role": "tool", "tool_call_id": tcid, "name": "read_file",
                     "content": f"内容{i} " + "y" * chars})
        msgs.append({"role": "assistant", "content": f"完成{i} " + "z" * chars})
    return msgs


def _find_raw_tc(messages: list[dict]) -> list[tuple[int, dict]]:
    """检测 raw 格式 tool_calls（缺 type 或扁平），返回 (msg_idx, tc) 列表。"""
    found = []
    for i, m in enumerate(messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                if tc.get("type") != "function" or not isinstance(tc.get("function"), dict):
                    found.append((i, tc))
    return found


def _check_pairing_intact(messages: list[dict]) -> bool:
    """验证 assistant(tool_calls) 与 tool result 的 id 配对完整。"""
    assistant_tc_ids = set()
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tcid = tc.get("id")
                if tcid:
                    assistant_tc_ids.add(tcid)
    tool_result_ids = {
        m.get("tool_call_id") for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }
    # 每个 tool result 都应有对应 assistant（不能有孤儿）
    orphans = tool_result_ids - assistant_tc_ids
    return len(orphans) == 0


class TestCompressStandardizesToolCalls:
    """验证压缩写回前 tool_calls 被标准化（P0 修复）。"""

    @pytest.mark.asyncio
    async def test_plugin_standardizes_raw_tool_calls_on_writeback(self):
        """raw 格式输入 → plugin.execute 写回的 messages 无 raw 格式。"""
        from pipeline.plugin import PluginContext
        from pipeline.types import StateKeys
        from plugins.input.context_window_guard.plugin import ContextWindowGuardPlugin
        from memory.memory_context_service import MemoryContextService

        context_window = 128000
        trigger_ratio = 0.5
        messages = _build_raw_dialog(40, 1800)

        # 压缩前确认：含大量 raw 格式
        raw_before = _find_raw_tc(messages)
        assert len(raw_before) >= 30, f"测试数据应含大量 raw 格式，实际 {len(raw_before)}"

        service = MemoryContextService(
            config={"context_window": context_window, "compress_trigger_ratio": trigger_ratio},
        )
        service.set_llm_call_fn(AsyncMock(return_value=_fake_compress_response()))

        # mock chunk_service 让保存不报错
        fake_chunk = MagicMock()
        fake_chunk.find_by_pipeline = AsyncMock(return_value=[])
        fake_chunk.save = AsyncMock(return_value="chunk-id")
        fake_chunk.delete = AsyncMock(return_value=None)
        service.setup(
            pipeline_id="std-test", session_id="s",
            context_window=context_window,
            compression_model_id="minimax-m3-guangfang",
            chunk_service=fake_chunk,
        )

        plugin = ContextWindowGuardPlugin(config={
            "trigger_ratio": trigger_ratio,
            "compression_model": "minimax-m3-guangfang",
        })

        ctx = PluginContext(
            state={
                "context_window": context_window,
                "messages": messages,
                StateKeys.PIPELINE_ID: "std-test",
                "llm_usage": {"input_tokens": context_window + 10000},
                "_tracked_msg_count": sum(1 for m in messages if m.get("role") != "system"),
            },
            _services={"context_service": service, "chunk_service": fake_chunk},
        )

        result = await plugin.execute(ctx)

        # 压缩应成功（非终止）
        assert not result.skip_remaining, "压缩应成功，不应终止管线"
        # 直接调 plugin.execute 不经 chain，state_updates 不会自动合并；
        # 标准化作用于写回的 messages（state_updates["messages"]）
        written = result.state_updates.get("messages")
        assert written is not None, "应写回 messages"

        # 核心断言：写回的 messages 无 raw 格式 tool_calls
        raw_after = _find_raw_tc(written)
        assert raw_after == [], (
            f"P0 修复后写回的 messages 不应含 raw 格式 tool_calls，"
            f"仍有 {len(raw_after)} 处: {raw_after[:3]}"
        )

        # 配对应保持完整（id 标准化会同步 tool result）
        assert _check_pairing_intact(written), "标准化后 tool 配对应保持完整，无孤儿 tool result"

        # 验证格式确实标准（有 type=function 和 function 嵌套）
        std_count = 0
        for m in written:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    assert tc.get("type") == "function", f"tool_call type 应为 function: {tc}"
                    assert isinstance(tc.get("function"), dict), f"应有 function 嵌套: {tc}"
                    std_count += 1
        print(f"\n[P0回归] 压缩写回 {len(written)} 条，标准化 tool_calls {std_count} 处，配对完整")

    @pytest.mark.asyncio
    async def test_standardization_preserves_valid_messages(self):
        """标准格式输入 → 标准化后仍标准，无误伤。"""
        from pipeline.plugin import PluginContext
        from pipeline.types import StateKeys
        from plugins.input.context_window_guard.plugin import ContextWindowGuardPlugin
        from memory.memory_context_service import MemoryContextService

        context_window = 128000
        messages = [{"role": "system", "content": "你是助手"}]
        # 标准格式 tool_calls
        for i in range(40):
            tcid = f"call_{i:024x}"[:29]
            messages.append({"role": "user", "content": "读文件 " + "x" * 1800})
            messages.append({"role": "assistant", "content": "", "tool_calls": [{
                "id": tcid, "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": f"f{i}.txt"})},
            }]})
            messages.append({"role": "tool", "tool_call_id": tcid, "name": "read_file",
                             "content": "内容 " + "y" * 1800})
            messages.append({"role": "assistant", "content": "完成 " + "z" * 1800})

        service = MemoryContextService(config={"context_window": context_window})
        service.set_llm_call_fn(AsyncMock(return_value=_fake_compress_response()))
        fake_chunk = MagicMock()
        fake_chunk.find_by_pipeline = AsyncMock(return_value=[])
        fake_chunk.save = AsyncMock(return_value="id")
        fake_chunk.delete = AsyncMock(return_value=None)
        service.setup(pipeline_id="std-ok", context_window=context_window,
                      compression_model_id="minimax-m3-guangfang", chunk_service=fake_chunk)

        plugin = ContextWindowGuardPlugin(config={
            "trigger_ratio": 0.5, "compression_model": "minimax-m3-guangfang",
        })
        ctx = PluginContext(
            state={
                "context_window": context_window, "messages": messages,
                StateKeys.PIPELINE_ID: "std-ok",
                "llm_usage": {"input_tokens": context_window + 10000},
                "_tracked_msg_count": sum(1 for m in messages if m.get("role") != "system"),
            },
            _services={"context_service": service, "chunk_service": fake_chunk},
        )

        result = await plugin.execute(ctx)
        assert not result.skip_remaining
        written = result.state_updates.get("messages")
        assert written is not None
        # 标准格式不应被破坏
        assert _find_raw_tc(written) == [], "标准格式不应出现 raw"
        assert _check_pairing_intact(written), "配对应完整"
        print(f"\n[P0回归] 标准输入未误伤: 写回 {len(written)} 条，全标准格式")
