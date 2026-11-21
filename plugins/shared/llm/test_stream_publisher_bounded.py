# @feature: FP-T07 llm api | @ci: python-coverage
"""_StreamPublisher 队列有界化（内存定案 §8 条款 3；批 3 最小补丁）。

契约（主方案 §8.1 条款 3 / ADR 2026-11-18-llm-two-tier-plugin-convergence）：
- 队列 maxsize 有界（模块常量 ``_STREAM_QUEUE_MAXSIZE``，缺省 1024 事件）；
- 满载策略：先丢 keepalive（不占容量、不驱逐业务事件）；相邻同 index
  的 text_delta/reasoning_delta 合并；仍满则丢最旧事件并置降级标记；
- 计数可观测（``stats()``：queued/maxsize/dropped_keepalive/dropped_events/
  degraded）；消费端停摆不再无界增长。

断行为不断实现：通过消费端实际收到的事件序列与 stats() 观察面断言。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_server() -> Any:
    """按显式路径加载 llm 插件 server 模块（唯一模块名隔离同名 server.py）。"""
    name = "llm_server_publisher_bounded_test"
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / "server.py")
    assert spec is not None, "cannot load llm plugin server.py"
    assert spec.loader is not None, "cannot load llm plugin server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


server = _load_server()


class FakeBus:
    def __init__(self) -> None:
        self.emits: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.emits.append((method, params))


def _publisher(bus: FakeBus, maxsize: int | None = None) -> Any:
    envelope = {"thread_id": "t1", "pipeline_id": "p1", "message_id": "a_1"}
    if maxsize is None:
        return server._StreamPublisher(bus, envelope)
    return server._StreamPublisher(bus, envelope, maxsize=maxsize)


def _drain(publisher: Any) -> None:
    """启动消费端并立即 stop（排空）——总线收到的事件即队列实际留存面。"""

    async def _run() -> None:
        publisher.start()
        await asyncio.sleep(0)
        await publisher.stop()

    asyncio.run(_run())


def _events(bus: FakeBus) -> list[str]:
    return [params["event"] for _, params in bus.emits]


def _payloads(bus: FakeBus, event: str) -> list[dict[str, Any]]:
    return [params["payload"] for _, params in bus.emits if params["event"] == event]


# ─────────────────────────── 常量与观测面 ───────────────────────────


def test_default_maxsize_follows_module_constant() -> None:
    """缺省队列上界 = 模块常量（有界且量级合理）。"""
    publisher = _publisher(FakeBus())
    assert publisher.stats()["maxsize"] == server._STREAM_QUEUE_MAXSIZE
    assert server._STREAM_QUEUE_MAXSIZE >= 64


def test_stats_shape_and_initial_values() -> None:
    """stats() 观测面字段齐全且初值为零。"""
    stats = _publisher(FakeBus(), maxsize=4).stats()
    assert set(stats) == {"queued", "maxsize", "dropped_keepalive", "dropped_events", "degraded"}
    assert stats == {
        "queued": 0,
        "maxsize": 4,
        "dropped_keepalive": 0,
        "dropped_events": 0,
        "degraded": False,
    }


# ─────────────────────────── 满载策略 ───────────────────────────


def test_full_queue_drops_oldest_business_event_and_marks_degraded() -> None:
    """仍满 → 丢最旧业务事件 + 降级标记（消费端实际收到的事件可证）。"""
    bus = FakeBus()
    publisher = _publisher(bus, maxsize=4)
    for idx in range(4):
        publisher.put("block_start", {"index": idx, "block_type": "text"})
    publisher.put("block_end", {"index": 4})

    stats = publisher.stats()
    assert stats["queued"] == 4
    assert stats["dropped_events"] == 1
    assert stats["degraded"] is True

    _drain(publisher)
    assert [p["index"] for p in _payloads(bus, "block_start")] == [1, 2, 3]
    assert [p["index"] for p in _payloads(bus, "block_end")] == [4]


def test_keepalive_dropped_when_full_without_evicting_business_events() -> None:
    """满载优先丢 keepalive：不占容量、不驱逐业务事件、不置降级标记。"""
    bus = FakeBus()
    publisher = _publisher(bus, maxsize=4)
    for idx in range(4):
        publisher.put("block_start", {"index": idx})

    publisher.put("keepalive", {})

    stats = publisher.stats()
    assert stats["queued"] == 4
    assert stats["dropped_keepalive"] == 1
    assert stats["dropped_events"] == 0
    assert stats["degraded"] is False

    _drain(publisher)
    assert [p["index"] for p in _payloads(bus, "block_start")] == [0, 1, 2, 3]
    assert "keepalive" not in _events(bus)


def test_queued_keepalive_evicted_first_when_full() -> None:
    """已积压在队首的 keepalive 遇满载驱逐优先出队：计 keepalive 丢弃、非降级。"""
    bus = FakeBus()
    publisher = _publisher(bus, maxsize=2)
    publisher.put("keepalive", {})
    publisher.put("block_start", {"index": 0})

    publisher.put("tool_call", {"index": 1, "tool_calls": []})

    stats = publisher.stats()
    assert stats["queued"] == 2
    assert stats["dropped_keepalive"] == 1
    assert stats["dropped_events"] == 0
    assert stats["degraded"] is False

    _drain(publisher)
    assert "keepalive" not in _events(bus)
    assert [p["index"] for p in _payloads(bus, "block_start")] == [0]
    assert [p["index"] for p in _payloads(bus, "tool_call")] == [1]


def test_adjacent_text_delta_merged_when_full() -> None:
    """满载 + 相邻同 index text_delta → 合并文本（不丢内容、不降级）。"""
    bus = FakeBus()
    publisher = _publisher(bus, maxsize=4)
    for idx in range(3):
        publisher.put("block_start", {"index": idx})
    publisher.put("text_delta", {"index": 3, "text": "a"})

    publisher.put("text_delta", {"index": 3, "text": "b"})

    stats = publisher.stats()
    assert stats["queued"] == 4
    assert stats["dropped_events"] == 0
    assert stats["degraded"] is False

    _drain(publisher)
    deltas = _payloads(bus, "text_delta")
    assert len(deltas) == 1
    assert deltas[0]["text"] == "ab"
    assert deltas[0]["index"] == 3


def test_text_delta_not_merged_when_different_index() -> None:
    """满载但相邻 delta index 不同 → 不合并，按最旧驱逐策略处理。"""
    bus = FakeBus()
    publisher = _publisher(bus, maxsize=2)
    publisher.put("text_delta", {"index": 0, "text": "a"})
    publisher.put("text_delta", {"index": 1, "text": "b"})

    publisher.put("text_delta", {"index": 2, "text": "c"})

    _drain(publisher)
    deltas = _payloads(bus, "text_delta")
    assert [d["index"] for d in deltas] == [1, 2]
    assert publisher.stats()["degraded"] is True


# ─────────────────────────── 未满载路径零扰动 ───────────────────────────


def test_below_capacity_keeps_all_events_in_order() -> None:
    """未满载：事件全量按序交付，sequence 单调（常态路径行为不变）。"""
    bus = FakeBus()
    publisher = _publisher(bus, maxsize=8)
    publisher.put("block_start", {"index": 0, "block_type": "text"})
    publisher.put("text_delta", {"index": 0, "text": "x"})
    publisher.put("block_end", {"index": 0})

    _drain(publisher)

    assert _events(bus) == ["block_start", "text_delta", "block_end"]
    assert [params["payload"]["sequence"] for _, params in bus.emits] == [0, 1, 2]
    assert publisher.stats()["dropped_events"] == 0


def test_stop_drains_pending_events_then_ignores_put() -> None:
    """stop 排空在队事件；关闭后 put 一律忽略（不无界增长、不抛错）。"""
    bus = FakeBus()
    publisher = _publisher(bus, maxsize=4)
    publisher.put("block_start", {"index": 0})
    publisher.put("block_end", {"index": 0})

    _drain(publisher)
    before = len(bus.emits)
    publisher.put("text_delta", {"index": 0, "text": "late"})

    assert len(bus.emits) == before
    assert publisher.stats()["queued"] == 0
