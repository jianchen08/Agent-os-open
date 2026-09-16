# -*- coding: utf-8 -*-
"""题集读取与批次展开（纯函数）。

题集格式（v2，对齐系统原生机制）：
- 断言 = task_submit 的 acceptance_criteria（内置指标 file_check/bash_check/
  semantic_check/human_review），评估闸门在任务收尾自动判定——本模块只做
  透传，不自建断言器。
- case.messages 拼接为 goal_description（{workspace} 占位符不再需要：任务
  默认 isolated 自动获得工作空间）。
- case.budget（可选，ADR 2026-09-16 成功谓词四元组）：{max_rounds,
  max_tokens, max_seconds}——聚合时配 results.metrics 做预算判定；
- case.difficulty / case.ood_axes（可选标注）：难度带与 OOD 轴（任务图/
  环境工具/语义/过程），供难度等值化与分层采样（二期消费）。
"""
from __future__ import annotations

import os
from typing import Any

import yaml

MAX_GOAL_LEN = 2000  # task_submit goal_description 上限


def load_suite(path: str) -> dict[str, Any]:
    data = yaml.safe_load(open(path, encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("cases"):
        raise ValueError(f"题集为空或格式错误: {path}")
    return data


def resolve_suite_from_profile(profile: dict[str, Any], tier: str = "smoke") -> str:
    """从模式 profile 解析题集路径（tier 档位；预留档位 null 视为缺席）。

    profile 经内核服务调用（mode.get_profile，server 层获取）透传进来——
    模式 profile 已内打包进模式插件目录，本模块不直读模式配置文件。
    """
    suite = profile.get("suite")
    if not isinstance(suite, dict) or not suite:
        raise ValueError(f"profile.suite 缺失或格式错误: {suite!r}")
    rel = suite.get(tier)
    if not isinstance(rel, str) or not rel.strip():
        raise ValueError(f"profile.suite.{tier} 未登记（档位缺席或 null）: {rel!r}")
    return rel


def case_to_task_args(case: dict[str, Any], run_tag: str,
                      project_root: str = "") -> dict[str, Any]:
    """把一个 case 展开为 task_submit 参数（按系统工具 schema）。

    workspace：每 case 独立评测工作空间（显式指定——产物落点确定，评估执行器
    自动注入任务 workspace，file_check 相对 path 精确命中）。
    target：缺省 main（agentos）——评测单元是从 main 入口进的任务链；
    case.target 可覆盖（环节对照试跑用）。
    """
    messages = [str(m) for m in (case.get("messages") or [])]
    goal = "\n".join(messages).strip()
    if len(goal) > MAX_GOAL_LEN:
        goal = goal[: MAX_GOAL_LEN - 20] + "…（截断）"
    criteria = {}
    for name, cfg in (case.get("acceptance_criteria") or {}).items():
        criteria[name] = cfg if isinstance(cfg, dict) else {"input_params": cfg}
    case_id = str(case["id"])
    args = {
        "target_type": "agent",
        "target_id": str(case.get("target") or "general_agent"),  # L2/L3（系统硬规则：任务不可提交 L1）
        "goal_title": f"eval-{case_id}-{run_tag}",
        "goal_description": goal,
        "acceptance_criteria": criteria,
    }
    # workspace 锚定（2026-09-14 用户裁定：按修改/执行目标分流，不混）：
    # - anchor=materials（缺省，评测执行阶段）：workspace = 评测物料区
    #   {SELF_EVOLVE_ROOT}/workspaces/{run}/{case_id}，plain 模式——该题物料与
    #   产物都落这里，不分化 worktree（评测执行不改主仓库）。
    # - anchor=repo（被测物是主仓库文件的题，如读 config/统计仓库）：workspace =
    #   主仓库根 + worktree 模式——分化主仓库分支副本，读得到全部仓库物料，
    #   产物经合并门控回流，不污染工作树。
    # - 显式 case.workspace 优先于 anchor。
    case_id_safe = case_id.replace("/", "_")
    if case.get("workspace"):
        args["workspace"] = str(case["workspace"])
        args["workspace_mode"] = str(case.get("workspace_mode") or "plain")
        return args
    anchor = str(case.get("anchor") or "materials")
    if anchor == "repo" and project_root:
        args["workspace"] = project_root
        args["workspace_mode"] = "worktree"
    else:
        root = os.environ.get("AGENTOS_EVAL_MATERIALS_DIR", "")
        if not root:
            prop = os.environ.get("AGENTOS_PROPOSALS_DIR", "")
            root = (os.path.dirname(prop) if prop
                    else os.path.join(os.path.dirname(project_root.rstrip("/\\")),
                                      "agentos_selfevolve"))
        args["workspace"] = os.path.join(root, "workspaces", run_tag, case_id_safe)
        args["workspace_mode"] = "plain"
    return args


def expand_batches(suite: dict[str, Any], cases: str | None = None,
                   run_tag: str | None = None,
                   project_root: str = "") -> list[dict[str, Any]]:
    """展开为批次清单；cases 为逗号分隔 id 过滤。"""
    tag = run_tag or time_tag()
    only = {c.strip() for c in cases.split(",")} if cases else None
    batches = []
    for case in suite.get("cases") or []:
        if only and case["id"] not in only:
            continue
        batches.append({
            "case_id": case["id"],
            "task_submit_args": case_to_task_args(case, tag, project_root),
            "budget": case.get("budget"),
            "difficulty": case.get("difficulty"),
        })
    return batches


def time_tag() -> str:
    import time
    return time.strftime("%Y%m%d_%H%M%S")
