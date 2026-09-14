# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""tasks 系统插件族缺口补测（行锚定 2026-09-14 插桩车道 coverage.xml）。

靶单与行为契约（断输入→输出/副作用，不钉实现）：
- service_access：plugins/shared 缺席时 import 期自举回 sys.path；
  get_project_registry 懒加载单例（二次调用命中缓存不重建）与初始化失败降级
  （返回 None + warning，不向消费方抛出）。
- storage：plugins/shared 缺席时 import 期自举；无存储根实例（_data_dir
  缺省）的读/写防御面按 docstring 契约安全降级（返回 None、不落盘）。
- events：上下文用量遥测脏值——窗口非数值形态 → 空 dict + warning 留痕；
  窗口可解析但非正 → 空 dict（通知侧按无遥测处理）。
- task_types.create_task：agent_level 字符串入参归一为 AgentLevel 枚举
  （与 priority int 归一互为对照）。
- _task_cleanup._cascade_cleanup_subtasks：后代枚举与逐个读取之间的并发删除
  （真实 storage 变更）→ 跳过该后代，不阻断其余后代清理。
- http_api：plugins/shared 缺席时 import 期自举；create_root_task 的
  acceptance_criteria 字段透传 task_submit 工具入参。
- reconcile.reconcile_startup：能力读面形状违约的收敛——state 读面返回非
  列表、runs 读面含非 dict 行、run 行无 pipeline_id、任务行无对应候选 run
  一律跳过（不裁不派发）；调和派发途中抛错 → warning 留痕并续扫后续行。

外部依赖替身边界：pipeline-executor / service-registry / tool-executor 均为
跨进程内核能力句柄（按外部依赖替身）；隔离管理器（Docker 面）按既有
test_state_cleanup.py 同款在边界替身。

结构性不可达（逐条说明，勿硬凑）：
1. storage.py:212（_persist_task 的 ``file_path is None`` 早退）——同函数
   206-207 行已按 ``not self._data_dir`` 提前返回，而 ``_get_task_file_path``
   仅在 ``_get_tree_dir`` 返回 None（同一条件的等价形态）时才返回 None：
   两个检查互斥，212 行在任何可达状态下都不可能命中（防御冗余，非缺陷）。
2. storage.py 的"无 data_dir"面（96/167/182/196/207 行）经公开 __init__
   不可达——``Path(resolved)`` 恒为真（pathlib.Path 无 __bool__/__len__，
   ``bool(Path('.')) is True``），resolved 由显式参数/env/多租户根三选一
   必得非空路径；本文件以直接构造无存储根实例钉住 docstring 声明的降级
   契约（分支语义正确），不声称经公开构造可达。
3. tasks/server.py:91-93（on_load 的 cleanup 能力降级 except KeyError）——
   FrontendEmitter.from_plugin 吞掉全部异常返回 None、
   set_cleanup_capabilities 是纯全局赋值，无 KeyError 触发路径
   （既有 test_server_gaps_tasks.py docstring 同口径）。
4. tools/task/tool.py:1392（resume 成功的 execution_warning 回填）——
   紧邻的 ``execution_warning = None`` 之后无任何赋值，条件恒假（死分支），
   见 test_task_tool_gaps.py docstring。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_HERE = Path(__file__).resolve().parent
_SHARED_ROOT = _HERE.parent.parent  # plugins/shared（project_registry / tenant_data 所在）

# 平铺裸名逐出名单：其它插件目录存在同名模块（service / storage / server /
# http_api 等），不逐出会把本插件解析到别家实现。
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
    "events",
    "reconcile",
    "project_registry",
    "state_fields",
)


@pytest.fixture(autouse=True)
def _isolate_tasks_plugin_modules():
    """裸名逐出 + 代际还原（与 test_state_cleanup.py / test_reconcile.py 同款）。"""
    d = str(_HERE)
    shared = str(_SHARED_ROOT)
    was_present = d in sys.path
    added_shared = False
    if d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)
    if shared not in sys.path:
        sys.path.insert(1, shared)
        added_shared = True
    evicted: dict[str, ModuleType] = {}
    for m in _EVICT_NAMES:
        if m in sys.modules:
            evicted[m] = sys.modules.pop(m)
    yield
    if d in sys.path:
        sys.path.remove(d)
    if was_present:
        sys.path.insert(0, d)
    if added_shared:
        sys.path.remove(shared)
    for m in _EVICT_NAMES:
        if m in evicted:
            sys.modules[m] = evicted[m]
        else:
            sys.modules.pop(m, None)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path or "."))


