"""llm.complete_stream 流式协议翻译——DSH 8 事件形态（块索引化）。

把 adapter 归一化 chunk（``on_chunk`` 契约：``{"type", "content"}`` 与
``{"type": "tool_call", "tool_calls": [...]}``）翻译为块索引化事件序列：

- ``block_start{index, block_type}`` / ``text_delta{index, text}`` /
  ``reasoning_delta{index, text}`` / ``tool_call_delta{index, id?, name?,
  arguments_delta}`` / ``block_end{index, block}`` / ``usage{input_tokens,
  output_tokens, ...}`` / ``finish{reason}`` / ``keepalive``。
- 每个 text/reasoning/tool-call 块独立 index，增量按 index 归组；类型切换先
  闭旧块再开新块。并行 tool_call 块（不同流 index）同时保持打开，增量按各自
  块索引发出，收尾统一闭合。
- tool 参数 ``arguments_delta`` 为原始 JSON 字符串增量累积，不中途解析。
- ``finish`` 幂等：断流兜底（finish 前异常）由调用方补发 ``finish{reason:
  error}``，重复触发安全。

事件词汇为 DSH 形态定稿（方案 2026-08-26 变更#1），旧名
``thinking_start/thinking_chunk/thinking_end/stream_chunk/stream_keepalive``
退役，本模块不产出旧事件。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StreamEvent:
    """一个待推送的流式事件（event 名 + 载荷）。"""

    event: str
    payload: dict[str, Any]


# finish reason 词汇收敛：LLM 返回的未知结束原因（如 content_filter）映射 stop，
# 消费端只按 stop/length/tool_calls/error 四值渲染。
_FINISH_REASON_MAP: dict[str | None, str] = {
    None: "stop",
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "error": "error",
}


def map_finish_reason(reason: str | None) -> str:
    """把 adapter 返回的 finish_reason 收敛为协议四值之一。"""
    return _FINISH_REASON_MAP.get(reason, "stop")


@dataclass
class _ToolBlockState:
    """单个 tool_call 块的累积状态（按块索引独立，并行工具互不污染）。"""

    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)


class StreamTranslator:
    """chunk → 块索引化事件 的纯翻译器（无 IO，可独立单测）。

    状态：当前活动的块（类型 + 索引）与各块累积内容。块索引按开块顺序分配
    （text/reasoning/tool_call 共享同一递增序列——DSH 形态块索引化）。
    并行 tool_call 块保持打开直到类型切换或 finish 统一闭合。
    """

    def __init__(self) -> None:
        self._next_index = 0
        # 当前活动块（最近写入的块）
        self._open_type: str | None = None
        self._open_index = -1
        self._open_text: list[str] = []
        # tool_call 块：流 index → 块索引；块索引 → 累积状态；打开顺序表
        self._tool_stream_to_block: dict[int, int] = {}
        self._tool_blocks: dict[int, _ToolBlockState] = {}
        self._open_tool_indices: list[int] = []
        self.finished = False

    # ── 块管理 ────────────────────────────────────────────

    def _tool_block_end(self, index: int) -> StreamEvent:
        """构造 tool_call 块的 block_end（块索引 → 累积 payload）。"""
        state = self._tool_blocks.get(index, _ToolBlockState())
        return StreamEvent(
            "block_end",
            {
                "index": index,
                "block": {
                    "block_type": "tool_call",
                    "id": state.id,
                    "name": state.name,
                    "arguments": "".join(state.arguments),
                },
            },
        )

    def _close_block(self) -> list[StreamEvent]:
        """闭当前活动块：产出 block_end{index, block}。无活动块返回空。"""
        if self._open_type is None:
            return []
        if self._open_type == "tool_call":
            index = self._open_index
            event = self._tool_block_end(index)
            self._open_tool_indices = [i for i in self._open_tool_indices if i != index]
        else:
            event = StreamEvent(
                "block_end",
                {
                    "index": self._open_index,
                    "block": {"block_type": self._open_type, "text": "".join(self._open_text)},
                },
            )
        self._open_type = None
        self._open_text = []
        return [event]

    def _close_all_tool_blocks(self) -> list[StreamEvent]:
        """闭全部仍打开的 tool_call 块（类型切换离开 tool_call 族时）。"""
        events = self._close_block()  # 活动块若是 tool_call 先闭
        for index in list(self._open_tool_indices):
            events.append(self._tool_block_end(index))
        self._open_tool_indices = []
        return events

    def _open_block(self, block_type: str) -> list[StreamEvent]:
        """开新块：先闭旧块（类型切换闭旧块），再产出 block_start。"""
        if self._open_type == "tool_call":
            # 离开 tool_call 族：闭全部打开的 tool 块（并行块不残留）
            events = self._close_all_tool_blocks()
        else:
            events = self._close_block()
        self._open_type = block_type
        self._open_index = self._next_index
        self._next_index += 1
        events.append(StreamEvent("block_start", {"index": self._open_index, "block_type": block_type}))
        return events

    # ── 事件翻译 ──────────────────────────────────────────

    def translate(self, chunk: dict[str, Any]) -> list[StreamEvent]:
        """翻译单个归一化 chunk 为事件列表（可为空）。"""
        if self.finished:
            return []
        chunk_type = chunk.get("type", "text")
        if chunk_type == "thinking":
            content = chunk.get("content", "")
            if not content:
                return []
            events = self._open_block("reasoning") if self._open_type != "reasoning" else []
            self._open_text.append(content)
            events.append(
                StreamEvent("reasoning_delta", {"index": self._open_index, "text": content})
            )
            return events
        if chunk_type == "thinking_end":
            # 思考块由 block_end 闭合（thinking_start/chunk/end 三事件退役）
            if self._open_type == "reasoning":
                return self._close_block()
            return []
        if chunk_type == "tool_call":
            return self._translate_tool_call(chunk)
        # text：正文增量
        content = chunk.get("content", "")
        if not content:
            return []
        events = self._open_block("text") if self._open_type != "text" else []
        self._open_text.append(content)
        events.append(StreamEvent("text_delta", {"index": self._open_index, "text": content}))
        return events

    def _translate_tool_call(self, chunk: dict[str, Any]) -> list[StreamEvent]:
        """工具调用增量：按流 index 归组累积，arguments_delta 原始字符串。"""
        events: list[StreamEvent] = []
        for tc in chunk.get("tool_calls", []):
            stream_idx = max(int(getattr(tc, "index", 0)), 0)
            # 流 index 首次出现 → 分配新块（并行工具块同时保持打开）
            if stream_idx not in self._tool_stream_to_block:
                if self._open_type is not None and self._open_type != "tool_call":
                    events.extend(self._close_block())
                block_index = self._next_index
                self._next_index += 1
                self._tool_stream_to_block[stream_idx] = block_index
                self._tool_blocks[block_index] = _ToolBlockState()
                self._open_tool_indices.append(block_index)
                self._open_type = "tool_call"
                self._open_index = block_index
                events.append(
                    StreamEvent("block_start", {"index": block_index, "block_type": "tool_call"})
                )
            block_index = self._tool_stream_to_block[stream_idx]
            state = self._tool_blocks[block_index]
            delta: dict[str, Any] = {"index": block_index}
            function = getattr(tc, "function", None)
            if function is not None:
                tc_id = getattr(tc, "id", None) or ""
                if tc_id and not state.id:
                    state.id = tc_id
                    delta["id"] = tc_id
                name = getattr(function, "name", "") or ""
                if name and not state.name:
                    state.name = name
                    delta["name"] = name
                arguments = getattr(function, "arguments", "") or ""
                if arguments:
                    state.arguments.append(arguments)
                    delta["arguments_delta"] = arguments
            if set(delta.keys()) != {"index"}:
                events.append(StreamEvent("tool_call_delta", delta))
        return events

    # ── 收尾 ──────────────────────────────────────────────

    def finish(self, reason: str, usage: dict[str, Any] | None = None) -> list[StreamEvent]:
        """收尾：闭全部块 → usage → finish。幂等：已 finish 返回空。"""
        if self.finished:
            return []
        self.finished = True
        events = self._close_all_tool_blocks() if self._open_type == "tool_call" else self._close_block()
        if usage:
            events.append(
                StreamEvent(
                    "usage",
                    {
                        "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
                        "output_tokens": int(usage.get("completion_tokens", 0) or 0),
                        "total_tokens": int(usage.get("total_tokens", 0) or 0),
                        "cached_tokens": int(usage.get("cached_tokens", 0) or 0),
                    },
                )
            )
        events.append(StreamEvent("finish", {"reason": reason}))
        return events
