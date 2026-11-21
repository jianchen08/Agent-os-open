# @feature: FP-0.2.grants_ledger P1 对账 | @ci: python-coverage
"""proto-grant JSON 真源 ↔ 账本/投影 对账测试（P0 遗留项 5，执行方案 §2 验收 2）。

防漂移契约（ADR 2026-11-19 D3「proto-grant 出现 JSON 真源 + 账本投影双面，
读取面防漂移由对账测试承接」）：
1. 投影双射：JSON 真源条目集合 == 账本投影条目集合（管道级免审批 / 永久白名单两真源文件）；
2. 审批卡授予面双写后：永久白名单 JSON 条目 == 账本 standing grant 条目 == 投影条目；
3. 管道级免审批 JSON 条目 == 账本 task grant 条目 == 投影条目（subject/tool/action/granularity 同构）；
4. 写区授权真源经 security_check 既有读取路径（project.load_zones 服务形态）可达，
   entries 路径集合 == 投影 resource_pattern 集合。

JSON 真源写面格式不动（v1 真源）；账本为双写过渡面。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_DIR = str(Path(__file__).resolve().parents[1])
_GRANTS_DIR = str(Path(_SHARED_DIR) / "grants")
for _p in (_SHARED_DIR, _THIS_DIR, _GRANTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_spec = importlib.util.spec_from_file_location(
    "security_check_plugin_proto_reconcile", str(Path(_THIS_DIR) / "plugin.py")
)
assert _spec is not None
assert _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import proto_adapters as grants_proto  # noqa: E402 — grants 平铺模块
from ledger import GrantsLedger  # noqa: E402

pytestmark = pytest.mark.unit

_STATE_DIR: Path | None = None


# ── 装配（与七断言文件同款最小桩）───────────────────────────────────────────


class FakeHiCap:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.cards: list[dict[str, Any]] = []
        self._last_options: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        if method == "create_choice":
            self.cards.append(params)
            self._last_options = list(params.get("options") or [])
            return {"request_id": f"req-{len(self.cards)}"}
        if method == "wait_for_choice":
            answer = self.answers.pop(0)
            semantics = ""
            for opt in self._last_options:
                if opt.get("id") == answer:
                    semantics = str(opt.get("semantics") or "")
                    break
            return {"selected_option": answer, "selected_semantics": semantics, "response_type": "answered"}
        raise AssertionError(f"unexpected hi cap.call: {method}")


class FakeLedgerCap:
    def __init__(self, ledger: GrantsLedger) -> None:
        self.ledger = ledger

    async def call(self, method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        if method == "request":
            return {"ok": True, "grant_id": self.ledger.request(**params)}
        if method == "authorize":
            grant = self.ledger.authorize(
                subject_kind=str(params["subject_kind"]),
                subject_id=str(params["subject_id"]),
                tool=str(params["tool"]),
                action=str(params["action"]),
            )
            return {"authorized": grant is not None, "grant": grant}
        if method == "consume":
            return {"ok": True, "grant": self.ledger.consume(grant_id=str(params["grant_id"]))}
        raise AssertionError(f"unexpected ledger method: {method}")


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    global _STATE_DIR  # noqa: PLW0603
    _STATE_DIR = tmp_path
    scope_mod = sys.modules["approval_scope"]
    monkeypatch.setattr(_mod, "_PERMISSION_MODES_FILE", str(tmp_path / "permission_modes.json"))
    monkeypatch.setattr(_mod, "_PERMISSION_MODES", {})
    scope_mod.PIPELINE_PERMISSION_MODES.clear()
    scope_mod.PIPELINE_PARENTS.clear()
    scope_mod.PERMANENT_WHITELIST.clear()
    _mod.set_human_interaction_cap(None)
    _mod.set_grants_caps(None, None)
    _mod.set_grants_ref_resolver(None)
    yield tmp_path
    _mod.set_human_interaction_cap(None)
    _mod.set_grants_caps(None, None)
    _mod.set_grants_ref_resolver(None)
    scope_mod.PIPELINE_PERMISSION_MODES.clear()
    scope_mod.PIPELINE_PARENTS.clear()
    scope_mod.PERMANENT_WHITELIST.clear()
    _STATE_DIR = None


def _ctx(command: str = "pip install requests", pipeline_id: str = "p1") -> Any:
    return _mod.PluginContext(
        state={
            "core_type": "tool_execute",
            "raw_tool_calls": [{"name": "bash", "args": {"command": command}}],
            "execution_contexts": [{"tool_name": "bash", "provider": "host"}],
            "pipeline_id": pipeline_id,
            "session_id": "s1",
            "user_id": "u1",
        },
        _services={},
    )


def _rules() -> list[dict[str, Any]]:
    return [
        {
            "name": "dangerous_commands",
            "tools": ["*"],
            "params": ["command", "cmd"],
            "action": "needs_approval",
            "patterns": [{"type": "keyword", "value": "pip install"}],
        }
    ]


def _json_entries(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ── 1. 投影双射（真源文件 → 投影，逐条同构无漂移）───────────────────────────


class TestProjectionBijection:
    def test_whitelist_source_set_equals_projection_set(self, tmp_path: Path) -> None:
        src = tmp_path / "permanent_whitelist.json"
        entries = ["git status", "npm run test", "pytest -q"]
        src.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        projected = grants_proto.project_whitelist(str(src))
        # 对账主断言：真源条目集合 == 投影条目集合（grant_id 稳定双射，无跳条/重复）
        source_ids = {f"proto:command_whitelist:{e}" for e in entries}
        assert {g["grant_id"] for g in projected} == source_ids
        assert {g["tool"] for g in projected} == set(entries)
        assert len(projected) == len(entries)

    def test_pipeline_modes_source_set_equals_projection_set(self, tmp_path: Path) -> None:
        src = tmp_path / "permission_modes_pipeline.json"
        modes = {"p-a": "bypass", "p-b": "default"}
        src.write_text(json.dumps(modes, ensure_ascii=False), encoding="utf-8")
        projected = grants_proto.project_pipeline_modes(str(src))
        assert {g["subject_id"] for g in projected} == set(modes.keys())
        for g in projected:
            assert g["scope"]["mode"] == modes[g["subject_id"]]
            assert g["granularity"] == "task"
            assert g["subject_kind"] == "pipeline"
            assert g["tool"] == "*"
            assert g["action"] == "*"

    def test_security_check_state_path_derivation_reaches_projection(self, tmp_path: Path, monkeypatch: Any) -> None:
        """真源落点经 security_check 同目录派生（state_file_path）可达投影——路径面同源。"""
        monkeypatch.setattr(_mod, "_PERMISSION_MODES_FILE", str(tmp_path / "permission_modes.json"))
        wl_path = Path(_mod._state_file_path("permanent_whitelist.json"))
        assert wl_path.parent == tmp_path
        wl_path.write_text(json.dumps(["git status"], ensure_ascii=False), encoding="utf-8")
        projected = grants_proto.project_whitelist(wl_path)
        assert [g["tool"] for g in projected] == ["git status"]


# ── 2. 审批卡授予面双写对账（真源 == 账本 == 投影）──────────────────────────


@pytest.mark.asyncio
async def test_permanent_whitelist_dual_write_reconciles() -> None:
    """永久白名单：批准后 JSON 条目 == 账本 standing grant 条目 == 投影条目。"""
    assert _STATE_DIR is not None
    ledger = GrantsLedger(db_path=str(_STATE_DIR / "grants.db"))
    hi = FakeHiCap(["approved_permanent_whitelist"])
    _mod.set_human_interaction_cap(hi)
    _mod.set_grants_caps(None, FakeLedgerCap(ledger))
    try:
        plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})
        result = await plugin.execute(_ctx(command="pip install requests"))
        assert not bool(result.state_updates.get("pre_decided_results"))
    finally:
        _mod.set_grants_caps(None, None)

    wl_path = _mod._state_file_path("permanent_whitelist.json")
    source_entries = set(_json_entries(Path(wl_path)))
    assert source_entries, "白名单真源须已落盘（v1 写面不动）"
    assert set(_mod._PERMANENT_WHITELIST) == source_entries

    # 账本双写行（approval_card 来源 standing）与真源集合一致
    ledger_rows = [r for r in ledger.list() if r["granularity"] == "standing"]
    assert {r["tool"] for r in ledger_rows} == source_entries
    assert all(r["subject_kind"] == "user" and r["subject_id"] == "*" for r in ledger_rows)
    assert all(r["source"] == "approval_card" for r in ledger_rows)

    # 投影与真源、与账本三方集合一致（防漂移对账主断言）
    projected = grants_proto.project_whitelist(wl_path)
    assert {g["grant_id"] for g in projected} == {f"proto:command_whitelist:{e}" for e in source_entries}
    assert {g["tool"] for g in projected} == {r["tool"] for r in ledger_rows}


@pytest.mark.asyncio
async def test_pipeline_scope_dual_write_reconciles() -> None:
    """管道级免审批：JSON 条目 == 账本 task grant 条目 == 投影条目（同构字段一致）。"""
    assert _STATE_DIR is not None
    ledger = GrantsLedger(db_path=str(_STATE_DIR / "grants.db"))
    hi = FakeHiCap(["approved_pipeline_scope", "confirm", "approved_once"])
    _mod.set_human_interaction_cap(hi)
    _mod.set_grants_caps(None, FakeLedgerCap(ledger))
    try:
        plugin = _mod.SecurityCheckPlugin(config={"enabled": True, "rules": _rules()})
        await plugin.execute(_ctx(command="pip install requests"))
    finally:
        _mod.set_grants_caps(None, None)

    pm_path = _mod._state_file_path("permission_modes_pipeline.json")
    source_modes = _json_entries(Path(pm_path))
    assert source_modes == {"p1": "bypass"}, "管道覆盖表真源写面不动"

    ledger_rows = [r for r in ledger.list(subject_kind="pipeline") if r["granularity"] == "task"]
    assert len(ledger_rows) == 1
    row = ledger_rows[0]
    assert row["subject_id"] == "p1"
    assert row["tool"] == "*"
    assert row["action"] == "*"
    assert row["source"] == "approval_card"

    projected = grants_proto.project_pipeline_modes(pm_path)
    assert len(projected) == 1
    proj = projected[0]
    # 投影 == 账本行同构字段对账
    assert proj["subject_kind"] == row["subject_kind"]
    assert proj["subject_id"] == row["subject_id"]
    assert proj["tool"] == row["tool"]
    assert proj["action"] == row["action"]
    assert proj["granularity"] == row["granularity"]
    # 真源条目集合 == 投影条目集合
    assert {g["subject_id"] for g in projected} == set(source_modes.keys())


# ── 3. 写区授权真源经既有读取路径接线（P0 遗留：loader 注入）───────────────


class FakeProjectCap:
    """project 假 capability：load_zones 形态与 security_check 既有消费一致。

    非 dict 结果原样透传（真源形态异常面可测）。
    """

    def __init__(self, result: Any) -> None:
        self.result = result

    async def call(self, method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        assert method == "load_zones"
        return dict(self.result) if isinstance(self.result, dict) else self.result


class FakeRef:
    def __init__(self, cap: FakeProjectCap | None) -> None:
        self._cap = cap

    def get_capability(self, name: str) -> Any:
        assert name == "project"
        if self._cap is None:
            raise KeyError("capability not injected: project")
        return self._cap


@pytest.mark.asyncio
async def test_zone_entries_via_service_loader_reconcile() -> None:
    """写区真源：security_check 既有读取路径（project.load_zones）→ loader → 投影，集合一致。"""
    ref = FakeRef(FakeProjectCap({
        "entries": [{"path": "D:/workspaces/task-1/**", "pipeline_id": "pipe-9"}, {"path": "V:/shared/**"}],
        "read_deny": [],
        "read_allow": [],
    }))
    entries = await _mod.zone_entries_via_project_service(ref)
    assert {e["path"] for e in entries} == {"D:/workspaces/task-1/**", "V:/shared/**"}

    grants = await grants_proto.project_zone_grants_async(lambda: entries)
    assert {g["scope"]["resource_pattern"] for g in grants} == {e["path"] for e in entries}
    assert grants[0]["subject_id"] == "pipe-9"
    assert all(g["source"] == "proto_projection" for g in grants)


@pytest.mark.asyncio
async def test_zone_service_unavailable_projects_empty() -> None:
    """真源服务不可达 → entries 空（范围收缩 fail-closed），投影为空不放大。"""
    entries = await _mod.zone_entries_via_project_service(FakeRef(None))
    assert entries == []
    grants = await grants_proto.project_zone_grants_async(lambda: entries)
    assert grants == []


@pytest.mark.asyncio
async def test_async_loader_supported_for_service_shape() -> None:
    """loader 可为 async 形态（服务调用天然异步）——投影统一入口双形态兼容。"""

    async def async_loader() -> list[dict[str, Any]]:
        return [{"path": "D:/ws/**", "pipeline_id": "p-1"}]

    grants = await grants_proto.project_zone_grants_async(async_loader)
    assert [g["scope"]["resource_pattern"] for g in grants] == ["D:/ws/**"]


# ── 4. 装配缝与真源降级面（diff coverage 清偿）──────────────────────────────


def test_get_grants_caps_resolver_raising_degrades_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """ref 解析缝异常 → (None, None)（执法侧 inert 降级，不抛出）。"""
    import approval_scope

    calls = {"n": 0}

    def _boom() -> Any:
        calls["n"] += 1
        raise RuntimeError("plugin ref gone")

    approval_scope.set_grants_caps(None, None)
    approval_scope.set_grants_ref_resolver(_boom)
    try:
        policy_cap, ledger_cap = approval_scope.get_grants_caps()
        assert policy_cap is None
        assert ledger_cap is None
        assert calls["n"] == 1
    finally:
        approval_scope.set_grants_ref_resolver(None)
        approval_scope.set_grants_caps(None, None)


@pytest.mark.asyncio
async def test_zone_service_malformed_result_projects_empty() -> None:
    """真源形态异常：非 dict 结果 / entries 非列表 / 条目非 dict → 空投影或跳条。"""
    ref = FakeRef(FakeProjectCap("not-a-dict"))
    assert await _mod.zone_entries_via_project_service(ref) == []

    ref2 = FakeRef(FakeProjectCap({"entries": "not-a-list"}))
    assert await _mod.zone_entries_via_project_service(ref2) == []

    ref3 = FakeRef(FakeProjectCap({"entries": ["str-entry", {"path": "D:/ws/**", "pipeline_id": "p-1"}]}))
    entries = await _mod.zone_entries_via_project_service(ref3)
    assert [e["path"] for e in entries] == ["D:/ws/**"]


@pytest.mark.asyncio
async def test_zone_async_loader_none_and_raising_projects_empty() -> None:
    """async loader 缺席/异常 → 空投影（与同步版语义一致）。"""
    assert await grants_proto.project_zone_grants_async(None) == []

    async def _boom() -> list[dict[str, Any]]:
        raise RuntimeError("service unreachable")

    assert await grants_proto.project_zone_grants_async(_boom) == []
