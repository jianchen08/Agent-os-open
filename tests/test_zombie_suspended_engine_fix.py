"""回归测试: 僵尸挂起引擎导致"发消息半天没反应"

BUG-FIX-fix_20260625_zombie_suspended_engine:
- 问题: 引擎在 suspend（wait 路由 / 等子任务）时被 cancel（最常见是用户点"停止"），
  _run_loop 的 cancel 路径不清 _suspended_state，留下 is_suspended=True 但
  engine_task 已终止的"僵尸"条目。_find_engine 先判 is_suspended 返回 "suspended"，
  inject_message 的 wake 无人 await → 消息永久堆积 → 前端表现为"发消息半天没反应"。
- 修复: _find_engine 检测到 engine_task 已死但 is_suspended/is_running=True 时，
  重置引擎为 idle，让消息走 _start_idle_engine 重启并消费堆积的 _inject_queue。
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_engine():
    from pipeline.engine import PipelineEngine
    from pipeline.registry import PluginRegistry
    from pipeline.route import InputRouteTable, OutputRouteTable

    return PipelineEngine(
        input_route_table=InputRouteTable(),
        output_route_table=OutputRouteTable(),
        plugin_registry=PluginRegistry(),
    )


def _register(engine, *, pipeline_id, engine_task):
    from pipeline.registry import get_engine_registry

    entry = get_engine_registry().register(pipeline_id, engine, thread_id="t-zombie")
    entry.engine_task = engine_task
    return entry


class TestZombieSuspendedEngine:
    """_find_engine 必须把 engine_task 已死的僵尸引擎重置为 idle。"""

    def test_suspended_with_dead_task_returns_idle(self):
        """engine_task=None + _suspended_state 残留 → 返回 idle 并复位。"""
        from pipeline.message_bus import _find_engine
        from pipeline.registry import get_engine_registry

        pid = "zombie-suspended-1"
        reg = get_engine_registry()
        engine = _make_engine()
        engine._suspended_state = {"user_input": "stale"}  # 挂起快照残留
        engine._run_started = True  # 跑过一轮
        engine._running = False
        _register(engine, pipeline_id=pid, engine_task=None)  # 任务已死

        try:
            _eng, state = _find_engine(pid)

            assert state == "idle"
            assert engine.is_suspended is False  # 已复位
            assert engine.is_idle is True  # 可走 _start_idle_engine
        finally:
            reg.unregister(pid)

    def test_running_with_dead_task_returns_idle(self):
        """is_running=True 但 engine_task 已死 → 同样复位为 idle。"""
        from pipeline.message_bus import _find_engine
        from pipeline.registry import get_engine_registry

        pid = "zombie-running-1"
        reg = get_engine_registry()
        engine = _make_engine()
        engine._running = True  # 卡在 running 标志
        engine._run_started = True
        engine._suspended_state = None
        _register(engine, pipeline_id=pid, engine_task=None)

        try:
            _eng, state = _find_engine(pid)

            assert state == "idle"
            assert engine.is_running is False  # 已复位
        finally:
            reg.unregister(pid)

    async def test_suspended_with_alive_task_returns_suspended(self):
        """engine_task 仍存活 + _suspended_state set → 正常返回 suspended（无误杀）。"""
        from pipeline.message_bus import _find_engine
        from pipeline.registry import get_engine_registry

        pid = "zombie-suspended-alive"
        reg = get_engine_registry()
        engine = _make_engine()
        engine._suspended_state = {"user_input": "stale"}
        engine._run_started = True
        engine._running = False

        alive_task = asyncio.create_task(asyncio.sleep(100))
        try:
            _register(engine, pipeline_id=pid, engine_task=alive_task)
            _eng, state = _find_engine(pid)

            assert state == "suspended"
            assert engine.is_suspended is True  # 未被复位（任务还活着）
        finally:
            alive_task.cancel()
            reg.unregister(pid)

    async def test_done_task_treated_as_dead(self):
        """engine_task.done()=True（已返回）也视为死，复位为 idle。"""
        from pipeline.message_bus import _find_engine
        from pipeline.registry import get_engine_registry

        pid = "zombie-done-1"
        reg = get_engine_registry()
        engine = _make_engine()
        engine._suspended_state = {"user_input": "stale"}
        engine._run_started = True

        async def _noop() -> None:
            return None

        done_task = asyncio.create_task(_noop())
        await done_task  # 跑到 done
        assert done_task.done()

        try:
            _register(engine, pipeline_id=pid, engine_task=done_task)
            _eng, state = _find_engine(pid)

            assert state == "idle"
            assert engine.is_suspended is False
        finally:
            reg.unregister(pid)

    def test_idle_engine_not_affected(self):
        """正常 idle 引擎（task=None 但未挂起）→ 返回 idle，不触发复位逻辑。"""
        from pipeline.message_bus import _find_engine
        from pipeline.registry import get_engine_registry

        pid = "idle-healthy-1"
        reg = get_engine_registry()
        engine = _make_engine()
        engine._run_started = False  # 真 idle
        engine._suspended_state = None
        _register(engine, pipeline_id=pid, engine_task=None)

        try:
            _eng, state = _find_engine(pid)
            assert state == "idle"
        finally:
            reg.unregister(pid)


class TestEngineTaskDeadHelper:
    """_is_engine_task_dead / _reset_engine_for_restart 单元测试。"""

    def test_none_task_is_dead(self):
        from pipeline.message_bus import _is_engine_task_dead
        from pipeline.pipeline_entry import PipelineEntry

        entry = PipelineEntry(engine=None)
        assert _is_engine_task_dead(entry) is True

    async def test_alive_task_not_dead(self):
        from pipeline.message_bus import _is_engine_task_dead
        from pipeline.pipeline_entry import PipelineEntry

        alive = asyncio.create_task(asyncio.sleep(100))
        try:
            entry = PipelineEntry(engine=None)
            entry.engine_task = alive
            assert _is_engine_task_dead(entry) is False
        finally:
            alive.cancel()

    def test_reset_clears_suspend_state(self):
        from pipeline.message_bus import _reset_engine_for_restart

        engine = _make_engine()
        engine._suspended_state = {"user_input": "x"}
        engine._wake_event = asyncio.Event()
        engine._running = True
        engine._run_started = True

        _reset_engine_for_restart(engine)

        assert engine._suspended_state is None
        assert engine._wake_event is None
        assert engine._running is False
        assert engine.is_idle is True
