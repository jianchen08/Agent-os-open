# @feature: FP-T07 llm api | @ci: python-coverage
"""批 5 生产改接：管道服务面 → 适配服务面（进程内）→ ``llm.complete_stream``。

真值：主方案 §6.0 收敛图（管道面→适配面=进程内调用；适配面→网关=跨进程契约
调用）+ §7.1 批 5 行 + ADR 2026-11-18-llm-two-tier-plugin-convergence。

契约：
1. **进程内调用路径**：``LLMCore.execute`` 的 LLM 调用经同插件同进程的
   ``llm_adapter.execute(route="gateway")`` 发起——管道面不再自己组装网关信封、
   也不再直连能力句柄（能力句柄只作为适配面的网关出口入参传递）；
2. **网关信封零漂移**：适配面产出的信封 = 改接前的（tool_name/plugin_id/
   args/_call_context）逐键同形；args 只含意图（model id）+ 冻结参数 + 上下文键，
   **端点/凭据不进入出站信封**（连接解析单源归网关）；
3. **等待上限**：适配面按 call_timeout + 60s 余量计算（模型条目 → defaults →
   300s 内部兜底），与改接前 ``_capability_timeout`` 同口径；
4. **半截/错误语义不变**：网关 partial 经适配面原样交付（管道面组装半截落库
   ops）；错误信封剥传输噪声后上抛（错误是值，异常不跨服务边界）；
5. **出口 fail-closed**：gateway 路线缺网关句柄 / 未知路线键即显式报错。

LLM 网关（llm_service）是外部依赖，能力句柄替身合法；适配面逻辑走真实代码。
"""

from __future__ import annotations

import asyncio
import dataclasses
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

# 平铺 import 路径（与 test_llm_core_execute_paths 同款）：_LLM_CORE_DIR 必须
# 压过 _SYSTEM_LLM_DIR——两处均有 adapter.py；_SYSTEM_LLM_DIR 供 _config_models。
for _d in (_SYSTEM_LLM_DIR, _SHARED_DIR, _LLM_CORE_DIR):
    if str(_d) in sys.path:
        sys.path.remove(str(_d))
    sys.path.insert(0, str(_d))

import llm_adapter  # noqa: E402  适配服务面（与 plugin.py 同进程同模块）

_MOD_NAME = "llm_core_adapter_rewire_under_test"


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
ModelIntent = _mod.ModelIntent

#: 生产形态 llm.yaml 视图：模型条目带全套声明（端点/凭据/参数/预算）。
_LLM_DATA: dict[str, Any] = {
    "providers": {
        "zhipu": {
            "type": "zai",
            "api_base": "https://zhipu.test/api/coding/paas/v4/",
            "api_key": "sk-zhipu-secret",
        }
    },
    "models": {
        "gw-glm-5.3-flash": {
            "provider": "zhipu",
            "model_name": "glm-5.3-flash",
            "api_base": "https://zhipu.test/api/coding/paas/v4/",
            "api_key": "sk-zhipu-secret",
            "context_window": 200000,
            "default_params": {
                "temperature": 0.3,
                "max_tokens": 4096,
                "reasoning_effort": "medium",
            },
            "call_timeout": 900,
            "stream_idle_timeout": 120,
        }
    },
    "defaults": {"chat": "gw-glm-5.3-flash", "call_timeout": 300, "stream_idle_timeout": 90},
}

_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "run shell",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
        },
    }
]


class _RecordingCaller:
    """伪网关句柄：记录 (method, params, timeout) 并回放预设信封。"""

    def __init__(self, envelope: Any = None) -> None:
        self._envelope = envelope if envelope is not None else _ok_envelope()
        self.calls: list[tuple[str, dict[str, Any], float | None]] = []

    async def __call__(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        self.calls.append((method, params, timeout))
        return self._envelope


def _ok_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "status": "streamed",
        "stream_id": "stream_test",
        "partial": None,
        "text": "ok",
        "tool_calls": [],
        "thinking_text": None,
        "usage": {},
        "finish_reason": "stop",
    }
    data.update(overrides)
    return data


def _ok_envelope(**overrides: Any) -> dict[str, Any]:
    return {"success": True, "data": _ok_data(**overrides), "error": None}


def _install_llm_config(monkeypatch: pytest.MonkeyPatch, data: dict[str, Any] | None = None) -> None:
    """注入 llm.yaml 命名空间（走真实 ModelConfigLoaderShim 代码路径）。"""
    import _config_models

    monkeypatch.setattr(_config_models, "_config", {"llm": _LLM_DATA if data is None else data})


