# @feature: FP-0.2.二 项目状态门 | @ci: python-coverage
"""project_state 工具测试（方案工作流状态查询/列举与门控迁移，ADR 2026-09-17）。

覆盖：query 快照；list 全量列举（空登记/多项目倒序/无需 project_id）；
transition 合法边（plan→running / running→plan）+ 持久化；缺 target/reason
拒绝；非法边 fail-closed；done 终态不可迁；同态幂等拒绝；项目不存在拒绝；
工具定义形状 + manifest 声明一致性。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

# 共享根自举（plugins/shared/ —— project_manager 包所在；包限定名导入免串扰）
_SHARED_ROOT = Path(__file__).resolve().parents[1]
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

from project_manager import project_registry as _pr  # noqa: E402,I001 —— 同包多行导入按自举序展开
from project_manager import project_state_tool  # noqa: E402

_PLUGIN_DIR = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离登记目录（不落真实 tasks 数据根）。"""
    monkeypatch.setenv("TASKS_STORAGE_DIR", str(tmp_path / "tasks_data"))
    return tmp_path


def _tool() -> Any:
    return project_state_tool.ProjectStateTool()


def _mk_project(title: str = "P", **kw: Any) -> str:
    p = _pr.ProjectModel(title=title, path="D:/x/p", **kw)
    _pr.ProjectRegistry().save(p)
    return p.id


class TestProjectStateQuery:
    async def test_query_returns_snapshot(self, env: Path) -> None:
        pid = _mk_project()
        r = await _tool().execute({"project_id": pid})
        assert r.success, r.error
        assert r.output["project_id"] == pid
        assert r.output["workflow_state"] == "plan"
        assert r.output["transitioned"] is False
        assert r.output["title"] == "P"

    async def test_query_exposes_mode_tags_and_default(self, env: Path) -> None:
        """query 载荷透出 mode_tags/default_mode（批 B8：骨架第零步/AGENTS.md 读通道）。"""
        pid = _mk_project(mode_tags=["coding", "research"], default_mode="coding")
        r = await _tool().execute({"project_id": pid})
        assert r.success, r.error
        assert r.output["mode_tags"] == ["coding", "research"]
        assert r.output["default_mode"] == "coding"
        # 未登记项目透出空值形态
        pid2 = _mk_project("素")
        r2 = await _tool().execute({"project_id": pid2})
        assert r2.output["mode_tags"] == []
        assert r2.output["default_mode"] == ""

    async def test_list_rows_expose_mode_tags_and_default(self, env: Path) -> None:
        """list 行透出两字段（面板/调度发现面同源）。"""
        _mk_project("L", mode_tags=["coding"], default_mode="coding")
        r = await _tool().execute({"action": "list"})
        assert r.success, r.error
        row = r.output["projects"][0]
        assert row["mode_tags"] == ["coding"]
        assert row["default_mode"] == "coding"

    async def test_unknown_project_rejected(self, env: Path) -> None:
        r = await _tool().execute({"project_id": "deadbeef0000"})
        assert not r.success
        assert r.error_code == "PROJECT_NOT_FOUND"

    async def test_missing_project_id_rejected(self, env: Path) -> None:
        r = await _tool().execute({})
        assert not r.success
        assert r.error_code == "MISSING_PROJECT_ID"


class TestProjectStateList:
    async def test_list_empty_registry(self, env: Path) -> None:
        r = await _tool().execute({"action": "list"})
        assert r.success, r.error
        assert r.output["projects"] == []
        assert r.output["total"] == 0

    async def test_list_returns_all_sorted_desc(self, env: Path) -> None:
        ids = {
            _mk_project("旧", updated_at="2026-09-01T00:00:00"),
            _mk_project("中", updated_at="2026-09-10T00:00:00"),
            _mk_project("新", updated_at="2026-09-20T00:00:00", workflow_state="running"),
        }
        r = await _tool().execute({"action": "list"})
        assert r.success, r.error
        assert r.output["total"] == 3
        rows = r.output["projects"]
        assert {row["project_id"] for row in rows} == ids
        stamps = [row["updated_at"] for row in rows]
        assert stamps == sorted(stamps, reverse=True), "list 须按 updated_at 倒序"
        by_title = {row["title"]: row for row in rows}
        assert by_title["新"]["workflow_state"] == "running"
        assert by_title["旧"]["workflow_state"] == "plan"

    async def test_list_without_id_ignores_bogus_project_id(self, env: Path) -> None:
        """list 不依赖 project_id——带错误 id 也不应被拒（发现面与单查面独立）。"""
        _mk_project("P1")
        r = await _tool().execute({"action": "list", "project_id": "deadbeef0000"})
        assert r.success, r.error
        assert r.output["total"] == 1
        assert r.output["projects"][0]["title"] == "P1"


