# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""llm_core 半截落库测试（统一路径：LLM 面唯一事实源 = llm_service）。

契约（新形态，2026-08-26 统一路径）：
- llm_core 经 capability caller（tool-executor.invoke → llm.complete_stream）
  调用；llm_service 在流中断/取消时把半截内容快照放进返回 dict 的
  ``partial`` 字段（跨进程可传），不再挂异常对象属性。
- ``_call_llm`` 见 partial → 组装 ``PartialStreamOutcome`` → execute 直接返回：
  半截 assistant 消息（``status: error|interrupted`` + ``llm_error_info``）
  经 ``messages._ops`` 落库；未闭合 tool_call（arguments 非完整 JSON）剥离；
  闭合 tool_call 保留 + 补占位 tool 结果（``status:"interrupted"``，assistant
  消息之后）；``raw_tool_calls`` 恒空（中断的调用绝不执行）；interrupted 路径
  置 ``ended=true``（引擎既有 ended 边界检查让 run 优雅收尾）。
- 无 partial（正常完成）→ 组装 LLMResponse 走成功路径；caller 未注入 → raise。
"""

from __future__ import annotations

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
# （_config_models 平铺 import）。插入顺序经设计：后插入者在 sys.path 更前，
# _LLM_CORE_DIR 必须压过 _SYSTEM_LLM_DIR——两处均有 adapter.py，
# 平铺 `import adapter` 须命中 llm_core 版（仅取 LLMResponse 类型）。
for _d in (_SYSTEM_LLM_DIR, _SHARED_DIR, _CORE_DIR, _LLM_CORE_DIR):
    if str(_d) in sys.path:
        sys.path.remove(str(_d))
    sys.path.insert(0, str(_d))

from llm_core.plugin import LLMCore, set_capability_caller  # noqa: E402

# ─────────────────── capability 伪调用器 ───────────────────


class _FakeCaller:
    """伪 capability caller：tool-executor.invoke 返回预设 dict。"""

    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        return self._result


def _partial_response(
    *,
    status: str = "error",
    text: str | None = None,
    thinking_text: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    error_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 llm_service 风格的 partial 返回 dict。"""
    result: dict[str, Any] = {
        "status": status,
        "stream_id": "stream_test",
        "partial": {
            "text": text,
            "thinking_text": thinking_text,
            "tool_calls": tool_calls or [],
            "usage": usage,
        },
        "text": None,
        "tool_calls": [],
        "thinking_text": None,
        "usage": {},
        "finish_reason": "interrupted" if status == "interrupted" else "error",
    }
    if error_info is not None:
        result["llm_error_info"] = error_info
    return result


