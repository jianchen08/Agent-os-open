# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core plugin.py execute 路径补测——模型解析/消息装配/信封校验/错误路径。

契约（0.2 统一路径）：LLM 面唯一事实源 = llm_service（经 capability caller
``invoke`` → ``llm.complete_stream``）；返回 dict 带 ``partial`` 走半截落库，
否则组装 LLMResponse。本文件覆盖既有测试未触及的 execute 分支：

- ``_apply_model_from_state``：state.model_tier → defaults.tiers 解析、llm.yaml
  配置更新 self、model_id 未配置保持现状；
- ``_build_messages``：compression_messages 剥离 ``_context_form``、history 剥离
  ``seq``/``tool_result``、multimodal 合并进 list 型 content、dynamic_vars 三种形态；
- 成功路径：tool_call arguments >100 字符诊断、tool_calls+thinking 的
  reasoning_content、finish_reason=length → output_truncated；
- 信封校验 fail-closed：非 dict 信封 / success=false / data 非 dict → RuntimeError。

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

    async def __call__(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
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
            {"role": "assistant", "content": "a", "_context_form": "x", "agent_id": "roleplay_agent"},
        ],
    }
    msgs = pre._build_messages(state)  # noqa: SLF001
    assert msgs[0] == {"role": "system", "content": "sys"}
    assert msgs[1] == {"role": "user", "content": "c1"}  # _context_form 剥离
    assert msgs[2] == {"role": "user", "content": "q"}  # seq 剥离
    assert msgs[3] == {"role": "tool", "tool_call_id": "call_abc123", "content": "r"}  # tool_result 剥离
    assert msgs[4] == {"role": "assistant", "content": "a"}  # _context_form/agent_id 剥离


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
    """无文本且无工具调用 → 不 emit append op（messages 键缺失）；空回复触发重试计数。"""
    caller = _FakeCaller(
        {"success": True, "data": _ok_response(text=None, tool_calls=[], thinking_text=None)}
    )
    result = await _make_plugin(caller).execute(_make_ctx(_base_state()))
    assert "messages" not in result
    assert result["raw_result"] is None
    # 空回复重试（2026-08-30 用户裁定）：首轮计数 1 + 置续跑标志回 LLM
    assert result["llm_empty_streak"] == 1
    assert result["_has_new_llm_input"] is True


# ─────────────────── 执行身份戳记（agent_id → 消息 blob） ───────────────────


@pytest.mark.parametrize(
    ("text", "tool_calls", "finish_reason"),
    [
        ("纯文本回复", [], "stop"),
        ("", [{"id": "call_abc123", "name": "bash", "args": "{}"}], "tool_calls"),
    ],
    ids=["plain", "tool_calls"],
)
async def test_success_stamps_agent_id_from_state(
    text: str, tool_calls: list, finish_reason: str
) -> None:
    """state 持久键 agent.id → assistant blob 戳记 agent_id（消息生成时的管道
    执行身份，前端气泡卡名/卡头像数据源）。纯文本与 tool_calls 两种产出形态
    都要戳记。"""
    caller = _FakeCaller(
        {
            "success": True,
            "data": _ok_response(
                text=text, tool_calls=tool_calls, finish_reason=finish_reason
            ),
        }
    )
    state = {**_base_state(), "agent.id": "roleplay_agent"}
    result = await _make_plugin(caller).execute(_make_ctx(state))

    assistant = result["messages"]["_ops"][0]["msg"]
    assert assistant["agent_id"] == "roleplay_agent"


@pytest.mark.parametrize(
    "agent_id_state",
    [None, ""],
    ids=["key_absent", "empty_string"],
)
async def test_success_no_agent_id_state_omits_stamp(agent_id_state: str | None) -> None:
    """无 agent.id（会话类管道未透传）或空串 → blob 不写 agent_id 键
    （缺省不污染，前端按可空类型兜底默认身份）。"""
    caller = _FakeCaller({"success": True, "data": _ok_response(text="ok")})
    state = _base_state()
    if agent_id_state is not None:
        state["agent.id"] = agent_id_state
    result = await _make_plugin(caller).execute(_make_ctx(state))

    assistant = result["messages"]["_ops"][0]["msg"]
    assert "agent_id" not in assistant


