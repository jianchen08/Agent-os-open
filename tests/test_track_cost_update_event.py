"""TrackPlugin cost_update 事件契约测试。

钉死 cost_update 事件推送给前端输入框进度条的数据契约：
- payload 必须含 pipeline_id（前端按 pipeline 分桶写入 contextUsageStore）
- token 必须是单轮值（取自 state["llm_usage"]，即本轮 API 返回，非跨轮累计）
- tool_execute 轮不推送（llm_usage 是上一轮残留，避免错误覆盖）

根因：原实现推送的是 _collect_token_usage 的累计 total_tokens（跨轮相加），
多轮时越加越大；且不带 pipeline_id，前端无法分桶 → 进度条恒为 0。

二次根因（运行时确诊）：原代码用 state.get("thread_id") 做发送守卫，
但 thread_id 不是 state 标准字段（恒为空），导致 if thread_id 直接跳过发送。
真实会话标识是 session_id；TargetedSink 可按 pipeline_id 从 registry 自解析，
不需要 thread_id 守卫。本测试覆盖 session_id 和无 session_id 两种路径。
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


class _FakeSink:
    """假 sink：记录所有 send_event 收到的事件 + sink 构造参数。"""

    def __init__(self, events: list[dict[str, Any]], calls: list[tuple]) -> None:
        self._events = events
        self._calls = calls
        self.send_event = AsyncMock(side_effect=self._record)

    async def _record(self, event: dict[str, Any]) -> bool:
        self._events.append(event)
        return True


def _patch_notifier_and_sink(events_out: list[dict[str, Any]], calls_out: list[tuple]):
    """patch ws_interaction_notifier + create_targeted_sink。

    create_targeted_sink 返回假 sink，并把构造参数记入 calls_out，
    便于断言传入的 session_id / pipeline_id。
    """
    def _factory(notifier, thread_id="", pipeline_id="", user_id=""):
        calls_out.append((thread_id, pipeline_id))
        return _FakeSink(events_out, calls_out)

    return (
        patch("channels.websocket.ws_handler.ws_interaction_notifier", object()),
        patch("pipeline.stream_bridge.create_targeted_sink", side_effect=_factory),
    )


# 真实 state 标准字段是 session_id，不是 thread_id
def _base_state(**overrides: Any) -> dict[str, Any]:
    state = {
        "core_type": "llm_call",
        "session_id": "session_001",
        "pipeline_id": PIPELINE_ID,
        "llm_usage": {
            "input_tokens": 1200,
            "output_tokens": 300,
            "total_tokens": 1500,
            "cached_tokens": 0,
        },
    }
    state.update(overrides)
    return state


def test_cost_update_carries_single_round_tokens_and_pipeline_id():
    """llm_call 轮：cost_update 必须含 pipeline_id + 单轮 total/input/output tokens。"""
    plugin = TrackPlugin()
    ctx = _make_ctx(_base_state())
    events: list[dict[str, Any]] = []
    calls: list[tuple] = []

    notifier_patch, sink_patch = _patch_notifier_and_sink(events, calls)
    with notifier_patch, sink_patch:
        _run(plugin._try_notify_cost_update(ctx))

    cost_events = [e for e in events if e.get("type") == "cost_update"]
    assert len(cost_events) == 1, f"应推送一个 cost_update，得到 {len(cost_events)}"
    data = cost_events[0]["data"]
    assert data["pipeline_id"] == PIPELINE_ID, "必须带 pipeline_id 供前端分桶"
    assert data["total_tokens"] == 1500, "total_tokens 必须是本轮单轮值"
    assert data["input_tokens"] == 1200
    assert data["output_tokens"] == 300


def test_cost_update_passes_session_id_to_sink():
    """会话标识 session_id 必须传给 create_targeted_sink 的 thread_id 参数。"""
    plugin = TrackPlugin()
    ctx = _make_ctx(_base_state(session_id="sess_xyz"))
    events: list[dict[str, Any]] = []
    calls: list[tuple] = []

    notifier_patch, sink_patch = _patch_notifier_and_sink(events, calls)
    with notifier_patch, sink_patch:
        _run(plugin._try_notify_cost_update(ctx))

    assert len(calls) == 1
    thread_id_arg, pipeline_id_arg = calls[0]
    assert thread_id_arg == "sess_xyz", "应把 session_id 作为会话标识传入 sink"
    assert pipeline_id_arg == PIPELINE_ID


def test_cost_update_skipped_on_tool_execute_round():
    """tool_execute 轮不推送 cost_update（llm_usage 是上一轮残留，避免错误覆盖）。"""
    plugin = TrackPlugin()
    ctx = _make_ctx(_base_state(core_type="tool_execute"))
    events: list[dict[str, Any]] = []
    calls: list[tuple] = []

    notifier_patch, sink_patch = _patch_notifier_and_sink(events, calls)
    with notifier_patch, sink_patch:
        _run(plugin._try_notify_cost_update(ctx))

    assert len(events) == 0, f"tool_execute 轮不应推送，得到 {len(events)} 个事件"


def test_cost_update_not_accumulated_across_rounds():
    """多轮场景：每轮推送的是本轮 llm_usage，不是跨轮累计。

    回归守护：原 bug 推送累计值（total 越加越大）。这里模拟第二轮 llm_usage
    较小，验证推送的就是第二轮的单轮值，而非叠加第一轮。
    """
    plugin = TrackPlugin()

    state_r1 = _base_state(llm_usage={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200})
    events_r1: list[dict[str, Any]] = []
    calls_r1: list[tuple] = []
    n1, s1 = _patch_notifier_and_sink(events_r1, calls_r1)
    with n1, s1:
        _run(plugin._try_notify_cost_update(_make_ctx(state_r1)))

    state_r2 = _base_state(llm_usage={"input_tokens": 1800, "output_tokens": 100, "total_tokens": 1900})
    events_r2: list[dict[str, Any]] = []
    calls_r2: list[tuple] = []
    n2, s2 = _patch_notifier_and_sink(events_r2, calls_r2)
    with n2, s2:
        _run(plugin._try_notify_cost_update(_make_ctx(state_r2)))

    assert len(events_r2) == 1
    assert events_r2[0]["data"]["total_tokens"] == 1900, (
        "第二轮推送的必须是第二轮单轮值 1900，而非累计 1200+1900=3100"
    )