# ═══════════════════════════════════════════════════════════
# 共享层自举（service_access:29 / storage:32 / http_api:39）
# ═══════════════════════════════════════════════════════════


class TestSharedRootBootstrap:
    """plugins/shared 不在 sys.path 时，模块 import 期自行插入（裸名共享件可达）。"""

    @pytest.mark.parametrize("module_name", ["service_access", "storage", "http_api"])
    def test_module_bootstraps_shared_root(self, module_name: str) -> None:
        """自举行真执行：删路径 + 逐出模块 → 重导入后路径回到 sys.path 且换新代际。

        先正常导入一次预热依赖缓存（http_api 顶部的 state_fields 裸名导入在
        自举行之前，依赖模块缓存解析）。
        """
        import importlib

        importlib.import_module(module_name)

        shared = _norm(str(_SHARED_ROOT))
        saved_path = list(sys.path)
        saved_mod = sys.modules[module_name]
        removed = [p for p in sys.path if _norm(p) == shared]
        for p in removed:
            sys.path.remove(p)
        try:
            sys.modules.pop(module_name, None)
            mod = importlib.import_module(module_name)
            assert any(_norm(p) == shared for p in sys.path), "自举行须把 plugins/shared 插回 sys.path"
            assert mod is not saved_mod, "重导入须产生新代际（证明 import 期自举行真执行）"
        finally:
            sys.path[:] = saved_path
            sys.modules[module_name] = saved_mod


# ═══════════════════════════════════════════════════════════
# service_access：项目登记簿单例
# ═══════════════════════════════════════════════════════════


class TestGetProjectRegistry:
    def test_lazy_singleton_caches_instance(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """首次调用懒构建并缓存；二次调用返回同一实例（不重复初始化）。"""
        import service_access

        monkeypatch.setattr(service_access, "_project_registry_instance", None)
        monkeypatch.setenv("TASKS_STORAGE_DIR", str(tmp_path / "tasks"))
        first = service_access.get_project_registry()
        assert first is not None
        assert service_access.get_project_registry() is first

    def test_init_failure_returns_none_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """构建抛错 → 返回 None（消费方降级），warning 留痕不静默。"""
        import service_access

        monkeypatch.setattr(service_access, "_project_registry_instance", None)

        def boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("registry init failed")

        fake = ModuleType("project_registry")
        fake.ProjectRegistry = boom  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "project_registry", fake)

        with caplog.at_level(logging.WARNING):
            assert service_access.get_project_registry() is None
        assert any("ProjectRegistry 初始化失败" in r.getMessage() for r in caplog.records)
        assert service_access._project_registry_instance is None, "失败不得留下半初始化缓存"


# ═══════════════════════════════════════════════════════════
# storage：无存储根防御面
# ═══════════════════════════════════════════════════════════


class TestStorageWithoutDataRoot:
    """docstring 契约：无 data_dir → 路径解析返回 None、持久化不落盘（见文件头说明 2）。"""

    @pytest.fixture
    def rootless(self, tmp_path: Path) -> Any:
        import storage as storage_mod

        st = storage_mod.TaskStorage(data_dir=tmp_path / "tasks")
        st._data_dir = None  # 直接构造"无存储根"状态（公开构造不可达，见 docstring）
        return st

    def test_load_all_is_noop(self, rootless: Any) -> None:
        rootless._load_all()
        assert rootless.list_all() == []

    def test_tree_dir_returns_none(self, rootless: Any) -> None:
        assert rootless._get_tree_dir("root-1") is None

    def test_task_file_path_returns_none(self, rootless: Any) -> None:
        assert rootless._get_task_file_path("root-1", "t-1") is None

    def test_ensure_tree_dir_returns_none(self, rootless: Any, tmp_path: Path) -> None:
        assert rootless._ensure_tree_dir("root-1") is None
        # 不落盘性质：磁盘上不出现 tree_* 目录
        assert not list((tmp_path / "tasks").glob("tree_*"))

    def test_persist_is_noop_but_memory_cache_updates(self, rootless: Any, tmp_path: Path) -> None:
        from task_types import TaskModel

        task = TaskModel(id="mem-only", title="只进内存")
        rootless.save(task)
        assert rootless.get("mem-only") is task
        assert not list((tmp_path / "tasks").glob("tree_*")), "无存储根时不得创建任何目录"

    def test_delete_without_root_does_not_raise(self, rootless: Any) -> None:
        from task_types import TaskModel

        rootless.save(TaskModel(id="mem-del", title="删除"))
        assert rootless.delete("mem-del") is True
        assert rootless.get("mem-del") is None


