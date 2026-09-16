# -*- coding: utf-8 -*-
"""真实轨迹筛选采集（纯函数核心）：任务终态记录 → 判定题集 + 轨迹样本库。

采集流水线（ADR 2026-09-16-trajectory-harvest-sourcing）：
  原始任务记录 → 终态过滤 → 去重（同题留最新）→ oracle 门控（带真实 AC 的
  成功任务才成 judged case）→ 清洗对齐（goal 长度）→ 模式归桶
  → 双产物（harvested 套件 + 轨迹样本库，含逐级筛选审计 = 选择日志）。

筛选立场：
- 失败轨迹是高价值样本——进轨迹库（归因/失败分类），不入派发题集
  （预期即失败，无裁决意义）；
- 无 oracle（AC 缺失）的成功任务不入判定集——成功标准必须先写成环境谓词；
- goal 正文超 task_submit 上限的入库不入集（截断会歪曲题目）；
- 全量任务（含被筛掉的）都落库带 disposition/reason——采样偏差可审计。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

MAX_GOAL_TEXT = 2000  # 对齐 suite_reader.MAX_GOAL_LEN（task_submit 上限）
TERMINAL_STATUSES = {"completed", "failed"}
LIBRARY_VERSION = "harvest-1.0"

# agent.id → 模式归桶（子串匹配，缺省 general 桶——经 batch 脚本显式路径跑）
MODE_BY_AGENT_RULES: tuple[tuple[str, str], ...] = (
    ("programming_orchestrator", "coding"),
    ("code_writer", "coding"),
    ("research_orchestrator", "research"),
    ("writing", "writing"),
    ("generation", "writing"),
    ("roleplay", "roleplay"),
    ("godot", "godot"),
)

_DEFAULT_TARGET = "general_agent"


def map_mode(agent_id: str | None) -> str:
    a = str(agent_id or "").lower()
    for needle, mode in MODE_BY_AGENT_RULES:
        if needle in a:
            return mode
    return "general"


def _norm_signature(text: str) -> str:
    """题面归一化签名：压缩空白后取 sha1 前 16 位（去重键）。"""
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def _slug(title: Any, pipeline_id: str) -> str:
    # \w 按 Unicode 语义保留 CJK 题名（本仓真实任务标题以中文为主）
    base = re.sub(r"[^\w-]+", "_", str(title or ""), flags=re.UNICODE).strip("_")[:40]
    return base or f"task_{pipeline_id[:8]}"


def _is_success(record: dict[str, Any]) -> bool:
    return record.get("status") == "completed"


def _judged_target(agent_id: str | None) -> str:
    agent = str(agent_id or "").strip()
    return agent or _DEFAULT_TARGET


def _make_case(record: dict[str, Any], case_id: str) -> dict[str, Any]:
    goal_text = str(record.get("description") or record.get("goal_title") or "")
    title = str(record.get("goal_title") or "")
    return {
        "id": case_id,
        "source": "eval-batch" if title.startswith("eval-") else "organic",
        "category": "harvested",
        "target": _judged_target(record.get("agent_id")),
        "messages": [goal_text],
        "acceptance_criteria": record.get("acceptance_criteria") or {},
        "anchor": "materials",
        "harvested_from": {
            "pipeline_id": record.get("pipeline_id"),
            "ended_at": record.get("ended_at"),
            "model": record.get("model"),
        },
    }


def harvest(tasks: list[dict[str, Any]], generated_tag: str = "") -> dict[str, Any]:
    """筛选主入口。tasks 为解码后的任务终态记录（见模块 docstring）。

    返回 {"suites": {mode: suite_dict}, "library": library_dict}。
    """
    audit = {"seen": len(tasks), "non_terminal": 0, "duplicates": 0,
             "oracle_missing": 0, "goal_too_long": 0,
             "judged": 0, "failure_library": 0}
    records: dict[str, dict[str, Any]] = {
        str(t.get("pipeline_id") or ""): t for t in tasks}
    disposition: dict[str, str] = {}
    reason: dict[str, str] = {}

    # ① 终态过滤：pending 等非终态任务不入候选
    for pid, t in records.items():
        if str(t.get("status") or "") not in TERMINAL_STATUSES:
            audit["non_terminal"] += 1
            disposition[pid], reason[pid] = "skipped", "non_terminal"

    # ② 同题去重：按题面签名留 ended_at 最新的一条（仅终态任务参与）
    by_sig: dict[str, str] = {}
    for pid, t in sorted(records.items()):
        if disposition.get(pid):
            continue
        sig = _norm_signature(str(t.get("description") or t.get("goal_title") or ""))
        prev = by_sig.get(sig)
        if prev is None:
            by_sig[sig] = pid
            continue
        audit["duplicates"] += 1
        if str(t.get("ended_at") or "") >= str(records[prev].get("ended_at") or ""):
            by_sig[sig] = pid
            loser = prev
        else:
            loser = pid
        disposition[loser], reason[loser] = "skipped", "duplicate"

    # ③ oracle 门控 + 清洗 + 归桶组套件
    suites: dict[str, dict[str, Any]] = {}
    used_case_ids: set[str] = set()
    for pid in sorted(by_sig.values()):
        record = records[pid]
        if not _is_success(record):
            audit["failure_library"] += 1
            disposition[pid], reason[pid] = "library_only", "failure_sample"
            continue
        ac = record.get("acceptance_criteria")
        if not isinstance(ac, dict) or not ac:
            audit["oracle_missing"] += 1
            disposition[pid], reason[pid] = "library_only", "oracle_missing"
            continue
        goal_text = str(record.get("description") or record.get("goal_title") or "")
        if len(goal_text) > MAX_GOAL_TEXT:
            audit["goal_too_long"] += 1
            disposition[pid], reason[pid] = "library_only", "goal_too_long"
            continue

        mode = map_mode(record.get("agent_id"))
        base_id = f"hv_{_slug(record.get('goal_title') or "", pid)}"
        case_id = base_id
        if case_id in used_case_ids:
            case_id = f"{base_id}_{pid[:8]}"
            n = 2
            while case_id in used_case_ids:
                case_id = f"{base_id}_{pid[:8]}_{n}"
                n += 1
        used_case_ids.add(case_id)
        suite = suites.setdefault(mode, {
            "name": f"harvested-{mode}",
            "mode": mode,
            "settle_timeout_seconds": 900,
            "cases": [],
        })
        suite["cases"].append(_make_case(record, case_id))
        audit["judged"] += 1
        disposition[pid], reason[pid] = "judged_suite", ""

    for suite in suites.values():
        suite["cases"].sort(key=lambda c: c["id"])

    library_records = []
    for pid in sorted(records):
        t = records[pid]
        library_records.append({
            "task_ref": pid,
            "run_ids": list(t.get("run_ids") or []),
            "title": str(t.get("goal_title") or ""),
            "mode": map_mode(t.get("agent_id")),
            "outcome": "success" if _is_success(t) else "failure",
            "disposition": disposition.get(pid, "skipped"),
            "reason": reason.get(pid, ""),
            "oracle": {
                "acceptance_criteria": t.get("acceptance_criteria"),
                "eval_summary": t.get("eval_summary"),
            },
            "replay": {
                "model": t.get("model"),
                "total_tokens": t.get("total_tokens"),
                "ended_at": t.get("ended_at"),
                "agent_id": t.get("agent_id"),
            },
            "trajectory_available": bool(t.get("run_ids")),
        })

    return {
        "suites": suites,
        "library": {
            "library_version": LIBRARY_VERSION,
            "generated_tag": generated_tag,
            "counts": audit,
            "records": library_records,
        },
    }
