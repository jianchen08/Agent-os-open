# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core plugin.py execute 路径补测——模型解析/消息装配/信封校验/错误路径。

契约（0.2 统一路径）：LLM 面唯一事实源 = llm_service（经 capability caller
``invoke`` → ``llm.complete_stream``）；返回 dict 带 ``partial`` 走半截落库，
否则组装 LLMResponse。本文件覆盖既有测试未触及的 execute 分支：

- ``_apply_model_from_state``：state.model_tier → defaults.tiers 解析、llm.yaml
  配置更新 self；三级来源全空（defaults.chat 也为空）→ 显式报错；
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

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LLM_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "llm_core"
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
_SYSTEM_LLM_DIR = _REPO_ROOT / "plugins" / "shared" / "llm"

# 平铺 import 路径：llm_core 平铺模块 + 共享根平铺模块（_libs 等）+ system 侧 llm
# （_config_models 平铺 import）。插入顺序经设计：后插入者在 sys.path 更前，
# _LLM_CORE_DIR 必须压过 _SYSTEM_LLM_DIR——两处均有 adapter.py，
# 平铺 `import adapter` 须命中 llm_core 版（仅取 LLMResponse 类型）。
for _d in (_SYSTEM_LLM_DIR, _SHARED_DIR, _LLM_CORE_DIR):
    if str(_d) in sys.path:
        sys.path.remove(str(_d))
    sys.path.insert(0, str(_d))

import llm_adapter  # noqa: E402  适配服务面（剥噪助手批 5 迁至此处）

_MOD_NAME = "llm_core_execute_paths_under_test"


def _load_plugin() -> Any:
    """加载 llm_core/plugin.py（唯一模块名，进程内缓存）。"""
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _LLM_CORE_DIR / "plugin.py")
    assert spec is not None, "cannot load llm_core plugin.py"
    assert spec.loader is not None, "cannot load llm_core plugin.py"
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


def _test_identity(**overrides: Any) -> Any:
    """构造调用意图视图（R4 后实例不持身份字段；单测显式传入快照）。

    批 5 收窄：视图无 api_key（凭据不进入管道面）；api_base 仅观测回显。
    """
    values: dict[str, Any] = {
        "model_id": "deepseek-v3",
        "model": "deepseek-v3",
        "provider": "openai",
        "api_base": "https://baseline.example.com",
        "context_window": 64000,
        "supports_vision": False,
        "default_params": {},
    }
    values.update(overrides)
    return _mod.ModelIntent(**values)


def test_constructor_holds_no_model_identity_fields() -> None:
    """R4 死代码面删除：构造期/实例级模型身份字段一律不存在（身份随快照走）。"""
    plugin = _make_plugin(_FakeCaller({"success": True, "data": _ok_response()}))
    for attr in (
        "_model_id",
        "_model",
        "_provider",
        "_api_base",
        "_api_key",
        "_context_window",
        "_default_params",
        "_supports_vision",
        "_vision_max_side",
        "_boot_identity",
    ):
        assert not hasattr(plugin, attr), f"死代码身份字段仍在场: {attr}"


# ─────────────────── 模型动态解析（_apply_model_from_state） ───────────────────


@pytest.fixture(autouse=True)
def _baseline_llm_config(monkeypatch: Any) -> None:
    """每个用例注入一条可绑定的最小 llm.yaml 视图。

    模型未解析现在显式报错（不再回退构造期身份），未测解析的用例需要一条
    默认模型配置；测特定配置的用例在用例体内 monkeypatch 覆盖本基线。
    """
    import _config_models

    monkeypatch.setattr(
        _config_models,
        "_config",
        {
            "llm": {
                "models": {
                    "deepseek-v3": {
                        "provider": "openai",
                        "model_name": "deepseek-v3",
                        "api_base": "https://baseline.example.com",
                        "api_key": "baseline-key",
                        "context_window": 64000,
                    }
                },
                "defaults": {"chat": "deepseek-v3"},
            }
        },
    )


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
                    }
                },
                "defaults": {"tiers": {"large": "deepseek-v4-pro"}, "chat": "deepseek-v4-pro"},
            }
        },
    )


async def test_empty_llm_config_and_empty_state_raises_defaults_chat(
    monkeypatch: Any,
) -> None:
    """llm 配置注入为空 + state 无模型意图 → 显式报错（2026-11-13 装机事故）。

    旧行为：回退构造期身份（gpt-4/openai），错误面被拖到 adapter 缺 api_base。
    新契约：模型调用只认管道模型 ID / llm.yaml 配置，解析为空即失败（可诊断、
    可行动），且失败发生在任何 LLM 调用之前。
    """
    import _config_models

    monkeypatch.setattr(_config_models, "_config", {})
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)

    with pytest.raises(RuntimeError, match=r"defaults\.chat"):
        await plugin.execute(_make_ctx(_base_state()))

    assert caller.calls == [], "模型未解析不得发起下游 LLM 调用"


