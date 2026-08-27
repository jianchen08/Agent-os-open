# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core plugin.py execute 路径补测——模型解析/消息装配/信封校验/错误路径。

契约（0.2 统一路径）：LLM 面唯一事实源 = llm_service（经 capability caller
``invoke`` → ``llm.complete_stream``）；返回 dict 带 ``partial`` 走半截落库，
否则组装 LLMResponse。本文件覆盖既有测试未触及的 execute 分支：

- ``_apply_model_from_state``：state.model_tier → defaults.tiers 解析、llm.yaml
  配置更新 self、model_id 未配置保持现状；
- ``_build_messages``：compression_messages 剥离 ``_context_form``、history 剥离
  ``seq``/``tool_result``、multimodal 合并进 list 型 content、dynamic_vars 三种形态；
- ``_writeback_cleaned_history``：normalize 清理写回 state["messages"]（含早退）；
- 成功路径：tool_call arguments >100 字符诊断、tool_calls+thinking 的
  reasoning_content、finish_reason=length → output_truncated；
- 信封校验 fail-closed：非 dict 信封 / success=false / data 非 dict → RuntimeError；
- 错误路径：tool_call 相关异常 → 重置配对缓存后上抛。

加载：importlib 唯一模块名装载 plugin.py（裸名 ``plugin`` 会被兄弟插件目录
串扰）；``_config_models`` 经 system/llm 目录平铺 import（与 server.py 同解析）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[5]
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

_MOD_NAME = "llm_core_execute_paths_under_test"


def _load_plugin() -> Any:
    """加载 llm_core/plugin.py（唯一模块名，进程内缓存）。"""
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _LLM_CORE_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None, "cannot load llm_core plugin.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load_plugin()
LLMCore = _mod.LLMCore
set_capability_caller = _mod.set_capability_caller


class _FakeCaller:
    """伪 capability caller：tool-executor.invoke 返回预设 dict 或抛预设异常。"""

    def __init__(self, result: Any = None, exc: BaseException | None = None) -> None:
        self._result = result
        self._exc = exc
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if self._exc is not None:
            raise self._exc
        return self._result


def _ok_response(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "streamed",
        "stream_id": "stream_test",
        "partial": None,
        "text": "ok",
        "tool_calls": [],
        "thinking_text": None,
        "usage": {},
        "finish_reason": "stop",
    }
    result.update(overrides)
    return result


def _make_plugin(caller: Any, config: dict[str, Any] | None = None) -> Any:
    set_capability_caller(caller)
    return LLMCore(
        config
        or {
            "provider": "openai",
            "model_name": "deepseek-v3",
            "default_params": {},
        }
    )


def _make_ctx(state: dict[str, Any]) -> Any:
    ctx = SimpleNamespace()
    ctx.state = state
    return ctx


def _base_state() -> dict[str, Any]:
    return {
        "messages": [{"role": "user", "content": "hi"}],
        "streaming": True,
        "pipeline_id": "test-execute-paths",
    }


# ─────────────────── 模型动态解析（_apply_model_from_state） ───────────────────


def _inject_llm_config(monkeypatch: Any) -> None:
    """注入 _config_models 配置桥：llm.yaml 含 models/defaults 段（monkeypatch 还原）。"""
    import _config_models

    monkeypatch.setattr(
        _config_models,
        "_config",
        {
            "llm": {
                "models": {
                    "deepseek-v4-pro": {
                        "provider": "deepseek",
                        "model_name": "deepseek-v4-pro",
                        "api_base": "https://api.example.com",
                        "api_key": "k1",
                        "context_window": 64000,
                        "default_params": {"temperature": 0.3},
                        "thinking_strength_params": {"high": {"reasoning_effort": "max"}},
                    }
                },
                "defaults": {"tiers": {"large": "deepseek-v4-pro"}, "chat": "deepseek-v4-pro"},
            }
        },
    )


