# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""llm_core 流式中途失败半截落库测试（聊天中断保留方案 2026-08-26 批次 A）。

契约：
- adapter：流已开始（首 chunk 已到达）后异常 → 异常对象挂
  ``llm_partial_snapshot``（text/thinking_text/tool_calls/usage 快照），
  不改异常类型；流未开始（建连/首 chunk 阶段）或零累积内容 → 无快照。
- plugin：快照存在 → execute 正常返回，半截 assistant 消息（``status:"error"`` +
  ``llm_error_info``）经 ``messages._ops`` 落库；未闭合 tool_call（arguments 非
  完整 JSON）剥离；闭合 tool_call 保留 + 补占位 tool 结果（``status:"interrupted"``，
  assistant 消息之后）；``raw_tool_calls`` 恒空（中断的调用绝不执行）；
  无快照 → 维持 raise（现状不变）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LLM_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "pipeline" / "core" / "llm_core"
_CORE_DIR = _LLM_CORE_DIR.parent
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
_SYSTEM_LLM_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "llm"
# 平铺 import 路径：llm_core 平铺模块 + pipeline namespace 包根 + system/llm
# （execute 内 key_pool/_config_models 平铺 import）。插入顺序经设计：后插入者
# 在 sys.path 更前，_LLM_CORE_DIR 必须压过 _SYSTEM_LLM_DIR——两处均有 adapter.py，
# 平铺 `import adapter` 须命中 llm_core 版（system/llm 是前身，无快照逻辑）；
# key_pool/_config_models 仅 system/llm 有，llm_core 目录找不到自然向后解析。
for _d in (_SYSTEM_LLM_DIR, _SHARED_DIR, _CORE_DIR, _LLM_CORE_DIR):
    if str(_d) in sys.path:
        sys.path.remove(str(_d))
    sys.path.insert(0, str(_d))

import adapter as _adapter  # noqa: E402
import litellm  # noqa: E402
from llm_core.plugin import LLMCore  # noqa: E402

_SNAPSHOT_ATTR = "llm_partial_snapshot"


# ─────────────────── adapter：快照挂载 ───────────────────


class _MidwayFailStream:
    """先吐预设 chunk、再抛指定异常的流（模拟流中途失败）。"""

    def __init__(self, chunks: list[Any], exc: BaseException) -> None:
        self._chunks = list(chunks)
        self._it = iter(self._chunks)
        self._exc = exc
        self.aclose_called = False
        self.completion_stream = SimpleNamespace(is_closed=False)

    def __aiter__(self) -> _MidwayFailStream:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._it)
        except StopIteration:
            raise self._exc from None

    async def aclose(self) -> None:
        self.aclose_called = True


class _StreamAdapter(_adapter._BaseLiteLLMAdapter):
    """``_do_completion`` 返回预设流。"""

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    async def _do_completion(self, **kwargs: Any) -> Any:
        return self._stream


def _text_chunk(content: str) -> SimpleNamespace:
    delta = SimpleNamespace(content=content, reasoning_content=None, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)])


def _thinking_chunk(reasoning: str) -> SimpleNamespace:
    delta = SimpleNamespace(content=None, reasoning_content=reasoning, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)])


async def test_midstream_failure_attaches_snapshot() -> None:
    """流开始后失败：text/thinking 累积快照挂到异常对象（类型不变）。"""
    exc = RuntimeError("midway boom")
    stream = _MidwayFailStream(
        [_text_chunk("Hello"), _thinking_chunk("plan")], exc
    )
    ad = _StreamAdapter(stream)

    with pytest.raises(RuntimeError, match="midway boom"):
        await ad._call_streaming(
            "m", [{"role": "user", "content": "x"}],
            inter_chunk_timeout=600, first_chunk_timeout=10,
        )

    snapshot = getattr(exc, _SNAPSHOT_ATTR)
    assert snapshot == {
        "text": "Hello",
        "thinking_text": "plan",
        "tool_calls": [],
        "usage": None,
    }


