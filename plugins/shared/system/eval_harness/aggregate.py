# -*- coding: utf-8 -*-
"""结果聚合：任务结果 → scorecard + 失败分类（纯函数）。

失败分类（behavior_class 精简版）是对复盘插件的输入预处理；评估原则的
完整执行（轨迹分析→改进建议）归属复盘插件 review_service。
成功判定谓词 = 终态 ∧ AC ∧ budget_ok（ADR 2026-09-16：budget 声明而
metrics 缺失时不判死，只标 budget_unverified——旧题集/旧账向后兼容）。
"""
from __future__ import annotations

from typing import Any

import evolution_metrics

# budget 声明键 → metrics 采集键（上限语义：metrics ≤ limit）
_BUDGET_KEYS = {"max_rounds": "rounds", "max_tokens": "tokens",
                "max_seconds": "seconds"}


def budget_verdict(case: dict[str, Any]) -> tuple[bool, bool]:
    """返回 (budget_ok, verified)。无声明 → (True, False)；有声明有 metrics
    → 逐项比较；有声明缺 metrics → (True, False)（不猜测不误判）。"""
    budget = case.get("budget") or {}
    limits = {k: v for k, v in budget.items() if k in _BUDGET_KEYS}
    if not limits:
        return True, False
    metrics = case.get("metrics") or {}
    if not isinstance(metrics, dict) or not metrics:
        return True, False
    for limit_key, metric_key in _BUDGET_KEYS.items():
        if limit_key in limits and metric_key in metrics:
            try:
                if float(metrics[metric_key]) > float(limits[limit_key]):
                    return False, True
            except (TypeError, ValueError):
                return True, False
    return True, True


def classify(case: dict[str, Any]) -> list[str]:
    """按失败面给行为类标签（复盘深分析的预处理）。"""
    classes: list[str] = []
    status = str(case.get("task_status") or "")
    criteria = case.get("criteria") or {}
    failed = [k for k, v in criteria.items() if not v]
    if status and status != "completed":
        classes.append("task_failure")
    if failed and status == "completed":
        # 任务完成但验收未过：产物形态/语义不符
        classes.append("acceptance_miss")
    if not classes and failed:
        classes.append("unclassified")
    if not classes and not budget_verdict(case)[0]:
        # 终态 + AC 全过但超预算：「多烧 token 换分」不计通过
        classes.append("budget_blowout")
    return classes


def summarize(mode: str, results: list[dict[str, Any]],
              baseline_passed: int | None = None) -> dict[str, Any]:
    passed = sum(1 for r in results if _case_passed(r))
    for r in results:
        r["passed"] = _case_passed(r)
        r["behavior_class"] = classify(r)
        ok, verified = budget_verdict(r)
        if r.get("budget") and not verified:
            r["budget_unverified"] = True
    out = {
        "mode": mode,
        "passed": passed,
        "total": len(results),
        "results": results,
    }
    if baseline_passed is not None:
        out["delta"] = passed - baseline_passed
    return out


def _case_passed(case: dict[str, Any]) -> bool:
    if str(case.get("task_status") or "") not in ("completed", "done", "success"):
        return False
    criteria = case.get("criteria") or {}
    if not (bool(criteria) and all(bool(v) for v in criteria.values())):
        return False
    return budget_verdict(case)[0]


def health_summary(ledger: dict[str, Any]) -> dict[str, Any]:
    """提案命中率/杠杆账（eval_health 工具与面板快照共用的同源口径）。"""
    proposals = ledger.get("proposals") or {}
    outcomes = [p.get("outcome") for p in proposals.values()]
    applied = sum(1 for o in outcomes if o == "applied")
    total = len(outcomes)
    rejected = sum(1 for o in outcomes if o == "rejected")
    levers: dict[str, dict[str, int]] = {}
    for p in proposals.values():
        lever = str(p.get("motivation", ""))[:30]
        if not lever:
            continue
        lv = levers.setdefault(lever, {"applied": 0, "total": 0})
        lv["total"] += 1
        if p.get("outcome") == "applied":
            lv["applied"] += 1
    return {"proposals_total": total, "proposals_applied": applied,
            "proposals_rejected": rejected,
            "hit_rate": round(applied / total, 3) if total else None,
            "reject_rate": round(rejected / total, 3) if total else None,
            "levers": levers}


def panel_snapshot(ledger: dict[str, Any]) -> dict[str, Any]:
    """自进化面板数据快照：reports/eval 账本 → 页面渲染投影。

    口径与 eval_health/aggregate 同源（health_summary 复用）；熔断计数
    尚未落账，breaker 恒为 None（页面按占位渲染）。
    """
    rounds = ledger.get("rounds") or []
    modes: dict[str, dict[str, Any]] = {}
    for r in rounds:
        entry = modes.setdefault(str(r.get("mode") or ""),
                                 {"rounds": 0, "passed": 0, "total": 0,
                                  "series": []})
        passed = int(r.get("passed") or 0)
        entry["rounds"] += 1
        entry["passed"] += passed
        entry["total"] += int(r.get("total") or 0)
        entry["series"].append({
            "run_id": str(r.get("run_id") or ""),
            "passed": passed, "total": int(r.get("total") or 0),
            "failed_cases": list(r.get("failed_cases") or []),
        })
    # scorecard 走势：基线 = 该模式首轮 passed，各轮 delta 相对基线
    for entry in modes.values():
        baseline = entry["series"][0]["passed"] if entry["series"] else 0
        for point in entry["series"]:
            point["delta_vs_baseline"] = point["passed"] - baseline
    hypotheses = [{"proposal_id": pid,
                   "target": str(p.get("target") or ""),
                   "motivation": str(p.get("motivation") or ""),
                   "outcome": str(p.get("outcome") or "")}
                  for pid, p in (ledger.get("proposals") or {}).items()]
    return {"modes": modes, "rounds_total": len(rounds),
            "hypotheses": hypotheses, "breaker": None,
            "evolution": evolution_metrics.verdict(ledger),
            **health_summary(ledger)}
