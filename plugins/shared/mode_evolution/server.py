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

import asyncio
import base64
import json
import os
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
import screening  # noqa: E402
import suite_reader  # noqa: E402
import workflow_ledger as wf_ledger  # noqa: E402
from _libs.mode_server_base import (  # noqa: E402
    html_text_response,
    load_mode_profile,
    register_profile_services,
)

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_EVAL_REPORTS = os.path.join(_PROJECT_ROOT, "reports", "eval")
_HELDOUT_DIR = os.environ.get("AGENTOS_HELDOUT_DIR", "").strip()


# ── 模式声明面（mode.describe / mode.get_profile，包内 mode.yaml 自持）────────
# 收拢为模式包（ADR 2026-09-28-self-evolution-mode-pack）后声明面自持，
# 原 requires_services 的跨插件 mode.get_profile 借道退役；跨**目标**模式
# 取声明面仍走 _fetch_mode_profile 服务调用（那是别的模式包的自有面）。
# 实现单源在 _libs 基面（六模式同构，AR 收敛刀）。

_PROFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mode.yaml")


def load_profile(path: str = _PROFILE_PATH) -> dict[str, Any]:
    """读取包内 mode.yaml（缺省本包种子；fail-closed 实现在 _libs 基面）。"""
    return load_mode_profile(path)


# 模式元数据服务（manifest capabilities.services；handler 别名供契约测试直调）
mode_describe, mode_get_profile = register_profile_services(
    plugin,
    _PROFILE_PATH,
    "返回自进化模式元数据（chain/权重/预算/profile 路径/panel_page_id）",
    "返回自进化模式包内 mode.yaml 解析后的内容",
)


def _read_json(path: str) -> dict[str, Any]:
    """读 JSON 文件（裸 open 的上下文管理器化，SIM115）。"""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@plugin.on_load
async def _on_load(_params: dict[str, Any]) -> None:
    """注入 card 质检服务调用方（provides namespace ``card`` 直连，T6c）。

    能力句柄在协程内懒解析：on_load 早于 capability 注入完成也能在真正调用
    时拿到（task_submit / resource_search 同款）；未注入 = content_audit
    fail-closed 拒绝（安检面缺席不可放行）。
    """

    async def _card_service(method: str, params_: dict[str, Any]) -> Any:
        handle = plugin.get_capability("card")
        return await handle.call(method, params_)

    proposal_mod.set_card_service_caller(_card_service)


@plugin.on_unload
async def _on_unload(_params: dict[str, Any]) -> None:
    """摘除服务调用方（防卸载窗口期残留句柄——isolation_guard 同款）。"""
    proposal_mod.set_card_service_caller(None)


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
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except ValueError:
            pass
    return {"rounds": [], "proposals": {}, "levers": {}}


def _save_ledger(state: dict[str, Any]) -> None:
    os.makedirs(_EVAL_REPORTS, exist_ok=True)
    with open(os.path.join(_EVAL_REPORTS, "eval_state.json"), "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)


# ── 回顾工作流埋点（步骤节点由真实工具调用驱动，不靠提示词上报）──────────────
# 四个业务工具 handler 在业务动作后落事件；事件层（workflow_ledger）写盘失败只
# warn、不抛——埋点失败不能让评测动作失败（非核心 best-effort，日志带
# wf_id/step 上下文即合规）。
async def _wf_begin(step: str, tool: str, mode: str = "", round_type: str = "",
                    inputs: dict[str, Any] | None = None) -> None:
    """登记步骤开始（T1 测量 / T5 裁决这类跨多次工具调用的长动作的起点）。

    落盘经 ``asyncio.to_thread`` 出事件循环：工作流文件与账本同盘（可能是慢盘/
    网络挂载），同步写在合宿宿主里会饿死心跳（watchdog 30s 判停滞整组
    ``os._exit``）——与 ``_git`` 同一纪律。
    """
    await asyncio.to_thread(wf_ledger.begin_step, "", step,
                            dict(inputs or {}), f"tool:{tool}", mode, round_type)


async def _wf_finish(step: str, tool: str, mode: str = "", round_type: str = "",
                     artifacts: dict[str, Any] | None = None,
                     status: str = "done") -> None:
    """登记步骤结束；该步骤无 running 节点时补一条已完成节点（事件即证据）。"""
    await asyncio.to_thread(wf_ledger.finish_step, "", step, status,
                            dict(artifacts or {}), None, f"tool:{tool}",
                            mode, round_type)


def _round_regressions(ledger: dict[str, Any], mode: str,
                       case_results: dict[str, bool]) -> list[str] | None:
    """本轮相对上一 treatment 轮的退化 case（T6.5 保留率复验的真实观测面）。

    case 集可轮换：只在前轮通过集上判退化，本轮未出现的 case 不算退化（与
    evolution_metrics.retention 同口径）。无前序 treatment 轮 → None（首轮无
    保留率可言，不冒领「零退化」）。
    """
    prev: dict[str, Any] | None = None
    for row in reversed(ledger.get("rounds") or []):
        if (row.get("mode") == mode
                and str(row.get("round_type") or "treatment") != "control"):
            prev = row
            break
    if prev is None:
        return None
    prev_ok = {str(k) for k, v in (prev.get("case_results") or {}).items() if v}
    return sorted(cid for cid in prev_ok if case_results.get(cid) is False)


# ── eval_query：评测查询面（三读合一） ───────────────────────────────────
# 原 eval_view_suite / eval.screen / eval_health 三件按工具面审查 §十 合并计划
# 收敛为一件（工具面退役·任务 C）：domain 必填枚举即约束，oneOf 按 domain 隔离
# 参数集，health 域只用 mode 一个参数（高频动作零解包）。实现体原地复用不复制：
# 三域各自由下方同名私有体承载（_eval_view_suite / _eval_screen / _eval_health）。
# 参数错经运行时教学错误自纠（schema oneOf 是给 LLM 的声明面约束）。
_EVAL_QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "domain": {
            "type": "string",
            "enum": ["suite", "screen", "health"],
            "description": "查询域（必填枚举）：suite=题集展开为 task_submit 批次清单"
                           "（用 tier/cases）；screen=轨迹库先行筛选证据量（用 "
                           "title_substring）；health=流程健康度与 P0-P3 进化裁决"
                           "（全账本口径，只用 mode 一个参数）",
        },
        "mode": {
            "type": "string",
            "description": "任务模式（全仓一个含义=模式包 id；suite/screen 经该模式"
                           "插件的 mode.get_profile 服务解析，health 为调用定位参数）",
        },
        "tier": {
            "type": "string",
            "description": "仅 domain='suite'：profile.suite 档位（缺省 smoke）",
        },
        "cases": {
            "type": "string",
            "description": "仅 domain='suite'：逗号分隔 case id 过滤",
        },
        "title_substring": {
            "type": "string",
            "description": "仅 domain='screen'：题名子串过滤（缺省按模式全量计）",
        },
    },
    "required": ["domain", "mode"],
    "oneOf": [
        {
            "properties": {
                "domain": {"const": "suite"},
                "mode": {},
                "tier": {},
                "cases": {},
            },
            "required": ["domain"],
            "additionalProperties": False,
        },
        {
            "properties": {
                "domain": {"const": "screen"},
                "mode": {},
                "title_substring": {},
            },
            "required": ["domain"],
            "additionalProperties": False,
        },
        {
            "properties": {
                "domain": {"const": "health"},
                "mode": {},
            },
            "required": ["domain"],
            "additionalProperties": False,
        },
    ],
}

