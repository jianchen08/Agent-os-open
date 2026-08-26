# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""KeyPoolAdapter 单元测试（llm_provider_keypool 提供者策略插件）。

覆盖重点（纯逻辑部分）：
- slot 选择 / key 拼参 / 模型字符串反查 / 前缀构造
  （_resolve_provider / _extract_model_name / _direct_call_with_slot）
- 多 key 重试编排与错误分类决策（_do_completion：BAD_REQUEST 不换 key、
  SERVICE_DOWN 退避、可恢复错误换 key、池耗尽转 RateLimitError、Router fallback）
- 流式 release 延迟绑定（_bind_release_to_stream）

litellm.acompletion 是唯一被替身注入的外部依赖（在独立线程 + 独立事件循环中
运行），其余（KeyPool/KeySlot/router_factory 映射表）用真实实现 + monkeypatch
配置。并发许可的归还/延迟用真实信号量行为断言（可观察副作用），不钉内部计数。

模块加载：keypool.py 依赖 llm_core 的 adapter 基类与 system/llm 的
key_pool/router_factory/exceptions/_config_models（裸名平铺 import），
用 importlib 显式路径 + 唯一模块名加载，加载前弹出裸名防劫持。
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import types
from pathlib import Path
from typing import Any

import litellm
import pytest

pytestmark = pytest.mark.unit

_KEYPOOL_DIR = Path(__file__).resolve().parent
_LLM_CORE_DIR = _KEYPOOL_DIR.parent / "llm_core"
_SYSTEM_LLM_DIR = _KEYPOOL_DIR.parents[2] / "system" / "llm"

# keypool.py 及其依赖链用到的裸模块名（平铺布局，加载前弹出防劫持）
_BARE_DEPS = (
    "_payload_diag",
    "_diagnostics",
    "_provider_registry",
    "adapter",
    "exceptions",
    "key_pool",
    "_config_models",
    "router_factory",
)


def _load_module(mod_name: str, path: Path) -> Any:
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _load_all() -> types.SimpleNamespace:
    for name in _BARE_DEPS:
        sys.modules.pop(name, None)
    _load_module("_payload_diag", _LLM_CORE_DIR / "_payload_diag.py")
    _load_module("_diagnostics", _LLM_CORE_DIR / "_diagnostics.py")
    _load_module("_provider_registry", _LLM_CORE_DIR / "_provider_registry.py")
    _load_module("adapter", _LLM_CORE_DIR / "adapter.py")
    _load_module("exceptions", _SYSTEM_LLM_DIR / "exceptions.py")
    _load_module("key_pool", _SYSTEM_LLM_DIR / "key_pool.py")
    _load_module("_config_models", _SYSTEM_LLM_DIR / "_config_models.py")
    _load_module("router_factory", _SYSTEM_LLM_DIR / "router_factory.py")
    kp = _load_module("keypool_agentd_test", _KEYPOOL_DIR / "keypool.py")
    return types.SimpleNamespace(
        kp=kp,
        router_factory=sys.modules["router_factory"],
        key_pool=sys.modules["key_pool"],
        exceptions=sys.modules["exceptions"],
        config_models=sys.modules["_config_models"],
    )


@pytest.fixture(scope="module")
def env() -> Any:
    loaded = _load_all()
    yield loaded
    # 清理：本模块加载的裸名（llm_core 的 adapter 与 system/llm 的 adapter 是
    # 同名不同文件）不得残留 sys.modules 劫持同会话内其他测试文件的裸名 import。
    for name in _BARE_DEPS:
        sys.modules.pop(name, None)
    sys.modules.pop("keypool_agentd_test", None)


def _make_adapter(env: Any) -> Any:
    return env.kp.KeyPoolAdapter(router=None)


def _make_slot(
    env: Any,
    key_id: str,
    api_key: str = "sk-test",
    api_base: str = "",
    max_concurrent: int = 2,
) -> Any:
    return env.key_pool.KeySlot(
        key_id=key_id, api_key=api_key, api_base=api_base, max_concurrent=max_concurrent
    )


def _make_pool(env: Any, slots: list[Any]) -> Any:
    return env.key_pool.KeyPool(slots, pool_id="test-pool")


