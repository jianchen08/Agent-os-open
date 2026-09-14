# -*- coding: utf-8 -*-
"""复盘改进建议（评估原则的规则驱动执行，纯函数）。

输入失败面与症状摘要，按 triage_rules.yaml 产出结构化改进建议。
原则（机制前置检查 / 三向分诊 / 杠杆映射 / 防过拟合）全部数据化在
规则文件里，本模块只做规则解释——改原则 = 改 yaml。
"""
from __future__ import annotations

import os
import re
from typing import Any

import yaml

_RULES_PATH = os.path.join(
    "..", "..", "..", "..", "config", "plugins", "review", "triage_rules.yaml")


def load_rules(project_root: str | None = None) -> dict[str, Any]:
    base = project_root or os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
    path = os.path.join(base, "config", "plugins", "review", "triage_rules.yaml")
    data = yaml.safe_load(open(path, encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"分诊规则格式错误: {path}")
    return data


def _case_text(case: dict[str, Any]) -> str:
    parts = [str(case.get(k) or "") for k in
             ("symptom_note", "trajectory_note", "case_id")]
    criteria = case.get("criteria")
    if isinstance(criteria, dict):
        parts.extend(str(k) for k, v in criteria.items() if not v)
    return " ".join(parts)


def suggest(cases: list[dict[str, Any]], project_root: str | None = None) -> dict[str, Any]:
    """cases: [{case_id, task_status, criteria{name:bool}, symptom_note?, trajectory_note?}]
    → 结构化改进建议（不立项/报告/立项+杠杆）。"""
    rules = load_rules(project_root)
    suggestions: list[dict[str, Any]] = []

    mech = rules.get("mechanism_checks") or []
    triage_cfg = rules.get("triage") or {}
    levers_cfg = rules.get("levers") or {}

    for case in cases:
        text = _case_text(case)
        case_out: dict[str, Any] = {"case_id": case.get("case_id"), "triage": None}

        # ① 机制前置检查：命中即不立项
        for check in mech:
            for pat in check.get("pattern") or []:
                if pat in text:
                    case_out["triage"] = "mechanism_covered"
                    case_out["ruling"] = check.get("ruling", "")
                    break
            if case_out["triage"]:
                break
        if case_out["triage"]:
            suggestions.append(case_out)
            continue

        # ② 三向分诊：缺陷/故障信号
        status = str(case.get("task_status") or "")
        traj = str(case.get("trajectory_note") or "")
        defect_hits = [s for s in (triage_cfg.get("harness_defect") or {}).get("signals") or []
                       if s in text]
        if defect_hits:
            case_out["triage"] = "harness_defect"
            case_out["action"] = (triage_cfg.get("harness_defect") or {}).get("action", "")
            case_out["signals"] = defect_hits
            suggestions.append(case_out)
            continue
        fault_hits = [s for s in (triage_cfg.get("system_fault") or {}).get("signals") or []
                      if s.lower() in traj.lower()]
        if fault_hits:
            case_out["triage"] = "system_fault"
            case_out["action"] = (triage_cfg.get("system_fault") or {}).get("action", "")
            case_out["signals"] = fault_hits
            suggestions.append(case_out)
            continue

        # ③ 失败面分类 → 杠杆
        failed = [k for k, v in (case.get("criteria") or {}).items() if not v]
        if status in ("completed", "done", "success") and not failed:
            case_out["triage"] = "pass"
            suggestions.append(case_out)
            continue
        behavior = "task_failure" if status not in ("completed", "done", "success") \
            else "acceptance_miss"
        if failed and status in ("completed", "done", "success"):
            behavior = "acceptance_miss"
        lever_cfg = levers_cfg.get(behavior) or {}
        case_out["triage"] = "improvement_space"
        case_out["behavior_class"] = behavior
        case_out["stage"] = lever_cfg.get("stage", "")
        case_out["levers"] = lever_cfg.get("candidates") or []
        case_out["noise_policy"] = rules.get("noise_policy", "")
        suggestions.append(case_out)

    n_improvable = sum(1 for s in suggestions if s["triage"] == "improvement_space")
    return {
        "total": len(cases),
        "improvable": n_improvable,
        "suggestions": suggestions,
        "anti_overfit": rules.get("anti_overfit") or {},
    }