async def test_model_tier_resolves_identity_snapshot(monkeypatch: Any) -> None:
    """state.model_tier → defaults.tiers 解析 → 本次调用身份快照（结果面回读）。

    R4 后身份不落实例字段：解析结果经 execute 返回的 state_updates 与本次
    调用参数（caller args）观测——语义期望不变，读面改指活契约。
    """
    _inject_llm_config(monkeypatch)
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    assert not hasattr(plugin, "_model_id"), "构造期身份字段已退役（R4）"

    result = await plugin.execute(_make_ctx({**_base_state(), "model_tier": "large"}))

    assert result["llm_model_id"] == "deepseek-v4-pro"
    assert result["llm_provider"] == "deepseek"
    assert result["llm_model"] == "deepseek-v4-pro"
    assert result["llm_api_base"] == "https://api.example.com"
    assert result["track.model_context_window"] == 64000
    # 调用通道：model 用 yaml key（model_id）做 deployment 匹配
    assert caller.calls[0][1]["args"]["model"] == "deepseek-v4-pro"
    # default_params 随身份快照进入调用参数（temperature=0.3）
    assert caller.calls[0][1]["args"]["temperature"] == 0.3


async def test_model_id_unknown_fails_round_visibly(monkeypatch: Any) -> None:
    """state.model_id 未命中 models 映射 → 本轮显式失败并点名模型，不静默回落。

    失败直写语义（对齐 R16⑧）：解析出的显式模型名不在可用列表时本轮以
    RuntimeError 终止（引擎据 plugin_errors 直写失败文本），且不得以任何
    其他模型发起调用装成功（缺陷⑥-A：旧行为 warning 后保持上一成功模型
    继续对话，用户切模型被静默丢弃）。
    """
    _inject_llm_config(monkeypatch)
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    with pytest.raises(RuntimeError, match="no-such-model"):
        await plugin.execute(_make_ctx({**_base_state(), "model_id": "no-such-model"}))
    assert caller.calls == [], "未命中模型不得以旧模型发起 LLM 调用（不装成功）"


async def test_defaults_chat_unknown_fails_round_visibly(monkeypatch: Any) -> None:
    """defaults.chat 指向映射外模型（⑥-A 实机触发路径：REST PUT 切失效模型）
    → 本轮失败点名该模型，不以其他模型继续对话。"""
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
                    }
                },
                "defaults": {"chat": "nonexistent-model"},
            }
        },
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    with pytest.raises(RuntimeError, match="nonexistent-model"):
        await plugin.execute(_make_ctx(_base_state()))
    assert caller.calls == [], "defaults.chat 未命中不得以旧模型发起 LLM 调用"


async def test_tier_key_unknown_falls_back_to_defaults_chat(monkeypatch: Any) -> None:
    """tier 键不在 tiers 映射 → 走 defaults.chat 兜底（合法回退，不受未命中
    失败语义影响——tiers 键解析与显式模型名查表是两条路径）。"""
    _inject_llm_config(monkeypatch)
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    result = await plugin.execute(_make_ctx({**_base_state(), "model_tier": "huge"}))
    assert result["llm_model"] == "deepseek-v4-pro"
    assert caller.calls[0][1]["args"]["model"] == "deepseek-v4-pro"


async def test_tier_resolved_model_missing_fails_visibly(monkeypatch: Any) -> None:
    """tier 键命中但解析出的模型名不在 models 映射 → 本轮失败点名（两路径
    汇聚于 models 查表：解析出的显式模型名未命中即失败，与来源无关）。"""
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
                    }
                },
                "defaults": {"tiers": {"large": "ghost-model"}, "chat": "deepseek-v4-pro"},
            }
        },
    )
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    with pytest.raises(RuntimeError, match="ghost-model"):
        await plugin.execute(_make_ctx({**_base_state(), "model_tier": "large"}))
    assert caller.calls == []


async def test_model_id_same_unchanged_conf_idempotent(monkeypatch: Any) -> None:
    """同一 model_id 且配置内容未变 → 解析幂等（结果面身份与配置视图一致）。"""
    _inject_llm_config(monkeypatch)
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller, {"model_id": "deepseek-v4-pro"})
    first = await plugin.execute(_make_ctx(_base_state()))
    second = await plugin.execute(_make_ctx(_base_state()))
    assert first["llm_model_id"] == second["llm_model_id"] == "deepseek-v4-pro"
    # 身份对齐当前配置视图（首执行即对齐基线，二执行幂等不变）
    assert first["llm_model"] == second["llm_model"] == "deepseek-v4-pro"
    assert first["track.model_context_window"] == second["track.model_context_window"] == 64000


