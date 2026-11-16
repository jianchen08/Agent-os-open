# @feature: FP-0.2.二 mode_personal 番茄定向计时 | @ci: python-coverage
"""PimFocusTimer 单测：定向任务、配置默认/单次覆盖、停止/非法标记。"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PKG = Path(__file__).resolve().parents[1]
_SHARED = _PKG.parent


def _load(name: str, mod_name: str) -> Any:
    for d in (_PKG, _SHARED):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    path = _PKG / f"{name}.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pim_store = _load("pim_store", "mode_personal_test_focus_store")
pim_focus = _load("pim_focus_timer", "mode_personal_test_focus_timer")

NOW = datetime(2026, 10, 12, 9, 0, 0)


class FakeSleep:
    def __init__(self) -> None:
        self.seconds: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)


@pytest.fixture
def store(tmp_path: Path) -> Any:
    return pim_store.PimStore(tmp_path / "data", clock=lambda: NOW)


def _make_timer(store: Any, sleep: FakeSleep) -> Any:
    timer = pim_focus.PimFocusTimer(store, now=lambda: NOW, sleep=sleep)
    store.set_on_change(timer.on_change)
    return timer


class TestFocusTimer:
    async def test_default_duration_25(self, store: Any) -> None:
        todo, _ = store.create("todo", {"title": "专注"})
        store.start_focus(todo["id"])
        sleep = FakeSleep()
        timer = _make_timer(store, sleep)
        timer.start()
        await _drain()
        assert sleep.seconds == [1500.0]
        settled = store.get("todo", todo["id"])
        assert settled["focus_started_at"] is None
        assert store.get_partner()["trees"] == 1

    async def test_configured_default_used(self, store: Any) -> None:
        store.set_focus_default_minutes(40)
        todo, _ = store.create("todo", {"title": "专注"})
        store.start_focus(todo["id"])
        sleep = FakeSleep()
        timer = _make_timer(store, sleep)
        timer.start()
        await _drain()
        assert sleep.seconds == [2400.0]

    async def test_explicit_duration_overrides(self, store: Any) -> None:
        todo, _ = store.create("todo", {"title": "专注"})
        store.start_focus(todo["id"], 10)
        sleep = FakeSleep()
        timer = _make_timer(store, sleep)
        timer.start()
        await _drain()
        assert sleep.seconds == [600.0]

    async def test_stop_cancels_and_no_tree(self, store: Any) -> None:
        todo, _ = store.create("todo", {"title": "专注"})
        store.start_focus(todo["id"])
        sleep = FakeSleep()
        timer = _make_timer(store, sleep)
        timer.start()
        store.stop_focus(todo["id"])
        await _drain()
        assert store.get_partner()["trees"] == 0
        assert store.get("todo", todo["id"])["status"] == "doing"

    async def test_invalid_marker_skipped(self, store: Any) -> None:
        todo, _ = store.create("todo", {"title": "专注"})
        store.update(
            "todo",
            todo["id"],
            {"status": "doing", "focus_started_at": "not-a-date"},
        )
        sleep = FakeSleep()
        timer = _make_timer(store, sleep)
        timer.start()
        await _drain()
        assert sleep.seconds == []
        assert store.get_partner()["trees"] == 0


async def _drain() -> None:
    """让定向任务跑完（fake sleep 零延迟）。"""
    import asyncio

    for _ in range(5):
        await asyncio.sleep(0)
