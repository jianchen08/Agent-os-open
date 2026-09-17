# -*- coding: utf-8 -*-
# @feature: FP-0.2.二 eval_harness 覆盖率收口 | @ci: python-coverage
"""eval_harness 纯函数面残余缺口补测（覆盖率收口批·第二件）。

靶面：aggregate.classify 未归类分支、proposal 七条校验的 schema/原子性/绝对路径
分支、suite_reader 题面 workspace 锚定三形态（显式 / anchor=repo / materials 缺省）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import aggregate  # noqa: E402
import proposal as proposal_mod  # noqa: E402
import suite_reader  # noqa: E402


# ── aggregate.classify ───────────────────────────────────────────────────
def test_classify_completed_with_failed_criteria_is_acceptance_miss():
    classes = aggregate.classify(
        {"task_status": "completed", "criteria": {"semantic_check": False}})
    assert classes == ["acceptance_miss"]


def test_classify_non_completed_status_is_task_failure():
    classes = aggregate.classify({"task_status": "failed", "criteria": {}})
    assert classes == ["task_failure"]


def test_classify_empty_status_with_failed_criteria_is_unclassified():
    """状态缺失但已有失败指标 → 归 unclassified（两主类都不成立时的兜底）。"""
    classes = aggregate.classify({"task_status": "", "criteria": {"x": False}})
    assert classes == ["unclassified"]


def test_classify_no_evidence_yields_no_classes():
    """状态 completed 且指标全通过 → 无行为类标签。"""
    assert aggregate.classify(
        {"task_status": "completed", "criteria": {"x": True}}) == []


# ── proposal.validate 残余分支 ───────────────────────────────────────────
def test_validate_missing_required_field_reports_schema_violation(tmp_path):
    bad = {"layer": "L3", "target": "config/rules/x.md", "change_type": "content_edit",
           "content": ""}  # content 为空
    violations = proposal_mod.validate(bad, str(tmp_path))
    assert any("缺必填字段 content" in v for v in violations)


def test_validate_illegal_change_type_reports_schema_violation(tmp_path):
    bad = {"layer": "L3", "target": "config/rules/x.md", "change_type": "rewrite_all",
           "content": "x"}
    violations = proposal_mod.validate(bad, str(tmp_path))
    assert any("change_type 非法" in v for v in violations)


def test_validate_schema_violations_short_circuit_before_path_checks(tmp_path):
    """schema 不过 → 立即返回，不再做白名单/冻结面判定（违规清单只含 schema 项）。"""
    violations = proposal_mod.validate({"layer": "", "target": "", "change_type": "",
                                        "content": ""}, str(tmp_path))
    assert violations
    assert all(v.startswith("schema:") for v in violations)


def test_validate_multifile_marker_is_atomicity_violation(tmp_path):
    content = "a\n---PROPOSAL-FILE---\nb\n"
    violations = proposal_mod.validate(
        {"layer": "L3", "target": "config/rules/multi.md", "change_type": "new_plugin",
         "content": content}, str(tmp_path))
    assert any("atomicity" in v for v in violations)


def test_validate_absolute_path_in_content_is_write_scope_violation(tmp_path):
    violations = proposal_mod.validate(
        {"layer": "L3", "target": "config/rules/abs.md", "change_type": "new_plugin",
         "content": "路径 C:\\\\Users\\\\someone\\\\file.txt 与 /home/user/x"}, str(tmp_path))
    assert any("write_scope" in v for v in violations)


def test_validate_normalizes_leading_dot_slash(tmp_path):
    """target 前导 "./" 被剥离后再判白名单（等价路径不因写法不同被误拒）。"""
    violations = proposal_mod.validate(
        {"layer": "L3", "target": "./config/rules/dot.md", "change_type": "new_plugin",
         "content": "x"}, str(tmp_path))
    assert violations == []


def test_validate_escape_above_root_is_rejected(tmp_path):
    violations = proposal_mod.validate(
        {"layer": "L3", "target": "../../etc/passwd", "change_type": "new_plugin",
         "content": "x"}, str(tmp_path))
    assert any("逃逸" in v for v in violations)


# ── suite_reader 题面 workspace 锚定三形态 ────────────────────────────────
def test_case_to_task_args_explicit_workspace_wins_over_anchor():
    """显式 case.workspace 优先于 anchor，缺省 workspace_mode=plain。"""
    args = suite_reader.case_to_task_args(
        {"id": "c1", "workspace": "/explicit/path", "anchor": "repo"},
        "T1", project_root="/repo/root")
    assert args["workspace"] == "/explicit/path"
    assert args["workspace_mode"] == "plain"


def test_case_to_task_args_explicit_workspace_mode_is_preserved():
    args = suite_reader.case_to_task_args(
        {"id": "c2", "workspace": "/explicit", "workspace_mode": "worktree"},
        "T1", project_root="/repo/root")
    assert args["workspace_mode"] == "worktree"


def test_case_to_task_args_anchor_repo_uses_project_root_worktree():
    """anchor=repo → 主仓根 + worktree 模式（读得到仓库物料，产物经门控回流）。"""
    args = suite_reader.case_to_task_args(
        {"id": "c3", "anchor": "repo"}, "T1", project_root="/repo/root")
    assert args["workspace"] == "/repo/root"
    assert args["workspace_mode"] == "worktree"


def test_case_to_task_args_default_materials_anchor_uses_materials_dir(monkeypatch):
    """缺省 anchor=materials → 评测物料区 plain 模式（路径含 run_tag 与 case_id）。"""
    monkeypatch.setenv("AGENTOS_EVAL_MATERIALS_DIR", "/materials")
    args = suite_reader.case_to_task_args({"id": "c/4"}, "T9", project_root="/repo/root")
    assert args["workspace"] == os.path.join("/materials", "workspaces", "T9", "c_4")
    assert args["workspace_mode"] == "plain"


def test_case_to_task_args_repo_anchor_without_project_root_falls_back_to_materials(
    monkeypatch,
):
    """anchor=repo 但未给 project_root → 退回物料区（不产出空 workspace）。"""
    monkeypatch.setenv("AGENTOS_EVAL_MATERIALS_DIR", "/materials")
    args = suite_reader.case_to_task_args({"id": "c5", "anchor": "repo"}, "T9",
                                          project_root="")
    assert args["workspace"].startswith("/materials")
    assert args["workspace_mode"] == "plain"


def test_budget_verdict_non_numeric_metric_or_limit_is_unverified():
    """budget 声明在但比较值不可数值化 → (True, False)：不猜测不误判。"""
    bad_metric = {"budget": {"max_tokens": 100}, "metrics": {"tokens": "many"}}
    assert aggregate.budget_verdict(bad_metric) == (True, False)
    bad_limit = {"budget": {"max_tokens": "few"}, "metrics": {"tokens": 12}}
    assert aggregate.budget_verdict(bad_limit) == (True, False)