async def test_same_model_param_change_reapplies(monkeypatch: Any) -> None:
    """同模型改参（id 不变，配置内容变）→ 新一轮解析整体重解析生效。

    口径（2026-10-20 裁定「模型配置改动 = 下一管道生效」）：**无绑定快照**的
    解析（新一轮解析/新 run）按当前配置内容整体重解析——同模型改参即生效；
    已有绑定快照的在飞 run 以启动时快照为准（参数改动留给下一管道，见
    test_llm_core_model_binding.py::test_inflight_run_keeps_startup_params）。
    凭证（api_key）两种口径都取当前配置视图（key 归 llm_service 解析，
    llm_core 不向其传 key）——本测经调用参数（default_params）观测改参生效。
    """
    _inject_llm_config(monkeypatch)
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller, {"model_id": "deepseek-v4-pro"})
    first = await plugin.execute(_make_ctx(_base_state()))
    assert first["track.model_context_window"] == 64000
    assert caller.calls[0][1]["args"]["temperature"] == 0.3

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
                        "api_base": "https://api.example.com/v2",
                        "api_key": "k2",
                        "context_window": 128000,
                        "default_params": {"temperature": 0.7},
                    }
                },
                "defaults": {"tiers": {"large": "deepseek-v4-pro"}, "chat": "deepseek-v4-pro"},
            }
        },
    )
    second = await plugin.execute(_make_ctx(_base_state()))

    assert second["llm_model_id"] == "deepseek-v4-pro"
    assert second["track.model_context_window"] == 128000
    assert second["llm_api_base"] == "https://api.example.com/v2"
    assert caller.calls[1][1]["args"]["temperature"] == 0.7


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


def test_build_messages_passes_half_thinking_assistant_through() -> None:
    """中断轮半截思考消息出站透传：content 空串直传、reasoning_content 保留。

    纯思考中断轮落库形态（assistant + content="" + reasoning_content 非空 +
    无 tool_calls）下一轮请求装配：整条不剔除（无 synthetic 标记）、内部字段
    （status/llm_error_info/seq）剥离、reasoning_content 不剥（provider 适配层
    采样保留策略下游消费）；无 tool_calls 即无配对面，直传即安全。
    """
    pre = LLMCore.__new__(LLMCore)
    state = {
        "messages": [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "中断前的思考",
                "status": "interrupted",
                "llm_error_info": {"error_type": "stream_timeout"},
                "seq": 12,
            },
            {"role": "user", "content": "继续"},
        ],
    }
    msgs = pre._build_messages(state)  # noqa: SLF001
    assert len(msgs) == 3, "半截思考 assistant 消息整条保留，不剔除"
    half = msgs[1]
    assert half["role"] == "assistant"
    assert half["content"] == ""
    assert half["reasoning_content"] == "中断前的思考"
    assert "status" not in half
    assert "llm_error_info" not in half
    assert "seq" not in half


def test_build_messages_excludes_synthetic_failure_messages() -> None:
    """引擎合成失败终态（synthetic 标记）整条剔除，正常 assistant 保留。

    失败原因是给人看的：synthetic assistant 消息不得进出站请求（模型对
    LLM 供应商层错误无处置能力）；同历史中正常消息与 user 消息不受影响。
    """
    pre = LLMCore.__new__(LLMCore)
    failure_msg = {
        "role": "assistant",
        "content": "本轮 run 失败：步骤执行失败且未产生任何回复（LLM 供应商调用失败[认证失败]）",
        "status": "error",
        "synthetic": True,
    }
    state = {
        "messages": [
            {"role": "user", "content": "q1"},
            failure_msg,
            {"role": "assistant", "content": "正常回复"},
            {"role": "user", "content": "q2"},
        ],
    }
    msgs = pre._build_messages(state)  # noqa: SLF001
    assert failure_msg not in msgs, "synthetic 失败终态不得出站"
    assert {"role": "assistant", "content": "正常回复"} in msgs
    assert [m["content"] for m in msgs if m["role"] == "user"] == ["q1", "q2"]


def test_build_messages_strips_synthetic_field_on_non_assistant_role() -> None:
    """synthetic 字段兜底剥离：非 assistant 角色带标记时仅剥字段不剔消息。

    stage_finalize 只对 assistant 写 synthetic；其他角色携带该标记属于
    异常形态——字段不属于任何 wire 契约（严格 provider 整请求拒绝），
    但用户消息内容本身必须保留。
    """
    pre = LLMCore.__new__(LLMCore)
    state = {
        "messages": [
            {"role": "user", "content": "q", "synthetic": True},
        ],
    }
    msgs = pre._build_messages(state)  # noqa: SLF001
    assert msgs == [{"role": "user", "content": "q"}]


def test_strip_transport_noise_keeps_cause_tail() -> None:
    """信封错误剥传输噪声：最后一跳 MCP protocol error 之后的根因保留。

    真实形态双层噪声（llm_core.execute 外层 + llm.complete_stream 内层），
    rfind 取最后一跳；分类摘要（LLM 供应商调用失败[认证失败]）在尾部存活，
    内核折叠 280 字截断才保得住根因。
    """
    noised = (
        "tool execution failed: MCP tool call failed: tool call failed: "
        "llm.complete_stream: MCP protocol error: [0] "
        "LLM 供应商调用失败[认证失败]（model=deepseek-v4）: "
        "AuthenticationError: Error code: 401 - Invalid API key"
    )
    assert llm_adapter._strip_transport_noise(noised) == (  # noqa: SLF001
        "LLM 供应商调用失败[认证失败]（model=deepseek-v4）: "
        "AuthenticationError: Error code: 401 - Invalid API key"
    )