_EVAL_QUERY_DESCRIPTION = (
    "评测查询面（三读合一）。"
    "domain='suite'：按模式取题集展开为 task_submit 批次清单（模式 profile 经服务"
    "调用获取，你无需知道题集路径；派发由你用自己的 task_submit 执行）。最小示例："
    "eval_query(domain='suite', mode='coding', tier='smoke')。"
    "domain='screen'：轨迹库先行筛选——假设失败簇对轨迹样本库核查证据量（命中 ≥2 条"
    "失败轨迹才允许立项；不足先复跑确认或继续采集，省真实题集运行预算；"
    "verdict='insufficient' 是裁决不是错误）。最小示例："
    "eval_query(domain='screen', mode='coding', title_substring='导出')。"
    "domain='health'：流程健康度（轮次/提案命中率/杠杆账）+ 自进化指标 P0-P3 裁决"
    "（学习率/迁移/保留率/方差/效率/回滚/一票否决，看 evolution.verdict 字段；"
    "熔断 verdict='vetoed' 后停止提案）。最小示例："
    "eval_query(domain='health', mode='coding')。"
)


@plugin.tool(name="eval_query", schema=_EVAL_QUERY_SCHEMA,
             description=_EVAL_QUERY_DESCRIPTION)
async def eval_query(domain: str, mode: str, tier: str = "",
                     cases: str = "", title_substring: str = "") -> dict[str, Any]:
    """按 domain 分发到三读实现体；跨域参数运行时教学错误（错误教改法）。"""
    if domain == "suite":
        if title_substring:
            return _err("title_substring 仅 domain='screen'（轨迹筛选）可用；"
                        "suite 域用 tier/cases，示例: eval_query(domain='suite', "
                        f"mode='{mode}', tier='smoke')")
        return await _eval_view_suite(mode, tier=tier or "smoke", cases=cases)
    if domain == "screen":
        if tier or cases:
            return _err("tier/cases 仅 domain='suite'（题集展开）可用；screen 域用 "
                        "title_substring，示例: eval_query(domain='screen', "
                        f"mode='{mode}', title_substring='<题名关键词>')")
        return await _eval_screen(mode, title_substring)
    if domain == "health":
        if tier or cases or title_substring:
            return _err("health 域只需 mode 一个参数（高频动作零解包）；tier/cases 属 "
                        "suite 域，title_substring 属 screen 域，示例: "
                        f"eval_query(domain='health', mode='{mode}')")
        return await _eval_health()
    return _err(f"domain 非法: {domain!r}（允许: 'suite' | 'screen' | 'health'）；"
                "示例: eval_query(domain='health', mode='<模式>')")


