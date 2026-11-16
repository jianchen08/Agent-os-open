# @feature: FP-T07 llm api | @ci: python-coverage
"""网关 provider 插件路由分支测试（2026-11-04 网关化第二步）。

契约（ADR 2026-11-04-llm-default-gateway-and-provider-plugins，DSH 要求对照）：
- providers.<name>.via=plugin 的模型改调 llm_provider.<provider_type>.* 契约方
  （传输在插件）；路由/池化/兜底/用量留网关
- 凭据两形态：provider 有 keys=网关托管（逐 slot 试、失败契约翻译自有
  异常进既有冷却/换 key 机）；无 keys=插件自持（单次调用，plugin 自理上游）
- 失败契约=data.llm_failure{code,message,retry_after?,request_id?} → 网关翻译
  结构化失败异常（复用 classify_error/冷却机制）
- via 未声明 → 内置直连路径原样（零迁移）；caller 未注入 → fail-closed
- _plugin_call_ctx 由 server 层携带（流事件信封），仅插件路径下发、内置路径
  必须弹出（不外泄上游）

外部依赖全桩（caller/route 查表/wire 层），绝不触网。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_s = str(_PLUGIN_DIR)
while _s in sys.path:
    sys.path.remove(_s)
sys.path.insert(0, _s)

for _bare in ("adapter", "exceptions", "key_pool", "router_factory"):
    sys.modules.pop(_bare, None)

import adapter as adapter_mod  # noqa: E402
import router_factory as _rf_mod  # noqa: E402

# ─────────────────────────── 桩件 ───────────────────────────


class _StubCaller:
    """provider 插件 caller 桩：记录 (ns, method, params) 并按队列出参。"""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def __call__(
        self, ns: str, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        self.calls.append((ns, method, dict(params)))
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _ok_data(text: str = "答复") -> dict[str, Any]:
    return {
        "status": "streamed",
        "text": text,
        "tool_calls": [],
        "thinking_text": None,
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        "finish_reason": "stop",
    }


def _fail_data(code: str, message: str = "上游故障") -> dict[str, Any]:
    return {"status": "error", "llm_failure": {"code": code, "message": message}}


def _make_adapter(caller: Any) -> adapter_mod.KeyPoolAdapter:
    ad = adapter_mod.KeyPoolAdapter(router=None)
    ad.set_plugin_provider_caller(caller)
    return ad


def _route(model_id: str, provider_type: str = "sample") -> dict[str, str]:
    return {"provider_type": provider_type}


def _patch_route(monkeypatch: pytest.MonkeyPatch, model_id: str | None, provider_type: str = "sample") -> None:
    """钉住路由查表：model_id 命中 → via plugin；None → 未声明（恒 None）。"""
    monkeypatch.setattr(
        _rf_mod,
        "get_plugin_provider_route",
        lambda mid: _route(mid, provider_type) if (model_id and mid == model_id) else None,
    )
    monkeypatch.setattr(_rf_mod, "get_provider_for_model", lambda mid: "sample-prov" if (model_id and mid == model_id) else "")
    monkeypatch.setattr(_rf_mod, "get_key_pool", lambda _p: None)


def _base_kwargs(**extra: Any) -> dict[str, Any]:
    kw: dict[str, Any] = {
        "model": "sample-model",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "_plugin_call_ctx": {"stream_id": "stream_x", "pipeline_id": "p1"},
    }
    kw.update(extra)
    return kw


# ─────────────────────────── 路由分支 ───────────────────────────


async def test_via_plugin_routes_to_caller_and_returns_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """via 声明模型 → 调 llm_provider.<type>.complete_stream；响应聚合透传；
    stream_ctx（流事件信封）下发插件；_plugin_call_ctx 不残留进上游参数面。"""
    _patch_route(monkeypatch, "sample-model")
    caller = _StubCaller([_ok_data("来自插件")])
    ad = _make_adapter(caller)

    resp = await ad._do_completion(**_base_kwargs())

    assert resp.text == "来自插件"
    assert resp.finish_reason == "stop"
    assert len(caller.calls) == 1
    ns, method, params = caller.calls[0]
    assert ns == "llm_provider.sample"
    assert method == "complete_stream"
    assert params["model"] == "sample-model"
    assert params["messages"] == [{"role": "user", "content": "hi"}]
    assert params["stream_ctx"]["stream_id"] == "stream_x"
    assert "_plugin_call_ctx" not in params


async def test_via_absent_uses_builtin_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """via 未声明 → 不进插件分支（无 caller 调用；无 pool 落 Router 原路径）。"""
    _patch_route(monkeypatch, None)
    caller = _StubCaller([_ok_data()])
    ad = _make_adapter(caller)

    with pytest.raises(RuntimeError, match="缺 api_base"):
        await ad._do_completion(**_base_kwargs())
    assert len(caller.calls) == 0


async def test_missing_caller_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """via 声明但 caller 未注入（提供方未装载）→ fail-closed 显式报错。"""
    _patch_route(monkeypatch, "sample-model")
    ad = adapter_mod.KeyPoolAdapter(router=None)  # 不注入 caller

    with pytest.raises(RuntimeError, match="fail-closed|provider caller"):
        await ad._do_completion(**_base_kwargs())


# ─────────────────────────── 失败契约翻译 ───────────────────────────


@pytest.mark.parametrize(
    ("code", "expected_code"),
    [
        ("rate_limit", "rate_limit"),
        ("auth", "auth"),
        ("quota", "quota"),
        ("service_down", "service_down"),
        ("bad_request", "bad_request"),
    ],
)
async def test_failure_contract_translated_to_own_exception(
    monkeypatch: pytest.MonkeyPatch, code: str, expected_code: str
) -> None:
    """llm_failure{code} → _PluginProviderFailure（自有失败词汇，kind 结构化
    供 KeySlot 冷却机直接消费，无第三方异常依赖）。未知 code 兜底
    service_down。"""
    _patch_route(monkeypatch, "sample-model")
    caller = _StubCaller([_fail_data(code)])
    ad = _make_adapter(caller)

    with pytest.raises(adapter_mod._PluginProviderFailure) as ei:
        await ad._do_completion(**_base_kwargs())
    assert ei.value.code == expected_code


# ─────────────────────────── 凭据两形态 ───────────────────────────


async def test_plugin_managed_no_pool_single_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """插件自持（provider 无 keys → 无 pool）：单次调用，不查 slot。"""
    _patch_route(monkeypatch, "sample-model")
    caller = _StubCaller([_ok_data("来自插件")])
    ad = _make_adapter(caller)
    monkeypatch.setattr(_rf_mod, "get_key_pool", lambda provider: None)

    resp = await ad._do_completion(**_base_kwargs())
    assert resp.text == "来自插件"
    assert "api_key" not in caller.calls[0][2]
    assert "api_base" not in caller.calls[0][2]


async def test_gateway_managed_keys_inject_slot_and_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """网关托管（provider 有 keys → pool）：slot 的 api_base/api_key 注入插件
    参数；首 key 失败（rate_limit 冷却）换次 key 重试成功。"""

    class _FakeSlot:
        def __init__(self, key_id: str, key: str, base: str) -> None:
            self.key_id = key_id
            self.api_key = key
            self.api_base = base
            self.handled: list[Any] = []
            self.released = False

        def release(self) -> None:
            self.released = True

        def on_success(self) -> None:
            pass

        def handle_error(self, info: Any) -> None:
            self.handled.append(info)

    slots = [_FakeSlot("k1", "sk-first", "https://first.example"), _FakeSlot("k2", "sk-second", "https://second.example")]

    class _FakePool:
        def __init__(self, slots: list[Any]) -> None:
            self.slots = slots
            self._i = 0

        async def acquire_slot(self) -> Any:
            slot = self.slots[self._i]
            self._i += 1
            return slot

    _patch_route(monkeypatch, "sample-model")
    caller = _StubCaller([_fail_data("rate_limit", "首 key 限流"), _ok_data("次 key 成功")])
    ad = _make_adapter(caller)
    fake_pool = _FakePool(slots)
    monkeypatch.setattr(_rf_mod, "get_key_pool", lambda provider: fake_pool if provider == "sample-prov" else None)
    monkeypatch.setattr(_rf_mod, "get_provider_for_model", lambda mid: "sample-prov" if mid == "sample-model" else "")

    resp = await ad._do_completion(**_base_kwargs())

    assert resp.text == "次 key 成功"
    assert caller.calls[0][2]["api_key"] == "sk-first"
    assert caller.calls[1][2]["api_key"] == "sk-second"
    assert caller.calls[1][2]["api_base"] == "https://second.example"
    assert slots[0].handled  # 首 key 进了冷却策略
    assert slots[0].released and slots[1].released


# ─────────────────────────── 路由查表（router_factory 面） ───────────────────────────


def test_plugin_provider_route_from_provider_via(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_router 按 providers.<name>.via=plugin 填路由表；reset 清空。"""
    data = {
        "models": {
            "m-plugin": {"provider": "prov-x", "model_name": "m-plugin"},
            "m-direct": {"provider": "prov-y", "model_name": "m-direct"},
        },
        "providers": {
            "prov-x": {"type": "openai", "via": "plugin", "provider_type": "sample"},
            "prov-y": {"type": "openai"},
        },
    }

    class _FakeLoader:
        def __init__(self, d: Any) -> None:
            self._d = d

        def load_llm_data(self) -> Any:
            return self._d

    try:
        _rf_mod.build_router(model_loader=_FakeLoader(data))
        assert _rf_mod.get_plugin_provider_route("m-plugin") == {"provider_type": "sample"}
        assert _rf_mod.get_plugin_provider_route("m-direct") is None
        assert _rf_mod.get_plugin_provider_route("unknown") is None
    finally:
        _rf_mod.reset_router()
    assert _rf_mod.get_plugin_provider_route("m-plugin") is None
