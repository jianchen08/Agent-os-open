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

import base64
import json
import os
import shutil
import time
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin
from agentos_plugin_sdk.capability import bind_capability_caller

plugin = AgentOSPlugin("eval_harness_service")

_paths = bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

import aggregate  # noqa: E402
import evolution_metrics  # noqa: E402
import proposal as proposal_mod  # noqa: E402
import suite_reader  # noqa: E402

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
_EVAL_REPORTS = os.path.join(_PROJECT_ROOT, "reports", "eval")
_HELDOUT_DIR = os.environ.get("AGENTOS_HELDOUT_DIR", "").strip()


def _err(msg: str) -> dict[str, Any]:
    return {"success": False, "error": msg}


def _tool_executor_caller() -> Any:
    """tool-executor 能力句柄的 async caller（跨插件工具/服务调用的既有通道）。"""
    te = plugin.get_capability("tool-executor")
    return bind_capability_caller(te, "tool-executor")


async def _fetch_mode_profile(mode: str) -> dict[str, Any]:
    """经内核服务调用取 mode.get_profile（跨插件服务面，tool-executor 显式 plugin_id）。

    模式 profile 已内打包进模式插件目录（出厂种子），消费只走服务调用，
    不直读他方文件。返回信封两种形态都容忍：{"data": {...}}（调用信封）
    或 profile 本体。非 dict / 缺 mode 键 = 服务返回无效，抛 ValueError。
    """
    invoke = _tool_executor_caller()
    res = await invoke("tool-executor.invoke",
                       {"tool_name": "mode.get_profile",
                        "plugin_id": f"mode_{mode}", "args": {}})
    data = res.get("data") if isinstance(res, dict) else None
    profile = data if isinstance(data, dict) else res
    if not isinstance(profile, dict) or not profile.get("mode"):
        raise ValueError(f"mode.get_profile 返回无效 profile: {str(res)[:200]}")
    return profile


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
            "mode": {"type": "string", "description": "任务模式（题集经该模式插件的 mode.get_profile 服务解析）"},
            "tier": {"type": "string", "description": "profile.suite 档位（缺省 smoke）"},
            "cases": {"type": "string", "description": "逗号分隔 case id 过滤"},
        },
        "required": ["mode"],
    },
    description="按模式取题集展开为 task_submit 批次清单（模式 profile 经服务调用获取，"
                "你无需知道题集路径；派发由你用自己的 task_submit 执行）",
)
async def eval_view_suite(mode: str, tier: str = "smoke", cases: str = "") -> dict[str, Any]:
    try:
        profile = await _fetch_mode_profile(mode)
        suite_rel = suite_reader.resolve_suite_from_profile(profile, tier)
        data = suite_reader.load_suite(os.path.join(_PROJECT_ROOT, suite_rel))
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        return _err(f"mode={mode} tier={tier}: {exc}")
    batches = suite_reader.expand_batches(data, cases or None)
    return {"success": True, "mode": data.get("mode", ""), "suite": suite_rel,
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
            "model": {"type": "string",
                      "description": "本轮执行模型 id（重放键：基线绑定模型口径落账）"},
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
                        "budget": {"type": "object",
                                   "description": "题集声明的 case 预算上限 "
                                                  "{max_rounds,max_tokens,max_seconds}"},
                        "metrics": {"type": "object",
                                    "description": "过程指标采集 {rounds,tokens,seconds}"
                                                   "（task_manage 可得；供 budget_ok 判定）"},
                    },
                    "required": ["case_id", "task_status", "criteria"],
                },
            },
        },
        "required": ["mode", "results"],
    },
    description="聚合评测结果出 scorecard（失败分类为复盘插件深分析的预处理；"
                "判定谓词=终态∧AC∧budget_ok，声明预算请附 metrics 采集）",
)
async def eval_summarize(mode: str, results: list[dict[str, Any]],
                         model: str = "") -> dict[str, Any]:
    ledger = _load_ledger()
    baseline = None
    for r in reversed(ledger.get("rounds") or []):
        if r.get("mode") == mode:
            baseline = int(r.get("passed") or 0)
            break
    summary = aggregate.summarize(mode, results, baseline)
    run_id = suite_reader.time_tag()
    path = _save_summary(run_id, mode, summary)
    # 重放键（ADR 2026-09-16）：material_ref/model/逐 case 结果落账——
    # 保留率、学习率曲线、去噪声复算的数据基座；旧账缺字段时指标降级
    # insufficient_data，不误判。
    ledger.setdefault("rounds", []).append({
        "run_id": run_id, "mode": mode,
        "passed": summary["passed"], "total": summary["total"],
        "failed_cases": [r["case_id"] for r in summary["results"] if not r["passed"]],
        "case_results": {str(r["case_id"]): bool(r["passed"])
                         for r in summary["results"]},
        "material_ref": _git_head(), "model": str(model or ""),
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
        invoke = _tool_executor_caller()
    except (OSError, ValueError, KeyError) as exc:
        return _err(str(exc))
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
            task_id = str(data_field.get("task_id") or data_field.get("id") or "")
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


# 驳回分类账（ADR 2026-09-16）：外因识别/判断力失败/用户否决等裁决证据
# 的可测化——归因准确率与 P3 一票否决都从这张分类账出。
REJECT_CATEGORIES = {
    "prediction_miss", "heldout_gap", "sentry_regression", "external_cause",
    "harness_defect", "system_fault", "user_veto", "judgment_failure", "other",
}


@plugin.tool(
    name="proposal_reject",
    schema={
        "type": "object",
        "required": ["proposal_id", "reason"],
        "properties": {
            "proposal_id": {"type": "string"},
            "reason": {"type": "string"},
            "category": {"type": "string",
                         "enum": sorted(REJECT_CATEGORIES),
                         "description": "驳回/回滚分类：外因记账、预测失准、"
                                        "哨兵退化、用户否决等（判定证据账）"},
        },
        "required": ["proposal_id", "reason"],
    },
    description="驳回提案：记录原因与分类（失败经验入账）；已应用的提案被"
                "用户裁决回滚时同样走本工具（outcome=reverted，存档保留供审计）",
)
async def proposal_reject(proposal_id: str, reason: str,
                          category: str = "other") -> dict[str, Any]:
    if category not in REJECT_CATEGORIES:
        return _err(f"category 非法: {category!r}（允许: {sorted(REJECT_CATEGORIES)}）")
    ledger = _load_ledger()
    prop = (ledger.get("proposals") or {}).get(proposal_id)
    if not prop:
        return _err(f"提案 {proposal_id} 不存在")
    if prop.get("outcome") == "applied":
        # 用户裁决回滚通道：命中率扣回自动化（2026-09-13 eeb2a2614 案例的账面化）
        prop["outcome"] = "reverted"
        prop["reason"] = reason
        prop["category"] = category
        _save_ledger(ledger)
        return {"success": True, "ok": True, "proposal_id": proposal_id,
                "outcome": "reverted",
                "note": "已应用提案标记 reverted，git 层回滚由用户执行"}
    prop["outcome"] = "rejected"
    prop["reason"] = reason
    prop["category"] = category
    prop_path = os.path.join(_EVAL_REPORTS, "proposals", f"{proposal_id}.json")
    if os.path.isfile(prop_path):
        os.remove(prop_path)
    _save_ledger(ledger)
    return {"success": True, "ok": True, "proposal_id": proposal_id}


# ── eval_health：轻量账本聚合 + P0-P3 自进化裁决 ─────────────────────────
@plugin.tool(
    name="eval_health",
    schema={"type": "object", "properties": {}},
    description="流程健康度（轮次/提案命中率/杠杆账）+ 自进化指标 P0-P3 裁决"
                "（学习率/迁移/保留率/方差/效率/回滚/一票否决，"
                "evolution.verdict 字段；熔断 verdict=vetoed 后停止提案）",
)
async def eval_health() -> dict[str, Any]:
    ledger = _load_ledger()
    evolution = evolution_metrics.verdict(ledger)
    return {"success": True, "rounds": len(ledger.get("rounds") or []),
            "evolution": evolution,
            "breaker_tripped": evolution["verdict"] == "vetoed",
            **aggregate.health_summary(ledger)}


# ── webview 面板页（自进化监控 tab）供给 ──────────────────────────────────
# 面板承载形态照抄仓内先例（monitoring payload_diag 页 / mode_* 面板页）：
# manifest 声明 GET /ext/<id>/page/evolution-panel（handler_capability=http.handle），
# 本文件按 path 返回包内 webview/ 单文件 HTML，并在供页时把账本聚合快照内联进
# 页面（页面 CSP connect-src 'none'，一期整体快照渲染，增量刷新归后续）。
# HttpHandleResponse 封装在此内联——共享根裸模块对用户空间播种副本不可达
# （同 mode_coding 种子的自包含约束）。

_PANEL_ENDPOINT = "/ext/eval_harness_service/page/evolution-panel"
_PANEL_HTML_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "webview", "evolution_panel.html"
)
_SNAPSHOT_ANCHOR = "null/*__SNAPSHOT_INJECT__*/"


def _json_response(payload: dict[str, Any], status: int = 200) -> dict[str, Any]:
    """任意 JSON 对象 → HttpHandleResponse（body base64，内核约定）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return {
        "status": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": base64.b64encode(body).decode("ascii"),
        "body_encoding": "base64",
    }


def _panel_html_response() -> dict[str, Any]:
    """包内面板页 + 账本快照内联 → HttpHandleResponse（text/html，body base64）。"""
    with open(_PANEL_HTML_PATH, encoding="utf-8") as fh:
        html = fh.read()
    if _SNAPSHOT_ANCHOR not in html:
        raise ValueError(f"面板页缺快照注入锚点: {_SNAPSHOT_ANCHOR}")
    snapshot = aggregate.panel_snapshot(_load_ledger())
    injected = json.dumps(snapshot, ensure_ascii=False).replace("</", "<\\/")
    html = html.replace(_SNAPSHOT_ANCHOR, injected, 1)
    return {
        "status": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": base64.b64encode(html.encode("utf-8")).decode("ascii"),
        "body_encoding": "base64",
    }


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object"},
            "query": {"type": "object"},
        },
    },
    description="HTTP endpoint handler for /ext/eval_harness_service/** (自进化面板页供给)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发：GET 面板页 HTML（内联数据快照）；未路由 path/method 返回 404 JSON。"""
    if path == _PANEL_ENDPOINT and method == "GET":
        try:
            return {"success": True, "data": _panel_html_response()}
        except (OSError, ValueError) as exc:
            return {
                "success": False,
                "error": f"panel html serve failed: {exc}",
                "data": _json_response({"error": "panel html unavailable"}, 500),
            }
    return {"success": True, "data": _json_response({"error": "not found", "path": path}, 404)}


def _git(args: list[str]) -> tuple[int, str]:
    import subprocess
    proc = subprocess.run(["git", *args], cwd=_PROJECT_ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _git_head() -> str:
    """当前物料基线 git short HEAD（重放键；非 git 环境返回空串不阻断评测）。"""
    code, out = _git(["rev-parse", "--short", "HEAD"])
    return out.strip().splitlines()[-1] if code == 0 and out.strip() else ""


if __name__ == "__main__":
    plugin.run()