async def test_pre_stream_failure_has_no_snapshot() -> None:
    """建连阶段失败（流未开始）：异常无快照 → 上层维持原 raise 路径。"""
    exc = asyncio.TimeoutError()

    class _RaisingAdapter(_adapter._BaseLiteLLMAdapter):
        async def _do_completion(self, **kwargs: Any) -> Any:
            raise exc

    # 建连超时按首 token 失败语义转 litellm.Timeout（现状不变）
    with pytest.raises(litellm.Timeout):
        await _RaisingAdapter()._call_streaming(
            "m", [{"role": "user", "content": "x"}],
            inter_chunk_timeout=600, first_chunk_timeout=10,
        )
    assert not hasattr(exc, _SNAPSHOT_ATTR)


async def test_midstream_failure_zero_content_no_snapshot() -> None:
    """流已开始但零累积内容（首 chunk 即失败）：不挂空快照。"""
    exc = RuntimeError("empty midway")
    stream = _MidwayFailStream([], exc)
    # 首个 __anext__ 直接抛异常（首 chunk 读取失败 → 流未真正开始）
    ad = _StreamAdapter(stream)

    with pytest.raises(RuntimeError, match="empty midway"):
        await ad._call_streaming(
            "m", [{"role": "user", "content": "x"}],
            inter_chunk_timeout=600, first_chunk_timeout=10,
        )
    assert not hasattr(exc, _SNAPSHOT_ATTR)


# ─────────────────── plugin：半截落库 ───────────────────


