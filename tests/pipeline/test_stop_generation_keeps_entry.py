"""停止生成不注销管道回归测试。

BUG-FIX-fix_20260629_stop_generation_unregisters_pipeline:
问题根因: app_factory.py 的 stop_generation 分支在投递 CONTROL 信号之外，
  还冗余调用 fail_task + cancel_pipeline。cancel_pipeline → message_bus.stop()
  会 unregister 删除 entry。删 revive 前 send 靠自动重建兜底，本分支删掉 revive
  后这条路径变成致命：用户在同一标签页再发消息，send 命中 I4（未注册直接拒绝）
  → 报"管道未注册，无法发送消息（请联系持有者先 register）"。

正确语义：停止生成 = 控制信号，只中断当前轮、引擎进 idle 待命（entry 保留），
  下次 send 命中 entry 走 _start_idle_engine 重启。
  删除 entry 的 message_bus.stop() 是"取消任务"的持有者级终结，不属于停止生成。

本测试锁定两条路径的边界：
  - CONTROL 信号（停止生成）：entry 保留，引擎可被 _find_engine 命中为 idle
  - message_bus.stop（取消任务）：entry 被移除（_find_engine 返回 None）
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from pipeline.engine_registry import get_engine_registry
from pipeline.message_bus import _find_engine, stop


@pytest.fixture(autouse=True)
def clean_registry():
    """每个测试前后清空全局 EngineRegistry。"""
    reg = get_engine_registry()
    reg._engines.clear()
    yield
    reg._engines.clear()


def _make_idle_engine() -> MagicMock:
    """构造一个停止后处于 idle 状态的引擎替身（run 已结束、_run_started 复位）。"""
    engine = MagicMock()
    engine.is_running = False
    engine.is_suspended = False
    engine.is_idle = True  # run finally 复位 _run_started 后的状态
    return engine


class TestStopGenerationKeepsEntry:
    """停止生成（CONTROL 信号路径）后 entry 必须保留，引擎命中为 idle。"""

    def test_stop_generation_signal_does_not_unregister(self) -> None:
        """投递 stop_generation 信号后 entry 仍在（停止生成不删 entry）。

        deliver_signal 的契约（见 engine._interrupt_engine_task 注释）：
        cancel engine_task 让 run() 的 finally 发 state_change(stopped)，
        引擎进 idle 待命（entry 不移除，可重发消息）。
        """
        reg = get_engine_registry()
        engine = _make_idle_engine()
        entry = reg.register("sig-stop-1", engine, thread_id="t1")
        # 模拟一个已完成的 engine_task（run 经 finally 走到 idle）
        mock_task = MagicMock()
        mock_task.done.return_value = True
        entry.engine_task = mock_task

        # 停止生成只走信号路径，不调 message_bus.stop
        engine.deliver_signal({"signal_type": "stop_generation"})

        # entry 必须保留——这正是"续发消息能走 idle 重启"的前提
        assert reg.get("sig-stop-1") is not None, (
            "停止生成不应注销管道：entry 丢失会导致续发消息报'未注册'"
        )

    def test_engine_idle_after_stop_can_be_found_for_restart(self) -> None:
        """停止生成后引擎为 idle，_find_engine 命中 idle（续发走 _start_idle_engine）。"""
        reg = get_engine_registry()
        engine = _make_idle_engine()
        reg.register("sig-restart-1", engine, thread_id="t1")

        found, state = _find_engine("sig-restart-1")

        assert found is engine, "停止后引擎仍应能被 _find_engine 找到"
        assert state == "idle", (
            f"停止生成后引擎应为 idle（供续发重启），得到 {state}"
        )

    def test_stop_actually_removes_entry(self) -> None:
        """message_bus.stop（取消任务）真正删除 entry（与停止生成对比的对照组）。"""
        reg = get_engine_registry()
        engine = _make_idle_engine()
        entry = reg.register("task-cancel-2", engine, thread_id="t1")
        # stop 不穿透私有成员：entry.engine.cleanup 是它调的公开清理
        entry.engine.cleanup = MagicMock()

        import asyncio

        asyncio.run(stop("task-cancel-2"))

        assert reg.get("task-cancel-2") is None, (
            "取消任务（message_bus.stop）必须删除 entry——这是与停止生成的边界"
        )
        found, _ = _find_engine("task-cancel-2")
        assert found is None, "stop 删 entry 后 _find_engine 应返回 None"
