# @feature: FP-0.2.二 llm_service key fail-closed | @vision: V2 全能闭环 | @ci: python-coverage
"""api_key 占位符未解析时调用直接报错（fail-closed）契约。

背景：``${VAR}`` 占位符（进程环境与项目根 .env 双源均无值）若照常发起
调用，字面量 ``${DEEPSEEK_API_KEY}`` 会作为 key 发往上游，得到无法排查的
鉴权 401（曾致 chat 静默退化为 echo）。契约：发起 HTTP 前抛
``LLMKeyUnresolvedError``，携带 model/provider/占位符供定位。

覆盖两条路径，各带真 key 正对照：
- KeyPool 池路径：slot 取到即校验该 key；
- ``_route_call`` 路径：Router 部署烘入的 key（模型级优先，回退 provider 槽）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
# 去重插入 [0]：车道共跑时其他插件目录可能残留于 sys.path 前部，
# 平铺 `import adapter` 会命中他插件同名模块。
for _m in ("adapter", "router_factory", "exceptions", "key_pool", "_config_models"):
    sys.modules.pop(_m, None)
if str(_PLUGIN_DIR) in sys.path:
    sys.path.remove(str(_PLUGIN_DIR))
sys.path.insert(0, str(_PLUGIN_DIR))

import adapter as llm_adapter  # noqa: E402  平铺 import，与生产代码一致
import router_factory  # noqa: E402
from exceptions import LLMKeyUnresolvedError  # noqa: E402
from key_pool import KeyPool, KeySlot  # noqa: E402

_MESSAGES = [{"role": "user", "content": "hi"}]


def _make_pool(api_key: str) -> KeyPool:
    return KeyPool(
        [KeySlot(key_id="k1", api_key=api_key, max_concurrent=1)],
        pool_id="minimax",
    )


class _RouterStub:
    """记录 acompletion 调用次数的 Router 桩（HTTP 边界替身）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def acompletion(self, **kwargs: Any) -> dict[str, bool]:
        self.calls += 1
        return {"ok": True}


def _patch_factory_maps(monkeypatch: pytest.MonkeyPatch, pool: KeyPool | None) -> None:
    """把 router_factory 的 model→provider / provider→pool 查表指到桩。"""
    monkeypatch.setattr(
        router_factory,
        "get_provider_for_model",
        lambda m: "minimax" if m == "MiniMax-M3" else "",
    )
    monkeypatch.setattr(router_factory, "get_key_pool", lambda p: pool)


def test_pool_slot_placeholder_fails_before_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """池路径：slot 的 key 是未解析占位符 → 抛 LLMKeyUnresolvedError，不发起调用。"""

    async def scenario() -> tuple[LLMKeyUnresolvedError, list[dict[str, Any]]]:
        _patch_factory_maps(monkeypatch, _make_pool("${MINIMAX_API_KEY}"))
        sent: list[dict[str, Any]] = []

        async def _fake_direct(**kwargs: Any) -> dict[str, bool]:
            sent.append(kwargs)
            return {"ok": True}

        ad = llm_adapter.KeyPoolAdapter(_RouterStub())
        monkeypatch.setattr(ad, "_direct_call_with_slot", _fake_direct)
        with pytest.raises(LLMKeyUnresolvedError) as ei:
            await ad._do_completion(model="minimax/MiniMax-M3", messages=_MESSAGES)
        return ei.value, sent

    exc, sent = asyncio.run(scenario())
    assert exc.provider == "minimax"
    assert "MINIMAX_API_KEY" in exc.placeholder
    assert sent == [], "占位符 key 不得发起任何上游调用"


def test_pool_slot_real_key_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """池路径正对照：真 key 放行直达直连调用。"""

    async def scenario() -> dict[str, bool]:
        _patch_factory_maps(monkeypatch, _make_pool("sk-real-key-123"))

        async def _fake_direct(**kwargs: Any) -> dict[str, bool]:
            assert kwargs.get("api_key") == "sk-real-key-123"
            return {"ok": True}

        ad = llm_adapter.KeyPoolAdapter(_RouterStub())
        monkeypatch.setattr(ad, "_direct_call_with_slot", _fake_direct)
        return await ad._do_completion(model="minimax/MiniMax-M3", messages=_MESSAGES)

    assert asyncio.run(scenario()) == {"ok": True}


def test_route_call_placeholder_fails_before_router(monkeypatch: pytest.MonkeyPatch) -> None:
    """_route_call 路径：模型级 api_key 是占位符 → 报错，router 不被调用。"""

    async def scenario() -> tuple[LLMKeyUnresolvedError, int]:
        router_stub = _RouterStub()
        monkeypatch.setattr(router_factory, "get_or_create_router", lambda ml: router_stub)
        _patch_factory_maps(monkeypatch, None)
        loader = SimpleNamespace(get_model_config=lambda m: {"api_key": "${FOO_KEY}"})
        import _config_models as cm  # noqa: PLC0415

        monkeypatch.setattr(cm, "get_model_config_loader", lambda: loader)
        ad = llm_adapter.KeyPoolAdapter(router_stub)
        with pytest.raises(LLMKeyUnresolvedError) as ei:
            await ad._route_call(model="openai/gpt-4", messages=_MESSAGES)
        return ei.value, router_stub.calls

    exc, calls = asyncio.run(scenario())
    assert exc.placeholder == "${FOO_KEY}"
    assert calls == 0, "占位符 key 不得触达 router.acompletion"


def test_route_call_real_key_reaches_router(monkeypatch: pytest.MonkeyPatch) -> None:
    """_route_call 正对照：真 key 正常走到 router。"""

    async def scenario() -> tuple[dict[str, bool], int]:
        router_stub = _RouterStub()
        monkeypatch.setattr(router_factory, "get_or_create_router", lambda ml: router_stub)
        _patch_factory_maps(monkeypatch, None)
        loader = SimpleNamespace(get_model_config=lambda m: {"api_key": "sk-real"})
        import _config_models as cm  # noqa: PLC0415

        monkeypatch.setattr(cm, "get_model_config_loader", lambda: loader)
        ad = llm_adapter.KeyPoolAdapter(router_stub)
        result = await ad._route_call(model="openai/gpt-4", messages=_MESSAGES)
        return result, router_stub.calls

    result, calls = asyncio.run(scenario())
    assert result == {"ok": True}
    assert calls == 1