def _make_plugin(caller: _FakeCaller) -> LLMCore:
    set_capability_caller(caller)
    return LLMCore(
        {"provider": "openai", "model_name": "deepseek-v3", "default_params": {}}
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


# ─────────────────── plugin：半截落库 ───────────────────


async def test_partial_error_returns_status_error_message() -> None:
    """error partial → 半截 assistant 消息 status=error + llm_error_info 落库。"""
    caller = _FakeCaller(
        _partial_response(
            status="error",
            text="部分回复",
            thinking_text="半截思考",
            error_info={"error_type": "RuntimeError", "error_message": "upstream broke"},
        )
    )
    result = await _make_plugin(caller).execute(_make_ctx(_base_state()))

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
    # error 路径不置 ended（引擎继续推进）
    assert result.get("ended") is not True
    # 调用通道：capability 短名 invoke（SDK 句柄组装 tool-executor.invoke 全名，
    # 传全名会双重前缀）→ llm.complete_stream
    assert caller.calls[0][0] == "invoke"
    assert caller.calls[0][1]["tool_name"] == "llm.complete_stream"


async def test_partial_usage_mapped() -> None:
    """partial 携带 usage → llm_usage 映射为管道口径（input/output/total/cached）。"""
    caller = _FakeCaller(
        _partial_response(
            text="x",
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cached_tokens": 1},
        )
    )
    result = await _make_plugin(caller).execute(_make_ctx(_base_state()))
    assert result["llm_usage"] == {
        "input_tokens": 7,
        "output_tokens": 3,
        "total_tokens": 10,
        "cached_tokens": 1,
    }


async def test_unclosed_tool_call_stripped() -> None:
    """未闭合 tool_call（arguments 半截 JSON）→ 从半截消息剥离，只留文本。"""
    caller = _FakeCaller(
        _partial_response(
            text="生成中断前的文本",
            tool_calls=[
                {"id": "call_half", "name": "file_write", "arguments": '{"code": "print('}
            ],
        )
    )
    result = await _make_plugin(caller).execute(_make_ctx(_base_state()))

    ops = result["messages"]["_ops"]
    assert len(ops) == 1  # 无占位 tool 消息
    msg = ops[0]["msg"]
    assert "tool_calls" not in msg
    assert msg["content"] == "生成中断前的文本"
    # 剥离的调用绝不执行
    assert result["raw_tool_calls"] == []


async def test_closed_tool_call_kept_with_placeholder_result() -> None:
    """闭合 tool_call → 保留 + 补占位 tool 结果（assistant 之后，status=interrupted）。"""
    caller = _FakeCaller(
        _partial_response(
            text="",
            tool_calls=[
                {"id": "call_00000000000000000000000a", "name": "bash", "arguments": '{"cmd": "ls"}'}
            ],
        )
    )
    result = await _make_plugin(caller).execute(_make_ctx(_base_state()))

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
    caller = _FakeCaller(
        _partial_response(
            tool_calls=[
                {"id": "call_bad", "name": "file_write", "arguments": '{"code": "pri'},
                {"id": "call_00000000000000000000000b", "name": "bash", "arguments": "{}"},
            ],
        )
    )
    result = await _make_plugin(caller).execute(_make_ctx(_base_state()))

    ops = result["messages"]["_ops"]
    assert len(ops) == 2  # 1 assistant + 1 占位
    kept_ids = [tc["id"] for tc in ops[0]["msg"]["tool_calls"]]
    assert kept_ids == ["call_00000000000000000000000b"]
    assert ops[1]["msg"]["tool_call_id"] == "call_00000000000000000000000b"


async def test_invalid_tool_call_id_normalized() -> None:
    """partial 携带非标准 tool_call_id → 标准化为 call_<hex>（与正常路径同规则）。"""
    caller = _FakeCaller(
        _partial_response(
            tool_calls=[
                {"id": "call_function_xxx_1", "name": "bash", "arguments": "{}"}
            ],
        )
    )
    result = await _make_plugin(caller).execute(_make_ctx(_base_state()))

    ops = result["messages"]["_ops"]
    tc_id = ops[0]["msg"]["tool_calls"][0]["id"]
    assert tc_id.startswith("call_")
    assert tc_id != "call_function_xxx_1"
    assert ops[1]["msg"]["tool_call_id"] == tc_id


async def test_interrupted_partial_sets_ended() -> None:
    """interrupted partial（调用方停止）→ status=interrupted + ended=true。"""
    caller = _FakeCaller(
        _partial_response(status="interrupted", text="半截回复")
    )
    result = await _make_plugin(caller).execute(_make_ctx(_base_state()))

    ops = result["messages"]["_ops"]
    assert ops[0]["msg"]["status"] == "interrupted"
    assert "llm_error_info" not in ops[0]["msg"]  # 取消不是错误
    assert result.get("ended") is True
    assert result["raw_tool_calls"] == []


async def test_interrupted_passthrough_run_id_only_for_chat() -> None:
    """会话轮次（run_id 注入）→ run_id 透传启用取消轮询；任务管道不传。"""
    caller = _FakeCaller(_partial_response(status="interrupted"))
    await _make_plugin(caller).execute(_make_ctx({**_base_state(), "run_id": "run-abc"}))
    args = caller.calls[0][1]["args"]
    assert args["run_id"] == "run-abc"

    caller2 = _FakeCaller(_partial_response(status="interrupted"))
    await _make_plugin(caller2).execute(
        _make_ctx({**_base_state(), "run_id": "run-abc", "task_id": "task-1"})
    )
    assert "run_id" not in caller2.calls[0][1]["args"]


# ─────────────────── plugin：成功路径 id 标准化（重构回归） ───────────────────


async def test_success_tool_calls_ids_normalized_inplace() -> None:
    """成功路径 tool_calls → 非标准 id 标准化并回写 raw_tool_calls 与 assistant 消息。"""
    caller = _FakeCaller(
        {
            "status": "streamed",
            "stream_id": "stream_test",
            "partial": None,
            "text": "calling tool",
            "tool_calls": [
                {"id": "call_function_xxx_9", "name": "bash", "args": '{"cmd":"ls"}'}
            ],
            "thinking_text": None,
            "usage": {},
            "finish_reason": "tool_calls",
        }
    )
    plugin = _make_plugin(caller)
    result = await plugin.execute(_make_ctx(_base_state()))

    raw_tc = result["raw_tool_calls"][0]
    assert raw_tc["id"].startswith("call_")
    assert raw_tc["id"] != "call_function_xxx_9"
    # assistant 消息与 raw_tool_calls 共用同一解析 id（tool_core 配对一致性）
    assistant = result["messages"]["_ops"][0]["msg"]
    assert assistant["tool_calls"][0]["id"] == raw_tc["id"]
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"cmd":"ls"}'


async def test_success_text_response_assembles_llmresponse() -> None:
    """正常完成（partial=None）→ LLMResponse 组装（text/usage/finish_reason）。"""
    caller = _FakeCaller(
        {
            "status": "streamed",
            "stream_id": "stream_test",
            "partial": None,
            "text": "Hello",
            "tool_calls": [],
            "thinking_text": "plan",
            "usage": {"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14, "cached_tokens": 0},
            "finish_reason": "stop",
        }
    )
    result = await _make_plugin(caller).execute(_make_ctx(_base_state()))

    assert result["raw_result"] == "Hello"
    assert result["raw_thinking"] == "plan"
    assert result["llm_usage"] == {
        "input_tokens": 5,
        "output_tokens": 9,
        "total_tokens": 14,
        "cached_tokens": 0,
    }
    assert result["messages"]["_ops"][0]["msg"]["content"] == "Hello"


async def test_no_caller_raises() -> None:
    """capability caller 未注入 → 明确错误上抛（接线 bug 早暴露）。"""
    set_capability_caller(None)
    plugin = LLMCore(
        {"provider": "openai", "model_name": "deepseek-v3", "default_params": {}}
    )
    with pytest.raises(RuntimeError, match="capability caller 未注入"):
        await plugin.execute(_make_ctx(_base_state()))