def test_strip_transport_noise_fallbacks() -> None:
    """剥噪回退：无标记原样返回 / 空尾原样返回 / 非字符串原样返回。"""
    plain = "tool not found"
    assert llm_adapter._strip_transport_noise(plain) == plain  # noqa: SLF001
    dangling = "MCP protocol error: [0] "
    assert llm_adapter._strip_transport_noise(dangling) == dangling  # noqa: SLF001
    assert llm_adapter._strip_transport_noise({"error": "dict 形态"}) == {  # noqa: SLF001
        "error": "dict 形态"
    }


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


# ─────────────────── 注入文本工具引用对账（卡4②，出站构造期拦截） ───────────────────

_TRIGGER_MSG = (
    "【系统纪律】本任务是定时触发对用户的提醒/通知。执行第一步必须调用 "
    "human_interaction 工具将提醒内容送达用户。\n"
    "[触发器通知] 触发器 '巡检' 已触发 (第 1 次)\n"
    "若 file 工具已恢复：先调用 file_read 核对原文后再送达。"
)


def _recon_state() -> dict[str, Any]:
    """缩水现场：file_read/human_interaction 声明在 tool_ids 但不在 tool_schemas。"""
    return {
        "tool_ids": ["file_read", "bash", "human_interaction"],
        "tool_schemas": [
            {"function": {"name": "file_read"}},
            {"function": {"name": "bash"}},
        ],
        "messages": [
            {"role": "user", "content": "普通用户消息，提到 file_read 不该被标注"},
            {"role": "user", "content": _TRIGGER_MSG},
        ],
    }


def test_dangling_tool_ref_in_trigger_message_gets_annotated() -> None:
    """注入指令引用声明但不在当前数组的工具 → 出站副本追加对账标注 + ERROR 留痕。

    事故形态（2026-10-17 装机）：触发器文本指挥「先调 file_read」，数组里
    已无此工具——标注直说不可用，替代模型盲猜编造。
    """
    pre = LLMCore.__new__(LLMCore)
    state = _recon_state()
    records: list[str] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            _mod.logger, "error",
            lambda msg, *a: records.append(str(msg) % a if a else str(msg)),
        )
        msgs = pre._build_messages(state)  # noqa: SLF001
    trigger_out = next(m for m in msgs if "[触发器通知]" in str(m.get("content", "")))
    content = trigger_out["content"]
    assert isinstance(content, str)
    assert "human_interaction" in content  # 标注点名缺席工具
    assert "file_read" in content
    assert "[系统对账]" in content  # 标注式拦截：保留原文 + 附加对账
    assert _TRIGGER_MSG in content  # 原文完整保留（不剔除用户内容）
    assert any("不在可用工具面" in r for r in records)


def test_dangling_annotation_leaves_state_untouched() -> None:
    """标注只作用于本次出站装配副本，state["messages"] 原样（幂等、不污染历史）。"""
    pre = LLMCore.__new__(LLMCore)
    state = _recon_state()
    before = [dict(m) for m in state["messages"]]
    pre._build_messages(state)  # noqa: SLF001
    assert state["messages"] == before


def test_no_dangling_refs_no_annotation() -> None:
    """引用的工具全部在面 → 无对账标注（性质：标注只由缺席触发，零缓存扰动）。"""
    pre = LLMCore.__new__(LLMCore)
    state = _recon_state()
    state["tool_schemas"].append({"function": {"name": "human_interaction"}})
    msgs = pre._build_messages(state)  # noqa: SLF001
    trigger_out = next(m for m in msgs if "[触发器通知]" in str(m.get("content", "")))
    assert "[系统对账]" not in str(trigger_out["content"])


def test_plain_user_message_never_annotated() -> None:
    """无注入标记的普通用户消息即使提到缺席工具也不标注（对账面=注入指令文本）。"""
    pre = LLMCore.__new__(LLMCore)
    state = _recon_state()
    msgs = pre._build_messages(state)  # noqa: SLF001
    plain = next(m for m in msgs if str(m.get("content", "")).startswith("普通用户消息"))
    assert "[系统对账]" not in str(plain["content"])


def test_missing_tool_state_skips_reconciliation() -> None:
    """tool_ids/tool_schemas 缺失（配置断链轮）→ 跳过对账，不误标。"""
    pre = LLMCore.__new__(LLMCore)
    state = {"messages": [{"role": "user", "content": _TRIGGER_MSG}]}
    msgs = pre._build_messages(state)  # noqa: SLF001
    assert "[系统对账]" not in str(msgs[-1]["content"])