# ─────────────────── 空回复重试（2026-08-30 用户裁定） ───────────────────


async def test_empty_response_retries_then_exhausts() -> None:
    """连续空回复：未超限每轮置续跑标志回 LLM 重试；超限（max=2 重试）后
    置 llm_empty_exhausted 停止续跑，交 output 步骤裁决任务失败。"""
    caller = _FakeCaller(
        {"success": True, "data": _ok_response(text=None, tool_calls=[], thinking_text=None)}
    )
    plugin = _make_plugin(caller)
    ctx = _make_ctx(_base_state())

    r1 = await plugin.execute(ctx)  # 第 1 次尝试：重试 #1
    assert r1["llm_empty_streak"] == 1
    assert r1["_has_new_llm_input"] is True
    assert "llm_empty_exhausted" not in r1

    ctx.state["llm_empty_streak"] = 1
    r2 = await plugin.execute(ctx)  # 第 2 次尝试：重试 #2
    assert r2["llm_empty_streak"] == 2
    assert r2["_has_new_llm_input"] is True

    ctx.state["llm_empty_streak"] = 2
    r3 = await plugin.execute(ctx)  # 第 3 次尝试：超限 → 耗尽
    assert r3["llm_empty_streak"] == 3
    assert r3["llm_empty_exhausted"] is True
    assert r3["_has_new_llm_input"] is False


async def test_empty_response_streak_resets_on_output() -> None:
    """重试后 LLM 有产出（文本）→ 计数复位 + 清续跑标志（防残留标志把
    后续纯文本轮误路由回 LLM 造成死循环）。"""
    caller = _FakeCaller({"success": True, "data": _ok_response(text="正常回复")})
    state = _base_state()
    state["llm_empty_streak"] = 2
    result = await _make_plugin(caller).execute(_make_ctx(state))
    assert result["llm_empty_streak"] == 0
    assert result["_has_new_llm_input"] is False
    assert "llm_empty_exhausted" not in result


async def test_empty_response_retry_limit_from_plugin_configs() -> None:
    """plugin_configs.llm_core.max_empty_retries 覆盖重试上限（每轮复位后应用）。"""
    caller = _FakeCaller(
        {"success": True, "data": _ok_response(text=None, tool_calls=[], thinking_text=None)}
    )
    plugin = _make_plugin(caller)
    ctx = _make_ctx(_base_state())
    ctx.state["plugin_configs"] = {"llm_core": {"max_empty_retries": 1}}

    r1 = await plugin.execute(ctx)  # 第 1 次：重试 #1（上限 1）
    assert r1["llm_empty_streak"] == 1
    assert r1["_has_new_llm_input"] is True

    ctx.state["llm_empty_streak"] = 1
    r2 = await plugin.execute(ctx)  # 第 2 次：超限 → 耗尽
    assert r2["llm_empty_streak"] == 2
    assert r2["llm_empty_exhausted"] is True
    assert r2["_has_new_llm_input"] is False


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


# ─────────────────── 错误路径：异常原样上抛 ───────────────────


async def test_tool_call_error_propagates() -> None:
    """tool_call 相关异常原样上抛（消息面修复由 llm_service 唯一关卡承担）。"""
    caller = _FakeCaller(exc=RuntimeError("tool_call pairing failed: insufficient tool messages"))
    with pytest.raises(RuntimeError, match="tool_call pairing failed"):
        await _make_plugin(caller).execute(_make_ctx(_base_state()))


async def test_non_tool_call_error_propagates() -> None:
    """非 tool_call 相关异常 → 原样上抛。"""
    caller = _FakeCaller(exc=RuntimeError("upstream connection refused"))
    with pytest.raises(RuntimeError, match="upstream connection refused"):
        await _make_plugin(caller).execute(_make_ctx(_base_state()))


