# @feature: FP-T07 llm api | @ci: python-coverage
"""llm adapter 直连轮询循环的 worker 异常出口补测（adapter.py 2008）。

契约：``_direct_call_with_slot`` 的主协程在「worker 已装箱异常、但首个
chunk 尚未入队且 done 未置位」的窗口内轮询，感知到 ``_exc_box`` 即原样抛出
原始异常（不等到 done 事件）——建连后立刻失败时，调用方应拿到上游原始异常
而非超时。

该出口是真实线程竞态窗口（worker 侧 ``_exc_box.append`` 与 ``_done_evt.set``
之间无 await）。为确定性命中，注入可控延迟到 ``threading.Event.set``
（OS 层同步原语属外部依赖边界，等同于 fake clock 手法）：worker 装箱后
置位被推迟，主协程 0.1s 轮询必然先观察到异常。litellm.acompletion 为外部
依赖，全桩；model/api_base 查表用真实 router_factory 函数的替身返回值。

不可达说明（逐条）：无——本文件目标行 2008 可达且已确定性覆盖。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_s = str(_PLUGIN_DIR)
while _s in sys.path:
    sys.path.remove(_s)
sys.path.insert(0, _s)

import adapter as adapter_mod  # noqa: E402  平铺 import，与生产代码一致
import key_pool as _kp_mod  # noqa: E402
import litellm as _litellm_mod  # noqa: E402
import router_factory as _rf_mod  # noqa: E402


class _FakeSlot:
    """KeySlot 桩：仅暴露直连所需凭证字段（不驱动限流状态机）。"""

    def __init__(self, key_id: str, api_key: str, api_base: str = "") -> None:
        self.key_id = key_id
        self.api_key = api_key
        self.api_base = api_base


@pytest.fixture(autouse=True)
def _pin_flat_modules() -> Any:
    """重绑平铺裸名槽位到本文件实例（同 test_adapter_gaps.py 惯例）。"""
    saved = {name: sys.modules.get(name) for name in ("key_pool", "router_factory")}
    sys.modules["key_pool"] = _kp_mod
    sys.modules["router_factory"] = _rf_mod
    try:
        yield
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


@pytest.fixture
def fake_route_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """router_factory 查表桩：model_id → provider/前缀/model_name 三函数。"""
    monkeypatch.setattr(_rf_mod, "get_provider_for_model", lambda model_id: "apigo")
    monkeypatch.setattr(_rf_mod, "get_litellm_prefix", lambda provider: "openai")
    monkeypatch.setattr(_rf_mod, "get_model_name_for_id", lambda model_id: "MiniMax-M3")


@pytest.fixture
def slow_event_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 ``threading.Event.set`` 推迟 0.3s 再置位（拉开真实竞态窗口）。

    仅用于放大「worker 装箱异常 ↔ done 置位」之间的既有时序间隙，
    不改写任何被测语义；测试结束自动还原。
    """
    real_set = threading.Event.set

    def _delayed_set(self: threading.Event) -> Any:
        time.sleep(0.3)
        return real_set(self)

    monkeypatch.setattr(threading.Event, "set", _delayed_set)


async def test_worker_immediate_error_raised_from_poll_loop(
    fake_route_tables: None,
    slow_event_set: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """建连即失败（无 chunk 入队）→ 主协程轮询窗口内感知并抛原始异常。

    锁定 adapter.py 2008 的 ``raise _exc_box[0]``：抛出的必须是原始异常类型
    与消息（不是 asyncio.TimeoutError，也不是 done 事件后的等价出口）。
    """
    async def _boom(**kwargs: Any) -> Any:
        raise RuntimeError("connect refused before first chunk")

    monkeypatch.setattr(_litellm_mod, "acompletion", _boom)
    adapter = adapter_mod.KeyPoolAdapter(router=None)

    with pytest.raises(RuntimeError, match="connect refused before first chunk"):
        await adapter._direct_call_with_slot(
            slot=_FakeSlot("k", "sk-x"),
            model="minimax-m3.1",
            messages=[],
            first_chunk_timeout=5,
        )


async def test_worker_immediate_error_propagates_original_type_not_timeout(
    fake_route_tables: None,
    slow_event_set: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """区分度输入：非 RuntimeError 的自定义异常同样原样透传（类型保真）。

    与上一条仅「异常类型」一面不同，共同锁定该出口不做异常翻译。
    """

    class _UpstreamGatewayError(Exception):
        pass

    async def _boom(**kwargs: Any) -> Any:
        raise _UpstreamGatewayError("gateway 502")

    monkeypatch.setattr(_litellm_mod, "acompletion", _boom)
    adapter = adapter_mod.KeyPoolAdapter(router=None)

    with pytest.raises(_UpstreamGatewayError, match="gateway 502"):
        await adapter._direct_call_with_slot(
            slot=_FakeSlot("k", "sk-y", api_base="https://slot.example.com"),
            model="minimax-m3.1",
            messages=[],
            first_chunk_timeout=5,
        )