# ─────────────────── 系统通知衰减（出站视图裁剪） ───────────────────


def _notify(task_id: str, verdict: str = "失败 ❌") -> dict[str, Any]:
    """构造 triggers_ext 自动父通知形态的历史消息（契约见 triggers/manager.py）。"""
    return {"role": "user", "content": f"[系统通知] 子任务 '写周报' (ID: {task_id}) {verdict}\n请决定后续操作。"}


def test_build_messages_decays_superseded_notification_keeps_last_per_task() -> None:
    """同 task_id 更新通知出现 → 旧通知裁剪，保留每 task 最后一条。"""
    pre = LLMCore.__new__(LLMCore)
    state = {
        "messages": [
            {"role": "user", "content": "q"},
            _notify("t_A", "失败 #1"),
            _notify("t_A", "失败 #2"),
            {"role": "user", "content": "继续"},
        ],
    }
    msgs = pre._build_messages(state)  # noqa: SLF001
    contents = [m["content"] for m in msgs if m.get("role") == "user"]
    assert "q" in contents
    assert "继续" in contents
    notify_msgs = [c for c in contents if isinstance(c, str) and c.startswith("[系统通知]")]
    assert len(notify_msgs) == 1, "同 task 仅保留最后一条通知"
    assert "失败 #2" in notify_msgs[0], "保留的是最新一条"
    assert all("失败 #1" not in c for c in contents), "旧通知已裁剪"


def test_build_messages_decays_notification_consumed_by_tool_call() -> None:
    """通知后编排器已产出工具调用 → 即使是该 task 最后一条也裁剪（已被消费）。"""
    pre = LLMCore.__new__(LLMCore)
    state = {
        "messages": [
            _notify("t_A"),
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "function": {"name": "task_submit", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            {"role": "assistant", "content": "已重派"},
        ],
    }
    msgs = pre._build_messages(state)  # noqa: SLF001
    assert all(not (isinstance(m.get("content"), str) and m["content"].startswith("[系统通知]")) for m in msgs), "被消费的通知不再进请求"
    # 工具对完整保留
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in msgs)
    assert any(m.get("role") == "tool" for m in msgs)


def test_build_messages_keeps_last_notification_without_subsequent_action() -> None:
    """末尾通知且编排器尚未产出工具调用 → 保留（唤醒锚点，不可裁）。"""
    pre = LLMCore.__new__(LLMCore)
    state = {"messages": [{"role": "user", "content": "q"}, _notify("t_A")]}
    msgs = pre._build_messages(state)  # noqa: SLF001
    assert msgs[1]["content"].startswith("[系统通知]"), "未被消费/取代的通知保留"


def test_build_messages_decay_is_per_task_isolated() -> None:
    """衰减按 task_id 分维度：t_A 被工具调用消费裁剪，t_B 末条保留。"""
    pre = LLMCore.__new__(LLMCore)
    state = {
        "messages": [
            _notify("t_A"),
            {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "task_submit", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            _notify("t_B"),
        ],
    }
    msgs = pre._build_messages(state)  # noqa: SLF001
    notify_contents = [m["content"] for m in msgs if isinstance(m.get("content"), str) and m["content"].startswith("[系统通知]")]
    assert len(notify_contents) == 1
    assert "t_B" in notify_contents[0]


def test_build_messages_non_notification_messages_untouched() -> None:
    """非系统通知（普通 user / 显式触发器消息 / 无 ID 锚的标记文本 / list content）一律不动。"""
    pre = LLMCore.__new__(LLMCore)
    state = {
        "messages": [
            {"role": "user", "content": "[系统通知] 手打文本无 ID 锚"},
            {"role": "user", "content": "触发器通知: 到点了"},
            {"role": "user", "content": [{"type": "text", "text": "[系统通知] 列表形态不识别"}]},
            {"role": "user", "content": "普通消息"},
        ],
    }
    msgs = pre._build_messages(state)  # noqa: SLF001
    assert len(msgs) == 4, "无可定维通知时不裁剪任何消息"


def test_build_messages_tool_pair_survives_notification_decay() -> None:
    """通知夹在工具对中间被裁剪时，assistant(tool_calls)+tool 消息原序保留。"""
    pre = LLMCore.__new__(LLMCore)
    tool_call_msg = {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "task_submit", "arguments": "{}"}}]}
    tool_result_msg = {"role": "tool", "tool_call_id": "c1", "content": "ok"}
    state = {
        "messages": [
            _notify("t_A", "失败 #1"),
            tool_call_msg,
            tool_result_msg,
            _notify("t_A", "失败 #2"),
        ],
    }
    msgs = pre._build_messages(state)  # noqa: SLF001
    assert len(msgs) == 3, "仅 #1 被裁（被取代且被消费）"
    assert msgs[0].get("tool_calls")
    assert msgs[1].get("role") == "tool"
    assert msgs[2]["content"].startswith("[系统通知]")
    assert "失败 #2" in msgs[2]["content"]


