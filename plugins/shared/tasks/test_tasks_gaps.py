# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""tasks 系统插件族缺口补测（行锚定 2026-09-14 插桩车道 coverage.xml）。

靶单与行为契约（断输入→输出/副作用，不钉实现）：
- service_access：plugins/shared 缺席时 import 期自举回 sys.path；
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
- http_api：恢复动作终态防线（幽灵续跑改判事故 2026-10-19 修复④B）——
  completed 拒绝派发显式失败、failed/timeout overlay 清失败注记、投影
  读面故障降级放行、其余状态现行为不变。
- reconcile.reconcile_startup：能力读面形状违约的收敛——state 读面返回非
  列表、runs 读面含非 dict 行、run 行无 pipeline_id、任务行无对应候选 run
  一律跳过（不告警不出清单）。
- reconcile 分歧告警（runs 退役收尾，纯告警对账）：run completed × 未决投影 →
  只告警零写零派发（永不托底裁 completed，事故 2026-09-28 回归）；
  classify_divergence 判定矩阵（告警类别只描述分歧形态，不产出裁决值）。

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
# 共享根 = 上一层（plugins/shared，_root_api/project_registry 所在）。写
# parent.parent 会落到 plugins——fixture 垫错层是「孤跑 _root_api 路径伪影」
# 的机制根因（模块缓存未暖时 reconcile/http_api 的裸导入必炸），两轮案底。
_SHARED_ROOT = _HERE.parent

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
    # 限定名（tasks 命名空间包）：与裸名同权逐出/还原，保证 tasks.service
    # 每例从当前 sys.path 形态新鲜解析，不残留其他测试的模块代际。
    "tasks",
    "tasks.service",
    "tasks.service_access",
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

    def test_list_by_status_filters_memory_cache(self, rootless: Any) -> None:
        """list_by_status 按 status 过滤内存账（被 server/timer_manager 消费的读面）。"""
        from task_types import TaskModel, TaskStatus

        def _mk(tid: str, status: TaskStatus) -> TaskModel:
            return TaskModel(
                id=tid, title=tid, status=status,
                created_at="2026-09-16T00:00:00Z", updated_at="2026-09-16T00:00:00Z",
            )

        rootless._tasks = {
            "t-run-1": _mk("t-run-1", TaskStatus.RUNNING),
            "t-run-2": _mk("t-run-2", TaskStatus.RUNNING),
            "t-done": _mk("t-done", TaskStatus.COMPLETED),
        }
        running = rootless.list_by_status(TaskStatus.RUNNING)
        assert sorted(x.id for x in running) == ["t-run-1", "t-run-2"]
        assert all(x.status == TaskStatus.RUNNING for x in running)
        assert [x.id for x in rootless.list_by_status(TaskStatus.COMPLETED)] == ["t-done"]

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
# reconcile：读面形状违约与纯告警对账
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
        """state 读面返回非列表 → 不告警不出清单（形状违约即放弃本轮）。"""
        import reconcile

        state = _FakeCapability({"list": {"rows": []}})
        runs = _FakeCapability({"pipeline-runs.list": [_run_row("failed", "p")]})

        assert await reconcile.reconcile_startup(state, runs) == []

    async def test_non_dict_runs_and_missing_pid_are_skipped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """runs 读面含非 dict 行 / run 行无 pipeline_id → 跳过，不影响同批合法 run。"""
        import logging

        import reconcile

        task_row = {
            "pipeline_id": "pipe-ok",
            "task.status": "running",
            # 根任务血缘键：补写面身份判别要求（_is_task_pipeline_row）
            "lineage.origin.kind": "root",
        }
        state = _FakeCapability({"list": [task_row], "update": {}})
        runs = _FakeCapability(
            {
                "pipeline-runs.list": [
                    "junk-run",
                    {"run_id": "r-nopid", "status": "failed", "created_at": "2026-09-06T09:00:00Z"},
                    _run_row("failed", "pipe-ok"),
                ]
            }
        )

        with caplog.at_level(logging.WARNING):
            divergences = await reconcile.reconcile_startup(state, runs)
        assert [d["pipeline_id"] for d in divergences] == ["pipe-ok"]
        assert [d["kind"] for d in divergences] == ["undecided_run_terminal"]
        assert sum("分歧" in r.getMessage() for r in caplog.records) == 1
        # failed 侧补随写已执行（装机电量：投影不再卡 running）
        assert divergences[0]["followed_write"] is True
        update_calls = [c for c in state.calls if c[0] == "update"]
        assert len(update_calls) == 1
        assert update_calls[0][1]["pipeline_id"] == "pipe-ok"
        assert update_calls[0][1]["fields"]["task.status"] == "failed"

    async def test_task_rows_without_candidate_run_get_no_run_evidence_knife(self) -> None:
        """无候选 run 的未决任务行 → 第五刀收束（ADR 2026-11-02-state-key-
        governance：提交后执行前崩溃的投影永久悬挂形态）；终态投影行不裁。"""
        import reconcile

        state = _FakeCapability(
            {
                "list": [
                    {
                        "pipeline_id": "pipe-no-run",
                        "task.status": "running",
                        # 血缘键：任务行身份（缺血缘键 = 会话行，2026-10-27 起
                        # 走非任务清扫面，不进本契约的「无候选 run 跳过」域）
                        "lineage.origin.kind": "root",
                    },
                    {
                        "pipeline_id": "pipe-other",
                        "task.status": "completed",
                        # 根任务血缘键：任务行身份（_is_task_pipeline_row）
                        "lineage.origin.kind": "root",
                    },
                ]
            }
        )
        runs = _FakeCapability({"pipeline-runs.list": [_run_row("failed", "pipe-other")]})

        divergences = await reconcile.reconcile_startup(state, runs)
        by_pid = {d["pipeline_id"]: d for d in divergences}
        assert by_pid["pipe-no-run"]["kind"] == "no_run_evidence_stopped", (
            "无 run 证据的未决残留由第五刀收束（stopped + run_aborted）"
        )
        assert by_pid["pipe-other"]["kind"] == "projection_completed_run_diverged", (
            "completed 投影 × 簿记 failed = 证据分歧（纯告警，不裁决）"
        )


