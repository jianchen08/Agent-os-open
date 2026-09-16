# -*- coding: utf-8 -*-
"""复盘改进建议（评估原则的规则驱动执行，纯函数）。

输入失败面与症状摘要，按 triage_rules.yaml 产出结构化改进建议。
原则（机制前置检查 / 四向分诊 / 杠杆映射 / 防过拟合）全部数据化在
规则文件里，本模块只做规则解释——改原则 = 改 yaml。
信号型分诊类按 TRIAGE_SIGNAL_CLASSES 顺序扫描（ADR 2026-09-16：外因
单独成类，区分「外部变化」与「自身退化」），新增信号类只改 yaml。
"""
from __future__ import annotations

import os
import re
from typing import Any

import yaml

# 信号型分诊类的扫描顺序（yaml triage.<class> 需含 signals+action）；
# 全部未命中才落入 improvement_space。
TRIAGE_SIGNAL_CLASSES = ("external_cause", "harness_defect", "system_fault")

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

        # ② 四向分诊：信号型类按序扫描（外因 → 评测缺陷 → 系统故障），
        #    症状+轨迹合并大小写不敏感匹配
        status = str(case.get("task_status") or "")
        traj = str(case.get("trajectory_note") or "")
        hay = (text + " " + traj).lower()
        for triage_class in TRIAGE_SIGNAL_CLASSES:
            cfg = triage_cfg.get(triage_class) or {}
            hits = [s for s in cfg.get("signals") or [] if s and s.lower() in hay]
            if hits:
                case_out["triage"] = triage_class
                case_out["action"] = cfg.get("action", "")
                case_out["signals"] = hits
                break
        if case_out["triage"]:
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
