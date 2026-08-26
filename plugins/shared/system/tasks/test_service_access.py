# @feature: FP-0.2.二 内部模块统一 manifest 化 | @vision: V3 可嵌入 | @ci: python-coverage
"""service_access.get_task_service 单例行为测试。

- 首次调用懒加载 TaskService 并缓存（进程内单例）；
- 缓存命中后返回同一实例（不重复初始化）；
- 初始化失败返回 None 并留 warning 日志。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent

_EVICT_NAMES = (
    "task_types",
    "state_machine",
    "storage",
    "service",
    "timer_manager",
    "agents_types",
    "enum_utils",
    "workspace",
    "service_access",
    "_task_cleanup",
    "_task_crud",
    "_task_state",
    "server",
    "http_api",
)


@pytest.fixture(autouse=True)
def _isolate_tasks_plugin_modules():
    """裸名逐出 + 代际还原（同 test_tasks_plugin.py，串扰防线）。"""
    d = str(_PLUGIN_DIR)
    was_present = d in sys.path
    if d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)
    evicted: dict[str, ModuleType] = {}
    for m in _EVICT_NAMES:
        if m in sys.modules:
            evicted[m] = sys.modules.pop(m)
    yield
    if d in sys.path:
        sys.path.remove(d)
    if was_present:
        sys.path.insert(0, d)
    for m in _EVICT_NAMES:
        if m in evicted:
            sys.modules[m] = evicted[m]
        else:
            sys.modules.pop(m, None)


class TestGetTaskService:
    def test_lazy_singleton(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import service_access

        monkeypatch.setattr(service_access, "_task_service_instance", None)
        first = service_access.get_task_service()
        assert first is not None
        second = service_access.get_task_service()
        assert second is first  # 缓存命中同一实例

    def test_init_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import service_access

        monkeypatch.setattr(service_access, "_task_service_instance", None)

        def boom() -> Any:
            raise RuntimeError("init failed")

        monkeypatch.setitem(sys.modules, "service", type("S", (), {"TaskService": boom})())
        assert service_access.get_task_service() is None