def test_build_messages_decay_property_at_most_one_notification_per_task() -> None:
    """性质断言：混合历史衰减后每 task_id 至多 1 条通知，且消息数只减不增。

    区分度输入：t_A/t_B 每条通知后都有工具调用（全被消费），t_C 末条通知
    后无任何动作（存活）——消费与存活动同一fixture 内符号量级相反。
    """
    pre = LLMCore.__new__(LLMCore)
    tool_call_msg = {"role": "assistant", "tool_calls": [{"id": "c9", "function": {"name": "task_submit", "arguments": "{}"}}]}
    history: list[dict[str, Any]] = []
    for tid in ("t_A", "t_B", "t_C"):
        for n in range(3):
            history.append({"role": "user", "content": f"轮次 {tid}-{n}"})
            history.append(_notify(tid, f"失败 #{n}"))
            if not (tid == "t_C" and n == 2):
                history.append(dict(tool_call_msg))
                history.append({"role": "tool", "tool_call_id": "c9", "content": "ok"})
    state = {"messages": history}
    msgs = pre._build_messages(state)  # noqa: SLF001
    assert len(msgs) <= len(history)
    per_task: dict[str, int] = {}
    for m in msgs:
        tid = _mod._system_notify_task_id(m)  # noqa: SLF001
        if tid is not None:
            per_task[tid] = per_task.get(tid, 0) + 1
    assert all(v <= 1 for v in per_task.values()), "每 task_id 至多存活 1 条通知"
    assert set(per_task) == {"t_C"}, "仅未被消费的末条通知存活"
    assert len(msgs) < len(history), "混合历史经衰减只减不增"


def test_build_messages_decay_view_only_state_untouched() -> None:
    """衰减只裁出站请求视图：state["messages"] 持久层原样（长度与内容不变）。"""
    pre = LLMCore.__new__(LLMCore)
    history = [
        {"role": "user", "content": "q", "seq": 1},
        _notify("t_A", "失败 #1"),
        _notify("t_A", "失败 #2"),
    ]
    pre._build_messages({"messages": history})  # noqa: SLF001
    assert len(history) == 3, "持久层消息数不变"
    assert history[1]["content"].startswith("[系统通知]")
    assert "失败 #1" in history[1]["content"]
    assert history[0].get("seq") == 1


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


async def test_envelope_success_false_error_denoised() -> None:
    """success=false 且 error 带 MCP 传输噪声 → RuntimeError 只携带根因尾部。

    传输前缀（tool execution failed/MCP tool call failed/…）到本插件为止，
    不再向上游（引擎 _plugin_errors → 内核折叠文案）扩散。
    """
    noised = (
        "tool execution failed: MCP tool call failed: tool call failed: "
        "llm.complete_stream: MCP protocol error: [0] "
        "LLM 供应商调用失败[限流]（model=deepseek-v4）: "
        "RateLimitError: Error code: 429"
    )
    caller = _FakeCaller(result={"success": False, "data": None, "error": noised})
    with pytest.raises(RuntimeError) as exc_info:
        await _make_plugin(caller).execute(_make_ctx(_base_state()))
    message = str(exc_info.value)
    assert message.startswith("llm.complete_stream 工具执行失败: LLM 供应商调用失败[限流]"), message
    assert "MCP" not in message.replace("llm.complete_stream", ""), message


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
        identity=_test_identity(),
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
        identity=_test_identity(),
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
        identity=_test_identity(),
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
        identity=_test_identity(),
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
        identity=_test_identity(),
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

    cancelled = plugin._build_partial_failure_result(
        partial, status="interrupted", error_info=None, identity=_test_identity()
    )
    assert cancelled["ended"] is True
    assert cancelled["router.stop_reason"] == "user_requested"

    errored = plugin._build_partial_failure_result(
        partial, status="error", error_info=None, identity=_test_identity()
    )
    assert "ended" not in errored
    assert "router.stop_reason" not in errored

    # ── 中断轮思考产出落库（用户裁定 2026-11-04 所有消息都要有出口）────────
    # 思考（reasoning）非空亦属可显示内容（聊天消息流权威化设计真值 20261027
    # 不变量 4 现行版）：纯思考中断轮必须落 assistant 消息——不落则内核事件桥
    # 按契约对 round 无 assistant 发 null 占位，前端流式占位失去权威坐标成为
    # 幽灵气泡（装机 kernel.log.2026-11-03 实证钉死消息流底部）。真零产出——
    # text/thinking/保留 tool_calls 全空——仍不落痕（空气泡防线，374 条存量
    # 教训不可回退）。有半截文本时照常落（半截保留语义不变）。


