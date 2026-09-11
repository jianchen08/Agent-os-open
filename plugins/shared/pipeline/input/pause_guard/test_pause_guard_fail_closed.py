# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""pause_guard 暂停守卫测试：fail-closed 注入失败阻断 + 暂停判定路径。

- tasks.types 不可用（注入导入失败）→ 异常上抛阻断本步（fail-closed），
  禁止降级为「不暂停」放行；
- 任务 paused（stopped）→ suspended 信号正常产出；
- 任务活跃 → 不暂停；task_service 未接线 → 降级不暂停（契约不变）。

测试不依赖真实内核：直接加载 plugin.py，task_service 用假对象，
tasks.types 以唯一模块名加载真实 task_types.py 后经 sys.modules 映射
（插件 import 硬编码 tasks.types，真实包在 0.2 平铺布局下不存在）。
"""

from __future__ import annotations

import importlib.util
import sys
import types as _types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_PIPELINE_ROOT = Path(__file__).resolve().parents[3]  # plugins/shared
_TASKS_DIR = _PIPELINE_ROOT / "system" / "tasks"

for _d in [_PLUGIN_DIR, _PIPELINE_ROOT, _TASKS_DIR]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load_plugin() -> Any:
    mod_name = "pause_guard_plugin_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> Any:
    return _load_plugin()


@pytest.fixture
def tasks_types_wired(monkeypatch: pytest.MonkeyPatch) -> Any:
    """把真实 task_types.py 映射为 tasks.types（正常路径的解析前提）。

    用 monkeypatch 登记还原，避免污染同进程其他测试的 sys.modules。
    模块须先入 sys.modules 再 exec（task_types 含字符串注解的 dataclass，
    dataclasses 解析时按模块名查 sys.modules）。
    """
    spec = importlib.util.spec_from_file_location(
        "pause_guard_tasks_types_real", _TASKS_DIR / "task_types.py"
    )
    assert spec is not None
    assert spec.loader is not None
    real = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, str(spec.name), real)
    spec.loader.exec_module(real)
    pkg = _types.ModuleType("tasks")
    pkg.types = real
    monkeypatch.setitem(sys.modules, "tasks", pkg)
    monkeypatch.setitem(sys.modules, "tasks.types", real)
    return real


class _Ctx:
    """最小 PluginContext 替身（state + get_service）。"""

    def __init__(self, state: dict[str, Any], service: Any = None) -> None:
        self.state = state
        self._service = service

    def get_service(self, name: str) -> Any:
        if self._service is None:
            raise KeyError(name)
        return self._service


class _TaskService:
    def __init__(self, task: Any) -> None:
        self._task = task

    def get_task(self, task_id: str) -> Any:
        return self._task


class _Task:
    def __init__(self, status: Any) -> None:
        self.status = status


def _ctx_with_task(mod: Any, task: Any) -> _Ctx:
    return _Ctx(
        state={mod.StateKeys.TASK_ID: "t1"},
        service=_TaskService(task),
    )


class TestTasksTypesFailClosed:
    @pytest.mark.parametrize("broken", [None, _types.SimpleNamespace()])
    async def test_import_failure_blocks_step(self, mod: Any, monkeypatch: pytest.MonkeyPatch, broken: Any) -> None:
        """tasks.types 导入失败/缺 TaskStatus → 异常上抛，不降级放行。"""
        monkeypatch.setitem(sys.modules, "tasks.types", broken)
        ctx = _ctx_with_task(mod, _Task(status="running"))
        with pytest.raises(ImportError):
            await mod.PauseGuardPlugin(config={}).execute(ctx)


class TestPauseDecision:
    async def test_stopped_task_suspends(self, mod: Any, tasks_types_wired: Any) -> None:
        ctx = _ctx_with_task(mod, _Task(status=tasks_types_wired.TaskStatus.STOPPED))
        result = await mod.PauseGuardPlugin(config={}).execute(ctx)
        assert result.state_updates["suspended"] is True
        assert result.state_updates["pause_guard.checked"]["paused"] is True

    async def test_running_task_continues(self, mod: Any, tasks_types_wired: Any) -> None:
        ctx = _ctx_with_task(mod, _Task(status=tasks_types_wired.TaskStatus.RUNNING))
        result = await mod.PauseGuardPlugin(config={}).execute(ctx)
        assert result.state_updates["pause_guard.checked"]["paused"] is False
        assert "suspended" not in result.state_updates

    async def test_service_unwired_degrades_to_no_pause(self, mod: Any) -> None:
        """task_service 未接线（KeyError）→ 降级不暂停（既有契约不变）。"""
        ctx = _Ctx(state={mod.StateKeys.TASK_ID: "t1"})
        result = await mod.PauseGuardPlugin(config={}).execute(ctx)
        assert result.state_updates["pause_guard.checked"]["paused"] is False
        assert "suspended" not in result.state_updates