class TestFindRootWithMissingParent:
    """父任务记录缺席（跨租户/已清理）→ 回溯截断在当前任务，不无限上溯也不炸。"""

    def test_missing_parent_breaks_walk_and_uses_current_id(self, tmp_path: Path) -> None:
        import storage as storage_mod
        from task_types import TaskModel

        st = storage_mod.TaskStorage(data_dir=tmp_path / "tasks")
        orphan = TaskModel(id="orphan-1", title="父不在", parent_task_id="ghost-parent")
        st.save(orphan)

        assert st._find_root_id(orphan) == "orphan-1"
        # 删除仍按自身为根：文件与内存记录都被清掉
        assert st.delete("orphan-1") is True
        assert st.get("orphan-1") is None

    def test_present_parent_chain_still_resolves_to_top(self, tmp_path: Path) -> None:
        """对照：父链完整时回溯到真根（证明截断仅在缺父时发生）。"""
        import storage as storage_mod
        from task_types import TaskModel

        st = storage_mod.TaskStorage(data_dir=tmp_path / "tasks")
        root = TaskModel(id="root-1", title="根")
        mid = TaskModel(id="mid-1", title="中", parent_task_id="root-1")
        leaf = TaskModel(id="leaf-1", title="叶", parent_task_id="mid-1")
        for t in (root, mid, leaf):
            st.save(t)

        assert st._find_root_id(leaf) == "root-1"


# ═══════════════════════════════════════════════════════════
# events：上下文用量遥测脏值
# ═══════════════════════════════════════════════════════════