async def test_model_tier_resolves_and_updates_self(monkeypatch: Any) -> None:
    """state.model_tier → defaults.tiers 解析 → llm.yaml 配置更新 self（含强度路由）。"""
    _inject_llm_config(monkeypatch)
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    assert plugin._model_id == ""  # noqa: SLF001

    await plugin.execute(_make_ctx({**_base_state(), "model_tier": "large"}))

    assert plugin._model_id == "deepseek-v4-pro"  # noqa: SLF001
    assert plugin._provider == "deepseek"  # noqa: SLF001
    assert plugin._model == "deepseek-v4-pro"  # noqa: SLF001
    assert plugin._api_base == "https://api.example.com"  # noqa: SLF001
    assert plugin._api_key == "k1"  # noqa: SLF001
    assert plugin._context_window == 64000  # noqa: SLF001
    assert plugin._default_params == {"temperature": 0.3}  # noqa: SLF001
    assert plugin._thinking_strength_params == {"high": {"reasoning_effort": "max"}}  # noqa: SLF001
    # 调用通道：model 用 yaml key（model_id）做 deployment 匹配
    assert caller.calls[0][1]["args"]["model"] == "deepseek-v4-pro"


async def test_model_id_unknown_keeps_current(monkeypatch: Any) -> None:
    """state.model_id 在 llm.yaml 未配置 → 保持当前 model 不阻断（调用方降级）。"""
    _inject_llm_config(monkeypatch)
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    await plugin.execute(_make_ctx({**_base_state(), "model_id": "no-such-model"}))
    assert plugin._model == "deepseek-v3"  # noqa: SLF001
    assert plugin._model_id == ""  # noqa: SLF001


async def test_model_id_same_skips_reparse(monkeypatch: Any) -> None:
    """已锁定同一 model_id → 跳过重复解析（幂等，不重复打 resolved 日志）。"""
    _inject_llm_config(monkeypatch)
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller, {"model_id": "deepseek-v4-pro"})
    await plugin.execute(_make_ctx(_base_state()))
    assert plugin._model_id == "deepseek-v4-pro"  # noqa: SLF001
    # 构造配置未变（model_name 仍是默认 gpt-4）——跳过解析即不更新 self
    assert plugin._model == "gpt-4"  # noqa: SLF001


# ─────────────────── _build_messages 装配分支 ───────────────────


def test_build_messages_strips_internal_fields() -> None:
    """compression/history 内部标记字段（_context_form/seq/tool_result）发送前剥离。"""
    pre = LLMCore.__new__(LLMCore)
    state = {
        "system_message": {"role": "system", "content": "sys"},
        "compression_messages": [
            {"role": "user", "content": "c1", "_context_form": "snapshot"},
        ],
        "messages": [
            {"role": "user", "content": "q", "seq": 3},
            {"role": "tool", "tool_call_id": "call_abc123", "content": "r", "tool_result": {"ok": True}},
            {"role": "assistant", "content": "a", "_context_form": "x"},
        ],
    }
    msgs = pre._build_messages(state)  # noqa: SLF001
    assert msgs[0] == {"role": "system", "content": "sys"}
    assert msgs[1] == {"role": "user", "content": "c1"}  # _context_form 剥离
    assert msgs[2] == {"role": "user", "content": "q"}  # seq 剥离
    assert msgs[3] == {"role": "tool", "tool_call_id": "call_abc123", "content": "r"}  # tool_result 剥离
    assert msgs[4] == {"role": "assistant", "content": "a"}  # _context_form 剥离


def test_build_messages_multimodal_merges_into_list_content() -> None:
    """最后一条 user 消息 content 已是 list → 直接 extend 多模态块。"""
    pre = LLMCore.__new__(LLMCore)
    state = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "看图"}]},
        ],
        "multimodal_content": [
            {"type": "image_url", "image_url": {"url": "https://a.com/x.png"}},
        ],
    }
    msgs = pre._build_messages(state)  # noqa: SLF001
    content = msgs[0]["content"]
    assert content == [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "https://a.com/x.png"}},
    ]


