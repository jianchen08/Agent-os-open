"""结果聚合：任务结果 → scorecard + 失败分类（纯函数）。

失败分类（behavior_class 精简版）是对复盘插件的输入预处理；评估原则的
完整执行（轨迹分析→改进建议）归属复盘插件 review_service。
成功判定谓词 = 终态 ∧ AC ∧ budget_ok（ADR 2026-09-16：budget 声明而
metrics 缺失时不判死，只标 budget_unverified——旧题集/旧账向后兼容）。
repeat 聚合（ADR 2026-09-29-eval-repeat-control-screening）：同 case_id 多条
结果 = 同题复跑，按全过口径（全过才算通过）归并为单 case 行，附逐次明细
（repeat_detail）供方差观测；单条结果行为与旧口径完全一致。
"""
from __future__ import annotations

import time
from datetime import date, timedelta
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
    for r in results:
        r["passed"] = _case_passed(r)
        r["behavior_class"] = classify(r)
        ok, verified = budget_verdict(r)
        if r.get("budget") and not verified:
            r["budget_unverified"] = True
    rows, repeat_detail = _merge_repeats(results)
    passed = sum(1 for row in rows if row["passed"])
    out = {
        "mode": mode,
        "passed": passed,
        "total": len(rows),
        "results": rows,
    }
    if repeat_detail:
        out["repeat_detail"] = repeat_detail
    if baseline_passed is not None:
        out["delta"] = passed - baseline_passed
    return out


def _merge_repeats(results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]],
                                                           dict[str, list[bool]]]:
    """同 case_id 多条结果归并为单 case 行（全过口径）。

    行内容取首次出现条目为底（保留 criteria/budget 等上下文），passed/
    behavior_class/task_status 按归组结果覆写——失败次序优先呈现（首个失败
    条目的状态；behavior_class 合并全部失败条目，保序去重）。repeat>1 的组
    附 repeat_total/repeat_passed/repeat_pass_rate，并产出逐次明细键。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for r in results:
        cid = str(r.get("case_id") or "")
        if cid not in groups:
            groups[cid] = []
            order.append(cid)
        groups[cid].append(r)
    rows: list[dict[str, Any]] = []
    repeat_detail: dict[str, list[bool]] = {}
    for cid in order:
        entries = groups[cid]
        row = dict(entries[0])
        row["case_id"] = cid
        row["passed"] = all(e["passed"] for e in entries)
        classes: list[str] = []
        for e in entries:
            if e["passed"]:
                continue
            for c in e["behavior_class"]:
                if c not in classes:
                    classes.append(c)
        row["behavior_class"] = classes
        row["task_status"] = next((str(e.get("task_status") or "")
                                   for e in entries if not e["passed"]),
                                  str(entries[0].get("task_status") or ""))
        if len(entries) > 1:
            row["repeat_total"] = len(entries)
            row["repeat_passed"] = sum(1 for e in entries if e["passed"])
            row["repeat_pass_rate"] = round(row["repeat_passed"] / len(entries), 3)
            repeat_detail[cid] = [bool(e["passed"]) for e in entries]
        rows.append(row)
    return rows, repeat_detail


def _case_passed(case: dict[str, Any]) -> bool:
    if str(case.get("task_status") or "") not in ("completed", "done", "success"):
        return False
    criteria = case.get("criteria") or {}
    if not (bool(criteria) and all(bool(v) for v in criteria.values())):
        return False
    return budget_verdict(case)[0]


def health_summary(ledger: dict[str, Any]) -> dict[str, Any]:
    """提案命中率/杠杆账（eval_query health 域与面板快照共用的同源口径）。"""
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


def panel_snapshot(ledger: dict[str, Any],
                   library: dict[str, Any] | None = None,
                   daily: dict[str, Any] | None = None) -> dict[str, Any]:
    """自进化面板数据快照：reports/eval 账本 → 页面渲染投影。

    口径与 eval_query health 域/aggregate 同源（health_summary 复用）；熔断计数
    尚未落账，breaker 恒为 None（页面按占位渲染）。library 为轨迹库投影
    （screening.library_summary 产物，缺失传 None——页面按占位渲染）。
    daily 为真实任务流量投影（daily_projection 产物，数据源缺失传 None）。
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
        point = {
            "run_id": str(r.get("run_id") or ""),
            "passed": passed, "total": int(r.get("total") or 0),
            "failed_cases": list(r.get("failed_cases") or []),
            "round_type": str(r.get("round_type") or "treatment"),
        }
        if isinstance(r.get("repeat_detail"), dict) and r["repeat_detail"]:
            point["repeats"] = r["repeat_detail"]
        entry["series"].append(point)
    # scorecard 走势：基线 = 该模式首轮 passed，各轮 delta 相对基线
    for entry in modes.values():
        baseline = entry["series"][0]["passed"] if entry["series"] else 0
        for point in entry["series"]:
            point["delta_vs_baseline"] = point["passed"] - baseline
    hypotheses = [{"proposal_id": pid,
                   "target": str(p.get("target") or ""),
                   "motivation": str(p.get("motivation") or ""),
                   "outcome": str(p.get("outcome") or ""),
                   "commit": str(p.get("commit") or "")}
                  for pid, p in (ledger.get("proposals") or {}).items()]
    return {"modes": modes, "rounds_total": len(rounds),
            "hypotheses": hypotheses, "breaker": None,
            "evolution": evolution_metrics.verdict(ledger),
            "controls": evolution_metrics.control_comparison(rounds),
            "library": library, "daily": daily,
            **health_summary(ledger)}