class TestContextUsageDirtyTelemetry:
    @pytest.mark.parametrize("bad_window", ["abc", ["x"]])
    def test_unparseable_window_degrades_with_warning(
        self, bad_window: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """窗口非数值形态（ValueError / TypeError 两路）→ 空 dict + warning 留痕。"""
        import events

        row = {"track.llm_usage": {"last_input_tokens": 5}, "context_window": bad_window}
        with caplog.at_level(logging.WARNING):
            assert events._context_usage(row) == {}
        assert any("上下文用量解析失败" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("window", [-5, "-3"])
    def test_non_positive_window_returns_empty(self, window: Any) -> None:
        """窗口可解析但非正（int 与数字串两路）→ 空 dict（按无遥测处理，不产生负占用）。"""
        import events

        row = {"track.llm_usage": {"last_input_tokens": 5}, "context_window": window}
        assert events._context_usage(row) == {}

    def test_positive_window_reports_occupancy(self) -> None:
        """对照性质断言：正窗口按 last_input_tokens/window 折算占用（0<pct<=100）。"""
        import events

        usage = events._context_usage(
            {"track.llm_usage": {"last_input_tokens": 25_600}, "context_window": 128_000}
        )
        assert usage["input_tokens"] == 25_600
        assert usage["context_window"] == 128_000
        assert 0 < usage["pct"] <= 100


# ═══════════════════════════════════════════════════════════
# task_types.create_task：枚举归一
# ═══════════════════════════════════════════════════════════


class TestCreateTaskFactoryNormalization:
    @pytest.mark.parametrize("raw_level", ["L2", "L3"])
    def test_string_agent_level_normalized_to_enum(self, raw_level: str) -> None:
        from agents_types import AgentLevel
        from task_types import create_task

        task = create_task(title="归一", agent_level=raw_level)
        assert isinstance(task.agent_level, AgentLevel)
        assert task.agent_level.value == raw_level

    def test_enum_input_passthrough(self) -> None:
        from agents_types import AgentLevel
        from task_types import create_task

        enum_level = AgentLevel.L1_MAIN
        assert create_task(title="原样", agent_level=enum_level).agent_level is enum_level


# ═══════════════════════════════════════════════════════════
# _task_cleanup：级联清理与并发删除相遇
# ═══════════════════════════════════════════════════════════


class TestCascadeCleanupConcurrentDeletion:
    async def test_descendant_deleted_between_enumeration_and_read_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """后代枚举与逐个读取之间被并发删除 → 跳过该后代，其余后代照常清理。

        并发删除以真实 storage 变更模拟（注入的 pipeline-executor 假件在处理
        第一个后代期间经公开 API 删除第二个后代——跨进程能力是外部依赖替身，
        storage 是真实依赖）。隔离容器面按既有测试同款在边界替身。
        """
        import _task_cleanup
        from service import TaskService

        svc = TaskService(data_dir=str(tmp_path))
        parent = await svc.create_task(title="父")
        first = await svc.create_task(title="先清理", parent_task_id=parent.id)
        second = await svc.create_task(title="并发被删", parent_task_id=parent.id)
        await svc.bind_pipeline_run(first.id, "pipe-first")

        prev_caps = (_task_cleanup._pipeline_executor, _task_cleanup._frontend_emitter)

        async def executor(params: dict[str, Any]) -> dict[str, Any]:
            if params.get("method") == "delete_pipeline" and svc.get_task(second.id) is not None:
                svc.hard_delete_sync(second.id)  # 并发删除：真实 storage 记录移除
            return {"ok": True}

        async def _cleanup_resources(_task_id: str) -> dict[str, Any]:
            return {"container_destroyed": False, "errors": []}

        try:
            _task_cleanup.set_cleanup_capabilities(executor, None)
            monkeypatch.setattr(svc, "_cleanup_task_resources", _cleanup_resources)
            stats = await svc._cascade_cleanup_subtasks(parent.id)
        finally:
            _task_cleanup.set_cleanup_capabilities(*prev_caps)

        assert stats["subtasks_deleted"] == 1, "只清理仍存在的后代（被删者跳过不重复计数）"
        assert stats["pipeline_files_cleaned"] == 1
        assert stats["errors"] == []
        assert svc.get_task(second.id) is None


# ═══════════════════════════════════════════════════════════
# reconcile：读面形状违约与派发故障
# ═══════════════════════════════════════════════════════════


class _FakeCapability:
    """fake 内核能力句柄：按 method 返回预置响应（未预置 → KeyError 语义）。"""

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        self.calls.append((method, params))
        if method not in self._responses:
            raise KeyError(f"unexpected capability method: {method}")
        resp = self._responses[method]
        if isinstance(resp, Exception):
            raise resp
        return resp


def _run_row(status: str, pid: str, created_at: str = "2026-09-06T08:00:00Z") -> dict[str, Any]:
    return {
        "run_id": f"run-{pid}-{status}",
        "status": status,
        "created_at": created_at,
        "pipeline_id": pid,
    }


class TestReconcileShapeViolations:
    async def test_non_list_state_rows_short_circuit(self) -> None:
        """state 读面返回非列表 → 不裁不派发（形状违约即放弃本轮）。"""
        import reconcile

        state = _FakeCapability({"list": {"rows": []}})
        runs = _FakeCapability({"pipeline-runs.list": [_run_row("failed", "p")]})
        bus = _FakeCapability({"emit_domain": {"ok": True}})

        assert await reconcile.reconcile_startup(state, runs, bus) == []
        assert [c for c in bus.calls if c[0] == "emit_domain"] == []

    async def test_non_dict_runs_and_missing_pid_are_skipped(self) -> None:
        """runs 读面含非 dict 行 / run 行无 pipeline_id → 跳过，不影响同批合法 run。"""
        import reconcile

        task_row = {"pipeline_id": "pipe-ok", "task.status": "running"}
        state = _FakeCapability({"list": [task_row], "update": {"ok": True}})
        runs = _FakeCapability(
            {
                "pipeline-runs.list": [
                    "junk-run",
                    {"run_id": "r-nopid", "status": "failed", "created_at": "2026-09-06T09:00:00Z"},
                    _run_row("failed", "pipe-ok"),
                ]
            }
        )
        bus = _FakeCapability({"emit_domain": {"ok": True}})

        reconciled = await reconcile.reconcile_startup(state, runs, bus)
        assert [r["pipeline_id"] for r in reconciled] == ["pipe-ok"]
        assert [c[1]["event"] for c in bus.calls] == ["task_failed"]

    async def test_task_rows_without_candidate_run_are_skipped(self) -> None:
        """任务行在 runs 候选中无对应 pipeline → 跳过（非分歧行，不裁）。"""
        import reconcile

        state = _FakeCapability(
            {
                "list": [
                    {"pipeline_id": "pipe-no-run", "task.status": "running"},
                    {"pipeline_id": "pipe-other", "task.status": "completed"},
                ]
            }
        )
        runs = _FakeCapability({"pipeline-runs.list": [_run_row("failed", "pipe-other")]})
        bus = _FakeCapability({"emit_domain": {"ok": True}})

        reconciled = await reconcile.reconcile_startup(state, runs, bus)
        assert [r["pipeline_id"] for r in reconciled] == ["pipe-other"]

    async def test_dispatch_failure_logs_and_continues_scan(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """调和派发途中抛错 → warning 留痕、该行不计数，续扫后续分歧行。

        触发面 = 能力读面返回的脏遥测对象（state 聚合行来自跨进程能力，
        形状违约以对象形态表达）：派生链取 `track.llm_usage` 时炸出非
        ValueError/TypeError 异常并原样上抛到调和面；同一批的第二行照常调和。
        """

        class _BrokenUsage(dict):
            def get(self, key: str, default: Any = None) -> Any:
                raise RuntimeError("telemetry object broken")

        import reconcile

        broken_row: dict[str, Any] = {
            "pipeline_id": "pipe-1",
            "task.status": "completed",
            "track.llm_usage": _BrokenUsage(),
            "context_window": 128000,
        }
        good_row = {"pipeline_id": "pipe-2", "task.status": "running"}
        state = _FakeCapability({"list": [broken_row, good_row], "update": {"ok": True}})
        runs = _FakeCapability(
            {"pipeline-runs.list": [_run_row("failed", "pipe-1"), _run_row("failed", "pipe-2")]}
        )
        bus = _FakeCapability({"emit_domain": {"ok": True}})

        with caplog.at_level(logging.WARNING):
            reconciled = await reconcile.reconcile_startup(state, runs, bus)

        assert [r["pipeline_id"] for r in reconciled] == ["pipe-2"]
        assert any("分歧行调和派发失败" in r.getMessage() for r in caplog.records)
        # 性质断言：失败行不产生任何 emit_domain（派发在派生阶段即中断）
        assert [c[1]["tags"]["pipeline_id"] for c in bus.calls] == ["pipe-2"]


# ═══════════════════════════════════════════════════════════
# http_api：根任务表单字段透传
# ═══════════════════════════════════════════════════════════


class _FakeToolExecutorHandle:
    """tool-executor 能力句柄替身：记录 invoke 参数并返回预置信封。"""

    def __init__(self, calls: list[dict[str, Any]], envelope: dict[str, Any]) -> None:
        self._calls = calls
        self._envelope = envelope

    async def call(self, method: str, params: dict[str, Any], timeout: Any = None) -> Any:
        assert method == "invoke"
        self._calls.append(params)
        return self._envelope


class TestCreateRootTaskAcceptanceCriteria:
    async def test_acceptance_criteria_passed_through_to_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """acceptance_criteria 非空 → 进 task_submit 工具入参（表单字段不静默丢弃）。"""
        import http_api

        calls: list[dict[str, Any]] = []
        handle = _FakeToolExecutorHandle(
            calls, {"success": True, "output": {"task_id": "rt-ac-1"}}
        )
        monkeypatch.setattr(http_api, "_capability", lambda _name: handle)

        criteria = {"items": [{"id": "m1", "desc": "跑通"}]}
        resp = await http_api.create_root_task(
            {
                "title": "根任务",
                "target_id": "agent-1",
                "thread_id": "thread-1",
                "acceptance_criteria": criteria,
            }
        )

        assert calls[0]["args"]["acceptance_criteria"] == criteria
        assert calls[0]["args"]["parent_agent_level"] == 1
        assert resp.id == "rt-ac-1"

    async def test_empty_acceptance_criteria_kept_out_of_args(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """对照：未传 acceptance_criteria → 不入工具入参。"""
        import http_api

        calls: list[dict[str, Any]] = []
        handle = _FakeToolExecutorHandle(
            calls, {"success": True, "output": {"task_id": "rt-ac-2"}}
        )
        monkeypatch.setattr(http_api, "_capability", lambda _name: handle)

        await http_api.create_root_task(
            {"title": "根任务", "target_id": "agent-1", "thread_id": "thread-1"}
        )
        assert "acceptance_criteria" not in calls[0]["args"]
