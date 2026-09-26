# @feature: FP-0.2.二 review improvement 缺口分支补测 | @ci: python-coverage
"""improvement（复盘改进建议）规则驱动分类的单元测试。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import improvement  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))


def test_mechanism_covered_not_filed():
    """机制前置检查：路径/工作空间类症状不立项。"""
    out = improvement.suggest([{
        "case_id": "c1", "task_status": "completed",
        "criteria": {"file_check": False},
        "symptom_note": "agent 把产物写到了别的工作空间路径",
    }], project_root=ROOT)
    assert out["suggestions"][0]["triage"] == "mechanism_covered"
    assert out["improvable"] == 0


def test_system_fault_reported():
    out = improvement.suggest([{
        "case_id": "c2", "task_status": "failed", "criteria": {},
        "trajectory_note": "sidecar 崩溃 Traceback exception",
    }], project_root=ROOT)
    assert out["suggestions"][0]["triage"] == "system_fault"
    assert out["improvable"] == 0


def test_improvement_space_with_levers():
    out = improvement.suggest([{
        "case_id": "c3", "task_status": "completed",
        "criteria": {"semantic_check": False},
    }], project_root=ROOT)
    s = out["suggestions"][0]
    assert s["triage"] == "improvement_space"
    assert s["behavior_class"] == "acceptance_miss"
    assert "persona" in s["levers"]


def test_task_failure_classified():
    out = improvement.suggest([{
        "case_id": "c4", "task_status": "failed", "criteria": {},
    }], project_root=ROOT)
    s = out["suggestions"][0]
    assert s["triage"] == "improvement_space"
    assert s["behavior_class"] == "task_failure"


def test_pass_case_no_suggestion():
    out = improvement.suggest([{
        "case_id": "c5", "task_status": "completed",
        "criteria": {"file_check": True},
    }], project_root=ROOT)
    assert out["suggestions"][0]["triage"] == "pass"
    assert out["improvable"] == 0


def test_rules_are_data_driven(tmp_path):
    """改规则 yaml 即改行为（原则数据化验证）。"""
    import yaml
    custom = tmp_path / "triage_rules.yaml"
    custom.write_text(yaml.safe_dump({
        "mechanism_checks": [{"pattern": ["自定义关键词"], "ruling": "不立项"}],
        "triage": {"system_fault": {"signals": ["boom"], "action": "报告"}},
        "levers": {"acceptance_miss": {"stage": "execution",
                                       "candidates": ["自定义杠杆"]}},
        "noise_policy": "n", "anti_overfit": {},
    }, allow_unicode=True), encoding="utf-8")
    orig = improvement._RULES_PATH
    # 直接以自定义 root 模拟：把规则放到 <root>/config/plugins/review/（load_rules 的实际路径）
    rules_dir = tmp_path / "config" / "plugins" / "review"
    rules_dir.mkdir(parents=True)
    custom.rename(rules_dir / "triage_rules.yaml")
    out = improvement.suggest([{
        "case_id": "c6", "task_status": "completed",
        "criteria": {"x": False}, "symptom_note": "出现自定义关键词",
    }], project_root=str(tmp_path))
    assert out["suggestions"][0]["triage"] == "mechanism_covered"
    assert orig == improvement._RULES_PATH


def test_external_cause_triaged_not_filed():
    """外因单独成类（ADR 2026-09-16）：供应商超时/内核重启窗口 → 记账不立项。"""
    out = improvement.suggest([
        {"case_id": "e1", "task_status": "failed", "criteria": {},
         "symptom_note": "本轮撞供应商超时窗口，批量任务集体未收敛"},
        {"case_id": "e2", "task_status": "failed", "criteria": {},
         "trajectory_note": "watcher 重载反复打断队列消费（外部窗口干扰）"},
    ], project_root=ROOT)
    assert [s["triage"] for s in out["suggestions"]] == ["external_cause", "external_cause"]
    assert out["improvable"] == 0
    assert all("外因" in s["action"] or "记账" in s["action"] for s in out["suggestions"])


def test_external_cause_scanned_before_other_signal_classes():
    """外因优先于系统故障类：同现时按 TRIAGE_SIGNAL_CLASSES 顺序取第一命中。"""
    assert improvement.TRIAGE_SIGNAL_CLASSES[0] == "external_cause"
    out = improvement.suggest([{
        "case_id": "e3", "task_status": "failed", "criteria": {},
        "symptom_note": "连接超时后看到 sidecar exception"}], project_root=ROOT)
    assert out["suggestions"][0]["triage"] == "external_cause"
