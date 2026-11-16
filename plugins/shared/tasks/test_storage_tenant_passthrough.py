# @feature: FP-0.2.一 反向调用租户透传（刀1b 直调延伸） | @vision: V4 多租户 | @ci: python-test
"""tasks 直调面租户透传测试（刀1b，清偿计划 §一刀1 延伸）。

task_submit/task_evaluate 对 tasks 存储的写入是进程内直调（shared 平铺模块
import，非 capability 反调）——刀1 的「反调回带 _tenant_id → 桥消费」契约
覆盖不到它。直调面的取租户点改为「显式形参 > SDK 身份 contextvar > default
回退」（contextvar 原头 = 刀1 `_identity` 下发，SDK server 调用边界捕获）。

覆盖（验收四条 + 实例选路）：
- maintenance 身份 contextvar 下建任务 → 文件落 {base}/maintenance/tasks/
- 无身份 context → default 回退不变（回归）
- 同租户 context 下 list/get 能读到直写件（读取对称）
- 跨租户不可见（maintenance context 读不到 default 直写件，反向亦然）
- get_task_service 按租户分化实例（同租户缓存命中，跨租户不串根）
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import ModuleType

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
    "workspace",
    "service_access",
    "_task_cleanup",
    "_task_crud",
    "_task_state",
    "tasks",
    "tasks.service",
    "tasks.service_access",
)


@pytest.fixture(autouse=True)
def _isolate_tasks_plugin_modules():
    """裸名逐出 + 代际还原（同 test_service_access.py，串扰防线）。"""
    d = str(_PLUGIN_DIR)
    was_present = d in sys.path
    if d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)
    evicted: dict[str, ModuleType] = {}
    for m in _EVICT_NAMES:
        if m in sys.modules:
            evicted[m] = sys.modules.pop(m)
    import service_access  # noqa: PLC0415 — 头部自举注入 shared/group/plugin 三级路径

    assert service_access  # 导入即完成路径装配
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


@pytest.fixture(autouse=True)
def _reset_service_singletons():
    """跨租户实例缓存清零（测试隔离，防同进程其他用例残留）。"""
    import service_access

    service_access.reset_singletons()
    yield
    service_access.reset_singletons()


def _bound(tenant: str):
    """绑定内核下发形态的身份（模拟刀1 _identity 捕获后的 contextvar 态）。"""
    from agentos_plugin_sdk.capability import bind_reverse_call_identity

    return bind_reverse_call_identity({"tenant_id": tenant, "session_id": f"s-{tenant}"})


def _reset_bound(token) -> None:
    from agentos_plugin_sdk.capability import reset_reverse_call_identity

    reset_reverse_call_identity(token)


def _create_task(title: str):
    from tasks.service import TaskService

    svc = TaskService()
    return svc, asyncio.run(svc.create_task(title=title))


class TestTenantContextvarRouting:
    def test_tenant_contextvar_routes_writes_to_tenant_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """验收①：maintenance 身份 contextvar 下建任务 → 文件落
        {base}/maintenance/tasks/（修前恒落 default）。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        token = _bound("maintenance")
        try:
            _, task = _create_task("直调租户写")
        finally:
            _reset_bound(token)

        landed = list((tmp_path / "maintenance" / "tasks").glob(f"tree_*/{task.id}.yaml"))
        assert landed, "任务文件必须落 maintenance 租户数据根"
        assert not (tmp_path / "default").exists(), "有身份调用不得污染 default 根"

    def test_no_identity_falls_back_to_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """验收②（回归）：无身份 context → default 回退不变。"""
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        _, task = _create_task("无身份回退")

        landed = list((tmp_path / "default" / "tasks").glob(f"tree_*/{task.id}.yaml"))
        assert landed, "无身份必须回退 default 根（现状语义）"
        assert not (tmp_path / "maintenance").exists(), "无身份不得凭空造租户根"

    def test_explicit_tenant_param_beats_contextvar(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """取租户序：显式形参 > contextvar（形参优先契约）。"""
        from storage import TaskStorage

        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        token = _bound("maintenance")
        try:
            storage = TaskStorage(tenant_id="other_tenant")
        finally:
            _reset_bound(token)
        assert storage._data_dir == tmp_path / "other_tenant" / "tasks"


class TestReadSymmetry:
    def test_same_tenant_context_reads_direct_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """验收③（读取对称）：同租户 context 下新实例 list/get 读到直写件。"""
        from tasks.service import TaskService

        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        token = _bound("maintenance")
        try:
            _, written = _create_task("同租户读对称")
            reader = TaskService()
        finally:
            _reset_bound(token)

        assert reader.get_task(written.id) is not None, "同租户 context 必须能读到直写件"
        assert any(t.id == written.id for t in reader.snapshot_tasks())

    def test_cross_tenant_invisible(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """验收④：跨租户不可见——maintenance context 读不到 default 直写件，
        default context 也读不到 maintenance 直写件（双向）。"""
        from tasks.service import TaskService

        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))
        # default 直写件
        _, default_task = _create_task("default 件")

        token = _bound("maintenance")
        try:
            _, maint_task = _create_task("maintenance 件")
            reader_under_maintenance = TaskService()
        finally:
            _reset_bound(token)

        assert reader_under_maintenance.get_task(default_task.id) is None, (
            "maintenance context 不得读到 default 直写件"
        )
        assert all(t.id != default_task.id for t in reader_under_maintenance.snapshot_tasks())
        assert reader_under_maintenance.get_task(maint_task.id) is not None

        # 反向：default context 读不到 maintenance 直写件
        reader_under_default = TaskService()
        assert reader_under_default.get_task(maint_task.id) is None, (
            "default context 不得读到 maintenance 直写件"
        )
        assert reader_under_default.get_task(default_task.id) is not None


class TestServiceAccessPerTenant:
    def test_get_task_service_instances_split_by_tenant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_task_service 懒构建单例按租户分化：同租户缓存命中同一实例，
        跨租户各归各的数据根（合宿 light 组多租户共进程的正确性前提）。"""
        import service_access

        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path))

        svc_default = service_access.get_task_service()
        assert svc_default is not None

        token = _bound("maintenance")
        try:
            svc_maint = service_access.get_task_service()
            svc_maint_again = service_access.get_task_service()
        finally:
            _reset_bound(token)

        assert svc_maint is not svc_default, "跨租户不得共享同一存储实例"
        assert svc_maint_again is svc_maint, "同租户缓存命中同一实例"
        assert str(svc_maint.storage._data_dir) == str(tmp_path / "maintenance" / "tasks")
        assert str(svc_default.storage._data_dir) == str(tmp_path / "default" / "tasks")