class _FakeStream:
    """带 aclose 的假流式响应（worker 线程迭代 + 主循环消费）。"""

    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = list(chunks)
        self.aclose_calls = 0

    def __aiter__(self) -> "_FakeStream":
        return self

    async def __anext__(self) -> Any:
        if self._chunks:
            return self._chunks.pop(0)
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.aclose_calls += 1


# ── _resolve_provider ──────────────────────────────────────────────


def test_resolve_provider_prefixed_and_bare_model(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """带/不带 litellm 前缀的 model_id 都能反查 provider（有 KeyPool 才返回）。"""
    adapter = _make_adapter(env)
    pool = _make_pool(env, [_make_slot(env, "k1")])
    monkeypatch.setattr(
        env.router_factory, "get_provider_for_model", lambda m: "zhipu_coding" if m == "glm-5.1" else ""
    )
    monkeypatch.setattr(env.router_factory, "get_key_pool", lambda p: pool if p == "zhipu_coding" else None)

    assert adapter._resolve_provider("zai/glm-5.1") == "zhipu_coding"
    assert adapter._resolve_provider("glm-5.1") == "zhipu_coding"


def test_resolve_provider_unknown_or_without_pool_returns_empty(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """provider 未映射 / 映射了但无 KeyPool → 空串（回退 Router）。"""
    adapter = _make_adapter(env)
    monkeypatch.setattr(env.router_factory, "get_provider_for_model", lambda m: "")
    monkeypatch.setattr(env.router_factory, "get_key_pool", lambda p: None)
    assert adapter._resolve_provider("zai/glm-5.1") == ""
    assert adapter._resolve_provider("") == ""

    # provider 有映射但无 KeyPool → 空
    monkeypatch.setattr(env.router_factory, "get_provider_for_model", lambda m: "apigo")
    assert adapter._resolve_provider("apigo/x") == ""


# ── _extract_model_name ───────────────────────────────────────────


def test_extract_model_name_strips_prefix(env: Any) -> None:
    adapter = _make_adapter(env)
    assert adapter._extract_model_name({"model": "zai/glm-5.1"}) == "glm-5.1"
    assert adapter._extract_model_name({"model": "glm-5.1"}) == "glm-5.1"
    assert adapter._extract_model_name({}) == ""


# ── _do_completion：编排与错误分类 ──────────────────────────────────


async def test_do_completion_no_pool_routes_to_router(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """无 KeyPool 的 provider → 直接走 Router 回退路径。"""
    adapter = _make_adapter(env)
    monkeypatch.setattr(env.router_factory, "get_provider_for_model", lambda m: "apigo")
    monkeypatch.setattr(env.router_factory, "get_key_pool", lambda p: None)

    async def _fake_route_call(**kwargs: Any) -> str:
        return "routed"

    monkeypatch.setattr(adapter, "_route_call", _fake_route_call)
    result = await adapter._do_completion(model="apigo/m1", messages=[{"role": "user", "content": "hi"}])
    assert result == "routed"


async def test_do_completion_empty_pool_falls_back(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """空 slot 池：零次重试，直接 Router fallback。"""
    adapter = _make_adapter(env)
    pool = _make_pool(env, [])
    monkeypatch.setattr(env.router_factory, "get_provider_for_model", lambda m: "apigo")
    monkeypatch.setattr(env.router_factory, "get_key_pool", lambda p: pool)

    async def _fake_route_call(**kwargs: Any) -> str:
        return "routed"

    monkeypatch.setattr(adapter, "_route_call", _fake_route_call)
    result = await adapter._do_completion(model="apigo/m1")
    assert result == "routed"


async def test_do_completion_empty_pool_fallback_fails_raises_original(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空池 + Router fallback 失败 → 抛 fallback 原始异常（无 last_exc 可抛）。"""
    adapter = _make_adapter(env)
    pool = _make_pool(env, [])
    monkeypatch.setattr(env.router_factory, "get_provider_for_model", lambda m: "apigo")
    monkeypatch.setattr(env.router_factory, "get_key_pool", lambda p: pool)

    async def _fake_route_call_fail(**kwargs: Any) -> Any:
        raise RuntimeError("router down")

    monkeypatch.setattr(adapter, "_route_call", _fake_route_call_fail)
    with pytest.raises(RuntimeError, match="router down"):
        await adapter._do_completion(model="apigo/m1")


async def test_do_completion_success_non_streaming_releases_permit(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非流式成功：真实 acquire_slot 取许可，finally 立即归还（可观察）。"""
    adapter = _make_adapter(env)
    slot = _make_slot(env, "k1", api_key="sk-1", max_concurrent=1)
    pool = _make_pool(env, [slot])
    monkeypatch.setattr(env.router_factory, "get_provider_for_model", lambda m: "apigo")
    monkeypatch.setattr(env.router_factory, "get_key_pool", lambda p: pool)

    async def _fake_direct(slot: Any, **kwargs: Any) -> Any:
        return {"choices": []}

    monkeypatch.setattr(adapter, "_direct_call_with_slot", _fake_direct)
    result = await adapter._do_completion(model="apigo/m1")
    assert result == {"choices": []}
    # 并发许可已归还：再次 acquire 立即成功
    await asyncio.wait_for(slot.acquire(), 0.5)


async def test_do_completion_streaming_defers_release_to_aclose(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """流式成功：release 延迟到 stream.aclose，流未关闭时许可不归还。"""
    adapter = _make_adapter(env)
    slot = _make_slot(env, "k1", api_key="sk-1", max_concurrent=1)
    pool = _make_pool(env, [slot])
    monkeypatch.setattr(env.router_factory, "get_provider_for_model", lambda m: "apigo")
    monkeypatch.setattr(env.router_factory, "get_key_pool", lambda p: pool)

    stream = _FakeStream([])

    async def _fake_direct(slot: Any, **kwargs: Any) -> Any:
        return stream

    monkeypatch.setattr(adapter, "_direct_call_with_slot", _fake_direct)
    result = await adapter._do_completion(model="apigo/m1")
    assert result is stream
    # release 已延迟：流未关闭时并发许可未归还
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(slot.acquire(), 0.2)
    await result.aclose()
    await asyncio.wait_for(slot.acquire(), 0.5)


async def test_do_completion_bad_request_raises_without_retry(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BAD_REQUEST 是不可恢复参数错误：直接抛，不换 key、不走 fallback。"""
    adapter = _make_adapter(env)
    slot1 = _make_slot(env, "k1")
    slot2 = _make_slot(env, "k2")
    pool = _make_pool(env, [slot1, slot2])
    monkeypatch.setattr(env.router_factory, "get_provider_for_model", lambda m: "apigo")
    monkeypatch.setattr(env.router_factory, "get_key_pool", lambda p: pool)

    async def _fake_acquire() -> Any:
        return slot1

    monkeypatch.setattr(pool, "acquire_slot", _fake_acquire)

    async def _fake_direct(slot: Any, **kwargs: Any) -> Any:
        raise litellm.BadRequestError(message="bad param", model="m1", llm_provider="apigo")

    monkeypatch.setattr(adapter, "_direct_call_with_slot", _fake_direct)

    async def _should_not_route(**kwargs: Any) -> Any:
        raise AssertionError("BAD_REQUEST 不应走 Router fallback")

    monkeypatch.setattr(adapter, "_route_call", _should_not_route)
    with pytest.raises(litellm.BadRequestError):
        await adapter._do_completion(model="apigo/m1")


async def test_do_completion_service_down_backoff_then_router_fallback(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SERVICE_DOWN：退避后换下一个 key，全部失败 → Router fallback。"""
    adapter = _make_adapter(env)
    slot1 = _make_slot(env, "k1")
    slot2 = _make_slot(env, "k2")
    pool = _make_pool(env, [slot1, slot2])
    monkeypatch.setattr(env.router_factory, "get_provider_for_model", lambda m: "apigo")
    monkeypatch.setattr(env.router_factory, "get_key_pool", lambda p: pool)

    slots_iter = iter([slot1, slot2])

    async def _fake_acquire() -> Any:
        return next(slots_iter)

    monkeypatch.setattr(pool, "acquire_slot", _fake_acquire)

    async def _fake_direct(slot: Any, **kwargs: Any) -> Any:
        raise litellm.ServiceUnavailableError(message="503 upstream down", llm_provider="apigo", model="m1")

    monkeypatch.setattr(adapter, "_direct_call_with_slot", _fake_direct)

    async def _noop_sleep(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(env.kp.asyncio, "sleep", _noop_sleep)

    handled: list[str] = []
    for s in (slot1, slot2):
        orig = s.handle_error

        def _spy(info: Any, _orig: Any = orig) -> None:
            handled.append(info.kind.value)
            _orig(info)

        monkeypatch.setattr(s, "handle_error", _spy)

    async def _fake_route_call(**kwargs: Any) -> str:
        return "fallback-ok"

    monkeypatch.setattr(adapter, "_route_call", _fake_route_call)
    result = await adapter._do_completion(model="apigo/m1")
    assert result == "fallback-ok"
    # 两个 key 都被按 SERVICE_DOWN 处理过（错误分类决策可观察）
    assert handled == ["service_down", "service_down"]


async def test_do_completion_recoverable_error_retries_next_key(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """可恢复错误（RATE_LIMIT）：换下一个 key 重试，成功即返回。"""
    adapter = _make_adapter(env)
    slot1 = _make_slot(env, "k1")
    slot2 = _make_slot(env, "k2")
    pool = _make_pool(env, [slot1, slot2])
    monkeypatch.setattr(env.router_factory, "get_provider_for_model", lambda m: "apigo")
    monkeypatch.setattr(env.router_factory, "get_key_pool", lambda p: pool)

    slots_iter = iter([slot1, slot2])

    async def _fake_acquire() -> Any:
        return next(slots_iter)

    monkeypatch.setattr(pool, "acquire_slot", _fake_acquire)

    async def _fake_direct(slot: Any, **kwargs: Any) -> Any:
        if slot is slot1:
            raise litellm.RateLimitError(message="rate limit exceeded", model="m1", llm_provider="apigo")
        return {"ok": True}

    monkeypatch.setattr(adapter, "_direct_call_with_slot", _fake_direct)
    result = await adapter._do_completion(model="apigo/m1")
    assert result == {"ok": True}
    # 限流 key 已进入冷却（公开状态），备用 key 未冷却
    assert slot1.is_cooling
    assert not slot2.is_cooling


async def test_do_completion_pool_exhausted_converts_to_rate_limit(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """池耗尽（acquire_slot 超时）→ 转业务可读 RateLimitError，fallback 失败时抛它。"""
    adapter = _make_adapter(env)
    slot1 = _make_slot(env, "k1")
    pool = _make_pool(env, [slot1])
    monkeypatch.setattr(env.router_factory, "get_provider_for_model", lambda m: "apigo")
    monkeypatch.setattr(env.router_factory, "get_key_pool", lambda p: pool)

    exhausted = env.exceptions.KeyPoolExhaustedError("test-pool", 60.0, ["k1...(cooling=True)"])

    async def _fake_acquire() -> Any:
        raise exhausted

    monkeypatch.setattr(pool, "acquire_slot", _fake_acquire)

    async def _fake_route_call_fail(**kwargs: Any) -> Any:
        raise RuntimeError("router down")

    monkeypatch.setattr(adapter, "_route_call", _fake_route_call_fail)
    with pytest.raises(litellm.RateLimitError) as excinfo:
        await adapter._do_completion(model="apigo/m1")
    assert excinfo.value.__cause__ is exhausted
    assert "所有 API key 不可用" in str(excinfo.value)


async def test_do_completion_pool_exhausted_fallback_succeeds(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """池耗尽 → Router fallback 成功时返回 fallback 结果。"""
    adapter = _make_adapter(env)
    slot1 = _make_slot(env, "k1")
    pool = _make_pool(env, [slot1])
    monkeypatch.setattr(env.router_factory, "get_provider_for_model", lambda m: "apigo")
    monkeypatch.setattr(env.router_factory, "get_key_pool", lambda p: pool)

    exhausted = env.exceptions.KeyPoolExhaustedError("test-pool", 60.0, ["k1...(cooling=True)"])

    async def _fake_acquire() -> Any:
        raise exhausted

    monkeypatch.setattr(pool, "acquire_slot", _fake_acquire)

    async def _fake_route_call(**kwargs: Any) -> str:
        return "fallback-ok"

    monkeypatch.setattr(adapter, "_route_call", _fake_route_call)
    result = await adapter._do_completion(model="apigo/m1")
    assert result == "fallback-ok"


async def test_do_completion_cancelled_propagates(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """用户取消：不冷却、直接抛，且 finally 归还并发许可。"""
    adapter = _make_adapter(env)
    slot1 = _make_slot(env, "k1", max_concurrent=1)
    pool = _make_pool(env, [slot1])
    monkeypatch.setattr(env.router_factory, "get_provider_for_model", lambda m: "apigo")
    monkeypatch.setattr(env.router_factory, "get_key_pool", lambda p: pool)

    async def _fake_acquire() -> Any:
        return slot1

    monkeypatch.setattr(pool, "acquire_slot", _fake_acquire)

    async def _fake_direct(slot: Any, **kwargs: Any) -> Any:
        raise asyncio.CancelledError()

    monkeypatch.setattr(adapter, "_direct_call_with_slot", _fake_direct)
    with pytest.raises(asyncio.CancelledError):
        await adapter._do_completion(model="apigo/m1")
    # 取消路径也归还许可
    await asyncio.wait_for(slot1.acquire(), 0.5)


async def test_do_completion_full_path_real_direct_call(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """关键路径走真实 _direct_call_with_slot（仅 litellm.acompletion 替身注入）。"""
    adapter = _make_adapter(env)
    slot = _make_slot(env, "k1", api_key="sk-1", api_base="https://api.example.com")
    pool = _make_pool(env, [slot])
    monkeypatch.setattr(
        env.router_factory,
        "get_provider_for_model",
        lambda m: "apigo" if m == "deepseek-v4-pro-apigo" else "",
    )
    monkeypatch.setattr(env.router_factory, "get_key_pool", lambda p: pool if p == "apigo" else None)
    monkeypatch.setattr(env.router_factory, "get_litellm_prefix", lambda p: "openai" if p == "apigo" else "")
    monkeypatch.setattr(
        env.router_factory,
        "get_model_name_for_id",
        lambda m: "deepseek-v4-pro" if m == "deepseek-v4-pro-apigo" else m,
    )

    captured: dict[str, Any] = {}

    async def _fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return {"choices": [{"message": {"content": "hi"}}]}

    monkeypatch.setattr(env.kp.litellm, "acompletion", _fake_acompletion)
    result = await adapter._do_completion(
        model="apigo/deepseek-v4-pro-apigo",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.7,
    )
    assert result["choices"][0]["message"]["content"] == "hi"
    # model_id → provider → litellm 前缀 + model_name 反查拼串
    assert captured["model"] == "openai/deepseek-v4-pro"
    assert captured["api_key"] == "sk-1"
    assert captured["api_base"] == "https://api.example.com"
    assert captured["num_retries"] == 0
    assert captured["timeout"] == 180.0
    assert captured["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["temperature"] == 0.7
    # 非流式成功：并发许可已归还
    await asyncio.wait_for(slot.acquire(), 0.5)


# ── _route_call ───────────────────────────────────────────────────


async def test_route_call_uses_latest_router(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """_route_call 每次动态获取最新 Router（不缓存 self._router）。"""
    adapter = _make_adapter(env)

    class _FakeRouter:
        async def acompletion(self, **kwargs: Any) -> str:
            return "routed"

    fake_router = _FakeRouter()
    called_with: list[Any] = []

    def _fake_get_or_create(loader: Any) -> Any:
        called_with.append(loader)
        return fake_router

    monkeypatch.setattr(env.router_factory, "get_or_create_router", _fake_get_or_create)
    loader = object()
    monkeypatch.setattr(env.config_models, "get_model_config_loader", lambda: loader)

    result = await adapter._route_call(model="m1", messages=[])
    assert result == "routed"
    assert called_with == [loader]


# ── _bind_release_to_stream ───────────────────────────────────────


async def test_bind_release_to_stream_releases_once(env: Any) -> None:
    """一次性标志：重复 aclose 只 release 一次（许可不重复归还）。"""
    adapter = _make_adapter(env)
    slot = _make_slot(env, "k1", max_concurrent=1)
    stream = _FakeStream([])
    adapter._bind_release_to_stream(stream, slot)
    await slot.acquire()  # 占掉唯一许可
    await stream.aclose()  # 释放
    await slot.acquire()  # 再占
    await stream.aclose()  # 第二次关闭：一次性标志阻止重复 release
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(slot.acquire(), 0.2)  # 许可未重复归还 → 阻塞


async def test_bind_release_to_stream_without_aclose(env: Any) -> None:
    """流对象无 aclose：release 照常执行，不抛错。"""
    adapter = _make_adapter(env)
    slot = _make_slot(env, "k1", max_concurrent=1)

    class _NoAcloseStream:
        pass

    stream = _NoAcloseStream()
    adapter._bind_release_to_stream(stream, slot)
    await slot.acquire()
    await stream.aclose()  # type: ignore[attr-defined]
    await asyncio.wait_for(slot.acquire(), 0.5)


async def test_bind_release_to_stream_aclose_timeout(
    env: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """aclose 半死挂起：超时放弃优雅关闭，release 已先行执行。"""
    adapter = _make_adapter(env)
    slot = _make_slot(env, "k1", max_concurrent=1)
    monkeypatch.setattr(env.kp, "_ACLOSE_TIMEOUT_SECONDS", 0.2)

    class _HangingStream:
        async def aclose(self) -> None:
            await asyncio.sleep(10.0)

    stream = _HangingStream()
    adapter._bind_release_to_stream(stream, slot)
    with caplog.at_level(logging.WARNING):
        await stream.aclose()
    assert any("stream.aclose 超时" in r.getMessage() for r in caplog.records)
    await asyncio.wait_for(slot.acquire(), 0.5)


async def test_bind_release_to_stream_aclose_exception(
    env: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """aclose 自身异常（非超时）：不阻断 finally 返回，release 已执行。"""
    adapter = _make_adapter(env)
    slot = _make_slot(env, "k1", max_concurrent=1)

    class _RaisingStream:
        async def aclose(self) -> None:
            raise RuntimeError("close failed")

    stream = _RaisingStream()
    adapter._bind_release_to_stream(stream, slot)
    with caplog.at_level(logging.DEBUG):
        await stream.aclose()  # 不抛错
    assert any("aclose 异常" in r.getMessage() for r in caplog.records)
    await asyncio.wait_for(slot.acquire(), 0.5)


# ── _direct_call_with_slot：key 拼参 / 模型反查 / 前缀构造 ─────────


async def test_direct_call_builds_litellm_kwargs(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """model_id → provider → 前缀 + model_name 反查拼 litellm 模型串，slot 凭证注入。"""
    adapter = _make_adapter(env)
    slot = _make_slot(env, "k1", api_key="sk-1", api_base="https://api.example.com")
    monkeypatch.setattr(
        env.router_factory,
        "get_provider_for_model",
        lambda m: "apigo" if m == "deepseek-v4-pro-apigo" else "",
    )
    monkeypatch.setattr(env.router_factory, "get_litellm_prefix", lambda p: "openai" if p == "apigo" else "")
    monkeypatch.setattr(
        env.router_factory,
        "get_model_name_for_id",
        lambda m: "deepseek-v4-pro" if m == "deepseek-v4-pro-apigo" else m,
    )

    captured: dict[str, Any] = {}

    async def _fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(env.kp.litellm, "acompletion", _fake_acompletion)
    result = await adapter._direct_call_with_slot(
        slot=slot,
        model="apigo/deepseek-v4-pro-apigo",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.7,
    )
    assert result == {"ok": True}
    assert captured["model"] == "openai/deepseek-v4-pro"
    assert captured["api_key"] == "sk-1"
    assert captured["api_base"] == "https://api.example.com"
    assert captured["num_retries"] == 0
    assert captured["timeout"] == 180.0
    assert captured["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["temperature"] == 0.7


async def test_direct_call_no_prefix_no_api_base(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """provider 未配置（空）→ 无前缀直连；slot 无 api_base → 不传。"""
    adapter = _make_adapter(env)
    slot = _make_slot(env, "k1", api_key="sk-1")
    monkeypatch.setattr(env.router_factory, "get_provider_for_model", lambda m: "")
    monkeypatch.setattr(env.router_factory, "get_litellm_prefix", lambda p: "")
    monkeypatch.setattr(env.router_factory, "get_model_name_for_id", lambda m: m)

    captured: dict[str, Any] = {}

    async def _fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(env.kp.litellm, "acompletion", _fake_acompletion)
    result = await adapter._direct_call_with_slot(slot=slot, model="glm-5.1")
    assert result == {"ok": True}
    assert captured["model"] == "glm-5.1"  # 无前缀：直接用 model_name
    assert "api_base" not in captured
    assert captured["api_key"] == "sk-1"


async def test_direct_call_timeout_resolution(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """超时解析：first_chunk_timeout 生效 / 显式 timeout 覆盖 / 非数值保持原值。"""
    adapter = _make_adapter(env)
    slot = _make_slot(env, "k1", api_key="sk-1")
    monkeypatch.setattr(env.router_factory, "get_provider_for_model", lambda m: "apigo")
    monkeypatch.setattr(env.router_factory, "get_litellm_prefix", lambda p: "openai")
    monkeypatch.setattr(env.router_factory, "get_model_name_for_id", lambda m: m)

    captured: dict[str, Any] = {}

    async def _fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(env.kp.litellm, "acompletion", _fake_acompletion)

    # first_chunk_timeout 生效
    await adapter._direct_call_with_slot(slot=slot, model="apigo/m1", first_chunk_timeout=0.5)
    assert captured["timeout"] == 0.5
    # 显式 timeout 覆盖 first_chunk_timeout
    await adapter._direct_call_with_slot(slot=slot, model="apigo/m1", first_chunk_timeout=0.5, timeout=30)
    assert captured["timeout"] == 30.0
    # 非数值 timeout（httpx.Timeout 对象等）→ 保持 first_chunk_timeout
    await adapter._direct_call_with_slot(slot=slot, model="apigo/m1", first_chunk_timeout=0.5, timeout=object())
    assert captured["timeout"] == 0.5
    # 现状契约：first_chunk_timeout 在 input_kwargs 构造后才被 pop，会随 **kwargs
    # 一并传给 litellm.acompletion（疑似缺陷，见报告）
    assert captured["first_chunk_timeout"] == 0.5


async def test_direct_call_streaming_returns_bridge(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """流式：worker 线程迭代 chunk 入队，主循环拿到 _ThreadedStreamBridge 消费。"""
    adapter = _make_adapter(env)
    slot = _make_slot(env, "k1", api_key="sk-1")
    monkeypatch.setattr(env.router_factory, "get_provider_for_model", lambda m: "apigo")
    monkeypatch.setattr(env.router_factory, "get_litellm_prefix", lambda p: "openai")
    monkeypatch.setattr(env.router_factory, "get_model_name_for_id", lambda m: m)

    stream = _FakeStream(["chunk-1", "chunk-2"])

    async def _fake_acompletion(**kwargs: Any) -> Any:
        return stream

    monkeypatch.setattr(env.kp.litellm, "acompletion", _fake_acompletion)
    result = await adapter._direct_call_with_slot(slot=slot, model="apigo/m1")
    assert isinstance(result, env.kp._ThreadedStreamBridge)
    chunks = [c async for c in result]
    assert chunks == ["chunk-1", "chunk-2"]
    assert stream.aclose_calls == 1  # worker 迭代结束后关闭底层流
    await result.aclose()


async def test_direct_call_worker_exception_propagates(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """worker 线程内 litellm 异常 → 装箱透传到主协程。"""
    adapter = _make_adapter(env)
    slot = _make_slot(env, "k1", api_key="sk-1")
    monkeypatch.setattr(env.router_factory, "get_provider_for_model", lambda m: "apigo")
    monkeypatch.setattr(env.router_factory, "get_litellm_prefix", lambda p: "openai")
    monkeypatch.setattr(env.router_factory, "get_model_name_for_id", lambda m: m)

    async def _fake_acompletion(**kwargs: Any) -> Any:
        raise ValueError("upstream boom")

    monkeypatch.setattr(env.kp.litellm, "acompletion", _fake_acompletion)
    with pytest.raises(ValueError, match="upstream boom"):
        await adapter._direct_call_with_slot(slot=slot, model="apigo/m1")


async def test_direct_call_worker_exc_observed_in_wait_loop(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """worker 异常在 done_evt 置位前被主循环观察到 → 等待循环内立即透传。

    拉大「异常入箱 → done 置位」窗口（Event.set 延迟 0.5s，主循环 0.1s 轮询），
    使主循环在 while 条件内命中 _exc_box 分支（而非循环后的兜底检查）。
    """
    import threading as _threading
    import time as _time

    adapter = _make_adapter(env)
    slot = _make_slot(env, "k1", api_key="sk-1")
    monkeypatch.setattr(env.router_factory, "get_provider_for_model", lambda m: "apigo")
    monkeypatch.setattr(env.router_factory, "get_litellm_prefix", lambda p: "openai")
    monkeypatch.setattr(env.router_factory, "get_model_name_for_id", lambda m: m)

    class _SlowSetEvent(_threading.Event):
        def set(self) -> None:
            _time.sleep(0.5)
            super().set()

    # keypool.py 在函数内 `import threading`，须 patch 真实模块（monkeypatch 自动恢复）
    monkeypatch.setattr(_threading, "Event", _SlowSetEvent)

    async def _fake_acompletion(**kwargs: Any) -> Any:
        raise ValueError("upstream boom")

    monkeypatch.setattr(env.kp.litellm, "acompletion", _fake_acompletion)
    with pytest.raises(ValueError, match="upstream boom"):
        await adapter._direct_call_with_slot(slot=slot, model="apigo/m1")


async def test_direct_call_streaming_close_evt_breaks_early(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """流式 worker 迭代中 close_evt 置位 → 提前 break 停止消费（aclose 仍执行）。"""
    adapter = _make_adapter(env)
    slot = _make_slot(env, "k1", api_key="sk-1")
    monkeypatch.setattr(env.router_factory, "get_provider_for_model", lambda m: "apigo")
    monkeypatch.setattr(env.router_factory, "get_litellm_prefix", lambda p: "openai")
    monkeypatch.setattr(env.router_factory, "get_model_name_for_id", lambda m: m)

    class _SlowStream(_FakeStream):
        async def __anext__(self) -> Any:
            await asyncio.sleep(0.2)  # 慢流：让 worker 在迭代中途停留，主协程可先返回
            return await super().__anext__()

    stream = _SlowStream(["chunk-1", "chunk-2", "chunk-3"])

    async def _fake_acompletion(**kwargs: Any) -> Any:
        return stream

    monkeypatch.setattr(env.kp.litellm, "acompletion", _fake_acompletion)
    result = await adapter._direct_call_with_slot(slot=slot, model="apigo/m1")
    assert isinstance(result, env.kp._ThreadedStreamBridge)
    # 消费一个 chunk 后立即关闭：worker 仍在迭代（慢流），close_evt 置位 → 提前 break
    first = await result.__anext__()
    assert first == "chunk-1"
    await result.aclose()
    # worker 提前退出：chunk-3 未被产出（break 生效），底层流被 finally 关闭
    remaining = []
    with pytest.raises(StopAsyncIteration):
        while True:
            remaining.append(await asyncio.wait_for(result.__anext__(), 2.0))
    assert "chunk-3" not in remaining
    assert stream.aclose_calls == 1


@pytest.mark.timing
async def test_direct_call_timeout_raises(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """首 token 超时包括在 litellm.acompletion 调用本身：到点抛 TimeoutError。"""
    adapter = _make_adapter(env)
    slot = _make_slot(env, "k1", api_key="sk-1")
    monkeypatch.setattr(env.router_factory, "get_provider_for_model", lambda m: "apigo")
    monkeypatch.setattr(env.router_factory, "get_litellm_prefix", lambda p: "openai")
    monkeypatch.setattr(env.router_factory, "get_model_name_for_id", lambda m: m)

    async def _fake_acompletion(**kwargs: Any) -> Any:
        await asyncio.sleep(10.0)
        return {"ok": True}

    monkeypatch.setattr(env.kp.litellm, "acompletion", _fake_acompletion)
    with pytest.raises(asyncio.TimeoutError, match="litellm.acompletion 超时"):
        await adapter._direct_call_with_slot(slot=slot, model="apigo/m1", first_chunk_timeout=0.3)