# ── suite 域实现体：开发集 → task_submit 批次清单（agent 自派） ───────────
async def _eval_view_suite(mode: str, tier: str = "smoke",
                           cases: str = "") -> dict[str, Any]:
    try:
        profile = await _fetch_mode_profile(mode)
        suite_rel = suite_reader.resolve_suite_from_profile(profile, tier)
        data = suite_reader.load_suite(os.path.join(_PROJECT_ROOT, suite_rel))
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        return _err(f"mode={mode} tier={tier}: {exc}")
    batches = suite_reader.expand_batches(data, cases or None)
    await _wf_begin("T1", "eval_query", mode=mode, inputs={
        "suite": suite_rel, "tier": tier, "cases": str(cases or ""),
        "case_ids": [b["case_id"] for b in batches],
    })
    return {"success": True, "mode": data.get("mode", ""), "suite": suite_rel,
            "batches": batches,
            "note": "逐条用自己的 task_submit 派发；完成后用 task_manage 查终态"
                    "与验收结果，喂给 eval_summarize 聚合"}


# ── eval_summarize：结果聚合 ─────────────────────────────────────────────
# 轮次类型（ADR 2026-09-29-eval-repeat-control-screening）：treatment=进化
# 进程轮（缺省）；control=零变更同预算对照轮（T4 对照臂）——不入基线链、
# 不进 P0-P3 进程序列，仅经 control_comparison 透出真增益参照。
ROUND_TYPES = ("treatment", "control")


@plugin.tool(
    name="eval_summarize",
    schema={
        "type": "object",
        "properties": {
            "mode": {"type": "string"},
            "model": {"type": "string",
                      "description": "本轮执行模型 id（重放键：基线绑定模型口径落账）"},
            "round_type": {"type": "string", "enum": list(ROUND_TYPES),
                           "description": "treatment=进化进程轮（缺省）；"
                                          "control=零变更对照轮（不入基线链）"},
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
                "判定谓词=终态∧AC∧budget_ok，声明预算请附 metrics 采集；"
                "repeat 题同 case_id 逐次喂入即自动归组全过口径；对照轮以 "
                "round_type=control 声明）",
)
async def eval_summarize(mode: str, results: list[dict[str, Any]],
                         model: str = "", round_type: str = "treatment") -> dict[str, Any]:
    if round_type not in ROUND_TYPES:
        return _err(f"round_type 非法: {round_type!r}（允许: {list(ROUND_TYPES)}）")
    ledger = _load_ledger()
    baseline = None
    for r in reversed(ledger.get("rounds") or []):
        # 基线链只认 treatment 轮：对照轮 delta（对照−最近 treatment）恰好
        # 是 control_comparison 的逐轮来源，不污染 treatment 间对比
        if (r.get("mode") == mode
                and str(r.get("round_type") or "treatment") != "control"):
            baseline = int(r.get("passed") or 0)
            break
    summary = aggregate.summarize(mode, results, baseline)
    run_id = suite_reader.time_tag()
    path = _save_summary(run_id, mode, summary)
    # 重放键（ADR 2026-09-16）：material_ref/model/逐 case 结果落账——
    # 保留率、学习率曲线、去噪声复算的数据基座；旧账缺字段时指标降级
    # insufficient_data，不误判。_git_head 是同步 subprocess（timeout 120s），
    # 必须出事件循环：async handler 直调会饿死合宿宿主心跳（watchdog 30s
    # 判停滞整组 os._exit）。
    round_row = {
        "run_id": run_id, "mode": mode, "round_type": round_type,
        "passed": summary["passed"], "total": summary["total"],
        "failed_cases": [r["case_id"] for r in summary["results"] if not r["passed"]],
        "case_results": {str(r["case_id"]): bool(r["passed"])
                         for r in summary["results"]},
        "material_ref": await asyncio.to_thread(_git_head),
        "model": str(model or ""),
    }
    if "repeat_detail" in summary:
        round_row["repeat_detail"] = summary["repeat_detail"]
    case_results = {str(r["case_id"]): bool(r["passed"]) for r in summary["results"]}
    regressed = _round_regressions(ledger, mode, case_results)
    ledger.setdefault("rounds", []).append(round_row)
    ledger["rounds"] = ledger["rounds"][-200:]
    _save_ledger(ledger)
    # 步骤落账：T1 测量收口（带本轮 run_id 作提案↔轮次的软关联键）+ T6.5 保留率
    # 复验（退化名单是本轮真实观测；首轮无前序轮 / 对照轮 → skipped，不冒领零退化）
    await _wf_finish("T1", "eval_summarize", mode=mode, round_type=round_type, artifacts={
        "run_id": run_id, "passed": summary["passed"], "total": summary["total"],
        "round_type": round_type, "model": str(model or ""),
    })
    if round_type == "control":
        await _wf_finish("T6.5", "eval_summarize", mode=mode, status="skipped",
                         artifacts={"run_id": run_id, "reason": "control_round"})
    elif regressed is None:
        await _wf_finish("T6.5", "eval_summarize", mode=mode, status="skipped",
                         artifacts={"run_id": run_id, "reason": "no_previous_round"})
    else:
        await _wf_finish("T6.5", "eval_summarize", mode=mode, artifacts={
            "run_id": run_id, "regressed_cases": regressed,
            "case_count": len(case_results),
        })
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
    await _wf_begin("T5", "eval_run_heldout", mode=mode,
                    inputs={"run_tag": tag,
                            "case_ids": [m["case_id"] for m in mapping]})
    return {"success": True, "run_tag": tag, "dispatched": mapping,
            "note": "用 task_manage 查这些任务的终态与验收结果；聚合时 mode 传 "
                    f"'{mode}'，case_id 与任务状态喂 eval_summarize。题面与"
                    "验收细节不向你返回（保密裁决）"}


# ── proposal：提案动作面（三写合一，pim 的 action 参数先例） ─────────────
# 原 proposal_submit / proposal_apply / proposal_reject 三件收敛为一件（工具面
# 审查 §十 合并计划）：action 必填枚举，oneOf 按 action 隔离参数集（submit 与
# apply/reject 参数集完全不相交）；实现体原地复用不复制。账本写面语义不变。
_PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["submit", "apply", "reject"],
            "description": "提案动作（必填枚举）：submit=提交物料更改提案（冻结面/"
                           "白名单/原子性/base 校验，不过即拒）；apply=应用已受理"
                           "提案（content 写主仓目标文件并 pathspec commit，应用前"
                           "复核）；reject=驳回提案记账（已应用提案被用户裁决回滚"
                           "同样走本 action，outcome=reverted 存档保留供审计）",
        },
        "layer": {
            "type": "string",
            "enum": ["L1", "L2", "L3"],
            "description": "仅 action='submit'：提案层",
        },
        "target": {
            "type": "string",
            "description": "仅 action='submit'：目标物料路径",
        },
        "change_type": {
            "type": "string",
            "description": "仅 action='submit'：变更类型",
        },
        "content": {
            "type": "string",
            "description": "仅 action='submit'：变更内容全文",
        },
        "motivation": {
            "type": "string",
            "description": "仅 action='submit'：动机/杠杆说明",
        },
        "base_hash": {
            "type": "string",
            "description": "仅 action='submit'：基线 git short HEAD（可选，防漂移）",
        },
        "proposal_id": {
            "type": "string",
            "description": "仅 action='apply'/'reject'：提案 id（submit 返回的 "
                           "proposal_id）",
        },
        "message": {
            "type": "string",
            "description": "仅 action='apply'：commit message（缺省 "
                           "'evolve: apply <proposal_id>'）",
        },
        "reason": {
            "type": "string",
            "description": "仅 action='reject'：驳回/回滚原因",
        },
        "category": {
            "type": "string",
            "enum": [
                "external_cause",
                "harness_defect",
                "heldout_gap",
                "judgment_failure",
                "other",
                "prediction_miss",
                "sentry_regression",
                "system_fault",
                "user_veto",
            ],
            "description": "仅 action='reject'：驳回/回滚分类（判定证据账：归因"
                           "准确率与 P3 一票否决的数据源；缺省 other）",
        },
    },
    "required": ["action"],
    "oneOf": [
        {
            "properties": {
                "action": {"const": "submit"},
                "layer": {},
                "target": {},
                "change_type": {},
                "content": {},
                "motivation": {},
                "base_hash": {},
            },
            "required": ["action", "layer", "target", "change_type",
                         "content", "motivation"],
            "additionalProperties": False,
        },
        {
            "properties": {
                "action": {"const": "apply"},
                "proposal_id": {},
                "message": {},
            },
            "required": ["action", "proposal_id"],
            "additionalProperties": False,
        },
        {
            "properties": {
                "action": {"const": "reject"},
                "proposal_id": {},
                "reason": {},
                "category": {},
            },
            "required": ["action", "proposal_id", "reason"],
            "additionalProperties": False,
        },
    ],
}