# 日常线状态桶（内核 RunStatus 序列化值全集：running/suspended/completed/
# failed/cancelled）——完成率把 cancelled 计入失败面（用户中止 = 体验问题，
# 不从分母摘除）；running/suspended 计在途。
_DAILY_OK = "completed"
_DAILY_FAIL = ("failed", "cancelled")
_DAILY_RUNNING = ("running", "suspended")
_DAILY_WINDOW = 7
_DAILY_FAILURE_CAP = 8


def _day_of(value: Any) -> str:
    """RFC3339 时间戳 → YYYY-MM-DD（前缀截取；非字符串/形态不符返回空串）。"""
    s = str(value or "")
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return ""


def daily_projection(runs: list[dict[str, Any]] | None,
                     today: str | None = None) -> dict[str, Any] | None:
    """真实任务流量（service-registry.pipeline-runs.list 行）→ 日常线投影。

    None 透传 = 数据源缺口（能力未注入/调用失败，页面按缺口渲染，与
    空流量区分——[] 是真实空态，照常出零值投影）。完成率 = completed /
    (completed + failed + cancelled)，分母 0 → None（不猜测）。started_at
    缺失/形态不符的行计入 window_total 但不入日桶。today 可注入固定时钟
    （YYYY-MM-DD，测试面）。
    """
    if runs is None:
        return None
    day = today or time.strftime("%Y-%m-%d")
    base = date.fromordinal(date.fromisoformat(day).toordinal())
    days = [(base - timedelta(days=i)).isoformat()
            for i in range(_DAILY_WINDOW - 1, -1, -1)]
    buckets: dict[str, dict[str, Any]] = {
        d: {"date": d, "total": 0, "completed": 0, "failed": 0} for d in days}
    running = 0
    failures: list[dict[str, Any]] = []
    for r in runs:
        status = str(r.get("status") or "")
        if status in _DAILY_RUNNING:
            running += 1
        # 失败信号流不依赖日期可解析——start_at 形态不符的失败行也进信号流
        if status in _DAILY_FAIL:
            failures.append({"pipeline_id": str(r.get("pipeline_id") or ""),
                             "thread_id": str(r.get("thread_id") or ""),
                             "run_id": str(r.get("run_id") or ""),
                             "status": status,
                             "started_at": str(r.get("started_at") or "")})
        bucket = buckets.get(_day_of(r.get("started_at")))
        if bucket is None:
            continue
        bucket["total"] += 1
        if status == _DAILY_OK:
            bucket["completed"] += 1
        elif status in _DAILY_FAIL:
            bucket["failed"] += 1
    today_bucket = buckets.get(day, {"total": 0, "completed": 0, "failed": 0})
    done, bad = today_bucket["completed"], today_bucket["failed"]
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "today": {"date": day,
                  "total": today_bucket["total"],
                  "completed": done,
                  "failed": bad,
                  "completion_rate": round(done / (done + bad), 3) if (done + bad) else None},
        "running": running,
        "week": [buckets[d] for d in days],
        "recent_failures": failures[:_DAILY_FAILURE_CAP],
        "window_total": len(runs),
    }


