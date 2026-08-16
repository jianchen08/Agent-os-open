# @feature: FP-0.2.可观测性 | @ci: python-coverage
"""TrackPlugin cost_update 事件契约测试（frontend.emit 出口，task_observability 1a）。

钉死 cost_update 推送给前端的数据契约（经 frontend.emit capability，ADR §3.5）：
- payload 必须含路由键 thread_id（= session_id）/ pipeline_id / message_id
  （内核 frontend.emit 分支与前端 resolvePipelineId/extractMessageId 硬门控）
- 单轮值（顶层）：取自 state["llm_usage"]（本轮 API 返回，表达当前上下文窗口
  占用，前端 ChatInput 进度条用）+ missed_tokens / cache_hit_ratio
- 累计值（cumulative.*）：取自 _collect_token_usage 跨轮累加的 total_* 字段
  （前端统计区用），missed = 总输入 - 缓存命中
- tool_execute 轮不推送（llm_usage 是上一轮残留，避免错误覆盖）
- frontend 服务未注入（旧内核 / 单测环境）静默跳过

0.1 遗留的 ws_interaction_notifier / create_targeted_sink 出口已删除；
0.2 按 task_observability 以 ctx.get_service("frontend") 的 FrontendEmitter 出口重写。
"""

import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tests._pipeline_plugin_path import add_plugin_dir  # noqa: E402

add_plugin_dir("output", "track")
from pipeline.plugin import PluginContext  # noqa: E402
from plugin import TrackPlugin  # noqa: E402

PIPELINE_ID = "pipeline_cost_001"