_PROPOSAL_DESCRIPTION = (
    "提案动作面（三写合一）。"
    "action='submit'：提交物料更改提案（冻结面/白名单/原子性/base 校验，不过即拒；"
    "target 落 mode_roleplay 三内容目录 yaml = 出厂内容提案，安检经 card 质检服务"
    "（判定单源 card_lint）：结构+suspicious 阻断、HTML 标注落账）。最小示例："
    "proposal(action='submit', layer='L2', "
    "target='config/agents/main/agentos.yaml', change_type='tool_ids', "
    "content='<变更全文>', motivation='<动机>')。"
    "action='apply'：应用已受理提案——content 写入主仓目标文件（系统自动校验并热"
    "生效）并 pathspec commit；出厂内容提案落 mode_roleplay 内容目录，装机生效须 "
    "bump 种子插件版本（提示随 note，实际 bump 人审批准环节执行）。最小示例："
    "proposal(action='apply', proposal_id='p20260101_120000')。"
    "action='reject'：驳回提案：记录原因与分类（失败经验入账）；已应用的提案被"
    "用户裁决回滚时同样走本 action（outcome=reverted，存档保留供审计）。最小示例："
    "proposal(action='reject', proposal_id='p20260101_120000', "
    "reason='<原因>', category='prediction_miss')。"
)

# 驳回分类账（ADR 2026-09-16）：外因识别/判断力失败/用户否决等裁决证据
# 的可测化——归因准确率与 P3 一票否决都从这张分类账出。
# 声明面：_PROPOSAL_SCHEMA 的 category.enum 是本集合的同步投影（新增分类双面
# 同改；test_eval_harness_server_gaps 锁步断言防漂移）。
REJECT_CATEGORIES = {
    "prediction_miss", "heldout_gap", "sentry_regression", "external_cause",
    "harness_defect", "system_fault", "user_veto", "judgment_failure", "other",
}

_SUBMIT_REQUIRED = ("layer", "target", "change_type", "content", "motivation")


@plugin.tool(name="proposal", schema=_PROPOSAL_SCHEMA,
             description=_PROPOSAL_DESCRIPTION)