# ═══════════════════════════════════════════════════════════
# reconcile：完成判据收紧（事故 2026-09-28 回归——簿记托底假完成通知）
# ═══════════════════════════════════════════════════════════


class TestRunCompletedNeverCompletesTask:
    """run 正常收尾 × 评估未通过/未发生 → 永不裁 completed（用户裁定）。

    事故 2026-09-28：熔断收尾的 run 落 completed，reconcile 簿记托底补写
    task.status=completed 并向父管道派发「已完成 ✅」——验收产物从未产出、
    task_evaluate 从未被调。runs 退役收尾后整个裁决面退役：该形态只告警，
    零写零派发（谁终态谁随写）。
    """

    async def test_missing_projection_warns_without_write_or_notify(self) -> None:
        """事故原形态：run completed + task.status 键缺席 → 只告警，零写零派发。"""
        import reconcile

        task_row = {
            "pipeline_id": "pipe-rc",
            "task.id": "pipe-rc",
            "lineage.parent_pipeline_id": "pipe-parent",
        }
        state = _FakeCapability({"list": [task_row]})
        runs = _FakeCapability({"pipeline-runs.list": [_run_row("completed", "pipe-rc")]})

        divergences = await reconcile.reconcile_startup(state, runs)

        assert [d["kind"] for d in divergences] == ["run_completed_evaluation_missing"]
        # 纯告警：零写面（update 未预置，写了即 KeyError；显式断言双保险）
        assert [c for c in state.calls if c[0] == "update"] == []

    async def test_running_projection_same_warning(self) -> None:
        """同契约第二输入：显式 running 投影同样只告警。"""
        import reconcile

        task_row = {
            "pipeline_id": "pipe-rc2",
            "task.status": "running",
            # 根任务血缘键：任务行身份（_is_task_pipeline_row）
            "lineage.origin.kind": "root",
        }
        state = _FakeCapability({"list": [task_row]})
        runs = _FakeCapability({"pipeline-runs.list": [_run_row("completed", "pipe-rc2")]})

        divergences = await reconcile.reconcile_startup(state, runs)

        assert [d["kind"] for d in divergences] == ["run_completed_evaluation_missing"]
        assert [c for c in state.calls if c[0] == "update"] == []

    async def test_deprecated_pending_evaluation_row_is_inert(self) -> None:
        """废弃值 pending_evaluation 残行（B 拍板 2026-10-16）→ 不构成分歧：
        零告警、零写面（不入未决集，全链路惰性）。"""
        import reconcile

        task_row = {
            "pipeline_id": "pipe-rc3",
            "task.status": "pending_evaluation",
            # 血缘键：任务行身份（缺血缘键 = 会话行，走非任务清扫面）
            "lineage.origin.kind": "root",
        }
        state = _FakeCapability({"list": [task_row]})
        runs = _FakeCapability({"pipeline-runs.list": [_run_row("completed", "pipe-rc3")]})

        assert await reconcile.reconcile_startup(state, runs) == []
        assert [c for c in state.calls if c[0] == "update"] == []


@pytest.mark.parametrize(
    ("run_status", "task_status", "expected"),
    [
        # 未决投影 × run 正常收尾 → 告警（不裁 completed）
        ("completed", "", "run_completed_evaluation_missing"),
        ("completed", "running", "run_completed_evaluation_missing"),
        ("completed", "pending", "run_completed_evaluation_missing"),
        ("completed", "evaluating", "run_completed_evaluation_missing"),
        # 废弃值残行（B 拍板 2026-10-16）：簿记任一侧都不构成分歧
        ("completed", "pending_evaluation", None),
        # 未决投影 × run 失败/取消 → 告警（保持；run 簿记 cancelled 为内核
        # run 域用户停止词，照旧）
        ("failed", "running", "undecided_run_terminal"),
        ("failed", "pending_evaluation", None),
        ("cancelled", "", "undecided_run_terminal"),
        # 排空对账（三分刀之一，ADR 2026-10-30）：投影 running × 簿记
        # suspended → 补写 stopped（重启排空遗留）
        ("suspended", "running", "suspended_run_drain"),
        # 僵尸告警（三分刀之二，ADR 2026-10-30）：投影真终态 × 簿记非终态
        # → 纯告警不裁决
        ("running", "completed", "zombie_terminal_projection"),
        ("running", "failed", "zombie_terminal_projection"),
        ("suspended", "failed", "zombie_terminal_projection"),
        # 投影权威：评估已通过，runs 失败/挂起 → 告警（保持）
        ("failed", "completed", "projection_completed_run_diverged"),
        ("suspended", "completed", "projection_completed_run_diverged"),
        # 一致行 / 在飞 / 可恢复 / 投影终态权威 → 不告警
        ("completed", "completed", None),
        ("completed", "failed", None),
        ("running", "running", None),
        ("suspended", "", None),
        ("suspended", "stopped", None),
        ("completed", "stopped", None),
    ],
)
def test_classify_divergence_matrix(
    run_status: str, task_status: str, expected: str | None
) -> None:
    """判定矩阵：告警类别只描述分歧形态，不产出裁决值（纯告警对账）。"""
    import reconcile

    assert reconcile.classify_divergence(run_status, task_status) == expected


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