def test_build_messages_dynamic_vars_three_forms() -> None:
    """dynamic_vars：dict 取 content / 非 dict 转 str / 空 content 不追加。"""
    pre = LLMCore.__new__(LLMCore)
    base = {"messages": [{"role": "user", "content": "q"}]}

    msgs = pre._build_messages({**base, "prompt.dynamic_vars": {"content": "now=1"}})  # noqa: SLF001
    assert msgs[-1] == {"role": "user", "content": "now=1"}

    msgs2 = pre._build_messages({**base, "prompt.dynamic_vars": "now=2"})  # noqa: SLF001
    assert msgs2[-1] == {"role": "user", "content": "now=2"}

    msgs3 = pre._build_messages({**base, "prompt.dynamic_vars": {"content": ""}})  # noqa: SLF001
    assert len(msgs3) == 1  # 空 content 不追加


# ─────────────────── _writeback_cleaned_history ───────────────────


async def test_call_llm_writeback_cleaned_history(monkeypatch: Any) -> None:
    """normalize 清理（孤儿 tool result 被丢）→ 清理后历史写回 state["messages"]。"""
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    state = {
        **_base_state(),
        "system_message": {"role": "system", "content": "sys"},
        "compression_messages": [{"role": "user", "content": "c1"}],
        "messages": [
            {"role": "user", "content": "q"},
            {"role": "tool", "tool_call_id": "call_orphan", "content": "r"},
        ],
        "prompt.dynamic_vars": {"content": "now=1"},
    }
    await plugin.execute(_make_ctx(state))
    # 孤儿 tool result 被配对校验丢弃 → 写回后只剩 user 消息
    assert state["messages"] == [{"role": "user", "content": "q"}]


async def test_call_llm_writeback_skipped_when_nothing_cleaned() -> None:
    """normalize 无清理（历史长度不变）→ 不写回（早退，state 原样）。"""
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    state = {
        **_base_state(),
        "system_message": {"role": "system", "content": "sys"},
        "messages": [{"role": "user", "content": "q"}],
    }
    await plugin.execute(_make_ctx(state))
    assert state["messages"] == [{"role": "user", "content": "q"}]


# ─────────────────── 成功路径组装分支 ───────────────────


async def test_success_long_arguments_diag_and_thinking_with_tool_calls() -> None:
    """tool_call arguments >100 字符触发诊断日志；tool_calls+thinking → reasoning_content。"""
    caller = _FakeCaller(
        {
            "success": True,
            "data": _ok_response(
                text="calling",
                thinking_text="plan",
                tool_calls=[
                    {"id": "call_abc123", "name": "bash", "args": "x" * 150}
                ],
                finish_reason="tool_calls",
            ),
        }
    )
    result = await _make_plugin(caller).execute(_make_ctx(_base_state()))

    assistant = result["messages"]["_ops"][0]["msg"]
    assert assistant["role"] == "assistant"
    assert assistant["reasoning_content"] == "plan"
    assert assistant["tool_calls"][0]["function"]["arguments"] == "x" * 150
    assert result["output_truncated"] is False


async def test_success_finish_reason_length_sets_output_truncated() -> None:
    """finish_reason=length（命中 max_tokens）→ output_truncated=True 供下游识别截断。"""
    caller = _FakeCaller(
        {"success": True, "data": _ok_response(text="半截回复", finish_reason="length")}
    )
    result = await _make_plugin(caller).execute(_make_ctx(_base_state()))
    assert result["output_truncated"] is True
    assert result["raw_result"] == "半截回复"


async def test_success_no_text_no_tool_calls_no_messages_update() -> None:
    """无文本且无工具调用 → 不 emit append op（messages 键缺失）。"""
    caller = _FakeCaller(
        {"success": True, "data": _ok_response(text=None, tool_calls=[], thinking_text=None)}
    )
    result = await _make_plugin(caller).execute(_make_ctx(_base_state()))
    assert "messages" not in result
    assert result["raw_result"] is None


# ─────────────────── 信封校验 fail-closed ───────────────────