def _wf_node(seq: int, step: str, title: str, status: str,
             started_at: str = "", ended_at: str = "",
             inputs: dict[str, Any] | None = None,
             artifacts: dict[str, Any] | None = None,
             driven_by: str = "") -> dict[str, Any]:
    """单个步骤节点的面板投影（标题按 step 定义补齐；artifacts 里的提案 id 提面）。"""
    artifacts = dict(artifacts or {})
    return {
        "seq": int(seq),
        "step": step,
        "title": title,
        "status": status,
        "started_at": str(started_at or ""),
        "ended_at": str(ended_at or ""),
        "inputs": dict(inputs or {}),
        "artifacts": artifacts,
        "proposal_ids": [str(p) for p in (artifacts.get("proposal_ids") or [])],
        "driven_by": str(driven_by or ""),
    }


def _instance(doc: dict[str, Any], titles: dict[str, str],
              trace_steps: list[dict[str, Any]] | None) -> dict[str, Any]:
    """工作流实例 → 面板实例投影（工具事件节点 + traces 派生节点合并）。"""
    nodes: list[dict[str, Any]] = []
    seq = 0
    for raw in doc.get("steps") or []:
        if not isinstance(raw, dict):
            continue
        seq += 1
        step = str(raw.get("step") or "")
        nodes.append(_wf_node(
            int(raw.get("seq") or seq), step, titles.get(step, ""),
            str(raw.get("status") or "running"),
            str(raw.get("started_at") or ""), str(raw.get("ended_at") or ""),
            raw.get("inputs") if isinstance(raw.get("inputs"), dict) else {},
            raw.get("artifacts") if isinstance(raw.get("artifacts"), dict) else {},
            str(raw.get("driven_by") or "")))
    seen = {n["step"] for n in nodes}
    for item in trace_steps or []:
        step = str(item.get("step") or "")
        if not step or step in seen:
            # 工具事件已落该步 → traces 派生不重复造节点（同一事实只留一份）
            continue
        seen.add(step)
        seq += 1
        nodes.append(_wf_node(seq, step, titles.get(step, ""),
                              str(item.get("status") or "done"),
                              str(item.get("at") or ""), str(item.get("at") or ""),
                              {}, {"evidence": str(item.get("evidence") or "")},
                              "traces.list_by_pipeline"))
    nodes.sort(key=lambda n: n["seq"])
    return {
        "wf_id": str(doc.get("wf_id") or ""),
        "mode": str(doc.get("mode") or ""),
        "round_type": str(doc.get("round_type") or "treatment"),
        "status": str(doc.get("status") or "running"),
        "close_reason": str(doc.get("close_reason") or ""),
        "opened_at": str(doc.get("opened_at") or ""),
        "closed_at": str(doc.get("closed_at") or ""),
        "pipeline_id": str(doc.get("pipeline_id") or ""),
        "nodes": nodes,
    }