# ═══════════════════════════════════════════════════════════
# reconcile：failed 侧补随写（装机 2026-10-10 B74/B75/B76 回归）
# ═══════════════════════════════════════════════════════════


class TestFailedSideFollowWrite:
    """投影未决 × 簿记 failed → 投影补随写 failed（唯一补写面）。

    装机三连实证：进程异常终止的 run 由内核启动清扫标 failed，在途失败
    写面（stop_check/task_reminder）从未执行、run.failed 域事件随进程死亡
    丢失——投影恒卡 running。failed 无假成功风险（不触 09-28 红线）；
    completed 侧与僵尸形态保持纯告警。
    """

    def _env(self, task_status: str, run_status: str, ended_at: str | None = None):
        import reconcile

        task_row = {
            "pipeline_id": "pipe-f",
            "task.status": task_status,
            # 根任务血缘键：补写面身份判别要求（_is_task_pipeline_row）
            "lineage.origin.kind": "root",
        }
        run = _run_row(run_status, "pipe-f")
        if ended_at is not None:
            run["ended_at"] = ended_at
        state = _FakeCapability({"list": [task_row], "update": {}})
        runs = _FakeCapability({"pipeline-runs.list": [run]})
        return reconcile, state, runs

    async def test_failed_bookkeeping_follow_writes_failed_projection(self) -> None:
        """主路径：running 投影 × failed 簿记 → 写 failed + 归因 + run_ended_at。"""
        reconcile, state, runs = self._env(
            "running", "failed", ended_at="2026-10-09T17:39:41Z"
        )

        divergences = await reconcile.reconcile_startup(state, runs)

        assert [d["kind"] for d in divergences] == ["undecided_run_terminal"]
        assert divergences[0]["followed_write"] is True
        update_calls = [c for c in state.calls if c[0] == "update"]
        assert len(update_calls) == 1, "幂等单写：一轮扫描至多一次补随写"
        fields = update_calls[0][1]["fields"]
        assert fields["task.status"] == "failed"
        assert fields["task.ended_at"] == "2026-10-09T17:39:41Z"
        assert fields["task.failure_class"] == "run_aborted"
        assert isinstance(fields["task.failure_reason"], str)
        assert len(fields["task.failure_reason"]) > 0

    async def test_follow_write_is_idempotent_across_rescans(self) -> None:
        """性质断言：补写后投影=failed，下一轮扫描不再进分歧集、零写（on_load 高频）。"""
        reconcile, state, runs = self._env("running", "failed")

        first = await reconcile.reconcile_startup(state, runs)
        assert first[0]["followed_write"] is True

        state._responses["list"] = [
            {
                "pipeline_id": "pipe-f",
                "task.status": "failed",
                # 根任务血缘键：身份守卫后行必须仍被识别为任务行
                "lineage.origin.kind": "root",
            }
        ]
        second = await reconcile.reconcile_startup(state, runs)

        assert second == []
        assert len([c for c in state.calls if c[0] == "update"]) == 1

    async def test_newer_running_run_blocks_follow_write(self) -> None:
        """在飞新 run 参与最新性收敛：旧 failed + 新 running → 无分歧零写（不踩复活任务）。"""
        import reconcile

        state = _FakeCapability(
            {
                "list": [
                    {
                        "pipeline_id": "pipe-x",
                        "task.status": "running",
                        # 根任务血缘键：任务行身份（_is_task_pipeline_row）
                        "lineage.origin.kind": "root",
                    }
                ],
                "update": {},
            }
        )
        runs = _FakeCapability(
            {
                "pipeline-runs.list": [
                    _run_row("failed", "pipe-x", created_at="2026-09-06T08:00:00Z"),
                    _run_row("running", "pipe-x", created_at="2026-09-06T09:00:00Z"),
                ]
            }
        )

        assert await reconcile.reconcile_startup(state, runs) == []
        assert [c for c in state.calls if c[0] == "update"] == []

    async def test_cancelled_bookkeeping_follow_writes_stopped_projection(self) -> None:
        """用户停止侧补随写（缺陷 I，R2 移动端 2026-10-15 实证）：停止随写被
        垂死 run 的步边界 stale 投影覆写回 running——与 failed 侧同构，投影
        未决即补写。词汇 = stopped（ADR 2026-10-30 词汇收敛：任务域
        cancelled 退役，用户停止 = stopped），不写 failure_class（停止是
        用户裁决不是失败，与停止写面同形状）。"""
        reconcile, state, runs = self._env(
            "running", "cancelled", ended_at="2026-10-15T12:00:00Z",
        )

        divergences = await reconcile.reconcile_startup(state, runs)

        assert [d["kind"] for d in divergences] == ["undecided_run_terminal"]
        assert divergences[0]["followed_write"] is True
        update_calls = [c for c in state.calls if c[0] == "update"]
        assert len(update_calls) == 1
        fields = update_calls[0][1]["fields"]
        assert fields["task.status"] == "stopped"
        assert fields["task.ended_at"] == "2026-10-15T12:00:00Z"
        assert "failure_class" not in fields

    async def test_follow_write_failure_degrades_to_warning(self) -> None:
        """写面故障 → 降级留痕（followed_write=False），分歧清单照常返回不阻断装载。"""
        import reconcile

        state = _FakeCapability(
            {
                "list": [
                    {
                        "pipeline_id": "pipe-w",
                        "task.status": "running",
                        # 根任务血缘键：补写面身份判别要求（_is_task_pipeline_row）
                        "lineage.origin.kind": "root",
                    }
                ],
                "update": RuntimeError("state 写面故障"),
            }
        )
        runs = _FakeCapability({"pipeline-runs.list": [_run_row("failed", "pipe-w")]})

        divergences = await reconcile.reconcile_startup(state, runs)

        assert [d["kind"] for d in divergences] == ["undecided_run_terminal"]
        assert divergences[0]["followed_write"] is False

    async def test_user_stop_follow_write_failure_degrades_to_warning(self) -> None:
        """用户停止侧写面故障 → 同款降级（followed_write=False），不阻断装载。"""
        import reconcile

        state = _FakeCapability(
            {
                "list": [
                    {
                        "pipeline_id": "pipe-ws",
                        "task.status": "running",
                        # 根任务血缘键：补写面身份判别要求（_is_task_pipeline_row）
                        "lineage.origin.kind": "root",
                    }
                ],
                "update": RuntimeError("state 写面故障"),
            }
        )
        runs = _FakeCapability({"pipeline-runs.list": [_run_row("cancelled", "pipe-ws")]})

        divergences = await reconcile.reconcile_startup(state, runs)

        assert [d["kind"] for d in divergences] == ["undecided_run_terminal"]
        assert divergences[0]["followed_write"] is False


