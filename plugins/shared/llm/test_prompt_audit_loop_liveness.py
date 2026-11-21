# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""LLM 出站同步段事件循环活性回归（2026-11-12 第 39 轮装机取证）。

事故：装机宿主 watchdog 于 09:58Z/10:05Z 两次心跳停滞 30.4s 自杀，取证栈为
``_log_prompt_body``（整份 prompt json.dumps + 正则脱敏 + 审计日志写盘）与
``minimax.adapt_messages_before_send``（大历史逐条浅拷贝）直接跑在合宿宿主
事件循环。两条路径都在 ``async def`` 调用链内同步执行，单次 >30s 即整组连坐
``os._exit(1)``。

回归口径：假身阻塞 0.3s，心跳最大间隔必须 <0.1s（``LoopTicker`` 口径）；
同时锁住返回/异常语义（下放线程不得改变调用契约）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parent
for _d in (str(_DIR), str(_SHARED)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import adapter as adapter_mod  # noqa: E402
import router_factory  # noqa: E402
from _libs.testing.loop_liveness import MAX_TICK_GAP, LoopTicker  # noqa: E402

_BLOCK_SECS = 0.3


def _slow(*_args: Any, **_kwargs: Any) -> None:
    time.sleep(_BLOCK_SECS)


async def _fake_stream(*_args: Any, **_kwargs: Any) -> str:
    return "ok"


def _patch_router(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router_factory, "get_provider_for_model", lambda _m: "prov")
    monkeypatch.setattr(router_factory, "get_provider_conf", lambda _p: {"api_base": "http://x"})
    monkeypatch.setattr(router_factory, "get_model_name_for_id", lambda m: m)


async def _direct_call(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> Any:
    _patch_router(monkeypatch)
    monkeypatch.setattr(adapter_mod.wire_client, "stream_chat", _fake_stream)
    slot = SimpleNamespace(api_base="http://x", api_key="k")
    return await adapter_mod.KeyPoolAdapter._direct_call_with_slot(
        None,
        slot,
        model="prov/m",
        messages=[{"role": "user", "content": "hi"}],
        **kwargs,
    )


async def test_direct_call_prompt_audit_keeps_event_loop_ticking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prompt 审计序列化（假身 0.3s）不得阻塞事件循环。"""
    monkeypatch.setattr(adapter_mod, "_log_prompt_body", _slow)
    monkeypatch.setattr(adapter_mod.wire_client, "build_body", lambda *_a, **_k: {"model": "m"})

    async with LoopTicker() as ticker:
        result = await _direct_call(monkeypatch, stream=True)

    assert result == "ok"
    assert ticker.max_gap < MAX_TICK_GAP, f"prompt 审计阻塞事件循环: {ticker.max_gap:.3f}s"


async def test_direct_call_build_body_keeps_event_loop_ticking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wire body 构建（大历史 json 序列化）不得阻塞事件循环。"""
    monkeypatch.setattr(adapter_mod, "_log_prompt_body", lambda *_a, **_k: None)
    monkeypatch.setattr(adapter_mod.wire_client, "build_body", _slow)

    async with LoopTicker() as ticker:
        result = await _direct_call(monkeypatch, stream=True)

    assert result == "ok"
    assert ticker.max_gap < MAX_TICK_GAP, f"wire body 构建阻塞事件循环: {ticker.max_gap:.3f}s"


async def test_completion_provider_adapter_keeps_event_loop_ticking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider 消息适配（大历史逐条拷贝）不得阻塞事件循环。"""

    def _slow_adapt(
        self: Any,
        adapter: Any,
        model: str,
        messages: list[dict[str, Any]],
        replay_reasoning: Any,
        kwargs: dict[str, Any],
    ) -> list[dict[str, Any]]:
        time.sleep(_BLOCK_SECS)
        return messages

    monkeypatch.setattr(
        adapter_mod._BaseLLMAdapter, "_adapt_messages_before_send_sync", _slow_adapt
    )

    ad = object.__new__(adapter_mod._BaseLLMAdapter)

    async def _fake_non_streaming(*_args: Any, **_kwargs: Any) -> str:
        return "ok"

    monkeypatch.setattr(ad, "_call_non_streaming", _fake_non_streaming, raising=False)

    async with LoopTicker() as ticker:
        result = await ad.completion(
            "minimax/MiniMax-M3.1-Flash",
            [{"role": "user", "content": "hi"}],
            stream=False,
        )

    assert result == "ok"
    assert ticker.max_gap < MAX_TICK_GAP, f"provider 适配阻塞事件循环: {ticker.max_gap:.3f}s"


async def test_completion_preserves_adapter_error_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """适配器异常仍原样上抛（下放线程不改错误契约）。"""

    def _boom_adapt(
        self: Any,
        adapter: Any,
        model: str,
        messages: list[dict[str, Any]],
        replay_reasoning: Any,
        kwargs: dict[str, Any],
    ) -> list[dict[str, Any]]:
        raise RuntimeError("adapter boom")

    monkeypatch.setattr(
        adapter_mod._BaseLLMAdapter, "_adapt_messages_before_send_sync", _boom_adapt
    )
    ad = object.__new__(adapter_mod._BaseLLMAdapter)

    with pytest.raises(RuntimeError, match="adapter boom"):
        await ad.completion(
            "minimax/MiniMax-M3.1-Flash",
            [{"role": "user", "content": "hi"}],
            stream=False,
        )


async def test_direct_call_preserves_prompt_audit_error_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审计写盘异常不得被线程下放吞掉。"""

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("audit write failed")

    monkeypatch.setattr(adapter_mod, "_log_prompt_body", _boom)
    monkeypatch.setattr(adapter_mod.wire_client, "build_body", lambda *_a, **_k: {"model": "m"})

    with pytest.raises(OSError, match="audit write failed"):
        await _direct_call(monkeypatch, stream=True)


async def test_ticker_detects_blocking_call() -> None:
    """仪器自检：同步阻塞 0.3s 必破阈值（防空转假绿）。"""
    async with LoopTicker() as ticker:
        time.sleep(_BLOCK_SECS)
    assert ticker.max_gap >= MAX_TICK_GAP
