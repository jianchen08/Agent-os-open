"""TrackPlugin cost_update 事件契约测试。

钉死 cost_update 事件推送给前端输入框进度条的数据契约：
- payload 必须含 pipeline_id（前端按 pipeline 分桶写入 contextUsageStore）
- token 必须是单轮值（取自 state["llm_usage"]，即本轮 API 返回，非跨轮累计）
- tool_execute 轮不推送（llm_usage 是上一轮残留，避免错误覆盖）

根因：原实现推送的是 _collect_token_usage 的累计 total_tokens（跨轮相加），
多轮时越加越大；且不带 pipeline_id，前端无法分桶 → 进度条恒为 0。
"""

import asyncio
import os
import sys
from typing import Any
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pipeline.plugin import PluginContext  # noqa: E402
from plugins.output.track.plugin import TrackPlugin  # noqa: E402

PIPELINE_ID = "pipeline_cost_001"


def _run(coro: Any) -> Any:
    """安全执行 async（兼容已在事件循环内的场景）。"""
    try:
        asyncio.get_running_loop()
        import concurrent.futures  # noqa: PLC0415
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result(timeout=10)
    except RuntimeError:
        return asyncio.run(coro)


def _make_ctx(state: dict[str, Any]) -> PluginContext:
    """构造最小 PluginContext（track 插件只用 ctx.state）。"""
    return PluginContext(state=state, config={})


def _patch_notifier_and_sink(events_out: list[dict[str, Any]]):
    """patch ws_interaction_notifier + create_targeted_sink，把推送的事件记入 events_out。

    create_targeted_sink 返回一个 send_event 是 AsyncMock 的假 sink。
    """
    fake_sink = _FakeSink(events_out)
    return (
        patch("channels.websocket.ws_handler.ws_interaction_notifier", object()),
        patch("pipeline.stream_bridge.create_targeted_sink", return_value=fake_sink),
    )


class _FakeSink:
    """假 sink：记录所有 send_event 收到的事件。"""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events
        self.send_event = AsyncMock(side_effect=self._record)

    async def _record(self, event: dict[str, Any]) -> bool:
        self._events.append(event)
        return True


def test_cost_update_carries_single_round_tokens_and_pipeline_id():
    """llm_call 轮：cost_update 必须含 pipeline_id + 单轮 total/input/output tokens。"""
    plugin = TrackPlugin()
    state = {
        "core_type": "llm_call",
        "thread_id": "thread_001",
        "pipeline_id": PIPELINE_ID,
        "llm_usage": {
            "input_tokens": 1200,
            "output_tokens": 300,
            "total_tokens": 1500,
            "cached_tokens": 0,
        },
    }
    ctx = _make_ctx(state)
    events: list[dict[str, Any]] = []

    notifier_patch, sink_patch = _patch_notifier_and_sink(events)
    with notifier_patch, sink_patch:
        _run(plugin._try_notify_cost_update(ctx))

    cost_events = [e for e in events if e.get("type") == "cost_update"]
    assert len(cost_events) == 1, f"应推送一个 cost_update，得到 {len(cost_events)}"
    data = cost_events[0]["data"]
    assert data["pipeline_id"] == PIPELINE_ID, "必须带 pipeline_id 供前端分桶"
    assert data["total_tokens"] == 1500, "total_tokens 必须是本轮单轮值"
    assert data["input_tokens"] == 1200
    assert data["output_tokens"] == 300


def test_cost_update_skipped_on_tool_execute_round():
    """tool_execute 轮不推送 cost_update（llm_usage 是上一轮残留，避免错误覆盖）。"""
    plugin = TrackPlugin()
    state = {
        "core_type": "tool_execute",
        "thread_id": "thread_001",
        "pipeline_id": PIPELINE_ID,
        "llm_usage": {"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500},
    }
    ctx = _make_ctx(state)
    events: list[dict[str, Any]] = []

    notifier_patch, sink_patch = _patch_notifier_and_sink(events)
    with notifier_patch, sink_patch:
        _run(plugin._try_notify_cost_update(ctx))

    assert len(events) == 0, f"tool_execute 轮不应推送，得到 {len(events)} 个事件"


def test_cost_update_not_accumulated_across_rounds():
    """多轮场景：每轮推送的是本轮 llm_usage，不是跨轮累计。

    回归守护：原 bug 推送累计值（total 越加越大）。这里模拟第二轮 llm_usage
    较小，验证推送的就是第二轮的单轮值，而非叠加第一轮。
    """
    plugin = TrackPlugin()

    # 第一轮
    state_r1 = {
        "core_type": "llm_call",
        "thread_id": "thread_001",
        "pipeline_id": PIPELINE_ID,
        "llm_usage": {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
    }
    events_r1: list[dict[str, Any]] = []
    n1, s1 = _patch_notifier_and_sink(events_r1)
    with n1, s1:
        _run(plugin._try_notify_cost_update(_make_ctx(state_r1)))

    # 第二轮（llm_usage 更新为新的单轮值，不是叠加）
    state_r2 = {
        "core_type": "llm_call",
        "thread_id": "thread_001",
        "pipeline_id": PIPELINE_ID,
        "llm_usage": {"input_tokens": 1800, "output_tokens": 100, "total_tokens": 1900},
    }
    events_r2: list[dict[str, Any]] = []
    n2, s2 = _patch_notifier_and_sink(events_r2)
    with n2, s2:
        _run(plugin._try_notify_cost_update(_make_ctx(state_r2)))

    assert len(events_r2) == 1
    assert events_r2[0]["data"]["total_tokens"] == 1900, (
        "第二轮推送的必须是第二轮单轮值 1900，而非累计 1200+1900=3100"
    )