# ─────────────────── 杂项：构造/属性/消息日志分支 ───────────────────


def test_default_priority() -> None:
    """priority=50。"""
    assert LLMCore({}).priority == 50


def test_resolve_image_ref_success_returns_data_url_with_empty_reason(tmp_path: Any) -> None:
    """成功路径不变：本地图片 → (data URL, 空原因)。"""
    f = tmp_path / "shot.png"
    f.write_bytes(b"png")
    data_url, reason = LLMCore._resolve_image_ref(str(f))  # noqa: SLF001
    assert data_url.startswith("data:image/png;base64,")
    assert reason == ""


def test_resolve_image_ref_absolute_path_not_a_file(tmp_path: Any) -> None:
    """绝对路径引用但文件不存在 → 空串 + 失败原因（供占位块显式化）。"""
    missing = tmp_path / "nope.png"
    data_url, reason = LLMCore._resolve_image_ref(str(missing))  # noqa: SLF001
    assert data_url == ""
    assert reason == "文件不存在"


def test_resolve_image_ref_oversize_returns_reason(tmp_path: Any) -> None:
    """图片超字节上限 → 空串 + 过大原因（不静默）。"""
    f = tmp_path / "big.png"
    f.write_bytes(b"x" * (LLMCore._MAX_IMAGE_BYTES + 1))  # noqa: SLF001
    data_url, reason = LLMCore._resolve_image_ref(str(f))  # noqa: SLF001
    assert data_url == ""
    assert "过大" in reason


def test_resolve_image_ref_read_oserror_returns_reason(tmp_path: Any, monkeypatch: Any) -> None:
    """读取文件抛 OSError → 空串 + 读取失败原因（降级不阻断但显式）。"""
    f = tmp_path / "shot.png"
    f.write_bytes(b"x")

    def _raise_oserror(*args: Any, **kwargs: Any) -> bytes:
        raise OSError("read denied")

    monkeypatch.setattr(Path, "read_bytes", _raise_oserror)
    data_url, reason = LLMCore._resolve_image_ref(str(f))  # noqa: SLF001
    assert data_url == ""
    assert reason == "文件读取失败"


def test_resolve_multimodal_blocks_missing_file_emits_notice_block(tmp_path: Any) -> None:
    """本地引用解析失败 → 不再静默丢弃，替换为 `[附件 … 解析失败：…]` 占位块；
    其余块（text/其他）原样保留。"""
    missing = tmp_path / "nope.png"
    blocks = [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": str(missing)}},
    ]
    resolved = LLMCore._resolve_multimodal_blocks(blocks)  # noqa: SLF001
    assert resolved[0] == {"type": "text", "text": "看图"}
    assert len(resolved) == 2
    notice = resolved[1]
    assert notice["type"] == "text"
    assert "解析失败" in notice["text"]
    assert str(missing) in notice["text"]


def test_resolve_multimodal_blocks_oversize_notice_carries_reason(tmp_path: Any) -> None:
    """超限图片占位块携带过大原因（有区分度输入，与文件缺失区分）。"""
    f = tmp_path / "big.png"
    f.write_bytes(b"x" * (LLMCore._MAX_IMAGE_BYTES + 1))  # noqa: SLF001
    resolved = LLMCore._resolve_multimodal_blocks(  # noqa: SLF001
        [{"type": "image_url", "image_url": {"url": str(f)}}]
    )
    assert len(resolved) == 1
    assert resolved[0]["type"] == "text"
    assert "过大" in resolved[0]["text"]


