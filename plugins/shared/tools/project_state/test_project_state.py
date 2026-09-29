# @feature: FP-0.2.二 项目状态门 | @ci: python-coverage
"""project_state 工具测试（方案工作流状态查询/列举与门控迁移，ADR 2026-09-17）。

覆盖：query 快照；list 全量列举（空登记/多项目倒序/无需 project_id）；
transition 合法边（plan→running / running→plan）+ 持久化；缺 target/reason
拒绝；非法边 fail-closed；done 终态不可迁；同态幂等拒绝；项目不存在拒绝；
工具定义形状 + manifest 声明一致性。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# 共享层自举（plugins/shared/ —— project_registry 所在）
_SHARED_ROOT = os.path.abspath(os.path.join(_PLUGIN_DIR, "..", ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)

# 串扰防线：共享层裸名逐出 + 置顶（与 test_project_registry 同款）
import project_registry as _pr  # noqa: E402


def _load_module() -> Any:
    mod_name = "project_state_tool_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "tool.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[mod_name]
        raise
    return module


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离登记目录（不落真实 tasks 数据根）。"""
    monkeypatch.setenv("TASKS_STORAGE_DIR", str(tmp_path / "tasks_data"))
    return tmp_path


def _tool() -> Any:
    mod = _load_module()
    return mod.ProjectStateTool()


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
        mod = _load_module()
        d = mod.ProjectStateTool.get_tool_definition()
        assert d.name == "project_state"
        # project_id 按 action 条件必填（query/transition 运行时校验），schema 不再整表必填
        assert d.input_schema.get("required") is None
        enum_vals = d.input_schema["properties"]["action"]["enum"]
        assert enum_vals == ["query", "list", "transition"]
        assert d.output_schema.get("required") is None
        assert "projects" in d.output_schema["properties"]

    def test_manifest_declaration_matches_definition(self) -> None:
        """manifest 声明与 tool.py 单源一致（声明单源化护栏）。"""
        import json

        mod = _load_module()
        d = mod.ProjectStateTool.get_tool_definition()
        manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        decl = manifest["capabilities"]["tools"][0]
        assert decl["description"] == d.description
        assert decl["input_schema"] == d.input_schema
        assert decl["output_schema"] == d.output_schema
