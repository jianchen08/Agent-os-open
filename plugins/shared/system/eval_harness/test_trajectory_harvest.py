# -*- coding: utf-8 -*-
# @feature: FP-0.2.二 真实轨迹筛选采集（ADR 2026-09-16-trajectory-harvest-sourcing） | @ci: python-coverage
"""trajectory_harvest 纯函数单测：终态过滤/去重/oracle 门控/归桶/双产物审计。

每种筛选分支 ≥2 组有区分度输入；库记录全量留痕（含被筛掉的），审计计数
与 disposition 互相自洽（选择日志性质断言）。
"""
from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

import trajectory_harvest as th  # noqa: E402


def _task(pid, status="completed", ac=None, desc="做一件事", title="t",
          agent="general_agent", ended="2026-09-16T10:00:00Z",
          model=None, tokens=None, run_ids=None, evs=None):
    return {"pipeline_id": pid, "goal_title": title, "description": desc,
            "status": status, "acceptance_criteria": ac,
            "eval_summary": evs, "model": model, "agent_id": agent,
            "total_tokens": tokens, "ended_at": ended,
            "run_ids": run_ids or [f"run-{pid}"]}


def _disposition(result, pid):
    for r in result["library"]["records"]:
        if r["task_ref"] == pid:
            return r["disposition"], r["reason"]
    raise AssertionError(f"{pid} 未落库（选择日志缺口）")


def _suite_cases(result, mode):
    return result["suites"][mode]["cases"]


# ── 终态过滤 ─────────────────────────────────────────────────────────────
def test_non_terminal_tasks_excluded_from_suite_but_kept_in_library():
    result = th.harvest([
        _task("p1", status="pending", ac={"file_check": {"input_params": {}}}),
        _task("p2", status="running", ac={"file_check": {"input_params": {}}}),
    ])
    assert result["suites"] == {}
    assert result["library"]["counts"]["non_terminal"] == 2
    assert _disposition(result, "p1") == ("skipped", "non_terminal")


def test_terminal_statuses_pass_the_gate():
    """completed 与 failed 都是终态——成败分流见后续用例，这里验门不过滤。"""
    result = th.harvest([
        _task("p1", status="completed", ac={"file_check": {"input_params": {}}}),
        _task("p2", status="failed"),
    ])
    assert _disposition(result, "p2") == ("library_only", "failure_sample")
    assert result["library"]["counts"]["failure_library"] == 1


# ── oracle 门控 ──────────────────────────────────────────────────────────
def test_success_without_ac_is_library_only():
    """成功但无真实 AC → 不成 judged case（成功标准必须先写成环境谓词）。"""
    result = th.harvest([
        _task("p1", ac=None, desc="题甲"),
        _task("p2", ac={}, desc="题乙"),
        _task("p3", ac="file_check: not a dict", desc="题丙"),
    ])
    assert result["suites"] == {}
    assert result["library"]["counts"]["oracle_missing"] == 3
    for pid in ("p1", "p2", "p3"):
        assert _disposition(result, pid) == ("library_only", "oracle_missing")


def test_success_with_real_ac_becomes_judged_case():
    ac = {"file_check": {"input_params": {"path": "a.txt", "check": "contains",
                                          "pattern": "x"}}}
    result = th.harvest([_task("p1", ac=ac, title="写报告", agent="general_agent")])
    case = _suite_cases(result, "general")[0]
    assert case["id"] == "hv_写报告"
    assert case["acceptance_criteria"] == ac  # oracle 原样透传
    assert case["harvested_from"]["pipeline_id"] == "p1"
    assert result["library"]["counts"]["judged"] == 1
    assert _disposition(result, "p1") == ("judged_suite", "")


# ── 清洗对齐：长度与去重 ─────────────────────────────────────────────────
def test_goal_too_long_goes_to_library_not_suite():
    result = th.harvest([
        _task("p1", ac={"file_check": {"input_params": {}}}, desc="字" * 2001),
        _task("p2", ac={"file_check": {"input_params": {}}}, desc="字" * 2000),
    ])
    assert [c["id"] for c in _suite_cases(result, "general")] == ["hv_t"]
    assert result["library"]["counts"]["goal_too_long"] == 1
    assert _disposition(result, "p1") == ("library_only", "goal_too_long")


