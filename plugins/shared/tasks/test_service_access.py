# @feature: FP-0.2.二 内部模块统一 manifest 化 | @vision: V3 可嵌入 | @ci: python-coverage
"""service_access.get_task_service 单例行为测试。

- 首次调用懒加载 TaskService 并缓存（进程内单例）；
- 缓存命中后返回同一实例（不重复初始化）；
- 初始化失败返回 None 并留 warning 日志；
- 裸名 `service` 槽位被同槽成员驻留污染（无/异物 TaskService）不影响
  解析（tasks.service 限定导入，M3 防误解析契约）。
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
    # 限定名（tasks 命名空间包）：与裸名同权逐出/还原，保证 tasks.service
    # 每例从当前 sys.path 形态新鲜解析，不残留其他测试的模块代际。
    "tasks",
    "tasks.service",
    "tasks.service_access",
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

        monkeypatch.setattr(service_access, "_task_service_instances", {})
        monkeypatch.setattr(service_access, "_injected_task_service", None)
        first = service_access.get_task_service()
        assert first is not None
        second = service_access.get_task_service()
        assert second is first  # 缓存命中同一实例

    def test_init_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import service_access

        monkeypatch.setattr(service_access, "_task_service_instances", {})
        monkeypatch.setattr(service_access, "_injected_task_service", None)

        def boom() -> Any:
            raise RuntimeError("init failed")

        monkeypatch.setitem(sys.modules, "tasks.service", type("S", (), {"TaskService": boom})())
        assert service_access.get_task_service() is None

    def test_flat_service_slot_pollution_does_not_break_resolution(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """裸名 `service` 槽位被同槽合宿成员驻留时解析不受污染（M3 限定导入）。

        复现合宿静息态形态：同槽首占成员（security_check/service.py）的模块
        常驻 sys.modules['service'] 且无 TaskService——给桩位塞异物哨兵类，
        断言 get_task_service 拿到的实例来自 tasks.service 而非裸名槽位；
        撤污后同构解析（两布局结果一致，性质断言）。
        """
        import service_access

        monkeypatch.setattr(service_access, "_task_service_instances", {})
        monkeypatch.setattr(service_access, "_injected_task_service", None)

        polluted = ModuleType("service")
        polluted.TaskService = object  # 异物哨兵：命中裸名槽位即解析出 object
        monkeypatch.setitem(sys.modules, "service", polluted)

        svc = service_access.get_task_service()
        assert svc is not None
        from tasks.service import TaskService  # noqa: PLC0415

        assert svc.__class__ is TaskService  # 非 object 哨兵 → 解析源是 tasks.service

        # 性质：污染/不污染两布局解析出同一实现类。
        monkeypatch.delitem(sys.modules, "service")
        monkeypatch.setattr(service_access, "_task_service_instances", {})
        svc_clean = service_access.get_task_service()
        assert svc_clean is not None
        assert svc_clean.__class__ is TaskService

    def test_injected_instance_wins_over_lazy_construction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """正门注入（tasks server.py on_load）优先：同进程消费方拿到的
        是带插件配置的front-door实例，而非零参自建克隆。"""
        import service_access

        monkeypatch.setattr(service_access, "_task_service_instances", {})
        monkeypatch.setattr(service_access, "_injected_task_service", None)

        sentinel = object()
        service_access.set_task_service(sentinel)
        try:
            assert service_access.get_task_service() is sentinel
        finally:
            service_access.set_task_service(None)
        # 注入清除后回到懒构建通道（返回 TaskService 实例，非哨兵）
        fallback = service_access.get_task_service()
        assert fallback is not None
        assert fallback is not sentinel

    def test_reset_singletons_clears_injected_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import service_access

        monkeypatch.setattr(service_access, "_task_service_instances", {})
        monkeypatch.setattr(service_access, "_injected_task_service", None)

        sentinel = object()
        service_access.set_task_service(sentinel)
        service_access.reset_singletons()
        assert service_access.get_task_service() is not sentinel
