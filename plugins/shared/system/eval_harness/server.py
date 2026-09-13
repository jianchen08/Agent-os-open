#!/usr/bin/env python3
"""自进化评测面（编排薄层）。

职责边界（向现有机制收敛后的定稿）：
- 派发/执行/隔离 = 任务系统（task_submit，任务默认 isolated 自动工作空间）
- 判定 = 评估闸门（task_submit 的 acceptance_criteria，内置指标体系）
- 复盘与改进建议 = 复盘插件 review_service（评估原则的承载地）
- 本插件只做：题集展开、结果聚合、提案校验与应用、held-out 保密派发。
- 不自建：HTTP/WS 客户端、断言器、状态存储、git 变更管理。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin

plugin = AgentOSPlugin("eval_harness_service")

_paths = bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

import aggregate  # noqa: E402
import proposal as proposal_mod  # noqa: E402
import suite_reader  # noqa: E402

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
_EVAL_REPORTS = os.path.join(_PROJECT_ROOT, "reports", "eval")
_HELDOUT_DIR = os.environ.get("AGENTOS_HELDOUT_DIR", "").strip()


def _err(msg: str) -> dict[str, Any]:
    return {"success": False, "error": msg}


def _save_summary(run_id: str, mode: str, payload: dict[str, Any]) -> str:
    out_dir = os.path.join(_EVAL_REPORTS, run_id)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "summary.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"run_id": run_id, "mode": mode, **payload}, fh,
                  ensure_ascii=False, indent=1)
    return path


def _load_ledger() -> dict[str, Any]:
    path = os.path.join(_EVAL_REPORTS, "eval_state.json")
    if os.path.isfile(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except ValueError:
            pass
    return {"rounds": [], "proposals": {}, "levers": {}}


def _save_ledger(state: dict[str, Any]) -> None:
    os.makedirs(_EVAL_REPORTS, exist_ok=True)
    with open(os.path.join(_EVAL_REPORTS, "eval_state.json"), "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)


# ── eval_view_suite：开发集 → task_submit 批次清单（agent 自派） ──────────
@plugin.tool(
    name="eval_view_suite",
    schema={
        "type": "object",
        "properties": {
            "suite": {"type": "string", "description": "题集 yaml 路径（相对项目根）"},
            "cases": {"type": "string", "description": "逗号分隔 case id 过滤"},
        },
        "required": ["suite"],
    },
    description="读题集展开为 task_submit 批次清单（派发由你用自己的 task_submit 执行）",
)
async def eval_view_suite(suite: str, cases: str = "") -> dict[str, Any]:
    try:
        data = suite_reader.load_suite(os.path.join(_PROJECT_ROOT, suite))
    except (OSError, ValueError) as exc:
        return _err(str(exc))
    batches = suite_reader.expand_batches(data, cases or None)
    return {"success": True, "mode": data.get("mode", ""),
            "batches": batches,
            "note": "逐条用自己的 task_submit 派发；完成后用 task_manage 查终态"
                    "与验收结果，喂给 eval_summarize 聚合"}


# ── eval_summarize：结果聚合 ─────────────────────────────────────────────
@plugin.tool(
    name="eval_summarize",
    schema={
        "type": "object",
        "required": ["mode", "results"],
        "properties": {
            "mode": {"type": "string"},
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "case_id": {"type": "string"},
                        "task_id": {"type": "string"},
                        "task_status": {"type": "string"},
                        "criteria": {"type": "object",
                                     "description": "验收指标名 → 是否通过(bool)"},
                    },
                    "required": ["case_id", "task_status", "criteria"],
                },
            },
        },
        "required": ["mode", "results"],
    },
    description="聚合评测结果出 scorecard（失败分类为复盘插件深分析的预处理）",
)
async def eval_summarize(mode: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    ledger = _load_ledger()
    baseline = None
    for r in reversed(ledger.get("rounds") or []):
        if r.get("mode") == mode:
            baseline = int(r.get("passed") or 0)
            break
    summary = aggregate.summarize(mode, results, baseline)
    run_id = suite_reader.time_tag()
    path = _save_summary(run_id, mode, summary)
    ledger.setdefault("rounds", []).append({
        "run_id": run_id, "mode": mode,
        "passed": summary["passed"], "total": summary["total"],
        "failed_cases": [r["case_id"] for r in summary["results"] if not r["passed"]],
    })
    ledger["rounds"] = ledger["rounds"][-200:]
    _save_ledger(ledger)
    return {"success": True, "run_id": run_id, "summary_path": path,
            "passed": summary["passed"], "total": summary["total"],
            "delta": summary.get("delta"),
            "results": [{"case_id": r["case_id"], "passed": r["passed"],
                         "task_status": r["task_status"],
                         "behavior_class": r["behavior_class"]}
                        for r in summary["results"]]}


# ── eval_run_heldout：隐藏集保密派发（题面不进 agent 上下文） ─────────────
@plugin.tool(
    name="eval_run_heldout",
    schema={
        "type": "object",
        "properties": {"mode": {"type": "string"}},
        "required": [],
    },
    description="held-out 隐藏集派发（题面保密：本工具内部派发任务，你只拿到 case↔task 映射；"
                "用 task_manage 查终态后喂 eval_summarize(mode=<同名>) 聚合）",
)
async def eval_run_heldout(mode: str = "coding") -> dict[str, Any]:
    heldout = os.environ.get("AGENTOS_HELDOUT_DIR", "").strip()
    if not heldout:
        return _err("AGENTOS_HELDOUT_DIR 未配置（fail-closed）")
    suite_path = os.path.join(heldout, f"{mode}.yaml")
    if not os.path.isfile(suite_path):
        return _err(f"held-out 题集不存在: {suite_path}")
    try:
        data = suite_reader.load_suite(suite_path)
    except (OSError, ValueError) as exc:
        return _err(str(exc))
    try:
        te = plugin.get_capability("tool-executor")
        invoke = __import__("agentos_plugin_sdk.capability", fromlist=["bind_capability_caller"]) \
            .bind_capability_caller(te, "tool-executor")
    except Exception as exc:  # noqa: BLE001
        return _err(f"tool-executor 能力不可用: {exc}")
    tag = suite_reader.time_tag()
    mapping = []
    for batch in suite_reader.expand_batches(data, run_tag=tag):
        args = dict(batch["task_submit_args"])
        args["goal_title"] = f"heldout-{batch['case_id']}-{tag}"
        try:
            res = await invoke("tool-executor.invoke",
                               {"tool_name": "task_submit",
                                "plugin_id": "task_submit_tool", "args": args})
        except Exception as exc:  # noqa: BLE001
            mapping.append({"case_id": batch["case_id"], "error": str(exc)[:200]})
            continue
        task_id = ""
        if isinstance(res, dict):
            data_field = res.get("data") or res
            task_id = str((data_field.get("task_id") or data_field.get("id") or ""))
        mapping.append({"case_id": batch["case_id"], "task_id": task_id})
    return {"success": True, "run_tag": tag, "dispatched": mapping,
            "note": "用 task_manage 查这些任务的终态与验收结果；聚合时 mode 传 "
                    f"'{mode}'，case_id 与任务状态喂 eval_summarize。题面与"
                    "验收细节不向你返回（保密裁决）"}


# ── proposal_submit / proposal_apply / proposal_reject ───────────────────
@plugin.tool(
    name="proposal_submit",
    schema={
        "type": "object",
        "required": ["layer", "target", "change_type", "content", "motivation"],
        "properties": {
            "layer": {"type": "string", "enum": ["L1", "L2", "L3"]},
            "target": {"type": "string"},
            "change_type": {"type": "string"},
            "content": {"type": "string"},
            "motivation": {"type": "string"},
            "base_hash": {"type": "string"},
        },
        "required": ["layer", "target", "change_type", "content", "motivation"],
    },
    description="提交物料更改提案（冻结面/白名单/原子性/base 校验，不过即拒）",
)
async def proposal_submit(layer: str, target: str, change_type: str,
                          content: str, motivation: str = "",
                          base_hash: str = "") -> dict[str, Any]:
    proposal = {"layer": layer, "target": target, "change_type": change_type,
                "content": content, "motivation": motivation,
                "base_hash": base_hash}
    violations = proposal_mod.validate(proposal, _PROJECT_ROOT)
    proposal_id = f"p{time.strftime('%Y%m%d_%H%M%S')}"
    ledger = _load_ledger()
    accepted = not violations
    ledger.setdefault("proposals", {})[proposal_id] = {
        "outcome": "submitted" if accepted else "rejected",
        "target": target, "violations": violations,
        "motivation": motivation[:200],
    }
    if accepted:
        os.makedirs(os.path.join(_EVAL_REPORTS, "proposals"), exist_ok=True)
        with open(os.path.join(_EVAL_REPORTS, "proposals", f"{proposal_id}.json"),
                  "w", encoding="utf-8") as fh:
            json.dump(proposal, fh, ensure_ascii=False, indent=1)
    _save_ledger(ledger)
    return {"success": True, "accepted": accepted, "proposal_id": proposal_id,
            "violations": violations}


@plugin.tool(
    name="proposal_apply",
    schema={
        "type": "object",
        "required": ["proposal_id"],
        "properties": {
            "proposal_id": {"type": "string"},
            "message": {"type": "string"},
        },
        "required": ["proposal_id"],
    },
    description="应用已受理提案：content 写入主仓目标文件（G2+热重载原生生效）并 pathspec commit",
)
async def proposal_apply(proposal_id: str, message: str = "") -> dict[str, Any]:
    ledger = _load_ledger()
    prop = (ledger.get("proposals") or {}).get(proposal_id)
    if not prop or prop.get("outcome") != "submitted":
        return _err(f"提案 {proposal_id} 不存在或状态不可应用")
    prop_path = os.path.join(_EVAL_REPORTS, "proposals", f"{proposal_id}.json")
    if not os.path.isfile(prop_path):
        return _err(f"提案存档缺失: {prop_path}")
    proposal = json.load(open(prop_path, encoding="utf-8"))
    violations = proposal_mod.validate(proposal, _PROJECT_ROOT)  # 应用前复核
    if violations:
        return _err(f"应用前复核未过: {violations}")
    target_rel = str(proposal["target"]).replace("\\", "/").lstrip("./")
    abs_target = os.path.join(_PROJECT_ROOT, target_rel)
    os.makedirs(os.path.dirname(abs_target) or ".", exist_ok=True)
    with open(abs_target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(str(proposal["content"]))
    code, out = _git(["add", "--", target_rel])
    if code != 0:
        return _err(f"git add 失败: {out}")
    code, out = _git(["commit", "-m",
                      message or f"evolve: apply {proposal_id}", "--", target_rel])
    if code != 0:
        return _err(f"git commit 失败: {out}")
    code, out = _git(["rev-parse", "--short", "HEAD"])
    commit_hash = out.strip().splitlines()[-1] if code == 0 else ""
    prop["outcome"] = "applied"
    prop["commit"] = commit_hash
    _save_ledger(ledger)
    return {"success": True, "applied": True, "commit": commit_hash,
            "target": target_rel,
            "note": "配置/插件变更由内核 G2 校验与热重载原生接管生效"}


@plugin.tool(
    name="proposal_reject",
    schema={
        "type": "object",
        "required": ["proposal_id", "reason"],
        "properties": {"proposal_id": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["proposal_id", "reason"],
    },
    description="驳回提案：删存档、记录原因（失败经验入账）",
)
async def proposal_reject(proposal_id: str, reason: str) -> dict[str, Any]:
    ledger = _load_ledger()
    prop = (ledger.get("proposals") or {}).get(proposal_id)
    if not prop:
        return _err(f"提案 {proposal_id} 不存在")
    prop["outcome"] = "rejected"
    prop["reason"] = reason
    prop_path = os.path.join(_EVAL_REPORTS, "proposals", f"{proposal_id}.json")
    if os.path.isfile(prop_path):
        os.remove(prop_path)
    _save_ledger(ledger)
    return {"success": True, "ok": True, "proposal_id": proposal_id}


# ── eval_health：轻量账本聚合 ────────────────────────────────────────────
@plugin.tool(
    name="eval_health",
    schema={"type": "object", "properties": {}},
    description="流程健康度：轮次/提案命中率/杠杆账（聚合自 reports/eval 账本）",
)
async def eval_health() -> dict[str, Any]:
    ledger = _load_ledger()
    proposals = ledger.get("proposals") or {}
    outcomes = [p.get("outcome") for p in proposals.values()]
    applied = sum(1 for o in outcomes if o == "applied")
    total = len(outcomes)
    levers: dict[str, dict[str, int]] = {}
    for p in proposals.values():
        lever = str(p.get("motivation", ""))[:30]
        if not lever:
            continue
        lv = levers.setdefault(lever, {"applied": 0, "total": 0})
        lv["total"] += 1
        if p.get("outcome") == "applied":
            lv["applied"] += 1
    return {"success": True, "rounds": len(ledger.get("rounds") or []),
            "proposals_total": total, "proposals_applied": applied,
            "hit_rate": round(applied / total, 3) if total else None,
            "levers": levers}


def _git(args: list[str]) -> tuple[int, str]:
    import subprocess
    proc = subprocess.run(["git", *args], cwd=_PROJECT_ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


if __name__ == "__main__":
    plugin.run()
