# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-coverage
"""stop_check 计时起点按 (pipeline_id, run_id) 重置的契约测试。

同 sidecar 单例被同一管道跨 run 复用（聊天会话每发一条消息 = 同 pipeline_id
起新 run，引擎每轮派发注入 run_id）。本文件锁定超时闸门语义 = 单次 run 执行
≤ timeout_seconds：

1. 同 pipeline 同 run_id 连续 execute：不重置，elapsed 跨轮累计可触发超时；
2. 同 pipeline 新 run_id 首次 execute：重置，run 间空闲挂钟不误杀新 run；
3. 跨 pipeline（run_id 相同也须重置）：既有行为回归。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[2]  # plugins/shared/
_SYSTEM = _SHARED / "system"

for _d in [_DIR, _SHARED, _SYSTEM]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from pipeline.plugin import PluginContext  # noqa: E402


def _load_plugin_module() -> Any:
    mod_name = "stop_check_plugin_run_reset_test"
    module_path = _DIR / "plugin.py"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, str(module_path))
    assert spec is not None, "Cannot load plugin.py"
    assert spec.loader is not None, "Cannot load plugin.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeClock:
    """可手动推进的单调时钟（注入替代 time.monotonic，禁真实 sleep）。"""

    def __init__(self) -> None:
        self._now = 1000.0

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _plugin_with_clock(monkeypatch: Any) -> tuple[Any, _FakeClock, Any]:
    mod = _load_plugin_module()
    clock = _FakeClock()
    monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
    plugin = mod.StopCheckPlugin(config={})
    return mod, clock, plugin


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=dict(state), config={}, _services={})


class TestTimerResetKey:
    @pytest.mark.parametrize("timeout,elapsed", [(50, 60), (1000, 2000)])
    def test_same_run_accumulates_and_times_out(
        self, monkeypatch: Any, timeout: int, elapsed: int
    ) -> None:
        """同 pipeline 同 run_id 连续 execute 不重置：首轮放行，elapsed 跨轮
        累计超线后 timeout 强停（同 run 内挂钟累计语义保留）。"""
        _mod, clock, plugin = _plugin_with_clock(monkeypatch)
        state = {
            "pipeline_id": "pipe-a",
            "run_id": "run-1",
            "iteration": 1,
            "timeout_seconds": timeout,
            "task.id": "",
        }

        first = _run(plugin.execute(_ctx(state)))
        assert first.state_updates.get("router.stop_reason", "") == ""

        clock.advance(elapsed)
        second = _run(plugin.execute(_ctx(state)))
        assert second.state_updates.get("router.stop_reason") == "timeout"
        assert second.state_updates.get("should_stop") is True

    @pytest.mark.parametrize("run_a,run_b", [("run-1", "run-2"), ("run-A", "run-B")])
    def test_new_run_id_resets_elapsed(
        self, monkeypatch: Any, run_a: str, run_b: str
    ) -> None:
        """同 pipeline 新 run_id 首次 execute 重置：新 run 拿到完整预算。

        反证锚点：若 run 切换未重置，新 run 第二轮 elapsed 已是 120s > 100s
        上限会被误杀；重置后第二轮 elapsed=60s 放行，第三轮同 run 累计 110s
        超线仍然强停（闸门未失效）。"""
        _mod, clock, plugin = _plugin_with_clock(monkeypatch)
        state_a = {
            "pipeline_id": "pipe-a",
            "run_id": run_a,
            "iteration": 1,
            "timeout_seconds": 100,
            "task.id": "",
        }
        state_b = {
            "pipeline_id": "pipe-a",
            "run_id": run_b,
            "iteration": 1,
            "timeout_seconds": 100,
            "task.id": "",
        }

        first = _run(plugin.execute(_ctx(state_a)))
        assert first.state_updates.get("router.stop_reason", "") == ""

        clock.advance(60)
        new_run_first = _run(plugin.execute(_ctx(state_b)))
        assert new_run_first.state_updates.get("router.stop_reason", "") == ""

        clock.advance(60)
        new_run_second = _run(plugin.execute(_ctx(state_b)))
        assert new_run_second.state_updates.get("router.stop_reason", "") == "", (
            "新 run 第二轮 elapsed 应为 60s（<100s 放行）；若为 120s 说明 run 切换未重置"
        )

        clock.advance(50)
        new_run_third = _run(plugin.execute(_ctx(state_b)))
        assert new_run_third.state_updates.get("router.stop_reason") == "timeout"

    @pytest.mark.parametrize(
        "pipe_a,pipe_b",
        [("pipe-a", "pipe-b"), ("911033681984", "922033681985")],
    )
    def test_cross_pipeline_resets(
        self, monkeypatch: Any, pipe_a: str, pipe_b: str
    ) -> None:
        """跨 pipeline 重置（既有行为回归）：前管道累计挂钟不泄漏到新管道。

        两管道故意用相同 run_id，隔离验证 identity 中 pipeline_id 分量的作用。"""
        _mod, clock, plugin = _plugin_with_clock(monkeypatch)
        state_a = {
            "pipeline_id": pipe_a,
            "run_id": "run-1",
            "iteration": 1,
            "timeout_seconds": 100,
            "task.id": "",
        }
        state_b = {
            "pipeline_id": pipe_b,
            "run_id": "run-1",
            "iteration": 1,
            "timeout_seconds": 100,
            "task.id": "",
        }

        first = _run(plugin.execute(_ctx(state_a)))
        assert first.state_updates.get("router.stop_reason", "") == ""

        clock.advance(120)
        second = _run(plugin.execute(_ctx(state_b)))
        assert second.state_updates.get("router.stop_reason", "") == "", (
            "跨 pipeline 必须重置计时；若 elapsed=120s 直接触发 timeout 说明未重置"
        )
