# -*- coding: utf-8 -*-
# @feature: FP-0.2.二 eval_harness 覆盖率收口 | @ci: python-coverage
"""eval_harness server.py 缺口分支补测（覆盖率收口批）。

靶面：账本读写（含损坏账本降级）、summary 落盘、聚合基线取数、held-out 派发
失败分支、提案三工具（submit/apply/reject）全链与拒绝分支、_git 封装。

隔离：所有用例把 server._EVAL_REPORTS 与 server._PROJECT_ROOT 指向 tmp_path，
真实读写 tmp 目录——提案应用真的写文件、真的跑 git（在 tmp 下 init 的仓），
不触碰主仓。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(__file__))

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))


def _load_server_module():
    """按唯一模块名装载 server.py（避开车道内裸名 server 的跨插件互覆）。"""
    path = os.path.join(os.path.dirname(__file__), "server.py")
    spec = importlib.util.spec_from_file_location("eval_harness_service_server", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server_env(tmp_path, monkeypatch):
    """把账本目录与项目根都指到 tmp：真实落盘但不污染主仓。"""
    server = _load_server_module()
    reports = tmp_path / "reports" / "eval"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(server, "_EVAL_REPORTS", str(reports))
    monkeypatch.setattr(server, "_PROJECT_ROOT", str(project))
    return server


def _init_git_repo(root: str) -> None:
    """在 tmp 项目根初始化 git 仓（提案 apply 路径真实依赖 git add/commit）。"""
    env = {**os.environ, "GIT_AUTHOR_NAME": "cov", "GIT_AUTHOR_EMAIL": "cov@local",
           "GIT_COMMITTER_NAME": "cov", "GIT_COMMITTER_EMAIL": "cov@local"}
    for args in (["init"], ["config", "user.email", "cov@local"],
                 ["config", "user.name", "cov"]):
        subprocess.run(["git", *args], cwd=root, capture_output=True, check=True, env=env)
    # 需要一个 HEAD 才能 commit（空仓 commit 也可，但 rev-parse 需有提交）
    (open(os.path.join(root, ".gitkeep"), "w")).close()
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=True, env=env)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True,
                   check=True, env=env)


# ── 账本读写 ─────────────────────────────────────────────────────────────
def test_load_ledger_absent_returns_empty_shape(server_env):
    """账本不存在 → 返回空结构（不抛错）。"""
    ledger = server_env._load_ledger()
    assert ledger == {"rounds": [], "proposals": {}, "levers": {}}


def test_save_then_load_ledger_roundtrip(server_env):
    """save→load 往返保留结构与中文内容（中文不转义）。"""
    server_env._save_ledger({"rounds": [{"mode": "coding", "passed": 3}],
                             "proposals": {}, "levers": {"杠杆": 1}})
    ledger = server_env._load_ledger()
    assert ledger["rounds"][0]["mode"] == "coding"
    assert ledger["levers"]["杠杆"] == 1
    raw = open(os.path.join(server_env._EVAL_REPORTS, "eval_state.json"),
               encoding="utf-8").read()
    assert "杠杆" in raw  # ensure_ascii=False


def test_load_ledger_corrupt_json_falls_back_to_empty(server_env):
    """账本损坏（非法 JSON）→ 降级为空结构而非崩溃。"""
    os.makedirs(server_env._EVAL_REPORTS, exist_ok=True)
    with open(os.path.join(server_env._EVAL_REPORTS, "eval_state.json"), "w",
              encoding="utf-8") as fh:
        fh.write("{ broken json")
    assert server_env._load_ledger() == {"rounds": [], "proposals": {}, "levers": {}}


# ── summary 落盘 ─────────────────────────────────────────────────────────
def test_save_summary_writes_run_id_mode_and_payload(server_env):
    """summary.json 含 run_id/mode 与负载字段，嵌套目录自动创建。"""
    path = server_env._save_summary("r1", "coding", {"passed": 2, "total": 3})
    assert os.path.isfile(path)
    data = json.load(open(path, encoding="utf-8"))
    assert data["run_id"] == "r1"
    assert data["mode"] == "coding"
    assert data["passed"] == 2
    assert data["total"] == 3


# ── eval_summarize：基线取数 ─────────────────────────────────────────────
def test_summarize_uses_latest_same_mode_round_as_baseline(server_env):
    """同 mode 的最近一轮 passed 作为 baseline 计算 delta；异 mode 轮次不参与。"""
    server_env._save_ledger({
        "rounds": [
            {"run_id": "old", "mode": "coding", "passed": 1, "total": 3},
            {"run_id": "other", "mode": "novel", "passed": 9, "total": 9},
            {"run_id": "new", "mode": "coding", "passed": 2, "total": 3},
        ],
        "proposals": {}, "levers": {},
    })

    out = asyncio.run(server_env.eval_summarize("coding", [
        {"case_id": "c1", "task_status": "completed", "criteria": {"semantic_check": True}},
        {"case_id": "c2", "task_status": "completed", "criteria": {"semantic_check": True}},
        {"case_id": "c3", "task_status": "failed", "criteria": {}},
    ]))

    assert out["success"] is True
    assert out["passed"] == 2
    assert out["total"] == 3
    # baseline = 最近一轮 coding 的 2 → delta = 2 - 2 = 0
    assert out["delta"] == 0
    assert os.path.isfile(out["summary_path"])
    # 轮次已入账，且本轮记录的失败 case 为 c3
    ledger = server_env._load_ledger()
    assert ledger["rounds"][-1]["run_id"] == out["run_id"]
    assert ledger["rounds"][-1]["failed_cases"] == ["c3"]


def test_summarize_no_baseline_leaves_delta_none(server_env):
    """无同 mode 历史轮次 → delta 为 None（首轮无对比）。"""
    out = asyncio.run(server_env.eval_summarize("coding", [
        {"case_id": "c1", "task_status": "completed", "criteria": {"x": True}},
    ]))
    assert out["delta"] is None
    assert out["passed"] == 1


def test_summarize_caps_ledger_rounds_at_200(server_env):
    """轮次账本滚动上限 200：超出后只保留最近 200 条。"""
    server_env._save_ledger({
        "rounds": [{"run_id": f"r{i}", "mode": "coding", "passed": 0, "total": 1}
                   for i in range(205)],
        "proposals": {}, "levers": {},
    })
    asyncio.run(server_env.eval_summarize("coding", [
        {"case_id": "c1", "task_status": "completed", "criteria": {"x": True}},
    ]))
    rounds = server_env._load_ledger()["rounds"]
    assert len(rounds) == 200
    assert rounds[-1]["mode"] == "coding"


def test_summarize_keeps_previous_rounds_from_non_matching_modes(server_env):
    """baseline 扫描跨全轮次但只认同 mode；异 mode 轮次在 delta 无匹配时置 None。"""
    server_env._save_ledger({
        "rounds": [{"run_id": "n1", "mode": "novel", "passed": 5, "total": 5}],
        "proposals": {}, "levers": {},
    })
    out = asyncio.run(server_env.eval_summarize("coding", [
        {"case_id": "c1", "task_status": "completed", "criteria": {"x": True}},
    ]))
    assert out["delta"] is None


# ── eval_run_heldout 失败分支 ────────────────────────────────────────────
def test_heldout_without_env_reports_fail_closed(server_env, monkeypatch):
    """AGENTOS_HELDOUT_DIR 未配置 → fail-closed 报错。"""
    monkeypatch.delenv("AGENTOS_HELDOUT_DIR", raising=False)
    out = asyncio.run(server_env.eval_run_heldout(mode="coding"))
    assert out["success"] is False
    assert "AGENTOS_HELDOUT_DIR" in out["error"]


def test_heldout_missing_suite_reports_error(server_env, tmp_path, monkeypatch):
    """held-out 目录存在但缺该 mode 题集 → 报缺失路径。"""
    monkeypatch.setenv("AGENTOS_HELDOUT_DIR", str(tmp_path))
    out = asyncio.run(server_env.eval_run_heldout(mode="coding"))
    assert out["success"] is False
    assert "不存在" in out["error"]


def test_heldout_dispatch_error_is_recorded_per_case(server_env, tmp_path, monkeypatch):
    """单条派发抛错 → 记入该 case 的 error 字段并继续后续 case（不中断整批）。"""
    (tmp_path / "coding.yaml").write_text(yaml.safe_dump(
        {"mode": "coding",
         "cases": [{"id": "h1", "messages": ["M1"], "acceptance_criteria": {}},
                   {"id": "h2", "messages": ["M2"], "acceptance_criteria": {}}]},
        allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("AGENTOS_HELDOUT_DIR", str(tmp_path))
    seen = []

    class _Cap:
        async def call(self, method, params, timeout=None):
            seen.append(params["args"]["goal_title"])
            if "h1" in params["args"]["goal_title"]:
                raise RuntimeError("派发通道故障")
            return {"data": {"task_id": "t-h2"}}

    class _Plug:
        def get_capability(self, name):
            return _Cap()

    monkeypatch.setattr(server_env, "plugin", _Plug())
    out = asyncio.run(server_env.eval_run_heldout(mode="coding"))

    assert out["success"] is True
    by_case = {d["case_id"]: d for d in out["dispatched"]}
    assert "派发通道故障" in by_case["h1"]["error"]
    assert by_case["h2"]["task_id"] == "t-h2"
    assert len(seen) == 2  # 失败后仍继续派发下一条


def test_heldout_missing_task_id_in_response_yields_empty_string(
    server_env, tmp_path, monkeypatch
):
    """派发响应缺 task_id/id → 映射里落空串（不抛错）。"""
    (tmp_path / "coding.yaml").write_text(yaml.safe_dump(
        {"mode": "coding",
         "cases": [{"id": "h1", "messages": ["M1"], "acceptance_criteria": {}}]},
        allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("AGENTOS_HELDOUT_DIR", str(tmp_path))

    class _Cap:
        async def call(self, method, params, timeout=None):
            return {"data": {"unrelated": True}}

    class _Plug:
        def get_capability(self, name):
            return _Cap()

    monkeypatch.setattr(server_env, "plugin", _Plug())
    out = asyncio.run(server_env.eval_run_heldout(mode="coding"))
    assert out["dispatched"][0]["task_id"] == ""


# ── proposal_submit / apply / reject ─────────────────────────────────────
_LEGAL_TARGET = "config/rules/cov_gap_rule.md"


def test_proposal_submit_accepted_writes_archive(server_env):
    """合法提案（L3 新文件）→ accepted=True，存档文件落盘且账本记录 submitted。"""
    out = asyncio.run(server_env.proposal_submit(
        layer="L3", target=_LEGAL_TARGET, change_type="new_plugin",
        content="规则正文", motivation="补齐缺口"))

    assert out["success"] is True
    assert out["accepted"] is True
    assert out["violations"] == []
    archive = os.path.join(server_env._EVAL_REPORTS, "proposals",
                           f"{out['proposal_id']}.json")
    assert os.path.isfile(archive)
    stored = server_env._load_ledger()["proposals"][out["proposal_id"]]
    assert stored["outcome"] == "submitted"
    assert stored["motivation"] == "补齐缺口"


def test_proposal_submit_rejected_records_violations(server_env):
    """非法提案（冻结面）→ accepted=False，违规入账且不落存档。"""
    out = asyncio.run(server_env.proposal_submit(
        layer="L1", target="kernel/crates/api/src/routes.rs", change_type="content_edit",
        content="x", motivation="试探"))

    assert out["success"] is True
    assert out["accepted"] is False
    assert any("frozen" in v for v in out["violations"])
    archive = os.path.join(server_env._EVAL_REPORTS, "proposals",
                           f"{out['proposal_id']}.json")
    assert not os.path.isfile(archive)
    assert server_env._load_ledger()["proposals"][out["proposal_id"]]["outcome"] == "rejected"


def test_proposal_submit_truncates_long_motivation(server_env):
    """motivation 超长 → 账本只留前 200 字符。"""
    out = asyncio.run(server_env.proposal_submit(
        layer="L3", target=_LEGAL_TARGET, change_type="new_plugin",
        content="x", motivation="长" * 500))
    stored = server_env._load_ledger()["proposals"][out["proposal_id"]]
    assert len(stored["motivation"]) == 200


def test_proposal_apply_unknown_id_is_error(server_env):
    """未知 proposal_id → 错误值（不抛异常）。"""
    out = asyncio.run(server_env.proposal_apply("p_ghost"))
    assert out["success"] is False
    assert "不存在或状态不可应用" in out["error"]


def test_proposal_apply_rejected_outcome_is_not_appliable(server_env):
    """状态非 submitted（已驳回）→ 拒绝应用。"""
    out = asyncio.run(server_env.proposal_submit(
        layer="L1", target="kernel/x.rs", change_type="content_edit",
        content="x", motivation="m"))
    asyncio.run(server_env.proposal_reject(out["proposal_id"], "不合规"))
    applied = asyncio.run(server_env.proposal_apply(out["proposal_id"]))
    assert applied["success"] is False
    assert "不存在或状态不可应用" in applied["error"]


def test_proposal_apply_missing_archive_is_error(server_env):
    """账本记为 submitted 但存档被删 → 报存档缺失。"""
    out = asyncio.run(server_env.proposal_submit(
        layer="L3", target=_LEGAL_TARGET, change_type="new_plugin",
        content="x", motivation="m"))
    archive = os.path.join(server_env._EVAL_REPORTS, "proposals",
                           f"{out['proposal_id']}.json")
    os.remove(archive)

    applied = asyncio.run(server_env.proposal_apply(out["proposal_id"]))
    assert applied["success"] is False
    assert "提案存档缺失" in applied["error"]


def test_proposal_apply_writes_file_and_commits(server_env):
    """全链成功路径：content 写入目标文件、git add+commit、账本记 applied+commit。"""
    _init_git_repo(server_env._PROJECT_ROOT)
    out = asyncio.run(server_env.proposal_submit(
        layer="L3", target=_LEGAL_TARGET, change_type="new_plugin",
        content="规则正文\n第二行\n", motivation="m"))

    applied = asyncio.run(server_env.proposal_apply(out["proposal_id"], message="evolve: cov"))
    assert applied["success"] is True
    assert applied["applied"] is True
    assert applied["target"] == _LEGAL_TARGET
    assert len(applied["commit"]) >= 7  # rev-parse --short

    written = open(os.path.join(server_env._PROJECT_ROOT, _LEGAL_TARGET),
                   encoding="utf-8").read()
    assert written == "规则正文\n第二行\n"

    stored = server_env._load_ledger()["proposals"][out["proposal_id"]]
    assert stored["outcome"] == "applied"
    assert stored["commit"] == applied["commit"]


def test_proposal_apply_git_commit_failure_reports_error(server_env, monkeypatch):
    """git commit 失败（非 git 目录）→ 如实报错，不谎报 applied。"""
    out = asyncio.run(server_env.proposal_submit(
        layer="L3", target=_LEGAL_TARGET, change_type="new_plugin",
        content="x", motivation="m"))
    # 项目根未 init git → add 就会失败
    applied = asyncio.run(server_env.proposal_apply(out["proposal_id"]))
    assert applied["success"] is False
    assert "git" in applied["error"]


def test_proposal_apply_recheck_rejects_tampered_archive(server_env):
    """应用前复核：存档被篡改为非法内容（冻结面）→ 拒绝应用。"""
    out = asyncio.run(server_env.proposal_submit(
        layer="L3", target=_LEGAL_TARGET, change_type="new_plugin",
        content="x", motivation="m"))
    archive = os.path.join(server_env._EVAL_REPORTS, "proposals",
                           f"{out['proposal_id']}.json")
    tampered = json.load(open(archive, encoding="utf-8"))
    tampered["target"] = "kernel/crates/api/src/routes.rs"
    with open(archive, "w", encoding="utf-8") as fh:
        json.dump(tampered, fh, ensure_ascii=False)

    applied = asyncio.run(server_env.proposal_apply(out["proposal_id"]))
    assert applied["success"] is False
    assert "应用前复核未过" in applied["error"]


def test_proposal_reject_unknown_id_is_error(server_env):
    """驳回未知提案 → 错误值。"""
    out = asyncio.run(server_env.proposal_reject("p_ghost", "无此提案"))
    assert out["success"] is False
    assert "不存在" in out["error"]


def test_proposal_reject_marks_outcome_and_removes_archive(server_env):
    """驳回已受理提案 → 存档删除、账本记 rejected+原因。"""
    out = asyncio.run(server_env.proposal_submit(
        layer="L3", target=_LEGAL_TARGET, change_type="new_plugin",
        content="x", motivation="m"))
    archive = os.path.join(server_env._EVAL_REPORTS, "proposals",
                           f"{out['proposal_id']}.json")
    assert os.path.isfile(archive)

    rejected = asyncio.run(server_env.proposal_reject(out["proposal_id"], "验收不达标"))
    assert rejected["success"] is True
    assert not os.path.isfile(archive)
    stored = server_env._load_ledger()["proposals"][out["proposal_id"]]
    assert stored["outcome"] == "rejected"
    assert stored["reason"] == "验收不达标"


def test_proposal_reject_without_archive_still_records(server_env):
    """存档缺失时驳回仍成功（幂等清理：不因缺文件报错）。"""
    out = asyncio.run(server_env.proposal_submit(
        layer="L3", target=_LEGAL_TARGET, change_type="new_plugin",
        content="x", motivation="m"))
    os.remove(os.path.join(server_env._EVAL_REPORTS, "proposals",
                           f"{out['proposal_id']}.json"))

    rejected = asyncio.run(server_env.proposal_reject(out["proposal_id"], "r"))
    assert rejected["success"] is True
    assert server_env._load_ledger()["proposals"][out["proposal_id"]]["outcome"] == "rejected"


# ── 重放键 / 驳回分类账 / P0-P3 裁决透出（ADR 2026-09-16） ───────────────
def test_eval_summarize_round_records_replay_keys(server_env):
    """round 落账携带重放键：逐 case 结果 / model / material_ref（非 git 根为空串）。"""
    results = [
        {"case_id": "c1", "task_status": "completed", "criteria": {"file_check": True}},
        {"case_id": "c2", "task_status": "completed", "criteria": {"file_check": False}},
    ]
    out = asyncio.run(server_env.eval_summarize("coding", results, model="deepseek-v4"))
    assert out["success"] is True
    round_row = server_env._load_ledger()["rounds"][-1]
    assert round_row["case_results"] == {"c1": True, "c2": False}
    assert round_row["model"] == "deepseek-v4"
    assert isinstance(round_row["material_ref"], str)  # tmp 非 git 根 → 空串不阻断


def test_eval_summarize_without_model_keeps_empty_replay_field(server_env):
    """未传 model → 落空串（字段形状稳定，指标侧按缺数据降级）。"""
    asyncio.run(server_env.eval_summarize("writing", [
        {"case_id": "w1", "task_status": "completed", "criteria": {"x": True}}]))
    assert server_env._load_ledger()["rounds"][-1]["model"] == ""


def test_proposal_reject_category_validation_and_ledger(server_env):
    """非法分类 → 错误值；合法分类（外因记账）入账。"""
    out = asyncio.run(server_env.proposal_submit(
        layer="L3", target=_LEGAL_TARGET, change_type="new_plugin",
        content="x", motivation="m"))
    bad = asyncio.run(server_env.proposal_reject(out["proposal_id"], "r",
                                                 category="随便写的"))
    assert bad["success"] is False and "category 非法" in bad["error"]

    out2 = asyncio.run(server_env.proposal_submit(
        layer="L3", target=_LEGAL_TARGET, change_type="new_plugin",
        content="y", motivation="m2"))
    ok = asyncio.run(server_env.proposal_reject(out2["proposal_id"], "供应商超时窗口",
                                                category="external_cause"))
    assert ok["success"] is True
    stored = server_env._load_ledger()["proposals"][out2["proposal_id"]]
    assert stored["category"] == "external_cause"


def test_proposal_reject_on_applied_marks_reverted_and_keeps_archive(server_env):
    """用户裁决回滚通道：applied → reverted，存档保留供审计（命中率自动扣回）。"""
    _init_git_repo(server_env._PROJECT_ROOT)
    out = asyncio.run(server_env.proposal_submit(
        layer="L3", target=_LEGAL_TARGET, change_type="new_plugin",
        content="正文", motivation="m"))
    applied = asyncio.run(server_env.proposal_apply(out["proposal_id"]))
    assert applied["success"] is True
    archive = os.path.join(server_env._EVAL_REPORTS, "proposals",
                           f"{out['proposal_id']}.json")

    reverted = asyncio.run(server_env.proposal_reject(
        out["proposal_id"], "派发守则教坏了 agent", category="user_veto"))
    assert reverted["success"] is True
    assert reverted["outcome"] == "reverted"
    assert os.path.isfile(archive)  # 已应用提案的存档保留（审计），不删
    stored = server_env._load_ledger()["proposals"][out["proposal_id"]]
    assert stored["outcome"] == "reverted"
    assert stored["category"] == "user_veto"
    assert stored["commit"] == applied["commit"]


def test_eval_health_returns_evolution_verdict_and_breaker(server_env):
    """eval_health 透出 P0-P3 裁决；用户否决 → breaker_tripped=True（熔断语义）。"""
    server_env._save_ledger({
        "rounds": [
            {"run_id": "r1", "mode": "coding", "passed": 5, "total": 10},
            {"run_id": "r2", "mode": "coding", "passed": 7, "total": 10},
        ],
        "proposals": {"p1": {"outcome": "reverted", "category": "user_veto"}},
        "levers": {}})
    out = asyncio.run(server_env.eval_health())
    assert out["success"] is True
    assert out["evolution"]["verdict"] == "vetoed"
    assert out["breaker_tripped"] is True
    assert out["evolution"]["metrics_version"]

    server_env._save_ledger({"rounds": [], "proposals": {}, "levers": {}})
    empty = asyncio.run(server_env.eval_health())
    assert empty["evolution"]["verdict"] == "insufficient_data"
    assert empty["breaker_tripped"] is False


# ── _git 封装 ────────────────────────────────────────────────────────────
def test_git_returns_code_and_merged_output(server_env):
    """_git 返回 (returncode, stdout+stderr)；失败命令带非零码与错误文本。"""
    _init_git_repo(server_env._PROJECT_ROOT)
    code, out = server_env._git(["rev-parse", "--short", "HEAD"])
    assert code == 0
    assert out.strip()

    code2, out2 = server_env._git(["rev-parse", "--verify", "no-such-ref"])
    assert code2 != 0
    assert out2.strip()