async def test_envelope_non_dict_raises() -> None:
    """caller 返回非 dict 信封 → RuntimeError（fail-closed，不盲取字段）。"""
    caller = _FakeCaller(result="not-a-dict")
    with pytest.raises(RuntimeError, match="信封形状异常"):
        await _make_plugin(caller).execute(_make_ctx(_base_state()))


async def test_envelope_success_false_raises() -> None:
    """信封 success=false（工具未注册/执行失败）→ RuntimeError 携带 error。"""
    caller = _FakeCaller(result={"success": False, "data": None, "error": "tool not found"})
    with pytest.raises(RuntimeError, match="工具执行失败"):
        await _make_plugin(caller).execute(_make_ctx(_base_state()))


async def test_envelope_data_non_dict_raises() -> None:
    """信封 data 非 dict → RuntimeError（返回形状异常）。"""
    caller = _FakeCaller(result={"success": True, "data": ["not", "a", "dict"]})
    with pytest.raises(RuntimeError, match="返回形状异常"):
        await _make_plugin(caller).execute(_make_ctx(_base_state()))


# ─────────────────── 错误路径：tool_call 相关异常重置配对缓存 ───────────────────


async def test_tool_call_error_resets_pairing_cache(monkeypatch: Any) -> None:
    """tool_call 相关异常 → 重置当前管道配对缓存后上抛（下次全量扫描）。"""
    import _message_normalizer

    monkeypatch.setattr(_message_normalizer, "_pairing_validated_len", {})
    _message_normalizer._pairing_validated_len["openai:llm_core:test-execute-paths"] = (2, "fp")

    caller = _FakeCaller(exc=RuntimeError("tool_call pairing failed: insufficient tool messages"))
    with pytest.raises(RuntimeError, match="tool_call pairing failed"):
        await _make_plugin(caller).execute(_make_ctx(_base_state()))

    assert "openai:llm_core:test-execute-paths" not in _message_normalizer._pairing_validated_len


async def test_non_tool_call_error_propagates_without_reset(monkeypatch: Any) -> None:
    """非 tool_call 相关异常 → 原样上抛，不触碰配对缓存。"""
    import _message_normalizer

    monkeypatch.setattr(_message_normalizer, "_pairing_validated_len", {})
    _message_normalizer._pairing_validated_len["openai:llm_core:test-execute-paths"] = (2, "fp")

    caller = _FakeCaller(exc=RuntimeError("upstream connection refused"))
    with pytest.raises(RuntimeError, match="upstream connection refused"):
        await _make_plugin(caller).execute(_make_ctx(_base_state()))

    assert "openai:llm_core:test-execute-paths" in _message_normalizer._pairing_validated_len


# ─────────────────── 杂项：构造/属性/消息日志分支 ───────────────────


def test_priority_and_adapter_injection() -> None:
    """priority=50；adapter 参数注入时 _adapter 保留（测试注入协议）。"""
    assert LLMCore({}).priority == 50
    injected = object()
    plugin = LLMCore({}, adapter=injected)  # type: ignore[arg-type]
    assert plugin._adapter is injected  # noqa: SLF001


def test_resolve_image_ref_absolute_path_not_a_file(tmp_path: Any) -> None:
    """绝对路径引用但文件不存在 → 空串（不阻断请求）。"""
    missing = tmp_path / "nope.png"
    assert LLMCore._resolve_image_ref(str(missing)) == ""  # noqa: SLF001


def test_resolve_image_ref_read_oserror_returns_empty(tmp_path: Any, monkeypatch: Any) -> None:
    """读取文件抛 OSError → warning + 空串（降级不阻断）。"""
    f = tmp_path / "shot.png"
    f.write_bytes(b"x")

    def _raise_oserror(*args: Any, **kwargs: Any) -> bytes:
        raise OSError("read denied")

    monkeypatch.setattr(Path, "read_bytes", _raise_oserror)
    assert LLMCore._resolve_image_ref(str(f)) == ""  # noqa: SLF001


