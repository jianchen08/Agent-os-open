# @feature: FP-0.2.〇 任务执行驱动 | @vision: V3 可嵌入 | @ci: python-coverage
"""child_task_guard 子任务活跃判定 TDD 测试（GAP-1 统一：读 state 聚合）。

主路径：活跃子任务 = state 聚合行中 lineage.parent_pipeline_id == 当前管道
且 task.status ∈ {pending, running, evaluating}（与 TaskStatus 枚举对齐）——不再依赖
task_service（YAML 只读镜像）；读面未注入时回退旧路径。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_PIPELINE_ROOT = Path(__file__).resolve().parents[3]  # plugins/shared

for _d in [_PLUGIN_DIR, _PIPELINE_ROOT, _PIPELINE_ROOT / "system" / "tasks"]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load_plugin() -> Any:
    mod_name = "child_task_guard_unify_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[mod_name]
        raise
    return module


@pytest.fixture
def mod() -> Any:
    return _load_module_clean()


def _load_module_clean() -> Any:
    mod_name = "child_task_guard_unify_test"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class _Ctx:
    """最小 PluginContext 替身（get_service 恒抛 KeyError → 回退路径不可达）。"""

    def get_service(self, name: str) -> Any:
        raise KeyError(name)


class TestStateAggregationRead:
    async def test_active_children_from_state_rows(self, mod: Any) -> None:
        """主路径：lineage.parent_pipeline_id 匹配 + task.status 活跃 → 判定挂起。"""
        rows = [
            {"pipeline_id": "child_running", "task.status": "running", "lineage.parent_pipeline_id": "parent_pipe"},
            {"pipeline_id": "child_pending", "task.status": "pending", "lineage.parent_pipeline_id": "parent_pipe"},
            {"pipeline_id": "child_done", "task.status": "completed", "lineage.parent_pipeline_id": "parent_pipe"},
            {"pipeline_id": "other_parent", "task.status": "running", "lineage.parent_pipeline_id": "other_pipe"},
            {"pipeline_id": "no_lineage", "task.status": "running"},
        ]
        mod.set_state_reader(lambda: rows)
        guard = mod.ChildTaskGuard(config={})
        try:
            has_active, ids = await guard._get_active_children("parent_pipe", None, _Ctx())
            assert has_active is True
            assert set(ids) == {"child_running", "child_pending"}, f"completed/异父/无血缘不计入: {ids}"
        finally:
            mod._state_reader = None

    async def test_no_active_children_returns_false(self, mod: Any) -> None:
        """无活跃子任务 → (False, [])，不挂起。"""
        rows = [
            {"pipeline_id": "c1", "task.status": "completed", "lineage.parent_pipeline_id": "parent_pipe"},
        ]
        mod.set_state_reader(lambda: rows)
        guard = mod.ChildTaskGuard(config={})
        try:
            has_active, ids = await guard._get_active_children("parent_pipe", None, _Ctx())
            assert (has_active, ids) == (False, [])
        finally:
            mod._state_reader = None

    async def test_fallback_when_reader_missing(self, mod: Any) -> None:
        """读面未注入 → 回退旧 task_service 路径（不崩）。"""
        guard = mod.ChildTaskGuard(config={})
        guard._get_task_service = lambda ctx: None  # type: ignore[method-assign]
        has_active, ids = await guard._get_active_children("parent_pipe", None, _Ctx())
        assert (has_active, ids) == (False, [])

    def test_state_reader_injection_point(self, mod: Any) -> None:
        """server.py on_load 注入点存在。"""
        assert hasattr(mod, "set_state_reader")
        assert mod._get_state_reader() is None
