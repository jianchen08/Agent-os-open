# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-coverage
"""stop_check sidecar 实例复用契约测试。

同 sidecar 进程内，一个插件实例被多个管道（多 agent）连续复用。本文件锁定：

1. 运行时参数复位：前一 agent 经 state 注入的 max_iterations / timeout_seconds
   不残留到下一 agent 的执行（每次 execute 先回构造默认再应用覆盖）；
2. 任务实际状态查询走公共服务接口：未注册服务时经 tasks.service_access
   单例兜底可查到终态；查询失败记 warning（与正常运行路径可区分），不崩溃。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
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
    mod_name = "stop_check_plugin_sidecar_test"
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


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ctx(state: dict[str, Any], services: dict[str, Any] | None = None) -> PluginContext:
    return PluginContext(state=dict(state), config={}, _services=services or {})


class TestRuntimeConfigResetPerAgent:
    def test_max_iterations_does_not_leak_across_agents(self) -> None:
        """Agent A 注入 max_iterations=3 触发上限终止；随后的 Agent B 未注入任何
        覆盖——同实例连续执行必须回到构造默认（20），iteration=5 正常放行。"""
        mod = _load_plugin_module()
        plugin = mod.StopCheckPlugin(config={})

        state_a = {
            "pipeline_id": "pipe-a",
            "iteration": 5,
            "max_iterations": 3,
            "timeout_seconds": -1,
            "task.id": "",
        }
        res_a = _run(plugin.execute(_ctx(state_a)))
        assert res_a.route_signal is not None, "Agent A 应命中自定义上限 3 终止"
        assert res_a.route_signal.route_type == "end"

        state_b = {"pipeline_id": "pipe-b", "iteration": 5, "task.id": ""}
        res_b = _run(plugin.execute(_ctx(state_b)))
        assert res_b.route_signal is None, (
            f"Agent B 无覆盖时应按默认上限放行，实际残留了 Agent A 的配置: "
            f"{res_b.state_updates}"
        )
        assert res_b.state_updates.get("router.stop_reason", "") == ""

    def test_timeout_override_applies_then_resets(self) -> None:
        """Agent A 注入 timeout_seconds=0 使其立即超时终止；随后 Agent B（无覆盖、
        同实例、不同管道）不得继承该 0 阈值——默认 600s 下刚启动的管道应放行。"""
        mod = _load_plugin_module()
        plugin = mod.StopCheckPlugin(config={})

        state_a = {"pipeline_id": "pipe-a", "iteration": 1, "timeout_seconds": 0, "task.id": ""}
        res_a = _run(plugin.execute(_ctx(state_a)))
        assert res_a.route_signal is not None, "Agent A 注入阈值 0 应立即命中超时终止"
        assert res_a.state_updates.get("router.stop_reason") == "timeout"

        # Agent B 无覆盖 → 默认 600s；新管道重置计时起点后 elapsed 近零，
        # 残留的 0 阈值会让它同样立即误判超时。
        state_b = {"pipeline_id": "pipe-b", "iteration": 1, "task.id": ""}
        res_b = _run(plugin.execute(_ctx(state_b)))
        assert res_b.route_signal is None, (
            "Agent B 无超时覆盖时不得继承 Agent A 注入的 timeout_seconds=0"
        )


class TestTaskStatusQueryChannel:
    """任务实时终态检测：state 聚合读面（0.2 单一真值，无 YAML 兜底）。"""

    def test_terminal_status_via_state_aggregation_ends(self, monkeypatch: Any) -> None:
        """聚合行 task.status=completed → 管道收到 end 信号而不是静默放过。

        外部终态写入（task_evaluate 经 pipeline-state.update）对运行中循环
        内存态不可见——聚合读面是唯一实时来源（用户裁定：任务终态当轮停止）。"""
        mod = _load_plugin_module()

        async def fake_reader():
            return [
                {"pipeline_id": "p", "task.id": "p", "task.status": "completed"},
            ]

        monkeypatch.setattr(mod, "_state_reader", fake_reader)
        plugin = mod.StopCheckPlugin(config={})
        state = {"pipeline_id": "p", "iteration": 6, "task.id": "p"}
        res = _run(plugin.execute(_ctx(state)))
        assert res.route_signal is not None, "聚合读到 completed 终态应收束管道"
        assert res.route_signal.route_type == "end"

    def test_failed_status_ends_and_running_passes(self, monkeypatch: Any) -> None:
        """failed 同样当轮收束；running 非终态不拦截。"""
        mod = _load_plugin_module()
        plugin = mod.StopCheckPlugin(config={})
        state = {"pipeline_id": "p", "iteration": 6, "task.id": "p"}

        async def failed_reader():
            return [{"pipeline_id": "p", "task.id": "p", "task.status": "failed"}]

        monkeypatch.setattr(mod, "_state_reader", failed_reader)
        res = _run(plugin.execute(_ctx(state)))
        assert res.route_signal is not None and res.route_signal.route_type == "end"

        async def running_reader():
            return [{"pipeline_id": "p", "task.id": "p", "task.status": "running"}]

        monkeypatch.setattr(mod, "_state_reader", running_reader)
        res = _run(plugin.execute(_ctx(state)))
        assert res.route_signal is None, "running 非终态，不应收束"

    def test_state_read_failure_warns_and_passes(
        self, monkeypatch: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """聚合读取抛错 → 记 warning（与正常轮次区分可见），本轮不放行也不崩。"""
        mod = _load_plugin_module()
        plugin = mod.StopCheckPlugin(config={})
        state = {"pipeline_id": "p", "iteration": 6, "task.id": "t-1"}

        async def _boom():
            raise RuntimeError("state bridge down")

        monkeypatch.setattr(mod, "_state_reader", _boom)

        with caplog.at_level("WARNING"):
            res = _run(plugin.execute(_ctx(state)))

        assert res.route_signal is None, "读取失败不应伪造终态信号"
        warnings = [
            r for r in caplog.records
            if r.levelname == "WARNING" and "stop_check" in r.name
        ]
        assert warnings, "任务状态读取失败必须以 warning 级别留下可见痕迹"


class TestLimitHitMarksTaskFailed:
    """超线（迭代上限/超时）= 任务失败（完成唯一判据是 task_evaluate 评估通过）。"""

    def test_max_iterations_marks_task_failed_for_task_pipeline(self) -> None:
        mod = _load_plugin_module()
        plugin = mod.StopCheckPlugin(config={})
        state = {"pipeline_id": "p", "iteration": 99, "max_iterations": 3, "task.id": "t1"}
        res = _run(plugin.execute(_ctx(state)))
        assert res.route_signal is not None
        assert res.state_updates.get("router.stop_reason") == "max_iterations"
        assert res.state_updates.get("task.status") == "failed"

    def test_timeout_marks_task_failed_for_task_pipeline(self) -> None:
        mod = _load_plugin_module()
        plugin = mod.StopCheckPlugin(config={})
        state = {"pipeline_id": "p", "iteration": 1, "timeout_seconds": 0, "task.id": "t1"}
        res = _run(plugin.execute(_ctx(state)))
        assert res.state_updates.get("router.stop_reason") == "timeout"
        assert res.state_updates.get("task.status") == "failed"

    def test_max_iterations_chat_pipeline_gets_no_task_status(self) -> None:
        """无 task 上下文的聊天管道超线终止：不写 task.*（防幽灵任务标记）。"""
        mod = _load_plugin_module()
        plugin = mod.StopCheckPlugin(config={})
        state = {"pipeline_id": "chat", "iteration": 99, "max_iterations": 3, "task.id": ""}
        res = _run(plugin.execute(_ctx(state)))
        assert res.state_updates.get("router.stop_reason") == "max_iterations"
        assert "task.status" not in res.state_updates