async def proposal(action: str, layer: str = "", target: str = "",
                   change_type: str = "", content: str = "",
                   motivation: str = "", base_hash: str = "",
                   proposal_id: str = "", message: str = "",
                   reason: str = "", category: str = "") -> dict[str, Any]:
    """按 action 分发到三写实现体；跨动作参数运行时教学错误（错误教改法）。"""
    if action == "submit":
        foreign = [k for k, v in (("proposal_id", proposal_id),
                                  ("message", message), ("reason", reason)) if v]
        if foreign:
            return _err(f"{'/'.join(foreign)} 仅 action='apply'/'reject' 可用；submit 的必填"
                        f"参数集: {list(_SUBMIT_REQUIRED)}（base_hash 可选），示例: "
                        "proposal(action='submit', layer='L2', target='<路径>', "
                        "change_type='<类型>', content='<全文>', motivation='<动机>')")
        missing = [k for k in _SUBMIT_REQUIRED
                   if not {"layer": layer, "target": target,
                           "change_type": change_type, "content": content,
                           "motivation": motivation}[k]]
        if missing:
            return _err(f"action='submit' 缺必填参数: {', '.join(missing)}（必填集: "
                        f"{list(_SUBMIT_REQUIRED)}，base_hash 可选），示例: "
                        "proposal(action='submit', layer='L2', target='<路径>', "
                        "change_type='<类型>', content='<全文>', motivation='<动机>')")
        return await _proposal_submit(layer=layer, target=target,
                                      change_type=change_type, content=content,
                                      motivation=motivation, base_hash=base_hash)
    if action == "apply":
        foreign = [k for k, v in (("layer", layer), ("target", target),
                                  ("change_type", change_type), ("content", content),
                                  ("motivation", motivation), ("reason", reason),
                                  ("category", category)) if v]
        if foreign:
            return _err(f"{'/'.join(foreign)} 仅 action='submit'/'reject' 可用；apply 只需 "
                        "proposal_id（message 可选），示例: "
                        "proposal(action='apply', proposal_id='<提案id>')")
        if not proposal_id:
            return _err("action='apply' 缺必填参数: proposal_id，示例: "
                        "proposal(action='apply', proposal_id='<提案id>')")
        return await _proposal_apply(proposal_id=proposal_id, message=message)
    if action == "reject":
        foreign = [k for k, v in (("layer", layer), ("target", target),
                                  ("change_type", change_type), ("content", content),
                                  ("motivation", motivation), ("message", message)) if v]
        if foreign:
            return _err(f"{'/'.join(foreign)} 仅 action='submit'/'apply' 可用；reject 必填 "
                        "proposal_id/reason（category 可选，缺省 other），示例: "
                        "proposal(action='reject', proposal_id='<提案id>', "
                        "reason='<原因>', category='prediction_miss')")
        if not proposal_id or not reason:
            return _err(f"action='reject' 缺必填参数: "
                        f"{', '.join(k for k, v in (('proposal_id', proposal_id), ('reason', reason)) if not v)}"
                        "，示例: proposal(action='reject', proposal_id='<提案id>', "
                        "reason='<原因>', category='prediction_miss')")
        return await _proposal_reject(proposal_id=proposal_id, reason=reason,
                                      category=category or "other")
    return _err(f"action 非法: {action!r}（允许: 'submit' | 'apply' | 'reject'）；"
                "示例: proposal(action='submit', layer='L2', target='<路径>', "
                "change_type='<类型>', content='<全文>', motivation='<动机>')")


async def _proposal_submit(layer: str, target: str, change_type: str,
                           content: str, motivation: str = "",
                           base_hash: str = "") -> dict[str, Any]:
    proposal = {"layer": layer, "target": target, "change_type": change_type,
                "content": content, "motivation": motivation,
                "base_hash": base_hash}
    violations = await proposal_mod.validate(proposal, _PROJECT_ROOT)
    proposal_id = f"p{time.strftime('%Y%m%d_%H%M%S')}"
    ledger = _load_ledger()
    accepted = not violations
    entry: dict[str, Any] = {
        "outcome": "submitted" if accepted else "rejected",
        "target": target, "violations": violations,
        "motivation": motivation[:200],
    }
    # 内容提案面（扩展波 B-Ⅱ）：受理条目带 content 面——kind/HTML 标注/
    # 装机版本 bump 提示（BuiltinFirst：实际 bump 由人审批准环节执行）。
    content_info: dict[str, Any] | None = None
    content_kind = proposal_mod.content_kind(target)
    if content_kind:
        _c_violations, annotations = await proposal_mod.content_audit(
            target, content, _PROJECT_ROOT)
        content_info = {
            "kind": content_kind,
            "annotations": annotations,
            "version_bump_required": True,
            "version_note": proposal_mod.content_version_note(_PROJECT_ROOT),
        }
        entry["content"] = content_info
    entry["round_id"] = await asyncio.to_thread(wf_ledger.current_round_id)
    ledger.setdefault("proposals", {})[proposal_id] = entry
    if accepted:
        os.makedirs(os.path.join(_EVAL_REPORTS, "proposals"), exist_ok=True)
        with open(os.path.join(_EVAL_REPORTS, "proposals", f"{proposal_id}.json"),
                  "w", encoding="utf-8") as fh:
            json.dump(proposal, fh, ensure_ascii=False, indent=1)
    _save_ledger(ledger)
    # T3 提案节点：受理/被拒都落（被拒也是真实发生过的提案动作，带违规清单）
    await _wf_finish("T3", "proposal", artifacts={
        "proposal_ids": [proposal_id], "accepted": accepted,
        "target": str(target), "violations": list(violations),
        "round_id": str(entry.get("round_id") or ""),
    }, status="done" if accepted else "failed")
    out: dict[str, Any] = {"success": True, "accepted": accepted,
                           "proposal_id": proposal_id, "violations": violations,
                           "round_id": entry["round_id"]}
    if content_info is not None:
        out["content"] = content_info
    return out


