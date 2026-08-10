"""message_bus.stop() 取消 engine_task 必须安静（不写 RAW_ERROR、不 fail_task）。

修复背景（BUG-20260716 stop_generation 级联杀子任务）：
用户停止生成父任务 → 父任务 fail_task → fail_task_cascade → cancel_task(子) →
子任务状态变 STOPPED → _on_task_state_changed → cancel_pipeline(子) →
message_bus.stop(子管道) → entry.engine_task.cancel()。

此前 stop() 取消 engine_task 时【不设】_user_stop_requested 标志，导致子引擎
_run_loop 的 CancelledError 走 else 分支：写 RAW_ERROR="Pipeline engine cancelled"、
emit_error、_mark_task_failed_on_engine_exit（覆盖上层设的 STOPPED）。
这就是用户看到的 "管道异常退出: Pipeline engine cancelled (source=Task-XXXX)"
+ "已重试 1/6 次" 的真正来源。

修复后：stop() cancel 前置 _user_stop_requested=True（经引擎公开方法
_mark_user_stop_requested），引擎走安静分支——不写 RAW_ERROR、不 fail_task，
尊重上层（cancel_pipeline 等）已设的任务终态。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from pipeline.engine import PipelineEngine
from pipeline.engine_registry import get_engine_registry
from pipeline.message_bus import stop
from pipeline.registry import PluginRegistry
from pipeline.route import InputRouteTable, OutputRouteTable
from pipeline.types import StateKeys


@pytest.fixture(autouse=True)
def clean_registry():
    reg = get_engine_registry()
    reg._engines.clear()
    yield
    reg._engines.clear()


def _min_state(pipeline_id: str) -> dict:
    return {
        StateKeys.PIPELINE_ID: pipeline_id,
        StateKeys.ITERATION: 0,
        StateKeys.ENDED: False,
        "messages": [{"role": "user", "content": "hi"}],
    }


class TestMessageBusStopQuiet:
    """stop() 取消 engine_task 必须让引擎安静退出（走 _user_stop_requested 分支）。"""

    def test_stop_sets_user_stop_flag_before_cancel(self) -> None:
        """stop() 调 cancel 前，引擎 _user_stop_requested 已被置 True。"""
        engine = PipelineEngine(
            input_route_table=InputRouteTable(),
            output_route_table=OutputRouteTable(),
            plugin_registry=PluginRegistry(),
        )
        engine.pipeline_id = "mb-stop-1"
        get_engine_registry().register("mb-stop-1", engine, thread_id="t1")
        assert engine._user_stop_requested is False  # 初始未置

        # 用一个"正在运行"的模拟 engine_task，让 stop() 走 cancel 分支
        entry = get_engine_registry().get("mb-stop-1")
        mock_task = MagicMock()
        mock_task.done.return_value = False
        entry.engine_task = mock_task

        asyncio.run(stop("mb-stop-1"))

        # 标志应被置 True（stop() 经 _mark_user_stop_requested 设置）
        assert engine._user_stop_requested is True, (
            "message_bus.stop() 取消 engine_task 前必须置 _user_stop_requested，"
            "否则引擎 else 分支会 fail_task 覆盖上层终态"
        )
        # mock_task 被 cancel
        mock_task.cancel.assert_called_once()

    def test_stop_driven_cancelled_engine_no_raw_error(self) -> None:
        """stop() 触发的引擎 CancelledError 走安静分支：state 无 RAW_ERROR。

        模拟级联场景：引擎 _run_loop 运行中，stop() 取消它的 engine_task。
        引擎应安静退出，不写 "Pipeline engine cancelled"。
        """
        engine = PipelineEngine(
            input_route_table=InputRouteTable(),
            output_route_table=OutputRouteTable(),
            plugin_registry=PluginRegistry(),
        )
        engine.pipeline_id = "mb-stop-2"
        get_engine_registry().register("mb-stop-2", engine, thread_id="t2")

        async def _blocking_iter(_eng, _state, _iter):  # noqa: ANN001
            # 阻塞直到被取消（模拟引擎运行中的 LLM await）
            await asyncio.sleep(100)

        async def _scenario():
            state = _min_state("mb-stop-2")
            # 启动 _run_loop 作为独立 task
            loop_task = asyncio.ensure_future(engine._run_loop(state, resumed=False))
            # 把它登记为 entry.engine_task，让 stop() 能取消它
            _entry = get_engine_registry().get("mb-stop-2")
            _entry.engine_task = loop_task
            await asyncio.sleep(0.05)  # 让引擎进入 run_iteration
            # 触发 stop（会 cancel engine_task = loop_task）
            await stop("mb-stop-2")
            # 收尾：loop_task 已被取消，await 它确认 _run_loop 走完 except/finally
            try:
                await loop_task
            except asyncio.CancelledError:
                pass
            return state

        fail_spy = AsyncMock()
        with patch("pipeline.engine.run_iteration", _blocking_iter), \
                patch.object(engine, "_mark_task_failed_on_engine_exit", fail_spy):
            state = asyncio.run(_scenario())

        # 安静分支：不写 RAW_ERROR（区别于 else 分支的 "Pipeline engine cancelled"）
        assert state.get(StateKeys.RAW_ERROR) in (None, ""), (
            "stop() 触发的取消不应写 RAW_ERROR——否则上层终态被引擎覆盖，"
            "产生 'Pipeline engine cancelled' 文案"
        )
        # 安静分支：不调 fail_task
        fail_spy.assert_not_called(), (
            "stop() 触发的取消不应 fail_task——上层 cancel_pipeline 已设任务终态"
        )

    def test_stop_with_done_engine_task_no_flag_change(self) -> None:
        """engine_task 已 done 时，stop() 不强制改标志（无需取消）。"""
        engine = PipelineEngine(
            input_route_table=InputRouteTable(),
            output_route_table=OutputRouteTable(),
            plugin_registry=PluginRegistry(),
        )
        engine.pipeline_id = "mb-stop-3"
        entry = get_engine_registry().register("mb-stop-3", engine)
        mock_task = MagicMock()
        mock_task.done.return_value = True  # 已完成
        entry.engine_task = mock_task

        asyncio.run(stop("mb-stop-3"))

        # engine_task 已 done，stop 不 cancel，标志保持 False（无 cancel 无需置标志）
        assert engine._user_stop_requested is False
        mock_task.cancel.assert_not_called()
        # 但 entry 应被 unregister
        assert get_engine_registry().get("mb-stop-3") is None
