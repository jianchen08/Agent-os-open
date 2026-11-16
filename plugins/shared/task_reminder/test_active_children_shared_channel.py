# @feature: FP-0.2.〇 任务执行驱动 | @ci: python-coverage
"""「有没有活跃子任务」共享单通道回归（task_reminder 单点承载）。

判定通道 = ``_libs/pipeline_base/active_children.find_active_children``。
child_task_guard 已退役并入 task_reminder（ADR 2026-10-30：等待挂起职责
单点化，同一职责不拆两个插件），本文件由「两插件同判」改钉 task_reminder
一侧的通道契约：

1. 主判定 = state 聚合行 lineage.parent_pipeline_id + task.status；
2. ``submitted_task_ids`` 是加速标记而非独立真相——标记与 state 行不一致时
   结论以 lineage+status 为准（标记在场不得把已完结判成活跃，标记缺失不得
   把活跃判成无）；
3. 读面不可用时回退链（加速标记 → task_service 枚举，查询异常上抛不吞）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types as _types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[0]  # plugins/shared/
_TASKS_DIR = _SHARED / "tasks"  # 裸名 task_types（回退枚举）解析前提

for _d in (_DIR, _SHARED, _TASKS_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load(path: Path, mod_name: str) -> Any:
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


reminder_mod = _load(_DIR / "plugin.py", "shared_channel_reminder_plugin")

from _libs.pipeline_base.active_children import (  # noqa: E402
    active_child_ids_from_rows,
    find_active_children,
)


class _Ctx:
    """最小 PluginContext 替身（get_service 恒 KeyError → 服务回退不可达）。"""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state = state or {}

    def get_service(self, name: str) -> Any:
        raise KeyError(name)


_ACTIVE_ROWS = [
    {"pipeline_id": "child_a", "task.status": "running", "lineage.parent_pipeline_id": "parent_pipe"},
    {"pipeline_id": "child_b", "task.status": "pending", "lineage.parent_pipeline_id": "parent_pipe"},
    {"pipeline_id": "child_done", "task.status": "completed", "lineage.parent_pipeline_id": "parent_pipe"},
    {"pipeline_id": "other", "task.status": "running", "lineage.parent_pipeline_id": "elsewhere"},
]


class TestSameHelperObject:
    def test_plugin_imports_shared_channel(self) -> None:
        """插件引用共享 helper（单通道不分叉的机械前提）。

        以 (来源模块, qualname) 判同源而非函数对象 is——全车道共跑下
        `_libs` 包实例可能被邻居的 sys.modules 操作分裂成两份（跨实例对象
        is 必假红，plugins-coverage 满套件实测）；来源指纹才是「导入而非
        复刻」的稳定判定。
        """
        from _libs.pipeline_base.active_children import (
            find_active_children as _direct,
        )

        shared = reminder_mod.find_active_children
        assert shared.__module__ == _direct.__module__, "必须来自共享单通道模块"
        assert shared.__qualname__ == _direct.__qualname__, "不得是本地复刻"


class TestStateRowsVerdict:
    """组1/组2：state 行有/无活跃子任务——主判定（lineage+task.status）。"""

    async def test_active_children_rows_verdict(self) -> None:
        reminder_mod.set_state_reader(lambda: _ACTIVE_ROWS)
        try:
            has_reminder = await reminder_mod.TaskReminder(config={})._has_active_children(
                "parent-task", _Ctx({"pipeline_id": "parent_pipe"})
            )
            assert has_reminder is True
            # 通道级 id 全集：只认本管道（parent_pipe）的活跃行，跨管道/完结行不混入
            assert (
                active_child_ids_from_rows(_ACTIVE_ROWS, "parent_pipe")
                == ["child_a", "child_b"]
            )
            has_direct, ids_direct = await find_active_children(
                "parent_pipe", None, state_reader=lambda: _ACTIVE_ROWS
            )
            assert (has_direct, ids_direct) == (True, ["child_a", "child_b"])
        finally:
            reminder_mod.set_state_reader(None)

    async def test_no_children_rows_verdict(self) -> None:
        rows = [
            {"pipeline_id": "c", "task.status": "completed", "lineage.parent_pipeline_id": "parent_pipe"},
        ]
        reminder_mod.set_state_reader(lambda: rows)
        try:
            has_reminder = await reminder_mod.TaskReminder(config={})._has_active_children(
                "parent-task", _Ctx({"pipeline_id": "parent_pipe"})
            )
            assert has_reminder is False
            has_direct, ids_direct = await find_active_children(
                "parent_pipe", None, state_reader=lambda: rows
            )
            assert (has_direct, ids_direct) == (False, [])
        finally:
            reminder_mod.set_state_reader(None)


class TestMarkerNeverOverridesRows:
    """组3：state 标记与服务/行记录不一致 → 结论以 lineage+status 为准。"""

    async def test_stale_marker_with_completed_rows_concludes_none(self) -> None:
        """标记在场（陈旧）但行显示子任务已完结 → 不算活跃（行胜过标记）。"""
        rows = [
            {"pipeline_id": "child_a", "task.status": "completed", "lineage.parent_pipeline_id": "parent_pipe"},
        ]
        reminder_mod.set_state_reader(lambda: rows)
        try:
            has_reminder = await reminder_mod.TaskReminder(config={})._has_active_children(
                "parent-task",
                _Ctx({"pipeline_id": "parent_pipe", "submitted_task_ids": ["child_a"]}),
            )
            assert has_reminder is False
        finally:
            reminder_mod.set_state_reader(None)

    async def test_missing_marker_with_active_rows_concludes_active(self) -> None:
        """标记缺失但行显示活跃 → 判活跃（标记缺失不改变结论）。"""
        reminder_mod.set_state_reader(lambda: _ACTIVE_ROWS)
        try:
            has_reminder = await reminder_mod.TaskReminder(config={})._has_active_children(
                "parent-task", _Ctx({"pipeline_id": "parent_pipe"})
            )
            assert has_reminder is True
        finally:
            reminder_mod.set_state_reader(None)


class TestFallbackChain:
    """读面不可用：回退链（加速标记 → task_service 枚举）。"""

    async def test_marker_accelerates_without_service_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """读面未注入 + 标记在场 → 判活跃且不触服务（廉价前缀）。"""
        reminder_mod.set_state_reader(None)

        def _boom() -> list[dict[str, Any]]:
            raise AssertionError("标记在场时不得触达 task_service")

        monkeypatch.setitem(
            sys.modules,
            "tasks.service_access",
            _types.SimpleNamespace(get_task_service=_boom),
        )
        has_reminder = await reminder_mod.TaskReminder(config={})._has_active_children(
            "parent-task", _Ctx({"submitted_task_ids": ["child_x"]})
        )
        assert has_reminder is True

    async def test_service_records_used_when_no_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """读面未注入 + 无标记 → 服务枚举定论。"""
        reminder_mod.set_state_reader(None)

        calls: list[str] = []

        def _by_status(status: str) -> list[Any]:
            calls.append("by_status")
            return [
                _types.SimpleNamespace(id="child_s", parent_pipeline_id="parent_pipe", status=status)
            ]

        def _subtasks(tid: str) -> list[Any]:
            calls.append("subtasks")
            return []

        service = _types.SimpleNamespace(
            list_by_status=_by_status,
            list_subtasks=_subtasks,
        )
        monkeypatch.setitem(
            sys.modules,
            "tasks.service_access",
            _types.SimpleNamespace(get_task_service=lambda: service),
        )
        has_direct, ids_direct = await find_active_children("parent_pipe", None, ctx=_Ctx())
        assert (has_direct, ids_direct) == (True, ["child_s"])
        # 回退枚举 = 三状态扫描；task_id 缺席不触发子任务枚举
        assert calls.count("by_status") == 3 and "subtasks" not in calls
        has_reminder = await reminder_mod.TaskReminder(config={})._has_active_children(
            "parent-task", _Ctx({"pipeline_id": "parent_pipe"})
        )
        assert has_reminder is True

    async def test_reader_failure_degrades_to_same_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """读面异常 → 降级回退链（与未注入同判，不崩插件步）。"""
        def _bad() -> list[dict[str, Any]]:
            raise RuntimeError("bridge down")

        reminder_mod.set_state_reader(_bad)

        service = _types.SimpleNamespace(
            list_by_status=lambda status: [],
            list_subtasks=lambda tid: [],
        )
        monkeypatch.setitem(
            sys.modules,
            "tasks.service_access",
            _types.SimpleNamespace(get_task_service=lambda: service),
        )
        try:
            has_reminder = await reminder_mod.TaskReminder(config={})._has_active_children(
                "parent-task", _Ctx()
            )
            assert has_reminder is False
        finally:
            reminder_mod.set_state_reader(None)
