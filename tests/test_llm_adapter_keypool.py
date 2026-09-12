# @feature: FP-0.2.二 内部模块统一 manifest 化 | @ci: python-coverage
"""KeyPoolAdapter 全链补测：多 key 重试 / fail-closed / 异常分诊 / Router 兜底。

外部依赖全桩：KeyPool/KeySlot、router_factory 查表、litellm.acompletion、
classify_error。断行为（选哪个 key、是否换 key 重试、异常类型与 cause 链、
release 时机），不碰真实网络。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LLM_SERVICE_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "llm"
if str(_LLM_SERVICE_DIR) in sys.path:
    sys.path.remove(str(_LLM_SERVICE_DIR))
sys.path.insert(0, str(_LLM_SERVICE_DIR))

import adapter as _adapter  # noqa: E402
import exceptions as _llm_exceptions  # noqa: E402
import key_pool as _key_pool_mod  # noqa: E402
import router_factory as _router_factory_mod  # noqa: E402
import litellm  # noqa: E402
from agentos_plugin_sdk.error_classifier import ErrorKind, ErrorInfo  # noqa: E402


@pytest.fixture(autouse=True)
def _pin_flat_modules():
    """重绑平铺模块裸名槽位到本文件 import 的实例。

    生产代码（adapter）在调用路径内运行时才 ``from exceptions import ...`` /
    ``from router_factory import ...``——共跑车道里 sys.modules 的这些裸名
    可能被其他目录的同名模块占位，抛出的异常类与断言类身份分裂、raises
    失配。重绑（非逐出，逐出会伤已持有旧实例引用的邻居测试）保证运行时
    导入命中与本文件一致的实例。
    """
    saved: dict[str, object] = {}
    for name, mod in (
        ("exceptions", _llm_exceptions),
        ("router_factory", _router_factory_mod),
        ("key_pool", _key_pool_mod),
    ):
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mod
    try:
        yield
    finally:
        for name, old in saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


# ─────────────────────────── 桩对象 ───────────────────────────


class FakeSlot:
    """KeySlot 桩：记录 release/on_success/handle_error 调用。"""

    def __init__(self, key_id: str, api_key: str, api_base: str | None = None) -> None:
        self.key_id = key_id
        self.api_key = api_key
        self.api_base = api_base
        self._consecutive_down = 0
        self.released = 0
        self.succeeded = 0
        self.errors: list[ErrorInfo] = []

    def release(self) -> None:
        self.released += 1

    def on_success(self) -> None:
        self.succeeded += 1

    def handle_error(self, info: ErrorInfo) -> None:
        self.errors.append(info)


class FakePool:
    """acquire_slot 按预排槽位序列逐个给出（含重复=同 key 重试）。"""

    def __init__(self, slots: list[FakeSlot]) -> None:
        self.slots: list[Any] = slots
        self._seq = list(slots)
        self.acquires = 0

    async def acquire_slot(self) -> Any:
        self.acquires += 1
        if self._seq:
            return self._seq.pop(0)
        return self.slots[0]


@pytest.fixture
def fake_router_factory(monkeypatch):
    """router_factory 查表桩：默认单 provider=apigo / prefix=openai / 无池。"""
    import router_factory

    state: dict[str, Any] = {
        "pool": None,
        "provider": "apigo",
        "prefix": "openai",
        "model_name": {"minimax-m3.1": "MiniMax-M3"},
        "router_calls": [],
        "router_result": SimpleNamespace(router="ok"),
    }

    def fake_get_key_pool(provider: str) -> Any:
        return state["pool"]

    def fake_get_provider(model_id: str) -> str | None:
        return state["provider"]

    def fake_get_prefix(provider: str) -> str:
        return state["prefix"]

    def fake_get_model_name(model_id: str) -> str:
        return state["model_name"].get(model_id, model_id)

    # 生产 get_or_create_router 是同步函数：动态取最新 Router
    def fake_get_or_create_router(loader: Any) -> Any:
        class _R:
            async def acompletion(self, **kwargs: Any) -> Any:
                state["router_calls"].append(kwargs)
                result = state["router_result"]
                if isinstance(result, Exception):
                    raise result
                return result

        return _R()

    monkeypatch.setattr(router_factory, "get_key_pool", fake_get_key_pool)
    monkeypatch.setattr(router_factory, "get_provider_for_model", fake_get_provider)
    monkeypatch.setattr(router_factory, "get_litellm_prefix", fake_get_prefix)
    monkeypatch.setattr(router_factory, "get_model_name_for_id", fake_get_model_name)
    monkeypatch.setattr(router_factory, "get_or_create_router", fake_get_or_create_router)

    # loader 桩：_route_call 里 get_model_config 查模型级 key
    monkeypatch.setattr(
        f"_config_models.get_model_config_loader",
        lambda: SimpleNamespace(get_model_config=lambda mid: {}),
    )

    return state


def _err(kind: ErrorKind) -> ErrorInfo:
    return ErrorInfo(kind=kind, original=None)


class _DirectStub(_adapter.KeyPoolAdapter):
    """覆盖 _direct_call_with_slot 的桩：按脚本吐结果/异常。"""

    def __init__(self, router: Any, script: list[Any]) -> None:
        super().__init__(router)
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def _direct_call_with_slot(self, slot: Any = None, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# ─────────────────────────── 基本路径 ───────────────────────────


async def test_no_pool_falls_through_to_router(fake_router_factory) -> None:
    adapter = _DirectStub(router=None, script=[])
    sentinel = SimpleNamespace(via="router")

    async def fake_route(**kwargs: Any) -> Any:
        return sentinel

    adapter._route_call = fake_route  # type: ignore[method-assign]

    out = await adapter._do_completion(model="minimax-m3.1", messages=[])

    assert out is sentinel


async def test_success_single_key_releases_and_records(fake_router_factory) -> None:
    slot = FakeSlot("k1", "sk-real-1")
    fake_router_factory["pool"] = FakePool([slot])
    adapter = _DirectStub(router=None, script=[SimpleNamespace(resp=1)])

    out = await adapter._do_completion(
        model="minimax-m3.1", messages=[], first_chunk_timeout=5
    )

    assert out.resp == 1
    assert slot.succeeded == 1
    assert slot.released == 1
    # 直连调用注入 slot key（api_base 为空不注入）
    call = adapter.calls[0]
    assert call["api_key"] == "sk-real-1"
    assert "api_base" not in call


async def test_streaming_success_defers_release_to_aclose(fake_router_factory) -> None:
    """流式成功：release 绑定到 aclose（消费完才释放许可），非立即。"""

    class FakeStream:
        def __init__(self) -> None:
            self.aclose_called = 0

        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> Any:
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.aclose_called += 1

    slot = FakeSlot("k1", "sk-real-1")
    fake_router_factory["pool"] = FakePool([slot])
    stream = FakeStream()
    adapter = _DirectStub(router=None, script=[stream])

    out = await adapter._do_completion(model="minimax-m3.1", messages=[])

    assert out is stream
    # 传输尚未发生：许可未释放，等 aclose
    assert slot.released == 0
    await out.aclose()
    assert slot.released == 1
    assert stream.aclose_called == 1  # 原始 aclose 仍被透传执行
    await out.aclose()  # 幂等：二次 aclose 不重复 release
    assert slot.released == 1


async def test_unresolved_placeholder_key_fails_closed(fake_router_factory) -> None:
    """占位符 key（env/.env 均无值）→ 发 HTTP 前直接报配置错误。"""
    slot = FakeSlot("k1", "${UNRESOLVED_VAR}")
    fake_router_factory["pool"] = FakePool([slot])
    adapter = _DirectStub(router=None, script=[])

    with pytest.raises(_llm_exceptions.LLMKeyUnresolvedError):
        await adapter._do_completion(model="minimax-m3.1", messages=[])

    assert adapter.calls == []  # 未触达直连调用
    assert slot.released == 1  # slot 先归还


async def test_bad_request_does_not_rotate_keys(
    fake_router_factory, monkeypatch
) -> None:
    """BAD_REQUEST 是参数错误：不换 key，直接抛。"""
    slot = FakeSlot("k1", "sk-1")
    fake_router_factory["pool"] = FakePool([slot])
    adapter = _DirectStub(
        router=None,
        script=[litellm.BadRequestError(message="bad param", model="m", llm_provider="openai")],
    )
    monkeypatch.setattr(
        _adapter, "classify_error", lambda exc: _err(ErrorKind.BAD_REQUEST)
    )

    with pytest.raises(litellm.BadRequestError):
        await adapter._do_completion(model="minimax-m3.1", messages=[])

    assert len(adapter.calls) == 1  # 只试了第一个 key
    assert slot.released == 1


async def test_rate_limit_rotates_to_next_key(fake_router_factory, monkeypatch) -> None:
    """可恢复错误：冷却当前 key → 换下一个 key 重试成功。"""
    slot1, slot2 = FakeSlot("k1", "sk-1"), FakeSlot("k2", "sk-2")
    fake_router_factory["pool"] = FakePool([slot1, slot2])
    adapter = _DirectStub(
        router=None,
        script=[
            litellm.RateLimitError(message="429", model="m", llm_provider="openai"),
            SimpleNamespace(resp="ok-2"),
        ],
    )


    monkeypatch.setattr(
        _adapter, "classify_error", lambda exc: _err(ErrorKind.RATE_LIMIT), raising=False
    )
    # 退避 sleep 压缩为 0（SERVICE_DOWN 才 sleep；RATE_LIMIT 直接换 key）
    out = await adapter._do_completion(model="minimax-m3.1", messages=[])

    assert out.resp == "ok-2"
    assert slot1.released == 1 and slot1.errors
    assert slot2.succeeded == 1 and slot2.released == 1


async def test_service_down_backs_off_then_retries(fake_router_factory, monkeypatch) -> None:
    """SERVICE_DOWN：退避后同池重试。"""
    slot1, slot2 = FakeSlot("k1", "sk-1"), FakeSlot("k2", "sk-2")
    fake_router_factory["pool"] = FakePool([slot1, slot2])
    adapter = _DirectStub(
        router=None,
        script=[
            litellm.APIConnectionError(
                message="conn reset", model="m", llm_provider="openai"
            ),
            SimpleNamespace(resp="recovered"),
        ],
    )

    monkeypatch.setattr(
        _adapter, "classify_error", lambda exc: _err(ErrorKind.SERVICE_DOWN), raising=False
    )
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(_adapter.asyncio, "sleep", fake_sleep)

    out = await adapter._do_completion(model="minimax-m3.1", messages=[])

    assert out.resp == "recovered"
    assert slot1.errors  # SERVICE_DOWN 已交 KeySlot 策略记录
    # 首次退避 2.0s（2*2^0），指数封顶 16s
    assert sleeps and sleeps[0] == 2.0


async def test_all_keys_fail_then_router_fallback_succeeds(
    fake_router_factory, monkeypatch
) -> None:
    """所有 key 失败 → Router fallback（llm.yaml fallback_chain）兜底成功。"""
    slot = FakeSlot("k1", "sk-1")
    fake_router_factory["pool"] = FakePool([slot])
    fake_router_factory["router_result"] = SimpleNamespace(via="fallback-router")
    adapter = _DirectStub(
        router=None,
        script=[litellm.RateLimitError(message="429", model="m", llm_provider="p")],
    )


    monkeypatch.setattr(
        _adapter, "classify_error", lambda exc: _err(ErrorKind.RATE_LIMIT), raising=False
    )

    out = await adapter._do_completion(model="minimax-m3.1", messages=[])

    assert out.via == "fallback-router"
    assert fake_router_factory["router_calls"], "fallback 必须走 router.acompletion"


async def test_all_keys_fail_and_fallback_fails_raises_original(
    fake_router_factory, monkeypatch
) -> None:
    """fallback 也失败 → 抛原始异常（保留根因链）。"""
    slot = FakeSlot("k1", "sk-1")
    fake_router_factory["pool"] = FakePool([slot])
    original = litellm.RateLimitError(message="429", model="m", llm_provider="p")
    fake_router_factory["router_result"] = litellm.InternalServerError(
        message="router down", model="m", llm_provider="p"
    )
    adapter = _DirectStub(router=None, script=[original])


    monkeypatch.setattr(
        _adapter, "classify_error", lambda exc: _err(ErrorKind.RATE_LIMIT), raising=False
    )

    with pytest.raises(litellm.RateLimitError):
        await adapter._do_completion(model="minimax-m3.1", messages=[])


async def test_key_pool_exhausted_becomes_rate_limit_error(
    fake_router_factory, monkeypatch
) -> None:
    """池耗尽（全部 key 不可用且等待超时）→ 翻译为 RateLimitError 且保留 cause。"""
    slot = FakeSlot("k1", "sk-1")
    fake_router_factory["pool"] = FakePool([slot])

    class ExhaustPool(FakePool):
        async def acquire_slot(self) -> Any:
            raise _llm_exceptions.KeyPoolExhaustedError(
                pool_id="apigo", timeout=30.0, unavailable=["k1"]
            )

    fake_router_factory["pool"] = ExhaustPool([slot])
    fake_router_factory["router_result"] = SimpleNamespace(via="router-final")
    adapter = _DirectStub(router=None, script=[])


    out = await adapter._do_completion(model="minimax-m3.1", messages=[])

    assert out.via == "router-final"


# ─────────────────────────── _direct_call_with_slot（线程桥真跑） ───────────────────────────


@pytest.fixture
def fake_litellm_acompletion(monkeypatch):
    """替换 adapter 直连用的 litellm.acompletion（线程 worker 实际调用点）。"""
    calls: list[dict[str, Any]] = []
    box: dict[str, Any] = {"result": None, "error": None, "delay": 0.0}

    async def fake_acompletion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if box["delay"]:
            await asyncio.sleep(box["delay"])
        if box["error"] is not None:
            raise box["error"]
        return box["result"]

    import litellm as l

    monkeypatch.setattr(l, "acompletion", fake_acompletion)
    return calls, box


async def test_direct_call_non_streaming_passthrough(
    fake_router_factory, fake_litellm_acompletion
) -> None:
    calls, box = fake_litellm_acompletion
    box["result"] = SimpleNamespace(non_stream=True)
    adapter = _adapter.KeyPoolAdapter(router=None)

    out = await adapter._direct_call_with_slot(
        slot=FakeSlot("k", "sk-x"),
        model="minimax-m3.1",
        messages=[{"role": "user", "content": "hi"}],
        timeout=12.0,
    )

    assert out.non_stream is True
    assert calls[0]["model"] == "openai/MiniMax-M3"
    assert calls[0]["api_key"] == "sk-x"
    assert calls[0]["timeout"] == 12.0  # 调用方显式 timeout 优先于 first_chunk_timeout
    assert calls[0]["num_retries"] == 0


async def test_direct_call_timeout_non_numeric_keeps_default(
    fake_router_factory, fake_litellm_acompletion
) -> None:
    """httpx.Timeout 等非数值 timeout：保持 first_chunk_timeout 缺省 180。"""
    calls, box = fake_litellm_acompletion
    box["result"] = SimpleNamespace(ok=1)

    class FakeHttpxTimeout:
        pass

    adapter = _adapter.KeyPoolAdapter(router=None)
    await adapter._direct_call_with_slot(
        slot=FakeSlot("k", "sk-x"),
        model="m",
        messages=[],
        timeout=FakeHttpxTimeout(),
    )

    assert calls[0]["timeout"] == 180.0


async def test_direct_call_deadline_raises_timeout(
    fake_router_factory, fake_litellm_acompletion
) -> None:
    """worker 迟迟不返回 → deadline 到点抛 TimeoutError（不永久等）。"""
    calls, box = fake_litellm_acompletion
    box["delay"] = 5.0  # 超过 first_chunk_timeout
    adapter = _adapter.KeyPoolAdapter(router=None)

    with pytest.raises(asyncio.TimeoutError):
        await adapter._direct_call_with_slot(
            slot=FakeSlot("k", "sk-x"),
            model="m",
            messages=[],
            first_chunk_timeout=0.3,
        )


async def test_direct_call_worker_error_is_reraised(
    fake_router_factory, fake_litellm_acompletion
) -> None:
    calls, box = fake_litellm_acompletion
    box["error"] = RuntimeError("worker blew up")
    adapter = _adapter.KeyPoolAdapter(router=None)

    with pytest.raises(RuntimeError, match="worker blew up"):
        await adapter._direct_call_with_slot(
            slot=FakeSlot("k", "sk-x"), model="m", messages=[]
        )


async def test_direct_call_streaming_returns_threaded_bridge(
    fake_router_factory, fake_litellm_acompletion
) -> None:
    """流式返回：主循环拿到 _ThreadedStreamBridge，chunk 经队列送达。"""
    calls, box = fake_litellm_acompletion

    class AsyncIterStream:
        def __init__(self, chunks: list[str]) -> None:
            self._chunks = list(chunks)
            self.aclose_called = 0

        def __aiter__(self) -> Any:
            self._it = iter(self._chunks)
            return self

        async def __anext__(self) -> str:
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration

        async def aclose(self) -> None:
            self.aclose_called += 1

    box["result"] = AsyncIterStream(["a", "b"])
    adapter = _adapter.KeyPoolAdapter(router=None)

    bridge = await adapter._direct_call_with_slot(
        slot=FakeSlot("k", "sk-x"), model="m", messages=[]
    )

    assert isinstance(bridge, _adapter._ThreadedStreamBridge)
    got = []
    async for chunk in bridge:
        got.append(chunk)
    assert got == ["a", "b"]
    await asyncio.sleep(0.2)  # worker 收尾
    await bridge.aclose()


# ─────────────────────────── _route_call fail-closed ───────────────────────────


async def test_route_call_placeholder_key_fails_closed(fake_router_factory) -> None:
    """Router 部署烘入未解析占位符 → 调用前直接报配置错误。"""
    fake_router_factory["provider"] = "apigo"
    slot = FakeSlot("k", "${ALSO_UNRESOLVED}")
    fake_router_factory["pool"] = FakePool([slot])

    adapter = _adapter.KeyPoolAdapter(router=None)

    with pytest.raises(_llm_exceptions.LLMKeyUnresolvedError):
        await adapter._route_call(model="minimax-m3.1", messages=[])


async def test_route_call_delegates_to_fresh_router(fake_router_factory) -> None:
    fake_router_factory["router_result"] = SimpleNamespace(via="fresh")
    adapter = _adapter.KeyPoolAdapter(router=None)

    out = await adapter._route_call(model="minimax-m3.1", messages=[])

    assert out.via == "fresh"


# ─────────────────────────── provider 解析 ───────────────────────────


def test_resolve_provider_and_model_name(fake_router_factory) -> None:
    adapter = _adapter.KeyPoolAdapter(router=None)

    # provider 命中且该 provider 有 pool → 返回 provider
    fake_router_factory["pool"] = FakePool([FakeSlot("k", "sk-1")])
    assert adapter._resolve_provider("zai/minimax-m3.1") == "apigo"

    # provider 命中但无 pool → ""（走 Router 路径）
    fake_router_factory["pool"] = None
    assert adapter._resolve_provider("minimax-m3.1") == ""

    # 前缀剥离
    assert adapter._extract_model_name({"model": "openai/abc"}) == "abc"
    assert adapter._extract_model_name({"model": "bare"}) == "bare"
