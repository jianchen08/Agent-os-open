"""task_idle_timer 兜底逻辑回归测试。

BUG-FIX-fix_20260629_waiting_recovery_deadlock:
若 pipeline 引擎 last_state[EXECUTION_STATUS] == "waiting_recovery"，
_engine_is_running 必须返回 False，让 idle_timer 在 idle_threshold
周期内识别并 fail，避免 wait 路径死挂数小时。

正常运行的引擎（is_running=True 且无 waiting_recovery）仍判 True。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from infrastructure.task_idle_timer import TaskIdleTimerMixin
from pipeline.types import StateKeys


class _DummyEngine:
    def __init__(self, last_state: dict | None = None, is_running: bool = True) -> None:
        self.last_state = last_state or {}
        self.is_running = is_running
        self.pipeline_id = "abc1234567890def"


class _DummyEntry:
    def __init__(self, engine: _DummyEngine | None, future_done: bool = False) -> None:
        self.engine = engine
        # engine_task: 用 SimpleNamespace 模拟 concurrent.futures.Future
        self.engine_task = SimpleNamespace(done=lambda: future_done)


class _DummyRegistry:
    def __init__(self, entries: list[_DummyEntry]) -> None:
        self._entries = entries

    def find_by_tag(self, _key: str, _value: str) -> list[_DummyEntry]:
        return self._entries


def _patch_registry(monkeypatch, entries: list[_DummyEntry]) -> None:
    """让 _engine_is_running 内部 import 拿到我们 fake 的 registry。"""
    fake_registry = _DummyRegistry(entries)
    import pipeline.registry as registry_mod
    monkeypatch.setattr(
        registry_mod, "get_engine_registry", lambda: fake_registry
    )


class _Holder(TaskIdleTimerMixin):
    """裸用 Mixin，绕开 TaskWorker 重型构造。"""


def test_waiting_recovery_returns_false(monkeypatch) -> None:
    """EXECUTION_STATUS=waiting_recovery 时应判 False（让 idle 兜底 fail）。"""
    engine = _DummyEngine(
        last_state={StateKeys.EXECUTION_STATUS: "waiting_recovery"},
        is_running=True,
    )
    _patch_registry(monkeypatch, [_DummyEntry(engine, future_done=False)])
    holder = _Holder()
    assert holder._engine_is_running("task1") is False


def test_normal_running_returns_true(monkeypatch) -> None:
    """普通 is_running=True 且非 waiting_recovery → 仍判 True。"""
    engine = _DummyEngine(
        last_state={StateKeys.EXECUTION_STATUS: "success"},
        is_running=True,
    )
    _patch_registry(monkeypatch, [_DummyEntry(engine, future_done=False)])
    holder = _Holder()
    assert holder._engine_is_running("task2") is True


def test_no_last_state_returns_true_when_running(monkeypatch) -> None:
    """last_state 空 + is_running=True → 仍判在跑（保持旧行为）。"""
    engine = _DummyEngine(last_state=None, is_running=True)
    _patch_registry(monkeypatch, [_DummyEntry(engine, future_done=False)])
    holder = _Holder()
    assert holder._engine_is_running("task3") is True


def test_no_entries_returns_false(monkeypatch) -> None:
    _patch_registry(monkeypatch, [])
    holder = _Holder()
    assert holder._engine_is_running("task4") is False


def test_engine_stopped_returns_false(monkeypatch) -> None:
    """engine 已停止 + Future done → False。"""
    engine = _DummyEngine(last_state={}, is_running=False)
    _patch_registry(monkeypatch, [_DummyEntry(engine, future_done=True)])
    holder = _Holder()
    assert holder._engine_is_running("task5") is False