class TestProjectStateTransition:
    async def test_plan_to_running_persists(self, env: Path) -> None:
        pid = _mk_project()
        r = await _tool().execute(
            {"project_id": pid, "action": "transition", "target_state": "running",
             "reason": "方案定稿，用户已确认"}
        )
        assert r.success, r.error
        assert r.output["transitioned"] is True
        assert r.output["workflow_state"] == "running"
        assert _pr.ProjectRegistry().get(pid).workflow_state == "running"

    async def test_running_back_to_plan(self, env: Path) -> None:
        pid = _mk_project(workflow_state="running")
        r = await _tool().execute(
            {"project_id": pid, "action": "transition", "target_state": "plan",
             "reason": "AC-3 traces_to 断链，修订回门"}
        )
        assert r.success, r.error
        assert r.output["workflow_state"] == "plan"

    async def test_missing_reason_rejected(self, env: Path) -> None:
        pid = _mk_project()
        r = await _tool().execute(
            {"project_id": pid, "action": "transition", "target_state": "running"}
        )
        assert not r.success
        assert r.error_code == "MISSING_REASON"

    async def test_missing_target_rejected_and_done_not_tool_reachable(self, env: Path) -> None:
        pid = _mk_project()
        r = await _tool().execute(
            {"project_id": pid, "action": "transition", "reason": "x"}
        )
        assert not r.success
        assert r.error_code == "MISSING_TARGET_STATE"
        r2 = await _tool().execute(
            {"project_id": pid, "action": "transition", "target_state": "done", "reason": "x"}
        )
        assert not r2.success
        assert r2.error_code == "MISSING_TARGET_STATE"

    async def test_illegal_transition_fail_closed(self, env: Path) -> None:
        pid = _mk_project(workflow_state="done")
        r = await _tool().execute(
            {"project_id": pid, "action": "transition", "target_state": "running", "reason": "x"}
        )
        assert not r.success
        assert r.error_code == "ILLEGAL_TRANSITION"

    async def test_same_state_rejected(self, env: Path) -> None:
        pid = _mk_project(workflow_state="running")
        r = await _tool().execute(
            {"project_id": pid, "action": "transition", "target_state": "running", "reason": "x"}
        )
        assert not r.success
        assert r.error_code == "ALREADY_IN_TARGET_STATE"


class TestProjectStateDefinition:
    def test_definition_shape(self) -> None:
        d = project_state_tool.ProjectStateTool.get_tool_definition()
        assert d.name == "project_state"
        # project_id 按 action 条件必填（query/transition 运行时校验），schema 不再整表必填
        assert d.input_schema.get("required") is None
        enum_vals = d.input_schema["properties"]["action"]["enum"]
        assert enum_vals == ["query", "list", "transition"]
        assert d.output_schema.get("required") is None
        assert "projects" in d.output_schema["properties"]

    def test_manifest_declares_merged_project_tool(self) -> None:
        """manifest 只声明合并工具 project（工具面退役任务C刀4），且与 server
        组装单源锁步；ProjectStateTool 保持 state 侧实现体（query/list/transition
        分支经 server 分发复用）。"""
        import importlib.util
        import json

        manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        declared = manifest["capabilities"]["tools"]
        assert [t["name"] for t in declared] == ["project"]

        spec = importlib.util.spec_from_file_location(
            "project_manager_server_lockstep_state", _PLUGIN_DIR / "server.py"
        )
        assert spec is not None
        assert spec.loader is not None
        server = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = server
        spec.loader.exec_module(server)
        assert declared[0]["input_schema"] == server._build_project_input_schema()
        # state 侧参数集（project_id/target_state/reason）保留在合并 schema
        merged_props = declared[0]["input_schema"]["properties"]
        for key in ("project_id", "target_state", "reason"):
            assert key in merged_props
        # 实现体定义仍在（oneOf transition 分支的运行时校验语义单源）
        d = project_state_tool.ProjectStateTool.get_tool_definition()
        assert d.name == "project_state"


# ═══════════════════════════════════════════════════════════
# project 合一入口契约（工具面退役任务C刀4：server 分发面）
# ═══════════════════════════════════════════════════════════


def _server() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "project_manager_server_dispatch", _PLUGIN_DIR / "server.py"
    )
    assert spec is not None
    assert spec.loader is not None
    server = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = server
    spec.loader.exec_module(server)
    return server


async def test_project_dispatch_rejects_unknown_action() -> None:
    """action 非法 → 错误值教改法（允许集 + 两向最小示例）。"""
    out = await _server().project(action="delete")
    assert out["success"] is False
    assert "'create' | 'query' | 'list' | 'transition'" in out["error"]


async def test_project_dispatch_cross_action_params_get_guidance() -> None:
    """跨动作参数误用 → 指出参数归属 action（create 与 state 两侧互斥）。"""
    out = await _server().project(action="create", goal="G", project_id="pid")
    assert out["success"] is False
    assert "project_id 仅 action='query'/'transition' 可用" in out["error"]

    out = await _server().project(action="query", project_id="pid", goal="G")
    assert out["success"] is False
    assert "goal 仅 action='create' 可用" in out["error"]


async def test_project_dispatch_query_missing_id_teaches_list() -> None:
    """query 缺 project_id → 实现体教学错误原样透出（指向 action=list）。"""
    out = await _server().project(action="query")
    assert out["success"] is False
    assert "project_id" in out["error"]
    assert "action=list" in out["error"]


async def test_project_dispatch_create_routes_to_create_tool(env) -> None:
    """create 分支真连实现体：经 server 分发创建项目（非 mock 直通）。"""
    out = await _server().project(action="create", goal="分发创建")
    assert out["created"] is True, out  # 成功路径直返 output（无 success 信封键）
    assert out["title"] == "分发创建"


async def test_project_dispatch_failure_envelope(env) -> None:
    """实现体失败（未知 project_id）→ server 失败信封（_run_project_tool 错误分支）。"""
    out = await _server().project(action="query", project_id="deadbeef0000")
    assert out["success"] is False
    assert "不存在" in out["error"]
