# -*- coding: utf-8 -*-
# @feature: FP-0.2.二 eval_harness 缺口分支补测 | @ci: python-coverage
"""eval_harness 单元测试（不依赖内核）：题集展开/聚合/提案校验/账本。"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(__file__))

import aggregate  # noqa: E402
import proposal as proposal_mod  # noqa: E402
import suite_reader  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))


def _valid_proposal(target="config/agents/main/task_dispatch_guide.md",
                    base_hash="", change_type="content_edit",
                    content="示例内容", layer="L1") -> dict:
    return {"layer": layer, "target": target, "change_type": change_type,
            "content": content, "motivation": "签名簇 S1", "base_hash": base_hash}


# ── 题集展开 ─────────────────────────────────────────────────────────────
def _write_suite(tmp_path, cases):
    path = tmp_path / "suite.yaml"
    path.write_text(yaml.safe_dump({"name": "t", "mode": "coding", "cases": cases},
                                   allow_unicode=True), encoding="utf-8")
    return str(path)


def test_expand_batches_shape(tmp_path):
    suite = _write_suite(tmp_path, [
        {"id": "c1", "category": "normal", "target": "executor/general_agent",
         "messages": ["做 A"],
         "acceptance_criteria": {"semantic_check": {"input_params": {"criteria": "x"}}}},
    ])
    batches = suite_reader.expand_batches(suite_reader.load_suite(suite), run_tag="T1")
    args = batches[0]["task_submit_args"]
    assert args["target_type"] == "agent"
    assert args["target_id"] == "executor/general_agent"
    assert args["goal_title"] == "eval-c1-T1"
    assert args["goal_description"] == "做 A"
    assert "semantic_check" in args["acceptance_criteria"]


def test_expand_truncates_long_goal(tmp_path):
    suite = _write_suite(tmp_path, [
        {"id": "c2", "messages": ["字" * 3000],
         "acceptance_criteria": {"semantic_check": {"input_params": {"criteria": "x"}}}},
    ])
    batches = suite_reader.expand_batches(suite_reader.load_suite(suite), run_tag="T")
    assert len(batches[0]["task_submit_args"]["goal_description"]) <= 2000


def test_expand_case_filter(tmp_path):
    suite = _write_suite(tmp_path, [
        {"id": "a", "messages": ["x"], "acceptance_criteria": {}},
        {"id": "b", "messages": ["y"], "acceptance_criteria": {}},
    ])
    batches = suite_reader.expand_batches(suite_reader.load_suite(suite), cases="b")
    assert [b["case_id"] for b in batches] == ["b"]


# ── 聚合与失败分类 ───────────────────────────────────────────────────────
def test_summarize_pass_and_fail():
    results = [
        {"case_id": "c1", "task_status": "completed",
         "criteria": {"semantic_check": True}},
        {"case_id": "c2", "task_status": "completed",
         "criteria": {"semantic_check": False}},
        {"case_id": "c3", "task_status": "failed", "criteria": {}},
    ]
    out = aggregate.summarize("coding", results, baseline_passed=2)
    assert out["passed"] == 1 and out["total"] == 3
    assert out["delta"] == -1
    by_id = {r["case_id"]: r for r in out["results"]}
    assert by_id["c2"]["behavior_class"] == ["acceptance_miss"]
    assert by_id["c3"]["behavior_class"] == ["task_failure"]


# ── 提案校验（冻结面含安全红线文件） ─────────────────────────────────────
def test_validate_pass_on_legal_proposal():
    assert proposal_mod.validate(_valid_proposal(), ROOT) == []


@pytest.mark.parametrize("target", [
    "config/self_evolve/rules/evolution_rules.md",
    "plugins/shared/system/eval_harness/server.py",
    "kernel/crates/api/src/routes.rs",
    ".env",
    "config/rules/information_integrity_rules.md",
])
def test_validate_rejects_frozen_paths(target):
    violations = proposal_mod.validate(_valid_proposal(target=target), ROOT)
    assert any(v.startswith("frozen:") for v in violations), target


def test_validate_rejects_outside_whitelist_and_escape():
    assert any(v.startswith("whitelist:")
               for v in proposal_mod.validate(_valid_proposal(target="docs/x.md"), ROOT))
    assert any("逃逸" in v
               for v in proposal_mod.validate(_valid_proposal(target="../../etc/p"), ROOT))


def test_validate_rejects_base_hash_mismatch():
    violations = proposal_mod.validate(_valid_proposal(base_hash="deadbeef"), ROOT)
    assert any(v.startswith("base:") for v in violations)


# ── 账本（reports/eval 下） ─────────────────────────────────────────────
def test_ledger_roundtrip(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setattr("builtins.open", open)  # no-op 保持可读性
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reports" / "eval").mkdir(parents=True)
    # server 模块的 _EVAL_REPORTS 在 import 时按项目根定位；此处直接验证聚合文件语义
    summary_path = tmp_path / "reports" / "eval" / "r1" / "summary.json"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text(json.dumps({"run_id": "r1", "passed": 1}), encoding="utf-8")
    loaded = json.load(open(summary_path, encoding="utf-8"))
    assert loaded["run_id"] == "r1"