async def test_interrupted_thinking_only_persists_message() -> None:
    """纯思考中断轮（thinking 非空、text 空、无 tool_calls）落 assistant 消息。

    落库消息带 reasoning_content 与正确 status；无 tool_calls 即无配对
    tool 结果消息；中断停止信号（ended/stop_reason）照常置位。
    """
    plugin = _make_plugin(_FakeCaller())
    partial: dict[str, Any] = {
        "text": "",
        "thinking_text": "先查上下文里的路由表再回答",
        "tool_calls": [],
        "usage": {},
    }

    result = plugin._build_partial_failure_result(
        partial, status="interrupted", error_info=None, identity=_test_identity()
    )
    ops = result["messages"]["_ops"]
    assert [op["op"] for op in ops] == ["set"], "仅一条 assistant 落库 op，无配对 tool 结果"
    persisted = ops[0]["msg"]
    assert persisted["role"] == "assistant"
    assert persisted["content"] == ""
    assert persisted["reasoning_content"] == "先查上下文里的路由表再回答"
    assert persisted["status"] == "interrupted"
    assert "tool_calls" not in persisted
    assert result["ended"] is True
    assert result["router.stop_reason"] == "user_requested"

    # 纯空白 text 同样走思考分支落库（strip 语义与文本分支一致，防拟合）
    ws = plugin._build_partial_failure_result(
        {**partial, "text": "   "},
        status="interrupted",
        error_info=None,
        identity=_test_identity(),
    )
    assert ws["messages"]["_ops"][0]["msg"]["reasoning_content"] == (
        "先查上下文里的路由表再回答"
    )


async def test_stream_error_thinking_only_persists_message() -> None:
    """纯思考流错误（如流看门狗强杀）：落库带 reasoning_content/status/错误信息。

    无 tool_calls 即无配对 tool 结果消息；error 语义不置 ended/stop_reason。
    """
    plugin = _make_plugin(_FakeCaller())
    error_info = {"error_type": "stream_timeout", "message": "inter-chunk watchdog"}
    result = plugin._build_partial_failure_result(
        {
            "text": None,
            "thinking_text": "已经推出前两个分支，第三个待验证",
            "tool_calls": [],
            "usage": {},
        },
        status="error",
        error_info=error_info,
        identity=_test_identity(),
    )
    ops = result["messages"]["_ops"]
    assert [op["op"] for op in ops] == ["set"], "仅一条 assistant 落库 op，无配对 tool 结果"
    persisted = ops[0]["msg"]
    assert persisted["role"] == "assistant"
    assert persisted["content"] == ""
    assert persisted["reasoning_content"] == "已经推出前两个分支，第三个待验证"
    assert persisted["status"] == "error"
    assert persisted["llm_error_info"] == error_info
    assert "ended" not in result
    assert "router.stop_reason" not in result


async def test_interrupted_true_zero_output_no_thinking_persists_no_message() -> None:
    """真零产出中断（text/thinking/tool_calls 全空）：不带 messages 键。

    空气泡防线保留（374 条存量教训）：全空不落任何消息；ended/stop_reason
    照常置位，run 收尾语义不受影响。
    """
    plugin = _make_plugin(_FakeCaller())
    result = plugin._build_partial_failure_result(
        {"text": "", "thinking_text": None, "tool_calls": [], "usage": {}},
        status="interrupted",
        error_info=None,
        identity=_test_identity(),
    )
    assert "messages" not in result, "真零产出中断不得落空 assistant 消息"
    assert result["ended"] is True, "中断停止信号照常置位"
    assert result["router.stop_reason"] == "user_requested"


async def test_stream_error_true_zero_output_no_thinking_persists_no_message() -> None:
    """真零产物流错误（text/thinking/tool_calls 全空）：不带 messages 键；
    ended/stop_reason 不置位（error 语义）。"""
    plugin = _make_plugin(_FakeCaller())
    partial: dict[str, Any] = {"text": None, "thinking_text": None, "tool_calls": [], "usage": {}}

    result = plugin._build_partial_failure_result(
        partial, status="error", error_info=None, identity=_test_identity()
    )
    assert "messages" not in result
    assert "ended" not in result
    assert "router.stop_reason" not in result


async def test_interrupted_with_partial_text_still_persists_message() -> None:
    """有半截文本的中断照常落库（半截保留语义不变）；空白文本视同零产出。"""
    plugin = _make_plugin(_FakeCaller())
    partial: dict[str, Any] = {"text": "半截", "thinking_text": None, "tool_calls": [], "usage": {}}
    result = plugin._build_partial_failure_result(
        partial, status="interrupted", error_info=None, identity=_test_identity()
    )
    assert "messages" in result and result["messages"]["_ops"], "半截文本保留落库"

    whitespace = plugin._build_partial_failure_result(
        {**partial, "text": "   "},
        status="interrupted",
        error_info=None,
        identity=_test_identity(),
    )
    assert "messages" not in whitespace, "纯空白文本且无思考视同真零产出，不落空消息"


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
    assert not hasattr(plugin, "_supports_vision"), "视觉能力随身份快照，不落实例字段"