class _SnapshotRaisingAdapter:
    """completion 抛携带部分快照的异常（模拟 adapter 半截快照链路）。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def completion(
        self, *, model: str, messages: list[dict[str, Any]], **kwargs: Any
    ) -> Any:
        raise self._exc


def _make_plugin(exc: Exception) -> LLMCore:
    return LLMCore(
        {"provider": "openai", "model_name": "deepseek-v3", "default_params": {}},
        adapter=_SnapshotRaisingAdapter(exc),  # type: ignore[arg-type]
    )


def _make_ctx(state: dict[str, Any]) -> Any:
    ctx = SimpleNamespace()
    ctx.state = state
    return ctx


def _base_state() -> dict[str, Any]:
    return {
        "messages": [{"role": "user", "content": "hi"}],
        "streaming": True,
        "pipeline_id": "test-partial-persist",
    }


async def test_partial_snapshot_returns_status_error_message() -> None:
    """有快照 → 正常返回：半截 assistant 消息 status=error + llm_error_info 落库。"""
    exc = RuntimeError("upstream broke")
    exc.llm_partial_snapshot = {  # type: ignore[attr-defined]
        "text": "部分回复",
        "thinking_text": "半截思考",
        "tool_calls": [],
        "usage": None,
    }
    result = await _make_plugin(exc).execute(_make_ctx(_base_state()))

    assert result["raw_result"] == "部分回复"
    assert result["raw_error"] is None
    assert result["raw_tool_calls"] == []
    ops = result["messages"]["_ops"]
    assert len(ops) == 1
    msg = ops[0]["msg"]
    assert msg["role"] == "assistant"
    assert msg["content"] == "部分回复"
    assert msg["status"] == "error"
    assert msg["llm_error_info"]["error_type"] == "RuntimeError"
    assert "upstream broke" in msg["llm_error_info"]["error_message"]
    assert msg["reasoning_content"] == "半截思考"


async def test_partial_usage_mapped() -> None:
    """快照携带 usage → llm_usage 映射为管道口径（input/output/total/cached）。"""
    exc = RuntimeError("usage path")
    exc.llm_partial_snapshot = {  # type: ignore[attr-defined]
        "text": "x",
        "thinking_text": None,
        "tool_calls": [],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cached_tokens": 1},
    }
    result = await _make_plugin(exc).execute(_make_ctx(_base_state()))
    assert result["llm_usage"] == {
        "input_tokens": 7,
        "output_tokens": 3,
        "total_tokens": 10,
        "cached_tokens": 1,
    }


async def test_unclosed_tool_call_stripped() -> None:
    """未闭合 tool_call（arguments 半截 JSON）→ 从半截消息剥离，只留文本。"""
    exc = RuntimeError("tc midway")
    exc.llm_partial_snapshot = {  # type: ignore[attr-defined]
        "text": "生成中断前的文本",
        "thinking_text": None,
        "tool_calls": [
            {"id": "call_half", "name": "file_write", "arguments": '{"code": "print('}
        ],
        "usage": None,
    }
    result = await _make_plugin(exc).execute(_make_ctx(_base_state()))

    ops = result["messages"]["_ops"]
    assert len(ops) == 1  # 无占位 tool 消息
    msg = ops[0]["msg"]
    assert "tool_calls" not in msg
    assert msg["content"] == "生成中断前的文本"
    # 剥离的调用绝不执行
    assert result["raw_tool_calls"] == []


async def test_closed_tool_call_kept_with_placeholder_result() -> None:
    """闭合 tool_call → 保留 + 补占位 tool 结果（assistant 之后，status=interrupted）。"""
    exc = RuntimeError("tc closed")
    exc.llm_partial_snapshot = {  # type: ignore[attr-defined]
        "text": "",
        "thinking_text": None,
        "tool_calls": [
            {"id": "call_00000000000000000000000a", "name": "bash", "arguments": '{"cmd": "ls"}'}
        ],
        "usage": None,
    }
    result = await _make_plugin(exc).execute(_make_ctx(_base_state()))

    ops = result["messages"]["_ops"]
    assert len(ops) == 2
    assistant = ops[0]["msg"]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["id"] == "call_00000000000000000000000a"
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"cmd": "ls"}'
    placeholder = ops[1]["msg"]
    assert placeholder["role"] == "tool"
    assert placeholder["tool_call_id"] == "call_00000000000000000000000a"
    assert placeholder["status"] == "interrupted"
    assert "生成中断" in placeholder["content"]
    # 保留仅为配对完整，绝不执行
    assert result["raw_tool_calls"] == []


async def test_mixed_tool_calls_only_closed_kept() -> None:
    """闭合与未闭合并存 → 只保留闭合的（区分度输入：剥离逻辑非全删/全留）。"""
    exc = RuntimeError("mixed")
    exc.llm_partial_snapshot = {  # type: ignore[attr-defined]
        "text": "",
        "thinking_text": None,
        "tool_calls": [
            {"id": "call_bad", "name": "file_write", "arguments": '{"code": "pri'},
            {"id": "call_00000000000000000000000b", "name": "bash", "arguments": "{}"},
        ],
        "usage": None,
    }
    result = await _make_plugin(exc).execute(_make_ctx(_base_state()))

    ops = result["messages"]["_ops"]
    assert len(ops) == 2  # 1 assistant + 1 占位
    kept_ids = [tc["id"] for tc in ops[0]["msg"]["tool_calls"]]
    assert kept_ids == ["call_00000000000000000000000b"]
    assert ops[1]["msg"]["tool_call_id"] == "call_00000000000000000000000b"


async def test_invalid_tool_call_id_normalized() -> None:
    """快照携带非标准 tool_call_id → 标准化为 call_<hex>（与正常路径同规则）。"""
    exc = RuntimeError("bad id")
    exc.llm_partial_snapshot = {  # type: ignore[attr-defined]
        "text": "",
        "thinking_text": None,
        "tool_calls": [
            {"id": "call_function_xxx_1", "name": "bash", "arguments": "{}"}
        ],
        "usage": None,
    }
    result = await _make_plugin(exc).execute(_make_ctx(_base_state()))

    ops = result["messages"]["_ops"]
    tc_id = ops[0]["msg"]["tool_calls"][0]["id"]
    assert tc_id.startswith("call_")
    assert tc_id != "call_function_xxx_1"
    assert ops[1]["msg"]["tool_call_id"] == tc_id


async def test_no_snapshot_still_raises() -> None:
    """无快照异常（流未开始/零内容）→ 维持 raise（现状不变）。"""
    exc = ValueError("hard fail")
    with pytest.raises(ValueError, match="hard fail"):
        await _make_plugin(exc).execute(_make_ctx(_base_state()))