async def test_call_llm_writeback_skipped_when_history_empty() -> None:
    """normalize 清理后历史为空（全被丢）→ 不写回（早退，state 原样）。"""
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    state = {
        **_base_state(),
        "system_message": {"role": "system", "content": "sys"},
        "messages": [{"role": "tool", "tool_call_id": "call_orphan", "content": "r"}],
    }
    await plugin.execute(_make_ctx(state))
    # 历史全被配对校验丢弃 → cleaned_history_len=0 → 早退不写回
    assert state["messages"] == [{"role": "tool", "tool_call_id": "call_orphan", "content": "r"}]


async def test_call_llm_message_logging_branches() -> None:
    """消息日志：name 字段、tool_calls 序列化失败（循环引用）回退 str。"""
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    # 循环引用藏在标准结构 tc 的 function 内：normalize 不改写标准 tc，
    # json.dumps(default=str) 对循环引用抛 ValueError → 回退 str(tc_list)
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic
    await plugin._call_llm(  # noqa: SLF001
        [
            {"role": "user", "content": "hi", "name": "bob"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_abc123", "type": "function", "function": {"name": "f", "arguments": cyclic}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_abc123", "content": "r"},
        ],
        _make_ctx(_base_state()),
        stream=False,
    )
    assert caller.calls[0][1]["tool_name"] == "llm.complete_stream"


async def test_call_llm_tool_schemas_passed_to_service() -> None:
    """state.tool_schemas 非空 → 经 kwargs["tools"] 透传给 llm.complete_stream。"""
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    schemas = [
        {"type": "function", "function": {"name": "bash"}},
        {"type": "function", "function": {"name": "read_file"}},
    ]
    await plugin._call_llm(  # noqa: SLF001
        [{"role": "user", "content": "hi"}],
        _make_ctx(
            {
                **_base_state(),
                "tool_schemas": schemas,
            }
        ),
        stream=False,
    )
    assert caller.calls[0][1]["args"]["model"] == "deepseek-v3"
    assert caller.calls[0][1]["args"]["tools"] == schemas


async def test_call_llm_call_context_carries_round_routing_keys() -> None:
    """params 级 _call_context 携带本轮路由键（thread/pipeline/message）——
    llm_service 流式事件信封（_resolve_envelope）只读它；args 级同名键会被内核
    按内建键剥离，故必须在 params 顶层。"""
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    await plugin._call_llm(  # noqa: SLF001
        [{"role": "user", "content": "hi"}],
        _make_ctx(
            {
                **_base_state(),
                "session_id": "thread-abc",
                "pipeline_id": "p_rounds",
                "message_id": "a_round1",
            }
        ),
        stream=False,
    )
    ctx = caller.calls[0][1]["_call_context"]
    assert ctx == {
        "thread_id": "thread-abc",
        "pipeline_id": "p_rounds",
        "message_id": "a_round1",
    }
    # 键缺失时不得损坏调用（空串兜底；pipeline_id 随 _base_state 一起存在）
    caller2 = _FakeCaller({"success": True, "data": _ok_response()})
    plugin2 = _make_plugin(caller2)
    await plugin2._call_llm(  # noqa: SLF001
        [{"role": "user", "content": "hi"}],
        _make_ctx(_base_state()),
        stream=False,
    )
    ctx2 = caller2.calls[0][1]["_call_context"]
    assert ctx2["thread_id"] == ""
    assert ctx2["message_id"] == ""
    assert ctx2["pipeline_id"] == _base_state()["pipeline_id"]


async def test_call_llm_tool_schemas_empty_not_carried() -> None:
    """state.tool_schemas 为空 → tools 以 None 透传（不进 llm.complete_stream 请求体）。"""
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    await plugin._call_llm(  # noqa: SLF001
        [{"role": "user", "content": "hi"}],
        _make_ctx(_base_state()),
        stream=False,
    )
    assert "tools" not in caller.calls[0][1]["args"]
    assert caller.calls[0][1]["args"]["model"] == "deepseek-v3"
