# @feature: task_service MCP 适配层 | @vision: V3 可嵌入 | @ci: python-coverage
"""tasks/server.py 适配层行为测试——工具面委托与生命周期。

走真实 TaskService（tmp data_dir，YAML 落盘真依赖）：
1. 生命周期：on_load 前取服务抛 RuntimeError；on_load 按 config.data_dir
   初始化；on_unload 置空；
2. task.create / task.get 往返与字段形状；
3. task.transition 合法迁移链（start/pause/resume/fail/complete_evaluation）
   与未知 action 拒绝；
4. task.list 三形态（全量/按状态/按父任务）；
5. task.cancel 级联与仅暂停两分支；
6. task.delete、task.get_transitions。

模块隔离：与 test_tasks_plugin.py 同范式（裸名逐出 + 代际还原），
见该文件 fixture 注释。
"""
from __future__ import annotations

import asyncio
import importlib.util
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
def srv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """加载 server.py 并以 tmp data_dir 完成 on_load。"""
    spec = importlib.util.spec_from_file_location(
        "tasks_server_adapter_test", str(_PLUGIN_DIR / "server.py")
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tasks_server_adapter_test"] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.plugin, "get_config", lambda: {"data_dir": str(tmp_path / "tasks")})
    asyncio.run(mod._on_load({}))
    yield mod
    asyncio.run(mod._on_unload({}))


class TestLifecycle:
    def test_get_service_before_load_raises(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "tasks_server_adapter_cold", str(_PLUGIN_DIR / "server.py")
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["tasks_server_adapter_cold"] = mod
        spec.loader.exec_module(mod)
        with pytest.raises(RuntimeError, match="not initialized"):
            mod._get_service()

    def test_on_load_initializes_and_unload_clears(self, srv: Any) -> None:
        assert srv._get_service() is not None
        asyncio.run(srv._on_unload({}))
        with pytest.raises(RuntimeError):
            srv._get_service()


class TestCreateGet:
    def test_create_returns_shape_and_get_roundtrip(self, srv: Any) -> None:
        created = asyncio.run(
            srv.task_create(title="写报告", description="周报", priority=3)
        )
        assert created["status"] == "pending"
        assert created["title"] == "写报告"
        got = asyncio.run(srv.task_get(created["id"]))
        assert got is not None
        assert got["id"] == created["id"]
        assert got["priority"] == 3
        assert got["description"] == "周报"
        assert got["parent_task_id"] is None

    def test_get_missing_returns_none(self, srv: Any) -> None:
        assert asyncio.run(srv.task_get("no-such-task")) is None


class TestTransition:
    def test_legal_chain_start_pause_resume_fail(self, srv: Any) -> None:
        t = asyncio.run(srv.task_create(title="t"))
        tid = t["id"]

        r = asyncio.run(srv.task_transition(tid, "start"))
        assert r == {"ok": True, "status": "running", "task_id": tid}

        r = asyncio.run(srv.task_transition(tid, "pause"))
        assert r["status"] == "stopped"

        r = asyncio.run(srv.task_transition(tid, "resume"))
        assert r["status"] == "running"

        r = asyncio.run(srv.task_transition(tid, "fail", reason="超时"))
        assert r["status"] == "failed"

    def test_complete_evaluation_passed_completes(self, srv: Any) -> None:
        t = asyncio.run(srv.task_create(title="e"))
        r = asyncio.run(
            srv.task_transition(
                t["id"], "complete_evaluation", passed=True, result={"score": 90}
            )
        )
        assert r["ok"] is True
        assert r["status"] == "completed"
        got = asyncio.run(srv.task_get(t["id"]))
        assert got["result"] == {"score": 90}

    def test_unknown_action_rejected(self, srv: Any) -> None:
        t = asyncio.run(srv.task_create(title="u"))
        r = asyncio.run(srv.task_transition(t["id"], "teleport"))
        assert r["ok"] is False
        assert "Unknown action" in r["error"]


class TestList:
    def test_list_all_and_by_status(self, srv: Any) -> None:
        a = asyncio.run(srv.task_create(title="a"))
        asyncio.run(srv.task_create(title="b"))
        all_list = asyncio.run(srv.task_list())
        assert all_list["total"] == 2

        asyncio.run(srv.task_transition(a["id"], "start"))
        running = asyncio.run(srv.task_list(status="running"))
        assert running["total"] == 1
        assert running["tasks"][0]["title"] == "a"
        assert running["tasks"][0]["status"] == "running"

    def test_list_subtasks_by_parent(self, srv: Any) -> None:
        parent = asyncio.run(srv.task_create(title="p"))
        asyncio.run(srv.task_create(title="c1", parent_task_id=parent["id"]))
        asyncio.run(srv.task_create(title="c2", parent_task_id=parent["id"]))
        subs = asyncio.run(srv.task_list(parent_task_id=parent["id"]))
        assert subs["total"] == 2
        assert {t["title"] for t in subs["tasks"]} == {"c1", "c2"}


class TestCancelDelete:
    def test_cancel_cascade_counts_children(self, srv: Any) -> None:
        parent = asyncio.run(srv.task_create(title="p"))
        asyncio.run(srv.task_create(title="c1", parent_task_id=parent["id"]))
        c2 = asyncio.run(srv.task_create(title="c2", parent_task_id=parent["id"]))
        r = asyncio.run(srv.task_cancel(parent["id"], reason="不需要了"))
        # 返回值为被级联取消的子任务数（父不计入）
        assert r == {"cancelled": 2, "task_id": parent["id"]}
        got = asyncio.run(srv.task_get(c2["id"]))
        assert got["status"] == "stopped"

    def test_cancel_without_cascade_pauses_only(self, srv: Any) -> None:
        t = asyncio.run(srv.task_create(title="solo"))
        asyncio.run(srv.task_transition(t["id"], "start"))
        r = asyncio.run(srv.task_cancel(t["id"], cascade=False))
        assert r == {"cancelled": 0, "task_id": t["id"]}
        got = asyncio.run(srv.task_get(t["id"]))
        assert got["status"] == "stopped"

    def test_delete_removes_task(self, srv: Any) -> None:
        t = asyncio.run(srv.task_create(title="d"))
        r = asyncio.run(srv.task_delete(t["id"]))
        assert r == {"deleted": True, "task_id": t["id"]}
        assert asyncio.run(srv.task_get(t["id"])) is None


class TestTransitions:
    def test_pending_transitions_listed(self, srv: Any) -> None:
        t = asyncio.run(srv.task_create(title="tr"))
        r = asyncio.run(srv.task_get_transitions(t["id"]))
        assert r["task_id"] == t["id"]
        assert "running" in r["transitions"]
        assert "completed" in r["transitions"]