def _state(**extra: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "messages": [{"role": "user", "content": "hi"}],
        "streaming": True,
        "pipeline_id": "pid-rewire",
        "run_id": "run-42",
        "session_id": "sess-9",
        "message_id": "msg-7",
        "agent_level": "L2",
        "tool_schemas": list(_TOOLS),
        "thinking_strength": '{"reasoning_effort": "high", "temperature": 0.99}',
    }
    state.update(extra)
    return state


def _make_plugin(caller: Any) -> Any:
    set_capability_caller(caller)
    return LLMCore({"provider": "openai", "model_name": "keep-constructor-identity"})


def _make_ctx(state: dict[str, Any]) -> Any:
    ctx = SimpleNamespace()
    ctx.state = state
    return ctx


# ─────────────────── 进程内调用路径（管道面 → 适配面） ───────────────────


def test_pipeline_face_invokes_adapter_face_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """管道面经 ``llm_adapter.execute`` 发起（进程内），不再自己直连能力句柄。"""
    _install_llm_config(monkeypatch)
    caller = _RecordingCaller()
    set_capability_caller(caller)
    seen: dict[str, Any] = {}

    async def _spy(model: str, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        seen["model"] = model
        seen["messages"] = messages
        seen["kwargs"] = kwargs
        return _ok_data()

    monkeypatch.setattr(llm_adapter, "execute", _spy)
    result = asyncio.run(_make_plugin(caller).execute(_make_ctx(_state())))

    assert seen["model"] == "gw-glm-5.3-flash", "适配面入口收到模型 id（意图）"
    assert [m["role"] for m in seen["messages"]] == ["user"]
    assert seen["kwargs"]["route"] == "gateway", "生产管道走网关出口路线"
    assert seen["kwargs"]["gateway"] is caller, "网关句柄经适配面出口传入（同一注入实例）"
    context = seen["kwargs"]["gateway_context"]
    assert isinstance(context, llm_adapter.GatewayContext)
    assert (context.thread_id, context.pipeline_id, context.message_id) == (
        "sess-9",
        "pid-rewire",
        "msg-7",
    )
    assert (context.run_id, context.agent_level) == ("run-42", "L2")
    assert caller.calls == [], "管道面不得自行调用能力句柄（网关调用是适配面内部出口）"
    assert result["raw_result"] == "ok"


def test_relay_envelope_matches_gateway_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """适配面产出的网关信封与改接前逐键同形（tools/messages/参数/上下文）。"""
    _install_llm_config(monkeypatch)
    caller = _RecordingCaller()
    asyncio.run(_make_plugin(caller).execute(_make_ctx(_state())))

    assert len(caller.calls) == 1
    method, params, timeout = caller.calls[0]
    assert method == "invoke"
    assert params["tool_name"] == "llm.complete_stream"
    assert params["plugin_id"] == "llm_service"
    assert params["_call_context"] == {
        "thread_id": "sess-9",
        "pipeline_id": "pid-rewire",
        "message_id": "msg-7",
    }
    args = params["args"]
    assert args["model"] == "gw-glm-5.3-flash"
    assert args["messages"] == [{"role": "user", "content": "hi"}]
    assert args["tools"] == _TOOLS
    assert args["agent_level"] == "L2"
    assert args["run_id"] == "run-42"
    # 冻结参数（绑定快照）+ 思考覆盖（白名单过滤掉 temperature）
    assert args["temperature"] == 0.3
    assert args["max_tokens"] == 4096
    assert args["reasoning_effort"] == "high"
    assert args["stream_idle_timeout"] == 120
    # 等待上限 = 模型 call_timeout + 60s 余量
    assert timeout == pytest.approx(960.0)


def test_relay_envelope_carries_no_connection_material(monkeypatch: pytest.MonkeyPatch) -> None:
    """出站信封只含意图与冻结参数——端点/凭据不出管道面（解析单源归网关）。"""
    _install_llm_config(monkeypatch)
    caller = _RecordingCaller()
    asyncio.run(_make_plugin(caller).execute(_make_ctx(_state())))

    _, params, _ = caller.calls[0]
    args = params["args"]
    assert "api_base" not in args
    assert "api_key" not in args
    assert "sk-zhipu-secret" not in str(params)


def test_relay_partial_snapshot_keeps_half_message_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """网关 partial 经适配面原样交付 → 管道面组装半截落库 ops（语义不变）。"""
    _install_llm_config(monkeypatch)
    caller = _RecordingCaller(
        {
            "success": True,
            "data": _ok_data(
                status="interrupted",
                partial={"text": "半", "thinking_text": "想", "tool_calls": [], "usage": None},
                text=None,
                finish_reason="interrupted",
            ),
        }
    )
    result = asyncio.run(_make_plugin(caller).execute(_make_ctx(_state())))

    assert result["ended"] is True
    assert result["router.stop_reason"] == "user_requested"
    ops = result["messages"]["_ops"]
    assert ops[0]["msg"]["content"] == "半"
    assert ops[0]["msg"]["status"] == "interrupted"
    assert ops[0]["msg"]["reasoning_content"] == "想"


def test_relay_error_envelope_strips_transport_noise(monkeypatch: pytest.MonkeyPatch) -> None:
    """网关错误信封 → 剥传输噪声后上抛（根因可见，异常不跨服务边界静默）。"""
    _install_llm_config(monkeypatch)
    caller = _RecordingCaller(
        {
            "success": False,
            "data": None,
            "error": (
                "MCP call failed: tool call failed: llm_core.execute: MCP protocol error: "
                "[0] llm.complete_stream 工具执行失败: LLM 供应商调用失败[认证失败]: invalid api key"
            ),
        }
    )
    with pytest.raises(RuntimeError, match="认证失败"):
        asyncio.run(_make_plugin(caller).execute(_make_ctx(_state())))


# ─────────────────── 适配面出口 fail-closed / 预算口径 ───────────────────


def test_gateway_route_without_relay_fails_closed() -> None:
    """gateway 路线缺网关句柄 → 显式报错（仅同宿主进程内可用）。"""
    face = llm_adapter.LLMAdapterFace(config_provider=lambda: {"llm": _LLM_DATA})
    with pytest.raises(RuntimeError, match="网关出口未注入"):
        asyncio.run(
            face.execute(
                model="gw-glm-5.3-flash",
                messages=[{"role": "user", "content": "hi"}],
                route="gateway",
            )
        )


def test_unknown_route_fails_closed() -> None:
    """未知路线键 → ValueError（两出口闭集，不猜默认）。"""
    face = llm_adapter.LLMAdapterFace(config_provider=lambda: {"llm": _LLM_DATA})
    with pytest.raises(ValueError, match="未知路线"):
        asyncio.run(
            face.execute(
                model="gw-glm-5.3-flash",
                messages=[{"role": "user", "content": "hi"}],
                route="carrier-pigeon",
            )
        )


def test_gateway_timeout_falls_back_to_internal_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型不在配置表 → 等待上限走 300s 内部兜底口径（+60 余量）。"""
    _install_llm_config(monkeypatch, {"models": {}, "defaults": {}})
    captured: list[float | None] = []

    async def _relay(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        captured.append(timeout)
        return _ok_envelope()

    asyncio.run(
        llm_adapter.LLMAdapterFace().execute(
            model="ghost-model",
            messages=[{"role": "user", "content": "hi"}],
            route="gateway",
            gateway=_relay,
        )
    )
    assert captured == [pytest.approx(360.0)]


def test_relay_omits_optional_context_keys() -> None:
    """无上下文/工具时省略可选键；缺省 GatewayContext 落 agent_level=L3。"""
    captured: list[dict[str, Any]] = []

    async def _relay(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        captured.append(params)
        return _ok_envelope()

    asyncio.run(
        llm_adapter.LLMAdapterFace(config_provider=lambda: {"llm": _LLM_DATA}).execute(
            model="gw-glm-5.3-flash",
            messages=[{"role": "user", "content": "hi"}],
            route="gateway",
            gateway=_relay,
        )
    )
    args = captured[0]["args"]
    assert "run_id" not in args
    assert "tools" not in args
    assert args["agent_level"] == "L3"
    assert captured[0]["_call_context"] == {"thread_id": "", "pipeline_id": "", "message_id": ""}


# ─────────────────── 模型解析意图化（无连接/凭据面） ───────────────────


def test_intent_view_has_no_credential_fields() -> None:
    """意图视图不持凭据（连接事实归网关——本视图字段面即窄化后的契约）。"""
    fields = {f.name for f in dataclasses.fields(ModelIntent)}
    assert "model_id" in fields
    assert "api_key" not in fields
    assert "api_base" in fields, "端点仅作观测回显（不参与调用；出站信封无此键）"


def test_binding_snapshot_excludes_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """绑定快照不含凭据材料（state 会进轨迹/投影面）。"""
    _install_llm_config(monkeypatch)
    caller = _RecordingCaller()
    result = asyncio.run(_make_plugin(caller).execute(_make_ctx(_state())))

    binding = result["llm_model_binding"]
    assert binding["model_id"] == "gw-glm-5.3-flash"
    assert "api_key" not in binding
    assert "sk-zhipu-secret" not in str(binding)
