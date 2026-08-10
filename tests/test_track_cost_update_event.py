"""TrackPlugin cost_update 事件契约测试。

钉死 cost_update 事件推送给前端的数据契约。payload 同时携带两套数据：

- 顶层单轮字段（input_tokens/output_tokens/total_tokens/cached_tokens）：
  取自 state["llm_usage"]（本轮 API 返回），表达「当前上下文窗口占用」。
  前端 ChatInput 进度条据此计算占窗比。
- cumulative.* 累计字段（命中/未命中/输出分别加总）：取自
  state["track.llm_usage"] 的 total_* 字段（跨轮累加），表达「整个管道累计消耗」。
  前端统计区据此显示「缓存命中输入 / 未命中输入 / 输出 分别加总」。

约束：
- payload 必须含 pipeline_id（前端按 pipeline 分桶写入 contextUsageStore）
- tool_execute 轮不推送（llm_usage 是上一轮残留，避免错误覆盖）
- cumulative.missed_tokens 必须等于 input_tokens - cached_tokens（下界 0）
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
        # track 插件跨轮累加后的累计值（_collect_token_usage 产出）
        "track.llm_usage": {
            "total_input_tokens": 5000,
            "total_output_tokens": 800,
            "total_tokens": 5800,
            "total_cached_tokens": 3000,
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
    # 顶层单轮字段：表达当前上下文窗口占用（供进度条）
    assert data["total_tokens"] == 1500, "total_tokens 必须是本轮单轮值"
    assert data["input_tokens"] == 1200
    assert data["output_tokens"] == 300
    assert data["cached_tokens"] == 0


def test_cost_update_carries_cumulative_tokens():
    """cost_update 必须带 cumulative 字段，含命中/未命中/输出三维度累计值。

    累计值取自 state["track.llm_usage"].total_*（跨轮累加），与单轮字段独立。
    missed_tokens = total_input_tokens - total_cached_tokens（下界 0）。
    """
    plugin = TrackPlugin()
    ctx = _make_ctx(_base_state())
    events: list[dict[str, Any]] = []
    calls: list[tuple] = []

    notifier_patch, sink_patch = _patch_notifier_and_sink(events, calls)
    with notifier_patch, sink_patch:
        _run(plugin._try_notify_cost_update(ctx))

    data = [e for e in events if e.get("type") == "cost_update"][0]["data"]
    assert "cumulative" in data, "必须带 cumulative 字段（累计消耗统计）"
    cum = data["cumulative"]
    assert cum["input_tokens"] == 5000, "累计输入 = track.total_input_tokens"
    assert cum["output_tokens"] == 800, "累计输出 = track.total_output_tokens"
    assert cum["cached_tokens"] == 3000, "累计缓存命中 = track.total_cached_tokens"
    assert cum["total_tokens"] == 5800, "累计总额 = track.total_tokens"
    assert cum["missed_tokens"] == 2000, "未命中 = 累计输入 - 累计命中 = 5000-3000"


def test_cost_update_cumulative_missed_tokens_floored_at_zero():
    """cached > input 的异常情况下，missed_tokens 不应为负（下界 0）。"""
    plugin = TrackPlugin()
    state = _base_state()
    state["track.llm_usage"] = {
        "total_input_tokens": 1000,
        "total_output_tokens": 200,
        "total_tokens": 1200,
        "total_cached_tokens": 1500,  # 异常：命中超过输入
    }
    ctx = _make_ctx(state)
    events: list[dict[str, Any]] = []
    calls: list[tuple] = []
    notifier_patch, sink_patch = _patch_notifier_and_sink(events, calls)
    with notifier_patch, sink_patch:
        _run(plugin._try_notify_cost_update(ctx))

    cum = [e for e in events if e.get("type") == "cost_update"][0]["data"]["cumulative"]
    assert cum["missed_tokens"] == 0, "missed_tokens 下界为 0，不得为负"


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
    """多轮场景：顶层单轮字段跟着每轮 llm_usage 走，cumulative 跟着 track 累计走。

    顶层 total_tokens 守护：必须是本轮 llm_usage 的单轮值，不叠加历史轮次
    （原 bug：把累计值推到顶层，多轮时 total 越加越大）。
    cumulative 守护：必须是本轮 track.llm_usage 的累计值，反映跨轮加总。
    两者独立，互不污染。
    """
    plugin = TrackPlugin()

    # 第一轮：llm_usage 单轮 1200，track 累计 1200
    state_r1 = _base_state(
        llm_usage={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
        **{
            "track.llm_usage": {
                "total_input_tokens": 1000,
                "total_output_tokens": 200,
                "total_tokens": 1200,
                "total_cached_tokens": 0,
            }
        },
    )
    events_r1: list[dict[str, Any]] = []
    calls_r1: list[tuple] = []
    n1, s1 = _patch_notifier_and_sink(events_r1, calls_r1)
    with n1, s1:
        _run(plugin._try_notify_cost_update(_make_ctx(state_r1)))

    # 第二轮：llm_usage 单轮 1900，但 track 累计已是 3100（1200+1900）
    state_r2 = _base_state(
        llm_usage={"input_tokens": 1800, "output_tokens": 100, "total_tokens": 1900},
        **{
            "track.llm_usage": {
                "total_input_tokens": 2800,
                "total_output_tokens": 300,
                "total_tokens": 3100,
                "total_cached_tokens": 500,
            }
        },
    )
    events_r2: list[dict[str, Any]] = []
    calls_r2: list[tuple] = []
    n2, s2 = _patch_notifier_and_sink(events_r2, calls_r2)
    with n2, s2:
        _run(plugin._try_notify_cost_update(_make_ctx(state_r2)))

    assert len(events_r2) == 1
    data_r2 = events_r2[0]["data"]
    # 顶层单轮字段：必须是第二轮单轮值 1900，不能是累计 3100
    assert data_r2["total_tokens"] == 1900, (
        "顶层 total_tokens 必须是第二轮单轮值 1900，而非累计 3100"
    )
    # cumulative 字段：必须是第二轮的累计值 3100，反映跨轮加总
    assert data_r2["cumulative"]["total_tokens"] == 3100, (
        "cumulative.total_tokens 必须是累计值 3100"
    )
    assert data_r2["cumulative"]["missed_tokens"] == 2300, (
        "cumulative.missed_tokens = 2800 - 500 = 2300"
    )