class _FakeFrontend:
    """假 frontend emitter：记录 emit(event, payload) 调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, event: str, payload: dict[str, Any]) -> None:
        self.calls.append((event, payload))


def _make_ctx(state: dict[str, Any], services: dict[str, Any] | None = None) -> PluginContext:
    """构造带可选 frontend 服务的 PluginContext。"""
    return PluginContext(state=state, config={}, _services=services or {})


def _base_state(**overrides: Any) -> dict[str, Any]:
    """llm_call 轮标准 state（真实会话标识是 session_id，不是 thread_id）。"""
    state = {
        "core_type": "llm_call",
        "session_id": "session_001",
        "pipeline_id": PIPELINE_ID,
        "message_id": "msg_001",
        "llm_usage": {
            "input_tokens": 1200,
            "output_tokens": 300,
            "total_tokens": 1500,
            "cached_tokens": 1000,
        },
        "track.llm_usage": {
            "total_input_tokens": 3200,
            "total_output_tokens": 700,
            "total_tokens": 3900,
            "total_cached_tokens": 2500,
            "total_missed_tokens": 700,
            "total_cache_hit_ratio": 2500 / 3200,
            "last_input_tokens": 1200,
            "last_output_tokens": 300,
            "last_cached_tokens": 1000,
            "last_missed_tokens": 200,
            "last_cache_hit_ratio": 1000 / 1200,
        },
    }
    state.update(overrides)
    return state


def _cost_calls(frontend: _FakeFrontend) -> list[dict[str, Any]]:
    return [payload for event, payload in frontend.calls if event == "cost_update"]


@pytest.mark.asyncio
async def test_cost_update_single_round_tokens_and_routing_keys() -> None:
    """llm_call 轮：cost_update 含路由键 + 单轮值 + cache 指标 + 累计值。"""
    plugin = TrackPlugin()
    frontend = _FakeFrontend()
    ctx = _make_ctx(_base_state(), {"frontend": frontend})
    usage = plugin._collect_token_usage(ctx)

    await plugin._try_notify_cost_update(ctx, usage)

    costs = _cost_calls(frontend)
    assert len(costs) == 1, f"应推送一个 cost_update，得到 {len(costs)}"
    data = costs[0]
    # 路由键（内核/前端硬门控）
    assert data["thread_id"] == "session_001"
    assert data["pipeline_id"] == PIPELINE_ID
    assert data["message_id"] == "msg_001"
    # 单轮值（本轮 API 返回，非累计）
    assert data["input_tokens"] == 1200
    assert data["output_tokens"] == 300
    assert data["cached_tokens"] == 1000
    assert data["total_tokens"] == 1500
    assert data["missed_tokens"] == 200
    assert data["cache_hit_ratio"] == pytest.approx(1000 / 1200)


@pytest.mark.asyncio
async def test_cost_update_carries_cumulative_block() -> None:
    """cumulative.* 表达整个管道累计消耗（前端统计区分桶加总用）。"""
    plugin = TrackPlugin()
    frontend = _FakeFrontend()
    ctx = _make_ctx(_base_state(), {"frontend": frontend})
    usage = plugin._collect_token_usage(ctx)

    await plugin._try_notify_cost_update(ctx, usage)

    data = _cost_calls(frontend)[0]
    cum = data["cumulative"]
    # 3200+1200 / 700+300 / 2500+1000
    assert cum["total_input"] == 4400
    assert cum["total_output"] == 1000
    assert cum["total_cached"] == 3500
    assert cum["missed"] == 900  # 4400 - 3500
    assert cum["total_tokens"] == 5400
    assert cum["cache_hit_ratio"] == pytest.approx(3500 / 4400)


@pytest.mark.asyncio
async def test_cost_update_skipped_on_tool_execute_round() -> None:
    """tool_execute 轮不推送（llm_usage 是上一轮残留，避免错误覆盖）。"""
    plugin = TrackPlugin()
    frontend = _FakeFrontend()
    state = _base_state(core_type="tool_execute")
    ctx = _make_ctx(state, {"frontend": frontend})
    usage = plugin._collect_token_usage(ctx)

    await plugin._try_notify_cost_update(ctx, usage)

    assert frontend.calls == [], "tool_execute 轮不应推送"


@pytest.mark.asyncio
async def test_cost_update_silent_without_frontend_service() -> None:
    """frontend 服务未注入（旧内核 / 无桥接）静默跳过，不抛异常。"""
    plugin = TrackPlugin()
    state = _base_state()
    ctx = _make_ctx(state)
    usage = plugin._collect_token_usage(ctx)

    # 前置：frontend 服务确未注入（get_service 对未注册服务抛 KeyError）
    with pytest.raises(KeyError):
        ctx.get_service("frontend")

    # 契约：静默路径不外泄任何异常（KeyError 在插件内部被吞掉）
    await plugin._try_notify_cost_update(ctx, usage)

    # 无副作用：state 未被写入任何 cost_update 相关痕迹
    assert not any(str(k).startswith("cost_update") for k in state)


@pytest.mark.asyncio
async def test_cost_update_not_accumulated_across_rounds() -> None:
    """多轮场景：每轮推送的是本轮 llm_usage 单轮值，不是跨轮累计。

    回归守护：原 bug 推送累计值（total 越加越大）。
    """
    plugin = TrackPlugin()

    state_r2 = _base_state(llm_usage={"input_tokens": 1800, "output_tokens": 100, "total_tokens": 1900})
    frontend = _FakeFrontend()
    ctx = _make_ctx(state_r2, {"frontend": frontend})
    usage = plugin._collect_token_usage(ctx)
    await plugin._try_notify_cost_update(ctx, usage)

    costs = _cost_calls(frontend)
    assert len(costs) == 1
    assert costs[0]["total_tokens"] == 1900, (
        "第二轮推送的必须是第二轮单轮值 1900，而非累计值"
    )


@pytest.mark.asyncio
async def test_execute_wires_notify_into_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """execute 主流程：token 追踪开启时经 frontend 推送一次 cost_update。"""
    plugin = TrackPlugin()
    frontend = _FakeFrontend()
    ctx = _make_ctx(_base_state(), {"frontend": frontend})

    await plugin.execute(ctx)

    costs = _cost_calls(frontend)
    assert len(costs) == 1
    assert costs[0]["pipeline_id"] == PIPELINE_ID
