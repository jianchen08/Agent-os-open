# -*- coding: utf-8 -*-
"""结果聚合：任务结果 → scorecard + 失败分类（纯函数）。

失败分类（behavior_class 精简版）是对复盘插件的输入预处理；评估原则的
完整执行（轨迹分析→改进建议）归属复盘插件 review_service。
"""
from __future__ import annotations

from typing import Any


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
    return classes


def summarize(mode: str, results: list[dict[str, Any]],
              baseline_passed: int | None = None) -> dict[str, Any]:
    passed = sum(1 for r in results if _case_passed(r))
    for r in results:
        r["passed"] = _case_passed(r)
        r["behavior_class"] = classify(r)
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
    return bool(criteria) and all(bool(v) for v in criteria.values())