def test_duplicate_signatures_keep_latest_and_mark_loser():
    ac: dict = {"file_check": {"input_params": {}}}
    result = th.harvest([
        _task("old", ac=ac, desc="同一道题", ended="2026-09-14T00:00:00Z"),
        _task("new", ac=ac, desc="同一道题 ", ended="2026-09-16T00:00:00Z"),
        _task("mid", ac=ac, desc="同一道题\n", ended="2026-09-15T00:00:00Z"),
    ])
    cases = _suite_cases(result, "general")
    assert len(cases) == 1
    assert cases[0]["harvested_from"]["pipeline_id"] == "new"
    assert result["library"]["counts"]["duplicates"] == 2
    assert _disposition(result, "old") == ("skipped", "duplicate")
    assert _disposition(result, "mid") == ("skipped", "duplicate")


def test_distinct_goals_with_same_title_both_kept():
    ac: dict = {"file_check": {"input_params": {}}}
    result = th.harvest([_task("p1", ac=ac, title="同题", desc="题面甲"),
                         _task("p2", ac=ac, title="同题", desc="题面乙")])
    assert len(_suite_cases(result, "general")) == 2  # 签名按题面不按标题


def test_case_id_collision_resolves_with_pipeline_suffix():
    ac: dict = {"file_check": {"input_params": {}}}
    result = th.harvest([_task("aaaa", ac=ac, title="同题", desc="甲"),
                         _task("bbbb", ac=ac, title="同题", desc="乙"),
                         _task("cccc", ac=ac, title="同题", desc="丙")])
    ids = sorted(c["id"] for c in _suite_cases(result, "general"))
    assert len(set(ids)) == 3 and ids[0] == "hv_同题"


# ── 模式归桶 ─────────────────────────────────────────────────────────────
def test_mode_mapping_by_agent_and_general_fallback():
    ac: dict = {"file_check": {"input_params": {}}}
    result = th.harvest([
        _task("p1", ac=ac, agent="programming_orchestrator_agent_v2", desc="题一"),
        _task("p2", ac=ac, agent="research_orchestrator_agent", desc="题二"),
        _task("p3", ac=ac, agent="general_agent", desc="题三"),
    ])
    assert set(result["suites"]) == {"coding", "research", "general"}
    assert th.map_mode("mode_godot/xxx") == "godot"
    assert th.map_mode(None) == "general"


# ── 失败样本与审计自洽 ───────────────────────────────────────────────────
def test_failure_samples_carry_oracle_and_replay_keys():
    ac = {"bash_check": {"input_params": {"command": "grep -q x a.txt"}}}
    evs = {"conclusion": "settled 但产物缺失"}
    result = th.harvest([_task("pf", status="failed", ac=ac, evs=evs,
                               model="deepseek-v4.1-flash", tokens=12345,
                               run_ids=["r1", "r2"])])
    rec = result["library"]["records"][0]
    assert rec["outcome"] == "failure" and rec["disposition"] == "library_only"
    assert rec["oracle"]["acceptance_criteria"] == ac
    assert rec["oracle"]["eval_summary"] == evs
    assert rec["replay"]["model"] == "deepseek-v4.1-flash"
    assert rec["replay"]["total_tokens"] == 12345
    assert rec["trajectory_available"] is True
    assert rec["run_ids"] == ["r1", "r2"]


def test_audit_counts_reconcile_with_library_dispositions():
    """选择日志自洽：audit 计数 = 库内 disposition 分布（性质断言）。"""
    ac: dict = {"file_check": {"input_params": {}}}
    tasks = [
        _task("p1", ac=ac, desc="题一"),                       # judged
        _task("p2", status="failed", desc="题二"),             # failure_library
        _task("p3", desc="题三"),                              # oracle_missing
        _task("p4", status="pending", desc="题四"),            # non_terminal
        _task("p5", ac=ac, desc="题一"),                       # duplicate（同 p1 题面）
        _task("p6", ac=ac, desc="字" * 3000),                  # goal_too_long
    ]
    result = th.harvest(tasks)
    counts = result["library"]["counts"]
    by_disp: dict[tuple[str, str], int] = {}
    for r in result["library"]["records"]:
        key = (r["disposition"], r["reason"])
        by_disp[key] = by_disp.get(key, 0) + 1
    assert counts["seen"] == len(result["library"]["records"]) == 6
    assert by_disp[("judged_suite", "")] == counts["judged"]
    assert by_disp[("library_only", "failure_sample")] == counts["failure_library"]
    assert by_disp[("library_only", "oracle_missing")] == counts["oracle_missing"]
    assert by_disp[("skipped", "non_terminal")] == counts["non_terminal"]
    assert by_disp[("skipped", "duplicate")] == counts["duplicates"]
    assert by_disp[("library_only", "goal_too_long")] == counts["goal_too_long"]