async def test_result_stamps_supports_vision_false_when_undeclared(monkeypatch: Any) -> None:
    """模型未声明 multimodal → False（fail-closed：不注图，走文本引导）。"""
    _inject_llm_config(monkeypatch)
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    state = {**_base_state(), "model_tier": "large"}

    result = await plugin.execute(_make_ctx(state))

    assert result["llm_supports_vision"] is False


async def test_track_stats_failure_degrades_without_losing_result() -> None:
    """追踪统计抛异常 → 观测面降级（warn 不上抛），本轮结果照常返回。"""
    caller = _FakeCaller(
        {
            "success": True,
            "data": _ok_response(
                usage={
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                    "cached_tokens": 0,
                }
            ),
        }
    )
    plugin = _make_plugin(caller)

    class _BrokenTrack:
        async def run(self, ctx: Any, usage: Any) -> dict[str, Any]:
            raise RuntimeError("track boom")

    original_track = plugin._track
    plugin._track = _BrokenTrack()
    try:
        result = await plugin.execute(_make_ctx(_base_state()))
    finally:
        plugin._track = original_track

    assert result["raw_result"] == "ok"
    assert result["llm_usage"]["model"] == "deepseek-v3"


async def test_partial_error_info_persists_into_message() -> None:
    """半截落库携带 llm_error_info → 随 assistant 消息持久化（错误是值不丢语义）。"""
    plugin = _make_plugin(_FakeCaller())
    partial: dict[str, Any] = {
        "text": "半截",
        "thinking_text": None,
        "tool_calls": [],
        "usage": {},
    }
    error_info = {"kind": "timeout", "message": "stream broken"}
    result = plugin._build_partial_failure_result(  # noqa: SLF001
        partial,
        status="error",
        error_info=error_info,
        identity=_test_identity(),
    )
    ops = result["messages"]["_ops"]
    persisted = ops[0]["msg"]
    assert persisted["llm_error_info"] == error_info


async def test_thinking_strength_params_merged_into_call() -> None:
    """state.thinking_strength 参数组 → 白名单过滤后并入调用 kwargs。"""
    caller = _FakeCaller({"success": True, "data": _ok_response()})
    plugin = _make_plugin(caller)
    await plugin._call_llm(  # noqa: SLF001
        [{"role": "user", "content": "hi"}],
        _make_ctx({**_base_state(), "thinking_strength": '{"reasoning_effort": "high"}'}),
        stream=False,
        identity=_test_identity(),
    )
    assert caller.calls[0][1]["args"]["reasoning_effort"] == "high"


# ─────────────────── 流式退化循环截断的终态处置（2026-11-07） ───────────────────


async def test_execute_terminates_on_repetition_with_no_output() -> None:
    """退化循环截断且正文/工具双空 → 终止并写明死因。

    事故形态：思考通道原地刷同一 emoji，正文为空、无工具调用。截断后若当正常
    轮继续，下一轮只会再退化一次（本用例锁死"零产出退化轮必须终止"）。
    """
    caller = _FakeCaller(
        {
            "success": True,
            "data": _ok_response(
                text="",
                thinking_text="🍦" * 600,
                finish_reason="length",
                stream_repetition=True,
            ),
        }
    )
    plugin = _make_plugin(caller)

    with pytest.raises(RuntimeError, match="退化循环"):
        await plugin.execute(_make_ctx(_base_state()))


async def test_execute_keeps_truncated_output_when_content_present() -> None:
    """有正文的截断轮保留半截内容，但打 llm_repetition_detected 交任务域
    收束（任务管道 failed，不自动重试；会话管道保留内容结束本轮）。"""
    caller = _FakeCaller(
        {
            "success": True,
            "data": _ok_response(
                text="已完成部分排查，剩余项如下",
                finish_reason="length",
                stream_repetition=True,
            ),
        }
    )
    plugin = _make_plugin(caller)

    result = await plugin.execute(_make_ctx(_base_state()))

    assert result["raw_result"] == "已完成部分排查，剩余项如下"
    assert result["llm_repetition_detected"] is True


async def test_execute_no_repetition_flag_streams_normally() -> None:
    """无退化标记（正常流）路径零变化——守卫不误伤正常轮。"""
    caller = _FakeCaller({"success": True, "data": _ok_response(text="", thinking_text="想了一下")})
    plugin = _make_plugin(caller)

    result = await plugin.execute(_make_ctx(_base_state()))

    assert result["raw_result"] == ""
    assert result["llm_repetition_detected"] is False


async def test_execute_repetition_with_tool_calls_does_not_flag_round() -> None:
    """带工具调用的截断轮不标记退化：调用可能是有效进展，交工具执行面。"""
    caller = _FakeCaller(
        {
            "success": True,
            "data": _ok_response(
                text="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "file_read", "arguments": "{}"},
                    }
                ],
                finish_reason="tool_calls",
                stream_repetition=True,
            ),
        }
    )
    plugin = _make_plugin(caller)

    result = await plugin.execute(_make_ctx(_base_state()))

    assert result["llm_repetition_detected"] is False
