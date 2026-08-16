# @ci: python-coverage
"""四个 Input 插件 get_service 失败路径测试（punch W-A6-lite）。

pause_guard / task_event_receiver / cost_control / isolation_guard 通过
ctx.get_service("task_service") 跨插件取服务；未接线（KeyError）时各自降级
（不暂停 / 不订阅 / 回退预算 / 空 metadata）。本文件断言：
1. 降级返回值语义保持不变；
2. 失败路径打 logger.warning 且**低频**（同一实例只打一次）。

跨插件服务接线本身属后续架构任务（各 plugin 文件头已登记）。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_INPUT_DIR = Path(__file__).resolve().parent
_SHARED_DIR = str(_INPUT_DIR.parents[1])  # plugins/shared/

_LOADERS = {
    "pause_guard": _INPUT_DIR / "pause_guard" / "plugin.py",
    "task_event_receiver": _INPUT_DIR / "task_event_receiver" / "plugin.py",
    "cost_control": _INPUT_DIR / "cost_control" / "plugin.py",
    "isolation_guard": _INPUT_DIR / "isolation_guard" / "plugin.py",
}


def _load(plugin_name: str) -> Any:
    """动态加载插件 plugin.py（连同其目录与 shared 根加入 sys.path）。"""
    plugin_dir = _LOADERS[plugin_name].parent
    for p in (str(plugin_dir), _SHARED_DIR):
        if p not in sys.path:
            sys.path.insert(0, p)
    mod_name = f"{plugin_name}_svc_warn_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _LOADERS[plugin_name])
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _ctx_without_service(state: dict[str, Any] | None = None) -> SimpleNamespace:
    """get_service 恒抛 KeyError 的最小 PluginContext 替身。"""

    def _get_service(_name: str) -> Any:
        raise KeyError(_name)

    return SimpleNamespace(state=state or {}, get_service=_get_service)


class TestPauseGuardServiceUnavailable:
    @pytest.mark.asyncio
    async def test_degrades_to_not_paused_with_low_freq_warning(self, caplog) -> None:
        mod = _load("pause_guard")
        plugin = mod.PauseGuardPlugin()
        ctx = _ctx_without_service({"task_id": "t-1"})
        with caplog.at_level(logging.WARNING):
            first = await plugin._do_work(ctx)
            second = await plugin._do_work(ctx)
        # 降级语义不变：不暂停 + 标注服务不可用
        assert first == {"pause_guard.checked": {"paused": False, "reason": "task_service unavailable"}}
        assert second["pause_guard.checked"]["paused"] is False
        # 低频：两次调用只打一条 warning
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "task_service 未接线" in r.message]
        assert len(warnings) == 1


class TestTaskEventReceiverServiceUnavailable:
    def test_subscribe_noop_with_low_freq_warning(self, caplog) -> None:
        mod = _load("task_event_receiver")
        plugin = mod.TaskEventReceiverPlugin()
        ctx = _ctx_without_service({"task_id": "t-1"})
        with caplog.at_level(logging.WARNING):
            plugin._try_subscribe(ctx)
            plugin._try_subscribe(ctx)
        # 降级语义不变：不订阅、无服务句柄
        assert plugin._subscribed is False
        assert plugin._task_service is None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "task_service 未接线" in r.message]
        assert len(warnings) == 1


class TestCostControlServiceUnavailable:
    def test_budget_falls_back_with_low_freq_warning(self, caplog) -> None:
        mod = _load("cost_control")
        plugin = mod.CostControlPlugin(config={"default_budget": 12345})
        ctx = _ctx_without_service({"task_id": "t-1", "cost_control.budget": 999})
        with caplog.at_level(logging.WARNING):
            assert plugin._resolve_budget(ctx) == 999  # 来源 2 回退
            ctx2 = _ctx_without_service({"task_id": "t-1"})
            assert plugin._resolve_budget(ctx2) == 12345  # 来源 3 回退
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "task_service 未接线" in r.message]
        assert len(warnings) == 1

    def test_invalid_metadata_budget_still_silent_fallback(self) -> None:
        """metadata token_budget 非法值回退默认（ValueError/TypeError 不打服务告警）。"""
        mod = _load("cost_control")
        plugin = mod.CostControlPlugin(config={"default_budget": 777})
        svc = SimpleNamespace(get_task=lambda _tid: SimpleNamespace(metadata={"token_budget": "not-a-number"}))

        def _get_service(name: str) -> Any:
            if name == "task_service":
                return svc
            raise KeyError(name)

        ctx = SimpleNamespace(state={"task_id": "t-1"}, get_service=_get_service)
        assert plugin._resolve_budget(ctx) == 777


class TestIsolationGuardServiceUnavailable:
    def test_metadata_degrades_to_empty_with_low_freq_warning(self, caplog) -> None:
        mod = _load("isolation_guard")
        plugin = mod.IsolationGuard()
        ctx = _ctx_without_service({"task_id": "t-1"})
        with caplog.at_level(logging.WARNING):
            assert plugin._get_task_metadata(ctx) == {}
            assert plugin._get_task_metadata(ctx) == {}
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "task_service 未接线" in r.message]
        assert len(warnings) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