def test_resolve_multimodal_blocks_http_url_passthrough_unchanged() -> None:
    """http(s)/data URL 不属于本地解析失败 → 原样透传（行为不变）。"""
    blocks = [
        {"type": "image_url", "image_url": {"url": "https://a.com/x.png"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    assert LLMCore._resolve_multimodal_blocks(blocks) == blocks  # noqa: SLF001


def test_build_messages_multimodal_failure_notice_reaches_llm_messages(tmp_path: Any) -> None:
    """装配级验证：解析失败的图片引用以占位块合并进最后一条 user 消息
    content——LLM 可见附件缺失说明（后端报错腿①）。"""
    missing = tmp_path / "gone.png"
    pre = LLMCore.__new__(LLMCore)
    state = {
        "messages": [{"role": "user", "content": "看这张图"}],
        "multimodal_content": [{"type": "image_url", "image_url": {"url": str(missing)}}],
    }
    msgs = pre._build_messages(state)  # noqa: SLF001
    content = msgs[0]["content"]
    assert isinstance(content, list)
    assert {"type": "text", "text": "看这张图"} in content
    notices = [b for b in content if b.get("type") == "text" and "解析失败" in b.get("text", "")]
    assert len(notices) == 1
    assert str(missing) in notices[0]["text"]
    # 失败引用不得再以 image_url 块形态进请求（旧行为是整块丢弃/空块）
    assert not any(b.get("type") == "image_url" for b in content)


async def test_call_llm_message_logging_branches() -> None:
    """消息日志：name 字段、tool_calls 序列化失败（循环引用）回退 str。"""
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    # 循环引用藏在标准结构 tc 的 function 内：json.dumps(default=str)
    # 对循环引用抛 ValueError → 回退 str(tc_list)
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


async def test_interrupted_persists_user_requested_stop_signal() -> None:
    """中断（用户停止）落库：ended=true + router.stop_reason=user_requested。

    引擎收尾 persist_run_end 据 stop_reason 落 run=cancelled（不再覆写为
    Completed）；普通 error 中断不带停止信号。
    """
    plugin = _make_plugin(_FakeCaller())
    partial: dict[str, Any] = {"text": "半截", "thinking_text": None, "tool_calls": [], "usage": {}}

    cancelled = plugin._build_partial_failure_result(partial, status="interrupted", error_info=None)
    assert cancelled["ended"] is True
    assert cancelled["router.stop_reason"] == "user_requested"

    errored = plugin._build_partial_failure_result(partial, status="error", error_info=None)
    assert "ended" not in errored
    assert "router.stop_reason" not in errored


# ─────────────────── run_id 透传（停止感知契约全管道统一） ───────────────────


async def test_run_id_passthrough_for_task_pipeline(monkeypatch: Any) -> None:
    """run_id 透传全管道同一契约：任务管道（task_id 非空）同样传——
    用户停止（dispatch_stop → run Suspended）对所有管道同等生效，
    无域别门控（差别只在 state 内容）。"""
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    state = {
        **_base_state(),
        "run_id": "run-abc123def456",
        "task_id": "task-abc123def456",
    }
    await plugin.execute(_make_ctx(state))
    _, params = caller.calls[0]
    assert params["args"]["run_id"] == "run-abc123def456"


async def test_run_id_absent_when_state_lacks_it() -> None:
    """state 无 run_id → 不传（感知锚缺省关闭，行为不变）。"""
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    await plugin.execute(_make_ctx(_base_state()))
    _, params = caller.calls[0]
    assert "run_id" not in params["args"]


async def test_success_llm_usage_carries_model_attribution() -> None:
    """llm_usage 随用量携带 model/provider——traces 按模型/按日聚合 token 的归属依据
    （state.llm_model 是可变当前值，diff 轨迹在模型未变的轮次不落盘，不能作归属）。"""
    caller = _FakeCaller(
        {
            "success": True,
            "data": _ok_response(
                usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
            ),
        }
    )
    result = await _make_plugin(caller).execute(_make_ctx(_base_state()))
    usage = result["llm_usage"]
    assert usage["total_tokens"] == 120
    assert usage["model"] == "deepseek-v3"
    assert usage["provider"] == "openai"
    # W2a 锚与 usage 同点位：有用量回写的成功轮必落锚（入参无字符账本 → 0）
    assert result["track.messages_chars_at_llm"] == 0


async def test_success_no_usage_keeps_llm_usage_empty() -> None:
    """上游未回 usage（空 dict 为假）→ llm_usage 为空 dict，不伪造归属。"""
    caller = _FakeCaller({"success": True, "data": _ok_response(usage={})})
    result = await _make_plugin(caller).execute(_make_ctx(_base_state()))
    assert result["llm_usage"] == {}


# ─────────────────── W2a 上下文粗门锚（字符锚 + 模型窗口） ───────────────────


async def test_success_stamps_chars_anchor_and_model_window(monkeypatch: Any) -> None:
    """真实调用成功 → 锚=入参 track.messages_chars（透传引擎记账数字）、
    窗口=本轮解析到的模型 context_window——与 usage 同点位写回。"""
    _inject_llm_config(monkeypatch)
    caller = _FakeCaller(
        {"success": True, "data": _ok_response(usage={"prompt_tokens": 100})}
    )
    plugin = _make_plugin(caller)
    state = {**_base_state(), "model_tier": "large", "track.messages_chars": 12345}
    result = await plugin.execute(_make_ctx(state))
    assert result["track.messages_chars_at_llm"] == 12345
    assert result["track.model_context_window"] == 64000
    assert result["llm_usage"]["input_tokens"] == 100


async def test_anchor_missing_chars_defaults_zero_and_unresolved_window_writes_zero(
    monkeypatch: Any,
) -> None:
    """入参无 track.messages_chars → 锚写 0；模型未配置 context_window → 窗口写 0
    （沿用既有"未配置"告警语义；窗口 0 使管道公式支除零 fail-soft 关闭，冷启动支兜底）。"""
    import _config_models

    monkeypatch.setattr(
        _config_models,
        "_config",
        {
            "llm": {
                "models": {
                    "bare": {
                        "provider": "x",
                        "model_name": "m",
                        "api_base": "https://a",
                        "api_key": "k",
                    }
                },
                "defaults": {"chat": "bare"},
            }
        },
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    result = await plugin.execute(_make_ctx(_base_state()))
    assert result["track.messages_chars_at_llm"] == 0
    assert result["track.model_context_window"] == 0


async def test_interrupted_partial_does_not_stamp_anchor(monkeypatch: Any) -> None:
    """流中断/取消的半截返回不落锚（非成功路径不 stamp，防非主轮覆盖锚）。"""
    _inject_llm_config(monkeypatch)
    caller = _FakeCaller(
        {"success": True, "data": _ok_response(partial={"text": "半截"}, status="interrupted")}
    )
    plugin = _make_plugin(caller)
    state = {**_base_state(), "track.messages_chars": 777}
    result = await plugin.execute(_make_ctx(state))
    assert result["raw_result"] == "半截"  # 确认走的是 partial 落库路径
    assert "track.messages_chars_at_llm" not in result
    assert "track.model_context_window" not in result


# ─────────────────── 模型视觉能力标志（llm_supports_vision） ───────────────────


async def test_result_stamps_supports_vision_true(monkeypatch: Any) -> None:
    """模型声明 multimodal.supports_image=true → state 键为 True（注图闸门开）。"""
    _inject_llm_config(monkeypatch)
    import _config_models

    monkeypatch.setitem(
        _config_models._config["llm"]["models"]["deepseek-v4-pro"],
        "multimodal",
        {"supports_image": True, "supported_image_types": ["image/png", "image/jpeg"]},
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    state = {**_base_state(), "model_tier": "large"}

    result = await plugin.execute(_make_ctx(state))

    assert result["llm_supports_vision"] is True
    assert plugin._supports_vision is True  # noqa: SLF001


async def test_result_stamps_supports_vision_false_when_undeclared(monkeypatch: Any) -> None:
    """模型未声明 multimodal → False（fail-closed：不注图，走文本引导）。"""
    _inject_llm_config(monkeypatch)
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    state = {**_base_state(), "model_tier": "large"}

    result = await plugin.execute(_make_ctx(state))

    assert result["llm_supports_vision"] is False