# ═══════════════════════════════════════════════════════════
# http_api：resume 如实映射（假恢复回归——2026-10-10 装机 B76）
# ═══════════════════════════════════════════════════════════


class TestResumeHonestSubmission:
    """_resume_task_pipeline 返回 (resumed, submitted, error)；空 run_id 且未派发不再假成功。"""

    def _patch_resume(
        self, monkeypatch: pytest.MonkeyPatch, resp: Any, task_status: str = "running"
    ) -> list[tuple[str, dict[str, Any]]]:
        import http_api

        calls: list[tuple[str, dict[str, Any]]] = []

        class _Handle:
            async def call(self, method: str, params: dict[str, Any], timeout=None):  # noqa: ANN001
                calls.append((method, params))
                if method == "pipeline-runs.list":
                    raise KeyError("unexpected")
                if method == "list":
                    return [{"pipeline_id": "t-1", "task.id": "t-1", "task.status": task_status}]
                if isinstance(resp, Exception):
                    raise resp
                return resp

        handles = {
            "pipeline-state": _Handle(),
            "pipeline-executor": _Handle(),
        }
        monkeypatch.setattr(http_api, "_capability", lambda name: handles[name])
        return calls

    async def test_dispatched_continuation_reports_submitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """簿记 failed（无在飞 run）→ 内核拉起续跑轮 dispatched=true → 真恢复。"""
        import http_api

        self._patch_resume(
            monkeypatch, {"status": "resumed", "run_id": "", "dispatched": True}
        )
        resumed, submitted, error = await http_api._resume_task_pipeline("t-1")
        assert (resumed, submitted, error) == (True, True, "")

    async def test_in_flight_resume_reports_not_submitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """在飞 run 幂等空转：run_id 非空 + dispatched=false → resumed 但未重提。"""
        import http_api

        self._patch_resume(
            monkeypatch, {"status": "resumed", "run_id": "run-1", "dispatched": False}
        )
        resumed, submitted, error = await http_api._resume_task_pipeline("t-1")
        assert (resumed, submitted, error) == (True, False, "")

    async def test_empty_run_id_without_dispatch_is_honest_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """删值实验锚点：空 run_id 且未派发 → (False, False)。

        修复前 `run_id is not None` 把空串当成功（假恢复：响应 success 而
        任务原地不动）。回退该判定本例必红。
        """
        import http_api

        self._patch_resume(
            monkeypatch, {"status": "resumed", "run_id": "", "dispatched": False}
        )
        resumed, submitted, error = await http_api._resume_task_pipeline("t-1")
        assert (resumed, submitted, error) == (False, False, "")

    async def test_capability_failure_stays_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """对照第二输入：能力面异常 → (False, False)，不向调用方抛出。"""
        import http_api

        self._patch_resume(monkeypatch, RuntimeError("executor 不可达"))
        assert await http_api._resume_task_pipeline("t-1") == (False, False, "")

    async def test_non_dict_response_is_honest_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """形状违约第三输入：响应非 dict → (False, False)（不误判成功）。"""
        import http_api

        self._patch_resume(monkeypatch, ["unexpected"])
        assert await http_api._resume_task_pipeline("t-1") == (False, False, "")

    async def test_resume_task_response_carries_honest_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HTTP 面：dispatched 透传 task_submitted，message 不再谎报已重提。"""
        import http_api

        self._patch_resume(
            monkeypatch, {"status": "resumed", "run_id": "run-9", "dispatched": False}
        )
        out = await http_api.resume_task("t-1", _user={"sub": "u-1"})
        assert out["success"] is True
        assert out["task_submitted"] is False
        assert "已重新提交执行" not in out["message"]

    async def test_resume_task_fake_success_now_404(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HTTP 面假恢复形态（空 run_id + 未派发）→ 404 如实报错，不再 success。"""
        import http_api

        self._patch_resume(
            monkeypatch, {"status": "resumed", "run_id": "", "dispatched": False}
        )
        raised = False
        try:
            await http_api.resume_task("t-1", _user={"sub": "u-1"})
        except http_api.APIError as exc:
            raised = exc.status_code == 404
        assert raised, "无可恢复对象必须 404，不得谎报恢复成功"

    async def test_resume_project_consumes_tuple_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resume_project 消费 (resumed, submitted) 元组：resumed 元素计数。"""
        import http_api

        async def fake_resume(task_id: str, user_id: str = "") -> tuple[bool, bool, str]:
            return (task_id == "p-ok", False, "")

        async def fake_child_pids(_project_id: str) -> list[str]:
            return ["p-ok", "p-dead"]

        monkeypatch.setattr(http_api, "_resume_task_pipeline", fake_resume)
        monkeypatch.setattr(http_api, "_project_child_pids", fake_child_pids)
        _row = {
            "id": "proj-1",
            "submitted_by": "u-1",
            "session_id": "",
            "title": "P",
            "status": "paused",
            "auto_execute": False,
            "created_at": "",
            "updated_at": "",
            "path": "",
        }

        async def fake_get_row(project_id: str) -> dict[str, Any]:
            assert project_id == "proj-1"
            return dict(_row)

        saved_rows: list[dict[str, Any]] = []

        async def fake_project_call(method: str, args: dict[str, Any]) -> dict[str, Any]:
            assert method == "save"
            saved_rows.append(args["project"])
            return {"project": args["project"]}

        monkeypatch.setattr(http_api, "_get_project_row", fake_get_row)
        monkeypatch.setattr(http_api, "_project_call", fake_project_call)

        out = await http_api.resume_project("proj-1", _user={"sub": "u-1"})
        assert out["project"]["status"] == "running"
        assert saved_rows
        assert saved_rows[0]["status"] == "active"


# ═══════════════════════════════════════════════════════════
# http_api：恢复动作终态防线（幽灵续跑改判事故 2026-10-19 修复④B）
# ═══════════════════════════════════════════════════════════


class TestResumeTerminalStateGuard:
    """_resume_task_pipeline 派发前读权威投影行 task.status（修复④B；ADR
    2026-10-30 扩为真终态全集）。

    装机事故：任务合法完成（评估通过落库 completed）后，无输入续跑轮被
    裸恢复（resume_pipeline + overlay 强改 running）拉起，评估闸门在
    reminder_exhausted 后把 completed 改判 failed。防线落任务域插件
    （内核 resume_pipeline 零任务知识，不加守卫）：
    - 真终态 completed/failed → 不调 resume_pipeline，返回显式失败
      （completed 裸恢复正是幽灵轮形态；failed 重试走 submit/continue
      职责面，恢复按钮语义是"恢复执行"而非重试）；
    - timeout 非真终态（可重入）→ 照常恢复 + overlay 追加清失败注记
      （null = 清除语义，重试轮不顶上一轮失败原因展示）；
    - 其余状态/缺行/投影读面故障 → 现行为不变（降级放行）。
    """

    def _patch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        resume_resp: Any,
        state_rows: list[dict[str, Any]] | None = None,
        state_error: Exception | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        import http_api

        if state_rows is None:
            state_rows = [{"pipeline_id": "t-1", "task.id": "t-1", "task.status": "running"}]
        calls: list[tuple[str, dict[str, Any]]] = []

        class _StateHandle:
            async def call(self, method: str, params: dict[str, Any], timeout=None):  # noqa: ANN001
                calls.append(("pipeline-state", params))
                assert method == "list"
                if state_error is not None:
                    raise state_error
                return state_rows

        class _ExecutorHandle:
            async def call(self, method: str, params: dict[str, Any], timeout=None):  # noqa: ANN001
                calls.append(("pipeline-executor", params))
                assert method == "resume_pipeline"
                if isinstance(resume_resp, Exception):
                    raise resume_resp
                return resume_resp

        handles = {
            "pipeline-state": _StateHandle(),
            "pipeline-executor": _ExecutorHandle(),
        }
        monkeypatch.setattr(http_api, "_capability", lambda name: handles[name])
        return calls

    async def test_completed_resume_rejected_without_dispatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """completed 投影 → 不调 resume_pipeline，显式失败带返工指路。"""
        import http_api

        calls = self._patch(
            monkeypatch,
            {"status": "resumed", "run_id": "run-1", "dispatched": True},
            state_rows=[{"pipeline_id": "t-1", "task.id": "t-1", "task.status": "completed"}],
        )

        resumed, submitted, error = await http_api._resume_task_pipeline("t-1")

        assert (resumed, submitted) == (False, False)
        assert "任务已完成" in error
        assert "如需返工请由上级带指令继续" in error
        assert [c for c in calls if c[0] == "pipeline-executor"] == [], (
            "已完成任务不得派发续跑轮（本事故幽灵轮触发源）"
        )

    async def test_resume_task_completed_surfaces_409(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HTTP 面如实回错误：completed 恢复 → 409 语义，消息带返工指路。"""
        import http_api

        self._patch(
            monkeypatch,
            {"status": "resumed", "run_id": "run-1", "dispatched": True},
            state_rows=[{"pipeline_id": "t-1", "task.id": "t-1", "task.status": "completed"}],
        )
        with pytest.raises(http_api.APIError) as ei:
            await http_api.resume_task("t-1", _user={"sub": "u-1"})
        assert ei.value.status_code == 409
        assert "任务已完成" in ei.value.message
        assert "如需返工请由上级带指令继续" in ei.value.message

    async def test_failed_resume_rejected_without_dispatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """failed 真终态（ADR 2026-10-30）→ 不调 resume_pipeline，显式失败带
        重试指路（重试走 submit/continue 职责面）。"""
        import http_api

        calls = self._patch(
            monkeypatch,
            {"status": "resumed", "run_id": "run-1", "dispatched": True},
            state_rows=[{"pipeline_id": "t-1", "task.id": "t-1", "task.status": "failed"}],
        )

        resumed, submitted, error = await http_api._resume_task_pipeline("t-1")

        assert (resumed, submitted) == (False, False)
        assert "任务已失败" in error
        assert "重新提交" in error or "continue" in error
        assert [c for c in calls if c[0] == "pipeline-executor"] == [], (
            "失败任务不得经 resume 拉起（重试走 submit/continue 职责面）"
        )

    async def test_resume_task_failed_surfaces_409(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HTTP 面如实回错误：failed 恢复 → 409 语义。"""
        import http_api

        self._patch(
            monkeypatch,
            {"status": "resumed", "run_id": "run-1", "dispatched": True},
            state_rows=[{"pipeline_id": "t-1", "task.id": "t-1", "task.status": "failed"}],
        )
        with pytest.raises(http_api.APIError) as ei:
            await http_api.resume_task("t-1", _user={"sub": "u-1"})
        assert ei.value.status_code == 409
        assert "任务已失败" in ei.value.message

    async def test_timeout_resume_clears_failure_annotations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """timeout 非真终态（可重入）→ 照常恢复；overlay = 3 基础键 + 3 清失败
        注记（恰 6 键）。"""
        import http_api

        calls = self._patch(
            monkeypatch,
            {"status": "resumed", "run_id": "", "dispatched": True},
            state_rows=[{"pipeline_id": "t-1", "task.id": "t-1", "task.status": "timeout"}],
        )

        resumed, submitted, error = await http_api._resume_task_pipeline("t-1")

        assert (resumed, submitted, error) == (True, True, "")
        resume_params = [p for name, p in calls if name == "pipeline-executor"]
        assert len(resume_params) == 1
        assert resume_params[0]["pipeline_id"] == "t-1"
        assert resume_params[0]["state_overlay"] == {
            "task.status": "running",
            "task_status": "running",
            "suspended_reason": None,
            "task.ended_at": None,
            "task.failure_class": None,
            "task.failure_reason": None,
        }

    async def test_resume_clears_suspended_reason_attribution(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """suspended（drain 排空挂起，P-23 片2）→ 恢复即清挂起归因。

        overlay 基础键携带 suspended_reason=None（ADR
        2026-11-04-run-suspended-reason-key）：G8 排空任务经任务面板恢复后，
        陈旧归因残留于 pipeline_state 会在后续 W1/W2 挂起时被面板误显为上次
        drain 原因（片1 复核移交要件①）。
        """
        import http_api

        calls = self._patch(
            monkeypatch,
            {"status": "resumed", "run_id": "run-5", "dispatched": False},
            state_rows=[{"pipeline_id": "t-1", "task.id": "t-1", "task.status": "suspended"}],
        )

        resumed, submitted, error = await http_api._resume_task_pipeline("t-1")

        assert (resumed, submitted, error) == (True, False, "")
        resume_params = [p for name, p in calls if name == "pipeline-executor"]
        assert len(resume_params) == 1
        assert resume_params[0]["state_overlay"]["suspended_reason"] is None

    @pytest.mark.parametrize("live_status", ["running", "suspended"])
    async def test_live_status_resume_keeps_current_behavior(
        self, monkeypatch: pytest.MonkeyPatch, live_status: str
    ) -> None:
        """running/suspended（回归保护）→ 现行为不变：默认 overlay 恰 3 键照常派发。"""
        import http_api

        calls = self._patch(
            monkeypatch,
            {"status": "resumed", "run_id": "run-2", "dispatched": False},
            state_rows=[
                {"pipeline_id": "t-1", "task.id": "t-1", "task.status": live_status}
            ],
        )

        resumed, submitted, error = await http_api._resume_task_pipeline("t-1")

        assert (resumed, submitted, error) == (True, False, "")
        resume_params = [p for name, p in calls if name == "pipeline-executor"]
        assert len(resume_params) == 1
        assert resume_params[0]["state_overlay"] == {
            "task.status": "running",
            "task_status": "running",
            "suspended_reason": None,
        }

    async def test_missing_projection_row_keeps_current_behavior(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缺行（投影聚合无此 pipeline 行）→ 现行为不变：放行 + 默认 overlay。"""
        import http_api

        calls = self._patch(
            monkeypatch,
            {"status": "resumed", "run_id": "run-3", "dispatched": False},
            state_rows=[{"pipeline_id": "other-task", "task.status": "completed"}],
        )

        resumed, submitted, error = await http_api._resume_task_pipeline("t-1")

        assert (resumed, submitted, error) == (True, False, "")
        resume_params = [p for name, p in calls if name == "pipeline-executor"]
        assert len(resume_params) == 1
        assert resume_params[0]["state_overlay"] == {
            "task.status": "running",
            "task_status": "running",
            "suspended_reason": None,
        }

    async def test_projection_read_failure_degrades_to_allow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """投影读面故障 → 降级放行（_read_task_status 保守风格）：默认 overlay 派发。"""
        import http_api

        calls = self._patch(
            monkeypatch,
            {"status": "resumed", "run_id": "run-4", "dispatched": False},
            state_error=RuntimeError("state 读面故障"),
        )

        resumed, submitted, error = await http_api._resume_task_pipeline("t-1")

        assert (resumed, submitted, error) == (True, False, "")
        resume_params = [p for name, p in calls if name == "pipeline-executor"]
        assert len(resume_params) == 1
        assert resume_params[0]["state_overlay"] == {
            "task.status": "running",
            "task_status": "running",
            "suspended_reason": None,
        }

    async def test_resume_rejects_session_row_without_dispatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """恢复 overlay 身份门（ADR 2026-11-06 D5 N7）：会话主管道行（无
        task.id 无 lineage.*，可带幽灵残留）不是任务 → 拒绝派发（overlay 的
        task.status=running 正是 09:02:01 会话残留的疑似写入面之一）。"""
        import http_api

        calls = self._patch(
            monkeypatch,
            {"status": "resumed", "run_id": "run-6", "dispatched": True},
            state_rows=[
                {
                    "pipeline_id": "t-1",
                    "task.status": "stopped",
                    "task.ended_at": "2026-11-06T09:02:01+00:00",
                    "task.owned.child-1.title": "子任务",
                }
            ],
        )

        resumed, submitted, error = await http_api._resume_task_pipeline("t-1")

        assert (resumed, submitted) == (False, False)
        assert error, "会话行拒绝必须显式报错"
        assert [c for c in calls if c[0] == "pipeline-executor"] == [], (
            "会话行不得派发续跑轮（overlay 不落会话）"
        )


# ═══════════════════════════════════════════════════════════
# reconcile：失败归因透传（2026-10-17 事故回归）
# ═══════════════════════════════════════════════════════════


class TestFailureAttributionPropagation:
    """failed 侧补随写必须原样透传 run 署名，不产成因。

    事故 2026-10-17：墙钟超时（run_stop_reason=timeout）被一律报成「运行进程
    异常终止（run 簿记由启动清扫标记 failed）」，而当日唯一一次启动清扫在 6
    小时前、reap 的是另一批 run——归因与事实无关，把排查方向带偏到"进程不稳定"
    上。契约：署名即证据（带出真因），无署名就如实说无（不拿猜测回填）。
    """

    async def _fields(self, stop_reason: str | None) -> dict[str, Any]:
        import reconcile

        task_row: dict[str, Any] = {
            "pipeline_id": "pipe-a",
            "task.status": "running",
            # 根任务血缘键：补写面身份判别要求（_is_task_pipeline_row）
            "lineage.origin.kind": "root",
        }
        if stop_reason is not None:
            task_row["run_stop_reason"] = stop_reason
        state = _FakeCapability({"list": [task_row], "update": {}})
        runs = _FakeCapability({"pipeline-runs.list": [_run_row("failed", "pipe-a")]})

        divergences = await reconcile.reconcile_startup(state, runs)

        assert [d["kind"] for d in divergences] == ["undecided_run_terminal"]
        assert divergences[0]["followed_write"] is True
        updates = [c for c in state.calls if c[0] == "update"]
        assert len(updates) == 1, "幂等单写：一轮扫描至多一次补随写"
        fields: dict[str, Any] = updates[0][1]["fields"]
        return fields

    @pytest.mark.parametrize(
        ("stop_reason", "expected_class"),
        [
            ("timeout", "run_stopped"),
            ("max_iterations", "run_stopped"),
            ("tool_fail_loop", "run_stopped"),
            ("my_plugin_gave_up", "run_stopped"),
            ("", "run_aborted"),
        ],
        ids=["timeout", "max_iterations", "tool_fail_loop", "plugin_declared", "unsigned"],
    )
    async def test_signature_present_yields_signed_class(
        self, stop_reason: str, expected_class: str
    ) -> None:
        """有署名 → run_stopped（原因已知）；空署名 → run_aborted（原因未知）。"""
        fields = await self._fields(stop_reason)

        assert fields["task.failure_class"] == expected_class

    async def test_timeout_reason_names_real_cause_not_a_guess(self) -> None:
        """墙钟超时的归因文本必须说超时，且不含任何猜测成因。"""
        fields = await self._fields("timeout")

        reason = fields["task.failure_reason"]
        assert "墙钟" in reason, reason
        assert "启动清扫" not in reason, reason
        assert "进程异常终止" not in reason, reason

    @pytest.mark.parametrize("empty", [None, ""], ids=["key_absent", "key_empty"])
    async def test_unsigned_run_says_unsigned_and_never_guesses(self, empty: str | None) -> None:
        """未署名如实说未记录：键缺席与空串两形态均不回填具体成因。"""
        fields = await self._fields(empty)

        reason = fields["task.failure_reason"]
        assert "未记录" in reason, reason
        assert "启动清扫" not in reason, reason
        assert "进程异常终止" not in reason, reason

    async def test_known_word_translated_unknown_word_verbatim(self) -> None:
        """词表内译人话；词表外的插件声明词原样带出（不猜语义，但证据不丢）。"""
        assert "预算耗尽" in (await self._fields("budget_exhausted"))["task.failure_reason"]
        assert "my_plugin_gave_up" in (await self._fields("my_plugin_gave_up"))["task.failure_reason"]

    @pytest.mark.parametrize(
        ("word", "needle"),
        [
            ("model_unavailable", "模型不可用"),
            ("llm_fail_loop", "LLM 连续同类失败循环"),
        ],
    )
    async def test_duplicate_check_declared_words_translated(self, word: str, needle: str) -> None:
        """duplicate_check 第一方署名词（ADR 2026-10-20-model-hot-swap-inflight-guard）
        收录词表：如实中文归因，不原词透传。"""
        assert needle in (await self._fields(word))["task.failure_reason"]


# ═══════════════════════════════════════════════════════════
# reconcile：父管道 owned 镜像同批随写（谁终态谁随写）
# ═══════════════════════════════════════════════════════════


class TestParentOwnedMirrorFollowWrite:
    """补随写同批纠正父管道 owned 镜像（task.owned.<id>.status）。

    事故 2026-10-20 双脏面：任务终态 failed，父管道 task.owned.<id>.status
    仍停在登记时的 running——父会话读它作答即读脏投影（「还在运行」）。
    镜像键的登记坐标 = 子管道 id（task_submit 登记面用子管道 id）。
    """

    def _env(self, run_status: str, parent: str | None):
        import reconcile

        task_row: dict[str, Any] = {"pipeline_id": "pipe-m", "task.status": "running"}
        if parent is not None:
            task_row["lineage.parent_pipeline_id"] = parent
        else:
            # 根任务：仍带血缘键（lineage.root 族），只是无父锚点——补写面
            # 身份判别（_is_task_pipeline_row）要求任务行必有血缘键
            task_row["lineage.origin.kind"] = "root"
        state = _FakeCapability({"list": [task_row], "update": {}})
        runs = _FakeCapability({"pipeline-runs.list": [_run_row(run_status, "pipe-m")]})
        return reconcile, state, runs

    @pytest.mark.parametrize(
        ("run_status", "expected"),
        # 簿记 cancelled（内核 run 域用户停止词）→ 任务域词汇 stopped
        [("failed", "failed"), ("cancelled", "stopped")],
    )
    async def test_follow_write_without_owned_mirror(
        self, run_status: str, expected: str
    ) -> None:
        """补随写契约（owned 镜像退役，ADR 2026-11-02-state-key-governance）：
        自身投影补写终态词汇；零 task.owned.* 写面（面板 join 子行取真值）。"""
        reconcile, state, runs = self._env(run_status, "pipe-parent")

        divergences = await reconcile.reconcile_startup(state, runs)

        assert divergences[0]["followed_write"] is True
        mirror_updates = [
            c for c in state.calls
            if c[0] == "update"
            and any(k.startswith("task.owned.") for k in c[1]["fields"])
        ]
        assert mirror_updates == [], "owned 镜像已退役，不得写 task.owned.*"
        own_updates = [
            c for c in state.calls
            if c[0] == "update" and c[1]["fields"].get("task.status") == expected
        ]
        assert len(own_updates) == 1, "自身投影必须补写终态词汇"

    async def test_root_task_without_parent_writes_no_mirror(self) -> None:
        """根任务（无父锚点）：无 owned 写面，不凭空造 owned 键。"""
        reconcile, state, runs = self._env("failed", None)

        divergences = await reconcile.reconcile_startup(state, runs)

        assert divergences[0]["followed_write"] is True
        assert [
            c for c in state.calls
            if c[0] == "update" and any(k.startswith("task.owned.") for k in c[1]["fields"])
        ] == []

    async def test_follow_write_failure_degrades_to_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """自身投影补随写失败只留告警（不抛不中断，下轮装载重试）。"""
        import logging

        reconcile, state, runs = self._env("failed", "pipe-parent")
        state._responses["update"] = RuntimeError("state write down")

        with caplog.at_level(logging.WARNING):
            divergences = await reconcile.reconcile_startup(state, runs)

        assert divergences[0]["followed_write"] is False
        assert any(
            "补随写失败" in r.getMessage() or "降级留痕" in r.getMessage()
            for r in caplog.records
        ), "补随写失败必须留告警"