def workflow_projection(ledger: dict[str, Any],
                        workflows: list[dict[str, Any]] | None = None,
                        defs: list[dict[str, Any]] | None = None,
                        edges: list[dict[str, Any]] | None = None,
                        trace_steps: dict[str, list[dict[str, Any]]] | None = None
                        ) -> dict[str, Any]:
    """步骤定义 + 工作流实例 + 改进产物 → 一体化工作流图投影（纯函数，无 IO）。

    一张图的三层内容同源产出：
    ① 骨架 = ``defs``/``edges``（来自 rules/flow_steps.yaml 的步骤枚举声明）；
    ② 实例 = ``instances[].nodes``（工具 handler 落账的真实节点，seq 严格递增；
       回边产生的第二次 T3 是新节点而非覆盖）；``trace_steps`` 是 traces 派生的
       T2/T7（不埋点，按证据补节点；证据缺失就不出节点）；
    ③ 改进产物 = ``threads``（提案 → 触点节点），并反向挂到节点 ``proposal_ids``。

    ``workflows`` 为 None/空 → ``instances == []`` 且 ``gaps`` 记录空态原因（页面
    按空态渲染骨架，不出现 status 色节点）。
    """
    defs = list(defs or [])
    edges = list(edges or [])
    titles = {str(d.get("step") or ""): str(d.get("title") or "") for d in defs}
    gaps: list[dict[str, str]] = []

    docs = [d for d in (workflows or []) if isinstance(d, dict)]
    instances = [_instance(d, titles, (trace_steps or {}).get(str(d.get("wf_id") or "")))
                 for d in docs]
    instances.sort(key=lambda i: i["opened_at"])
    if not instances:
        gaps.append({"key": "instances", "reason": "no_workflow_data",
                     "detail": "尚无回顾工作流记录——自下一轮 T1 工具调用起自动生成"})
    if trace_steps is None:
        gaps.append({"key": "T2/T7", "reason": "traces_unavailable",
                     "detail": "traces.list_by_pipeline 能力未注入，T2 归因/T7 记忆回填"
                               "节点按缺口占位（不造节点）"})

    focus = ""
    for inst in reversed(instances):
        if inst["status"] == "running":
            focus = inst["wf_id"]
            break
    if not focus and instances:
        focus = instances[-1]["wf_id"]

    # 改进产物（改进线）：提案账 + 节点触点（按 seq 顺序，跨轮即多触点）
    touches: dict[str, list[dict[str, Any]]] = {}
    for inst in instances:
        for node in inst["nodes"]:
            for pid in node["proposal_ids"]:
                touches.setdefault(pid, []).append({
                    "wf_id": inst["wf_id"], "seq": node["seq"],
                    "step": node["step"], "status": node["status"],
                    "round_id": str((node["artifacts"] or {}).get("run_id") or ""),
                })
    threads: list[dict[str, Any]] = []
    for pid, prop in (ledger.get("proposals") or {}).items():
        if not isinstance(prop, dict):
            continue
        linked = sorted(touches.get(pid, []), key=lambda t: t["seq"])
        threads.append({
            "proposal_id": str(pid),
            "outcome": str(prop.get("outcome") or ""),
            "target": str(prop.get("target") or ""),
            "motivation": str(prop.get("motivation") or ""),
            "commit": str(prop.get("commit") or ""),
            "category": str(prop.get("category") or ""),
            "round_id": str(prop.get("round_id") or ""),
            "touches": linked,
            "linked": bool(linked),
        })
    threads.sort(key=lambda t: (t["linked"], t["proposal_id"]), reverse=True)
    linked_n = sum(1 for t in threads if t["linked"])

    return {
        "defs": defs,
        "edges": edges,
        "instances": instances,
        "focus": focus,
        "threads": threads,
        "gaps": gaps,
        "summary": {
            "instances_total": len(instances),
            "nodes_total": sum(len(i["nodes"]) for i in instances),
            "proposals_total": len(threads),
            "proposals_linked": linked_n,
            "proposals_unlinked": len(threads) - linked_n,
        },
    }