async def _proposal_apply(proposal_id: str, message: str = "") -> dict[str, Any]:
    ledger = _load_ledger()
    prop = (ledger.get("proposals") or {}).get(proposal_id)
    if not prop or prop.get("outcome") != "submitted":
        return _err(f"提案 {proposal_id} 不存在或状态不可应用")
    prop_path = os.path.join(_EVAL_REPORTS, "proposals", f"{proposal_id}.json")
    if not os.path.isfile(prop_path):
        return _err(f"提案存档缺失: {prop_path}")
    proposal = _read_json(prop_path)
    violations = await proposal_mod.validate(proposal, _PROJECT_ROOT)  # 应用前复核
    if violations:
        return _err(f"应用前复核未过: {violations}")
    target_rel = str(proposal["target"]).replace("\\", "/").lstrip("./")
    abs_target = os.path.join(_PROJECT_ROOT, target_rel)
    os.makedirs(os.path.dirname(abs_target) or ".", exist_ok=True)
    with open(abs_target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(str(proposal["content"]))
    # 三段 git 均为同步 subprocess（_git timeout=120s）：大仓索引锁等待/
    # 钩子/慢盘都落在这里，直调会饿死合宿宿主心跳（watchdog 30s 判停滞
    # 整组 os._exit）。to_thread 透传返回值、异常与超时语义不变。
    code, out = await asyncio.to_thread(_git, ["add", "--", target_rel])
    if code != 0:
        return _err(f"git add 失败: {out}")
    code, out = await asyncio.to_thread(
        _git,
        ["commit", "-m", message or f"evolve: apply {proposal_id}", "--", target_rel],
    )
    if code != 0:
        return _err(f"git commit 失败: {out}")
    code, out = await asyncio.to_thread(_git, ["rev-parse", "--short", "HEAD"])
    commit_hash = out.strip().splitlines()[-1] if code == 0 else ""
    prop["outcome"] = "applied"
    prop["commit"] = commit_hash
    note = "配置/插件变更由系统自动校验并热生效"
    if proposal_mod.content_kind(target_rel):
        # 出厂内容不在 G2/热重载面（卡/世界书/包随读随扫），装机生效走种子
        # 版本 bump + sync（人审批准环节执行，BuiltinFirst 严格大于内置副本）
        note = proposal_mod.content_version_note(_PROJECT_ROOT)
    _save_ledger(ledger)
    await _wf_finish("T6", "proposal", artifacts={
        "proposal_ids": [proposal_id], "commit": commit_hash,
        "target": target_rel, "outcome": "applied",
    })
    return {"success": True, "applied": True, "commit": commit_hash,
            "target": target_rel, "note": note}


async def _proposal_reject(proposal_id: str, reason: str,
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
        await _wf_finish(wf_ledger.reject_step(category), "proposal", artifacts={
            "proposal_ids": [proposal_id], "category": category,
            "outcome": "reverted", "reason": str(reason)[:200],
        })
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
    await _wf_finish(wf_ledger.reject_step(category), "proposal", artifacts={
        "proposal_ids": [proposal_id], "category": category,
        "outcome": "rejected", "reason": str(reason)[:200],
    })
    return {"success": True, "ok": True, "proposal_id": proposal_id}


# ── health 域实现体：轻量账本聚合 + P0-P3 自进化裁决 ─────────────────────
async def _eval_health() -> dict[str, Any]:
    """流程健康度 + P0-P3 裁决；健康度查询即 T8 循环决策步骤的真实发生点。

    T1 前置的 health 体检（工作流尚未开或尚无节点）不落 T8 节点——那是闸门
    检查，不是循环决策；已有节点之后的 health 才是 T8 出口，并据裁决收口工作流
    （verdict=vetoed 是明确出口；其余无法从 health 单点判定具体出口，显式记
    unknown 而不编造四选一）。
    """
    ledger = _load_ledger()
    evolution = evolution_metrics.verdict(ledger)
    verdict = str(evolution.get("verdict") or "")
    breaker = verdict == "vetoed"
    wf_id = wf_ledger.resolve_wf_id()
    opened = wf_ledger.load_workflow(wf_id)
    if opened is not None and (opened.get("steps") or []):
        await _wf_finish("T8", "eval_query",
                         artifacts={"verdict": verdict, "breaker_tripped": breaker})
        wf_ledger.close_workflow(wf_id, "vetoed" if breaker else "unknown")
    return {"success": True, "rounds": len(ledger.get("rounds") or []),
            "evolution": evolution,
            "breaker_tripped": breaker,
            **aggregate.health_summary(ledger)}


# ── screen 域实现体：轨迹库先行筛选（T2 立项前置证据核查） ────────────────
async def _eval_screen(mode: str, title_substring: str = "") -> dict[str, Any]:
    path = _library_path()
    hypothesis = {"mode": mode, "title_substring": title_substring}
    if not path or not os.path.isfile(path):
        return {"success": True,
                **screening.screen(hypothesis, {})}
    try:
        library = _read_json(path)
    except (OSError, ValueError) as exc:
        return _err(f"轨迹库读取失败 {path}: {exc}")
    return {"success": True, "library": path,
            **screening.screen(hypothesis, library)}


def _library_path() -> str:
    """轨迹库文件解析：env 显式指定优先，否则 reports/eval 下字典序最新。"""
    return (os.environ.get(screening.LIBRARY_ENV, "").strip()
            or screening.find_latest_library(_EVAL_REPORTS))


# ── webview 面板页（自进化监控 tab）供给 ──────────────────────────────────
# 面板承载形态照抄仓内先例（monitoring payload_diag 页 / mode_* 面板页）：
# manifest 声明 GET /ext/<id>/page/evolution-panel（handler_capability=http.handle），
# 本文件按 path 返回包内 webview/ 单文件 HTML，并在供页时把账本聚合快照内联进
# 页面（页面 CSP connect-src 'none'，一期整体快照渲染，增量刷新归后续；
# 重取快照 = 页面 location.reload() 重新供页，导航不受 connect-src 限制）。
# 双线数据面：评测线 = reports/eval 账本投影；日常线 = 供页时经
# service-registry.pipeline-runs.list 拉真实任务运行行（daily_projection，
# 拉取失败 None → 页面按缺口态渲染，不阻断供页）。
# HttpHandleResponse 封装单源在共享根 _libs 基面（mode_server_base，同六模式
# 收敛口径）；快照注入为本模式特有，仍在本文件。

_PANEL_ENDPOINT = "/ext/mode_evolution/page/evolution-panel"
_PANEL_HTML_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "webview", "evolution_panel.html"
)
_SNAPSHOT_ANCHOR = "null/*__SNAPSHOT_INJECT__*/"
# 供页读最近 20 个工作流（目录上限 200，一次只投影最近 N 轮，控供页耗时）
_PANEL_WORKFLOW_LIMIT = 20


def _json_response(payload: dict[str, Any], status: int = 200) -> dict[str, Any]:
    """任意 JSON 对象 → HttpHandleResponse（body base64，内核约定）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return {
        "status": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": base64.b64encode(body).decode("ascii"),
        "body_encoding": "base64",
    }


# 步骤派生证据：T2 归因（复盘插件工具）与 T7 记忆回填（内核 memory 工具）没有
# 本插件 handler 可埋，按用户裁定走 traces.list_by_pipeline **派生**，不埋点。
_TRACE_STEP_TOOLS = {"T2": ("review_improvement",), "T7": ("memory",)}


def _trace_rows_to_steps(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """traces 行 → 派生步骤事件（工具名是真实调用证据；无命中返回空列表）。

    证据面：``patch_data.execution_contexts[].tool_name``（实测形态）与
    ``patch_data.tool_name``（直挂形态）两向兼容；行结构不符就跳过，不臆断。
    """
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        patch = row.get("patch_data")
        names: set[str] = set()
        if isinstance(patch, dict):
            for ctx in patch.get("execution_contexts") or []:
                if isinstance(ctx, dict) and ctx.get("tool_name"):
                    names.add(str(ctx["tool_name"]))
            if patch.get("tool_name"):
                names.add(str(patch["tool_name"]))
        for step in ("T2", "T7"):
            if step in found:
                continue
            for tool in _TRACE_STEP_TOOLS[step]:
                if tool in names:
                    found[step] = {"step": step,
                                   "at": str(row.get("created_at") or ""),
                                   "evidence": f"tool:{tool}"}
                    break
    return [found[k] for k in sorted(found)]


async def _derive_trace_steps(wf_id: str, pipeline_id: str
                              ) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    """按 pipeline 派生 T2/T7 步骤；返回 ``({wf_id: [...]}, 能力是否可用)``。

    best-effort：能力未注入/调用失败一律 ``({}, False)``——投影层据此记缺口，
    不造节点（无运行证据不猜；ADR 2026-09-16 同口径）。
    """
    if not wf_id or not pipeline_id:
        return {}, False
    try:
        handle = plugin.get_capability("service-registry")
        caller = bind_capability_caller(handle, "service-registry")
        res = await caller("traces.list_by_pipeline", {"pipeline_id": pipeline_id})
    except Exception:  # noqa: BLE001 —— 读面降级：能力缺失不阻断供页
        return {}, False
    rows = res.get("data") if isinstance(res, dict) else res
    if not isinstance(rows, list):
        return {}, False
    steps = _trace_rows_to_steps([r for r in rows if isinstance(r, dict)])
    return ({wf_id: steps} if steps else {}), True


def _workflow_projection(ledger: dict[str, Any] | None = None,
                         workflows: list[dict[str, Any]] | None = None,
                         trace_steps: dict[str, list[dict[str, Any]]] | None = None
                         ) -> dict[str, Any]:
    """工作流图投影（骨架 + 实例 + 改进产物）；trace_steps=None 即记 T2/T7 缺口。"""
    return aggregate.workflow_projection(
        ledger if ledger is not None else _load_ledger(),
        workflows if workflows is not None
        else wf_ledger.list_workflows(limit=_PANEL_WORKFLOW_LIMIT),
        defs=wf_ledger.step_catalog(), edges=wf_ledger.flow_edges(),
        trace_steps=trace_steps,
    )


async def _workflow_projection_with_traces() -> dict[str, Any]:
    """供页用：先按 traces 派生 T2/T7 再投影（派生不可用则按缺口出图）。"""
    ledger = _load_ledger()
    workflows = wf_ledger.list_workflows(limit=_PANEL_WORKFLOW_LIMIT)
    focus = ""
    for doc in reversed(workflows):
        if str(doc.get("status") or "running") == "running":
            focus = str(doc.get("wf_id") or "")
            break
    if not focus and workflows:
        focus = str(workflows[-1].get("wf_id") or "")
    pipeline_id = ""
    for doc in workflows:
        if str(doc.get("wf_id") or "") == focus:
            pipeline_id = str(doc.get("pipeline_id") or "")
    trace_steps, trace_ok = await _derive_trace_steps(focus, pipeline_id)
    return _workflow_projection(ledger, workflows,
                                trace_steps if trace_ok else None)


def _panel_html_response(daily: dict[str, Any] | None = None,
                         workflow: dict[str, Any] | None = None
                         ) -> dict[str, Any]:
    """包内面板页 + 账本快照内联 → HttpHandleResponse（text/html，body base64）。

    快照含 ``workflow`` 通道（骨架 defs/edges + 本次回顾实例节点 + 改进产物）；
    供页走 ``_workflow_projection_with_traces``（T2/T7 按 traces 派生），直调缺省
    按无派生投影（缺口如实记）。
    """
    with open(_PANEL_HTML_PATH, encoding="utf-8") as fh:
        html = fh.read()
    if _SNAPSHOT_ANCHOR not in html:
        raise ValueError(f"面板页缺快照注入锚点: {_SNAPSHOT_ANCHOR}")
    snapshot = aggregate.panel_snapshot(_load_ledger(),
                                        library=_load_library_summary(),
                                        daily=daily)
    snapshot["workflow"] = (workflow if workflow is not None
                            else _workflow_projection())
    injected = json.dumps(snapshot, ensure_ascii=False).replace("</", "<\\/")
    return html_text_response(html.replace(_SNAPSHOT_ANCHOR, injected, 1))


async def _load_recent_runs(
        headers: dict[str, str] | None = None) -> list[dict[str, Any]] | None:
    """service-registry.pipeline-runs.list → 真实任务运行行（日常线数据面）。

    行契约（list_pipelines_inner）：run_id/status/started_at/ended_at/
    pipeline_id/thread_id。能力未注入（KeyError）/调用失败/返回形态不符
    一律 None——面板日常线按缺口态渲染，不阻断供页（与
    _load_library_summary 的缺失容错同款）。租户取请求头
    X-AgentOS-Tenant（缺省 default）。
    """
    try:
        handle = plugin.get_capability("service-registry")
        caller = bind_capability_caller(handle, "service-registry")
        tenant = ""
        if headers:
            tenant = str(headers.get("X-AgentOS-Tenant")
                         or headers.get("x-agentos-tenant") or "")
        params: dict[str, Any] = {"limit": 500}
        if tenant:
            params["tenant_id"] = tenant
        res = await caller("pipeline-runs.list", params)
    except Exception:  # noqa: BLE001 — 能力缺失/超时/形态不符统一归缺口
        return None
    rows = res.get("data") if isinstance(res, dict) else res
    if not isinstance(rows, list):
        return None
    return [r for r in rows if isinstance(r, dict)]


def _load_library_summary() -> dict[str, Any] | None:
    """最新轨迹库 → 面板投影；缺失/损坏返回 None（页面按占位渲染，不阻断供页）。"""
    path = _library_path()
    if not path or not os.path.isfile(path):
        return None
    try:
        return screening.library_summary(_read_json(path))
    except (OSError, ValueError):
        return None


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
    """按 path 分发：GET 面板页 HTML（内联数据快照，含日常线真实任务流量
    投影——service-registry 拉取失败按缺口渲染）；未路由 path/method 返回 404 JSON。"""
    if path == _PANEL_ENDPOINT and method == "GET":
        try:
            daily = aggregate.daily_projection(await _load_recent_runs(headers))
            workflow = await _workflow_projection_with_traces()
            return {"success": True,
                    "data": _panel_html_response(daily=daily, workflow=workflow)}
        except (OSError, ValueError) as exc:
            return {
                "success": False,
                "error": f"panel html serve failed: {exc}",
                "data": _json_response({"error": "panel html unavailable"}, 500),
            }
    return {"success": True, "data": _json_response({"error": "not found", "path": path}, 404)}


def _git(args: list[str]) -> tuple[int, str]:
    """git 子进程封装（同步阻塞，timeout=120s）。

    async handler 内必须经 ``asyncio.to_thread`` 调用：直调会同步阻塞合宿
    宿主事件循环，饿死心跳（watchdog 30s 判停滞整组 ``os._exit``）。
    """
    import subprocess
    proc = subprocess.run(["git", *args], cwd=_PROJECT_ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120,
                          check=False)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _git_head() -> str:
    """当前物料基线 git short HEAD（重放键；非 git 环境返回空串不阻断评测）。"""
    code, out = _git(["rev-parse", "--short", "HEAD"])
    return out.strip().splitlines()[-1] if code == 0 and out.strip() else ""


if __name__ == "__main__":
    plugin.run()
