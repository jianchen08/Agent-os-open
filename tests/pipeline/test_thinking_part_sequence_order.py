"""思考/正文 part 的 sequence 块级顺序单元测试。

钉死根因修复：sequence 必须按 part 块（thinking block / text block / tool）分配，
同一块内连续 chunk 共享一个 sequence，块切换时递增。

根因（e2e 抓包实锤，消息 b54e4f7e）：
  原实现每个 chunk 都调 _next_part_seq()，导致思考 token 把计数器推到 ~60，
  正文从 seq=62 开始；工具后第二轮思考 seq=77 > 正文(62)，
  前端按 sequence 数值排序 → 第二段思考被排到正文下方、且与正文数值范围重叠交错。

修复后期望（按块分配）：
  思考块1 → seq=1（所有思考1 chunk 共享）
  正文块  → seq=2（所有正文 chunk 共享）
  工具    → seq=3
  思考块2 → seq=4（所有思考2 chunk 共享）
  排序后与实际输出顺序一致，不再交错。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
pytestmark = pytest.mark.timing
# §9.4: 时序不变量门禁 — 此文件的测试断言可观察行为（事件顺序/间隔/超时边界/资源回收），
# 不含实现细节断言（mock.call_count/私有方法），破坏不变量的改动在 CI 阶段即被拦截。

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from pipeline.stream_bridge import PipelineStreamBridge
from pipeline.sink import IOutputSink


class _EventSink(IOutputSink):
    """Mock Sink：记录所有推送事件。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_event(self, event: dict[str, Any]) -> bool:
        self.events.append(event)
        return True

    @property
    def sink_id(self) -> str:
        return "order-sink"


def _run(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
        import concurrent.futures  # noqa: PLC0415
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result(timeout=10)
    except RuntimeError:
        return asyncio.run(coro)


@pytest.fixture
def bridge() -> PipelineStreamBridge:
    sink = _EventSink()
    return PipelineStreamBridge("pipe-order", sink, message_id="msg-order")


def _seqs(bridge: PipelineStreamBridge, etype: str) -> list[int]:
    """提取某类事件的 data.sequence 列表（按推送顺序）。"""
    out = []
    for e in bridge.output_sink.events:  # type: ignore[attr-defined]
        if e.get("type") == etype:
            s = (e.get("data") or {}).get("sequence")
            if isinstance(s, int):
                out.append(s)
    return out


class TestPartSequenceBlockOrder:
    """sequence 必须按 part 块分配，保证多段思考时思考块总在后续正文之前。"""

    def test_thinking_then_text_thinking_seq_lt_text(self, bridge: PipelineStreamBridge) -> None:
        """思考 → 正文：所有思考 chunk 的 sequence 必须 < 正文 sequence。"""
        # 思考块（多 chunk）
        for c in ["我", "在", "思考"]:
            _run(bridge._handle_chunk({"type": "thinking", "content": c}))
        # 正文块（多 chunk）
        for c in ["最终", "回复"]:
            _run(bridge._handle_chunk({"type": "text", "content": c}))

        think_seqs = _seqs(bridge, "thinking_chunk")
        text_seqs = _seqs(bridge, "stream_chunk")

        # 关键：思考的所有 sequence 都小于正文的所有 sequence
        assert think_seqs, "应产生 thinking_chunk"
        assert text_seqs, "应产生 stream_chunk"
        assert max(think_seqs) < min(text_seqs), (
            f"思考 sequence({think_seqs}) 必须 < 正文({text_seqs})，"
            "否则前端排序会把思考排到正文之后"
        )
        # 同一块内共享同一 sequence（块级，非 chunk 级）
        assert len(set(think_seqs)) == 1, "同一思考块的所有 chunk 应共享一个 sequence"

    def test_text_then_thinking_second_think_block_new_seq(self, bridge: PipelineStreamBridge) -> None:
        """正文 → 第二段思考：第二段思考是新块，sequence 必须大于正文（反映到达顺序）。"""
        # 正文
        _run(bridge._handle_chunk({"type": "text", "content": "正文一段"}))
        # 第二段思考
        _run(bridge._handle_chunk({"type": "thinking", "content": "二次思考"}))

        text_seqs = _seqs(bridge, "stream_chunk")
        think2_seqs = _seqs(bridge, "thinking_chunk")

        assert text_seqs and think2_seqs
        # 第二段思考是后到达的块，sequence 应大于正文（到达顺序）
        assert min(think2_seqs) > max(text_seqs), (
            f"第二段思考({think2_seqs}) 应 > 正文({text_seqs})，反映到达顺序"
        )

    def test_full_flow_thinking_text_tool_thinking_order(self, bridge: PipelineStreamBridge) -> None:
        """完整流程 思考→正文→工具→思考：各块 sequence 单调反映到达顺序，无交错。"""
        # 思考块1
        _run(bridge._handle_chunk({"type": "thinking", "content": "思考1"}))
        # 正文
        _run(bridge._handle_chunk({"type": "text", "content": "正文"}))
        # 工具
        _run(bridge._handle_chunk({
            "type": "tool_start", "tool_name": "file_read", "call_id": "c1",
        }))
        # 思考块2
        _run(bridge._handle_chunk({"type": "thinking", "content": "思考2"}))

        think1 = _seqs(bridge, "thinking_chunk")[0]
        text_seq = _seqs(bridge, "stream_chunk")[0]
        tool_seq = _seqs(bridge, "tool_start")[0]
        # 思考块2 的 thinking_start（第二段思考触发新 thinking_start）
        think2_starts = _seqs(bridge, "thinking_start")

        # 到达顺序：思考1 < 正文 < 工具 < 思考2
        assert think1 < text_seq < tool_seq
        assert think2_starts[-1] > tool_seq, (
            f"第二段思考({think2_starts[-1]}) 应 > 工具({tool_seq})，反映到达顺序"
        )

    def test_thinking_then_more_thinking_no_new_block(self, bridge: PipelineStreamBridge) -> None:
        """连续思考 chunk（无中间正文/工具）属同一块，只触发一次 thinking_start。"""
        for c in ["思", "考", "连续"]:
            _run(bridge._handle_chunk({"type": "thinking", "content": c}))

        starts = _seqs(bridge, "thinking_start")
        chunks = _seqs(bridge, "thinking_chunk")

        # 同一块：只一次 thinking_start，所有 chunk 共享其 sequence
        assert len(starts) == 1, "连续思考应只触发一次 thinking_start"
        assert len(set(chunks)) == 1, "同一思考块所有 chunk 共享同一 sequence"
        assert starts[0] == chunks[0]
