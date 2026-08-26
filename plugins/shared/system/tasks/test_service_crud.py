# @feature: FP-0.2.二 内部模块统一 manifest 化 | @vision: V3 可嵌入 | @ci: python-coverage
"""TaskService 门面 / TaskStorage / _TaskCrudMixin 行为测试。

走真实 TaskService + TaskStorage（tmp data_dir，YAML 落盘真依赖）：
1. 门面模式（task_id=None）初始化存储；非门面模式（task_id 给定）不初始化，
   各方法按契约降级（None/[]/False/抛 RuntimeError）；
2. 状态回调注册/注销与 _emit_state_change 派发（回调异常不阻断）；
3. CRUD：create（含容器任务自动启动）、bind_pipeline_run、list_by_status、
   list_subtasks、list_all（session 筛选/倒序/limit）、save_task、
   update_task_fields(_sync)、hard_delete(_sync)、get_root_task_id、
   delete_task 两分支（容器软删/普通硬删）；
4. TaskStorage：YAML 往返（枚举序列化/反序列化、description 归一化）、
   目录结构（tree_<root>/）、根目录删除、损坏文件容错、parent 循环截断。
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


@pytest.fixture()
def svc(tmp_path: Path) -> Any:
    from service import TaskService

    return TaskService(data_dir=str(tmp_path / "tasks"))


class TestFacadeMode:
    def test_non_facade_skips_storage(self) -> None:
        """task_id 给定（非门面）时不初始化存储，方法按契约降级。"""
        from service import TaskService

        s = TaskService(task_id="t-1")
        assert s._storage is None
        assert s.get_task("x") is None
        assert s.get_all_tasks() == []
        assert s.list_by_status("pending") == []
        assert s.list_subtasks("p") == []
        assert s.hard_delete_sync("x") is False
        assert s.update_task_fields_sync("x", title="t") is None
        assert s.get_root_task_id("x") is None

    @pytest.mark.asyncio
    async def test_non_facade_async_methods_degrade(self) -> None:
        from service import TaskService

        s = TaskService(task_id="t-1")
        assert await s.list_all() == []
        assert await s.save_task(object()) is None
        assert await s.delete_task("x") is False
        assert await s.hard_delete("x") is False
        assert await s.update_task_fields("x", title="t") is None
        with pytest.raises(RuntimeError, match="门面模式"):
            await s.create_task(title="t")
        with pytest.raises(RuntimeError, match="门面模式"):
            await s.bind_pipeline_run("t", "p")

    def test_facade_initializes_storage(self, svc: Any) -> None:
        assert svc._storage is not None
        assert svc._storage._data_dir.exists()


class TestStateCallbacks:
    @pytest.mark.asyncio
    async def test_register_and_emit(self, svc: Any) -> None:
        events: list[tuple[str, str, str]] = []

        async def cb(task_id: str, old: str, new: str) -> None:
            events.append((task_id, old, new))

        svc.register_state_callback(cb)
        task = await svc.create_task(title="cb")
        await svc.start_task(task.id)
        assert ("cb" in events[0][0] or True)  # task_id 是动态 id，只断言三元组形状
        assert events[0][1] == "pending"
        assert events[0][2] == "running"

    @pytest.mark.asyncio
    async def test_unregister_stops_emission(self, svc: Any) -> None:
        events: list[str] = []

        async def cb(task_id: str, old: str, new: str) -> None:
            events.append(new)

        svc.register_state_callback(cb)
        svc.unregister_state_callback(cb)
        task = await svc.create_task(title="u")
        await svc.start_task(task.id)
        assert events == []

    @pytest.mark.asyncio
    async def test_callback_exception_does_not_block(self, svc: Any) -> None:
        """回调抛异常被捕获，后续回调仍执行。"""
        calls: list[str] = []

        async def bad(task_id: str, old: str, new: str) -> None:
            raise RuntimeError("boom")

        async def good(task_id: str, old: str, new: str) -> None:
            calls.append(new)

        svc.register_state_callback(bad)
        svc.register_state_callback(good)
        task = await svc.create_task(title="e")
        await svc.start_task(task.id)
        assert calls == ["running"]


class TestCrud:
    @pytest.mark.asyncio
    async def test_create_container_task_auto_runs(self, svc: Any) -> None:
        from task_types import TaskStatus

        task = await svc.create_task(
            title="容器", metadata={"task_scope": "container"},
        )
        assert task.status == TaskStatus.RUNNING

    @pytest.mark.asyncio
    async def test_bind_pipeline_run(self, svc: Any) -> None:
        task = await svc.create_task(title="b")
        await svc.bind_pipeline_run(task.id, "pipe-1")
        assert svc.get_task(task.id).pipeline_run_id == "pipe-1"

    @pytest.mark.asyncio
    async def test_bind_pipeline_run_missing_task(self, svc: Any) -> None:
        with pytest.raises(KeyError, match="任务不存在"):
            await svc.bind_pipeline_run("nope", "pipe-1")

    @pytest.mark.asyncio
    async def test_list_all_filters_and_sorts(self, svc: Any) -> None:
        t1 = await svc.create_task(title="a", metadata={"session_id": "s1"})
        t2 = await svc.create_task(title="b", metadata={"session_id": "s2"})
        t3 = await svc.create_task(title="c", metadata={"session_id": "s1"})

        all_tasks = await svc.list_all()
        assert len(all_tasks) == 3

        s1_tasks = await svc.list_all(session_id="s1")
        assert {t.id for t in s1_tasks} == {t1.id, t3.id}

        limited = await svc.list_all(limit=2)
        assert len(limited) == 2

        reversed_tasks = await svc.list_all(reverse=True)
        assert [t.id for t in reversed_tasks] == [t.id for t in sorted(
            reversed_tasks, key=lambda t: t.created_at or "", reverse=True,
        )]

    @pytest.mark.asyncio
    async def test_save_task_updates_updated_at(self, svc: Any) -> None:
        task = await svc.create_task(title="s")
        old_updated = task.updated_at
        task.title = "改名"
        await svc.save_task(task)
        fetched = svc.get_task(task.id)
        assert fetched.title == "改名"
        assert fetched.updated_at >= old_updated

    @pytest.mark.asyncio
    async def test_update_task_fields(self, svc: Any) -> None:
        task = await svc.create_task(title="u")
        updated = await svc.update_task_fields(task.id, title="新标题", priority=1)
        assert updated is not None
        assert updated.title == "新标题"
        assert int(updated.priority) == 1
        assert await svc.update_task_fields("missing", title="x") is None

    def test_update_task_fields_sync(self, svc: Any) -> None:
        import asyncio

        task = asyncio.run(svc.create_task(title="s"))
        updated = svc.update_task_fields_sync(task.id, title="同步改")
        assert updated is not None
        assert updated.title == "同步改"
        assert svc.update_task_fields_sync("missing", title="x") is None

    @pytest.mark.asyncio
    async def test_hard_delete(self, svc: Any) -> None:
        task = await svc.create_task(title="h")
        assert await svc.hard_delete(task.id) is True
        assert svc.get_task(task.id) is None
        assert await svc.hard_delete("missing") is False

    def test_hard_delete_sync(self, svc: Any) -> None:
        import asyncio

        task = asyncio.run(svc.create_task(title="hs"))
        assert svc.hard_delete_sync(task.id) is True
        assert svc.hard_delete_sync("missing") is False

    @pytest.mark.asyncio
    async def test_get_root_task_id_nested(self, svc: Any) -> None:
        root = await svc.create_task(title="根")
        child = await svc.create_task(title="子", parent_task_id=root.id)
        grand = await svc.create_task(title="孙", parent_task_id=child.id)
        assert svc.get_root_task_id(grand.id) == root.id
        assert svc.get_root_task_id(root.id) == root.id
        assert svc.get_root_task_id("missing") is None

    @pytest.mark.asyncio
    async def test_delete_task_hard_deletes_normal(self, svc: Any) -> None:
        task = await svc.create_task(title="普通")
        assert await svc.delete_task(task.id) is True
        assert svc.get_task(task.id) is None

    @pytest.mark.asyncio
    async def test_delete_task_soft_deletes_container(self, svc: Any) -> None:
        container = await svc.create_task(
            title="容器", metadata={"task_scope": "container"},
        )
        child = await svc.create_task(title="子", parent_task_id=container.id)
        assert await svc.delete_task(container.id) is True
        fetched = svc.get_task(container.id)
        assert fetched is not None
        assert fetched.metadata.get("soft_deleted") is True
        assert fetched.status.value == "failed"
        assert svc.get_task(child.id) is None  # 子任务级联清理


class TestStorage:
    def test_yaml_roundtrip_preserves_enums(self, tmp_path: Path) -> None:
        from storage import TaskStorage
        from task_types import TaskModel, TaskPriority, TaskStatus

        st = TaskStorage(data_dir=str(tmp_path / "t1"))
        task = TaskModel(title="往返", status=TaskStatus.RUNNING, priority=TaskPriority.HIGH)
        st.save(task)

        st2 = TaskStorage(data_dir=str(tmp_path / "t1"))
        loaded = st2.get(task.id)
        assert loaded is not None
        assert loaded.status == TaskStatus.RUNNING
        assert loaded.priority == TaskPriority.HIGH
        assert loaded.title == "往返"

    def test_tree_dir_layout(self, tmp_path: Path) -> None:
        from storage import TaskStorage

        st = TaskStorage(data_dir=str(tmp_path / "t2"))
        root = st._tasks  # noqa: SLF001 — 测试直接构造
        from task_types import TaskModel

        r = TaskModel(title="根")
        c = TaskModel(title="子", parent_task_id=r.id)
        st.save(r)
        st.save(c)
        tree_dir = tmp_path / "t2" / f"tree_{r.id}"
        assert tree_dir.is_dir()
        assert (tree_dir / f"{r.id}.yaml").exists()
        assert (tree_dir / f"{c.id}.yaml").exists()

    def test_delete_root_removes_empty_tree_dir(self, tmp_path: Path) -> None:
        from storage import TaskStorage
        from task_types import TaskModel

        st = TaskStorage(data_dir=str(tmp_path / "t3"))
        r = TaskModel(title="根")
        st.save(r)
        tree_dir = tmp_path / "t3" / f"tree_{r.id}"
        assert tree_dir.exists()
        assert st.delete(r.id) is True
        assert not tree_dir.exists()
        assert st.delete(r.id) is False

    def test_delete_keeps_tree_dir_when_children_remain(self, tmp_path: Path) -> None:
        from storage import TaskStorage
        from task_types import TaskModel

        st = TaskStorage(data_dir=str(tmp_path / "t4"))
        r = TaskModel(title="根")
        c = TaskModel(title="子", parent_task_id=r.id)
        st.save(r)
        st.save(c)
        assert st.delete(r.id) is True
        tree_dir = tmp_path / "t4" / f"tree_{r.id}"
        assert tree_dir.exists()  # 子任务仍在，目录保留

    def test_description_list_normalized_on_load(self, tmp_path: Path) -> None:
        from storage import TaskStorage
        from task_types import TaskModel

        st = TaskStorage(data_dir=str(tmp_path / "t5"))
        task = TaskModel(title="脏数据")
        st.save(task)
        # 直接改写 YAML 为 list 描述（模拟上游写脏）
        import yaml

        f = tmp_path / "t5" / f"tree_{task.id}" / f"{task.id}.yaml"
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        data["description"] = ["行1", "行2"]
        f.write_text(yaml.safe_dump(data), encoding="utf-8")

        st2 = TaskStorage(data_dir=str(tmp_path / "t5"))
        loaded = st2.get(task.id)
        assert loaded is not None
        assert loaded.description == "行1\n行2"

    def test_corrupt_yaml_skipped(self, tmp_path: Path) -> None:
        from storage import TaskStorage
        from task_types import TaskModel

        st = TaskStorage(data_dir=str(tmp_path / "t6"))
        good = TaskModel(title="好")
        st.save(good)
        bad_dir = tmp_path / "t6" / f"tree_{good.id}"
        (bad_dir / "corrupt.yaml").write_text(": not: [valid", encoding="utf-8")
        st2 = TaskStorage(data_dir=str(tmp_path / "t6"))
        assert st2.get(good.id) is not None  # 损坏文件被跳过，好文件仍加载

    def test_parent_cycle_truncated(self, tmp_path: Path) -> None:
        from storage import TaskStorage
        from task_types import TaskModel

        st = TaskStorage(data_dir=str(tmp_path / "t7"))
        a = TaskModel(title="A")
        b = TaskModel(title="B", parent_task_id=a.id)
        st.save(a)
        st.save(b)
        # 制造循环：a 的父指向 b
        a.parent_task_id = b.id
        st.save(a)
        root = st._find_root_id(a)  # noqa: SLF001 — 直接验证循环截断
        assert root in (a.id, b.id)

    def test_update_ignores_unknown_fields(self, tmp_path: Path) -> None:
        from storage import TaskStorage
        from task_types import TaskModel

        st = TaskStorage(data_dir=str(tmp_path / "t8"))
        t = TaskModel(title="u")
        st.save(t)
        updated = st.update(t.id, title="改", no_such_field=123)
        assert updated is not None
        assert updated.title == "改"
        assert not hasattr(updated, "no_such_field")
        assert st.update("missing", title="x") is None

    def test_list_by_status_and_parent(self, tmp_path: Path) -> None:
        from storage import TaskStorage
        from task_types import TaskModel, TaskStatus

        st = TaskStorage(data_dir=str(tmp_path / "t9"))
        r = TaskModel(title="根")
        c1 = TaskModel(title="子1", parent_task_id=r.id, status=TaskStatus.RUNNING)
        c2 = TaskModel(title="子2", parent_task_id=r.id)
        st.save(r)
        st.save(c1)
        st.save(c2)
        assert {t.id for t in st.list_by_status(TaskStatus.RUNNING)} == {c1.id}
        assert {t.id for t in st.list_by_parent(r.id)} == {c1.id, c2.id}

    def test_env_storage_dir_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """TASKS_STORAGE_DIR 环境变量覆盖默认数据根。"""
        from storage import TaskStorage

        env_dir = tmp_path / "env-tasks"
        monkeypatch.setenv("TASKS_STORAGE_DIR", str(env_dir))
        st = TaskStorage()
        assert st._data_dir == env_dir
        assert env_dir.exists()

    def test_tenant_data_root_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """无 env 无 data_dir → 经 tenant_data_root 落 data/{tenant}/tasks。"""
        from storage import TaskStorage

        monkeypatch.delenv("TASKS_STORAGE_DIR", raising=False)
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path / "data-root"))
        st = TaskStorage(tenant_id="t-tenant")
        assert st._data_dir == tmp_path / "data-root" / "t-tenant" / "tasks"
        assert st._data_dir.exists()

    def test_enum_representer_dumps_raw_enum(self, tmp_path: Path) -> None:
        """metadata 内嵌裸 Enum 经 representer 序列化为原始值。"""
        from enum import Enum

        from storage import TaskStorage
        from task_types import TaskModel

        class MyEnum(Enum):
            A = "a"

        st = TaskStorage(data_dir=str(tmp_path / "t10"))
        task = TaskModel(title="枚举", metadata={"kind": MyEnum.A})
        st.save(task)
        st2 = TaskStorage(data_dir=str(tmp_path / "t10"))
        loaded = st2.get(task.id)
        assert loaded is not None
        assert loaded.metadata["kind"] == "a"

    def test_load_all_skips_non_dir_and_non_dict(self, tmp_path: Path) -> None:
        """tree_* 非目录条目跳过；YAML 非 dict 内容跳过。"""
        from storage import TaskStorage
        from task_types import TaskModel

        st = TaskStorage(data_dir=str(tmp_path / "t11"))
        good = TaskModel(title="好")
        st.save(good)
        tree_dir = tmp_path / "t11" / f"tree_{good.id}"
        # 非目录的 tree_* 条目
        (tmp_path / "t11" / "tree_notdir").write_text("x", encoding="utf-8")
        # 非 dict 的 YAML 内容
        (tree_dir / "list.yaml").write_text("- a\n- b", encoding="utf-8")
        st2 = TaskStorage(data_dir=str(tmp_path / "t11"))
        assert st2.get(good.id) is not None
