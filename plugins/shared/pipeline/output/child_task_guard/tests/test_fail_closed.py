# @feature: FP-0.2.〇 任务执行驱动 | @vision: V3 可嵌入 | @ci: python-coverage
"""child_task_guard fail-closed 测试：查询异常上抛，不按「无活跃子任务」放行。

回退路径（state 读面未注入）走旧 task_service 查询——查询异常必须上抛阻断
管道步（对齐 security_check 的 fail-closed 纪律），吞掉异常按「无活跃子任务」
放行会让父管道在仍有活跃子任务时提前终止。
"""

from __future__ import annotations

import importlib.util
import sys
import types as _types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_PIPELINE_ROOT = Path(__file__).resolve().parents[4]  # plugins/shared
_TASKS_DIR = _PIPELINE_ROOT / "system" / "tasks"

for _d in [_PLUGIN_DIR, _PIPELINE_ROOT, _TASKS_DIR]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load_plugin() -> Any:
    mod_name = "child_task_guard_fail_closed_test"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod(monkeypatch: pytest.MonkeyPatch) -> Any:
    module = _load_plugin()
    module.set_state_reader(None)  # 强制走旧 task_service 回退路径
    # 真实 task_types.py 映射为 tasks.types（插件 import 硬编码 tasks.types，
    # 真实包在 0.2 平铺布局下不存在）；monkeypatch 登记还原防串扰。
    # 模块须先入 sys.modules 再 exec（task_types 含字符串注解的 dataclass，
    # dataclasses 解析时按模块名查 sys.modules）。
    spec = importlib.util.spec_from_file_location(
        "child_task_guard_fc_tasks_types", _TASKS_DIR / "task_types.py"
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
    return module


class _ServiceCtx:
    """最小 PluginContext 替身：get_service 固定返回注入的 task_service。"""

    def __init__(self, service: Any, state: dict[str, Any] | None = None) -> None:
        self.state: dict[str, Any] = state if state is not None else {"pipeline_id": "parent_pipe"}
        self._service = service

    def get_service(self, name: str) -> Any:
        if name == "task_service":
            return self._service
        raise KeyError(name)


class _BoomService:
    """查询即故障的 task_service 桩（DB 不可达等）。"""

    def list_by_status(self, status: Any) -> list:
        raise RuntimeError("db down")

    def list_subtasks(self, task_id: str) -> list:
        raise RuntimeError("db down")


class _OkService:
    """正常返回的 task_service 桩。"""

    def __init__(self, tasks_by_status: list, subtasks: list) -> None:
        self._tasks_by_status = tasks_by_status
        self._subtasks = subtasks

    def list_by_status(self, status: Any) -> list:
        return self._tasks_by_status

    def list_subtasks(self, task_id: str) -> list:
        return self._subtasks


class _T:
    def __init__(self, tid: str, parent_pipeline_id: str | None = None, status: Any = "running") -> None:
        self.id = tid
        self.parent_pipeline_id = parent_pipeline_id
        self.status = status


class TestQueryFailureFailsClosed:
    async def test_list_by_status_error_raises(self, mod: Any) -> None:
        """list_by_status 查询异常 → 上抛，不按「无活跃子任务」放行。"""
        guard = mod.ChildTaskGuard(config={})
        ctx = _ServiceCtx(_BoomService())
        with pytest.raises(RuntimeError, match="db down"):
            await guard._get_active_children("parent_pipe", None, ctx)
        with pytest.raises(RuntimeError, match="db down"):
            await guard.execute(ctx)

    async def test_list_subtasks_error_raises(self, mod: Any) -> None:
        """list_subtasks 查询异常 → 上抛（task_id 回退分支同纪律）。"""
        guard = mod.ChildTaskGuard(config={})
        ctx = _ServiceCtx(_BoomService())
        with pytest.raises(RuntimeError, match="db down"):
            await guard._get_active_children("", "task_1", ctx)


class TestNormalPathUnchanged:
    async def test_active_children_found(self, mod: Any) -> None:
        service = _OkService([_T("c1", parent_pipeline_id="parent_pipe")], [])
        guard = mod.ChildTaskGuard(config={})
        has_active, ids = await guard._get_active_children("parent_pipe", None, _ServiceCtx(service))
        assert has_active is True
        assert ids == ["c1"]

    async def test_no_children_returns_false(self, mod: Any) -> None:
        service = _OkService([], [])
        guard = mod.ChildTaskGuard(config={})
        has_active, ids = await guard._get_active_children("parent_pipe", "task_1", _ServiceCtx(service))
        assert has_active is False
        assert ids == []

    async def test_subtask_active_status_detected(self, mod: Any) -> None:
        service = _OkService([], [_T("st1", status="pending")])
        guard = mod.ChildTaskGuard(config={})
        has_active, ids = await guard._get_active_children("", "task_1", _ServiceCtx(service))
        assert has_active is True
        assert ids == ["st1"]
