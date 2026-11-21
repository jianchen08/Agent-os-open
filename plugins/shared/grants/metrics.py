"""grants 运营指标聚合（P3）——只读聚合，不加写面。

设计真值：执行方案 §9（指标口径）+ 任务书（数据源 = grants/grant_events 两表 +
security_audit.jsonl 若可达；取不到的数据源如实降级 null + 说明，禁造数）。

四指标口径与数据现实（本模块 docstring 即契约，测试锁定）：

- **审批卡弹出率**（弹卡次数 / 分级调用总数）：弹卡事件在既有数据面**无记录**
  ——审批卡弹出不经 grants 账本（账本只记授予结果 issue）也不经
  security_audit.jsonl（只记 auto 放行 / 白名单增删 / 管道档授予）。故本率恒
  降级 ``value=None`` + 降级说明；可观测的弹卡侧计数（approval_card issue、
  audit jsonl 三类授予面事件）落 ``components``，执法面未来补弹卡审计事件后
  本端点口径自动闭合。
- **grant 命中率（省弹次数）**：authorize 命中数 /（命中数 + 弹卡数）——分母
  弹卡数同上不可得，率恒降级；**省弹次数 = consume 事件数**完全可得，落
  ``components.saved_popups``。
- **撤销→生效耗时**（revoke 返回 → 下一次同类调用被拒墙钟差）：被拒事件
  （authorize miss → 弹卡）不经两源，无法配对计算，恒降级；账本面生效证明 =
  撤销后同 grant 零再消费（``components.post_revoke_consumes`` 恒 0，>0 即
  生效异常，由本计数器实证）。端到端墙钟差由 P3 验收实测承载（执行方案 §9）。
- **未分类拦截数**：classify 未分类 deny 事件数（ADR D2 fail-closed 计数），
  完全可得；坏条目（invalid_entry）deny 分立计数不混入。

审计文件落点解析与 ``ledger.resolve_db_path`` 同构：settings 覆盖
（``security_audit_file``，测试/dev 注入口）> 用户数据根（security_check
``security_audit.jsonl`` 同目录口径）> 共享根 ``data/`` dev 姿态回落。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ledger import _parse_ts

#: 指标键闭集（端点响应 metrics 对象的键集，测试锁定）。
METRIC_KEYS = ("approval_card_popup_rate", "grant_hit_rate", "revoke_to_effect", "uncategorized_blocked")

#: security_check 审计事件 → 弹卡侧计数键（mode_switch 只数授予方向 action=grant）。
_AUDIT_EVENT_KEYS = ("auto_allow_events", "pipeline_mode_grants", "whitelist_adds")


def resolve_audit_path(settings: dict[str, Any] | None = None) -> str:
    """security_audit.jsonl 落点解析（与 ledger.resolve_db_path 回落链同构）。"""
    try:
        from _root_api import user_data_dir  # noqa: PLC0415

        data_dir = user_data_dir()
        if data_dir:
            return str(Path(data_dir) / "security_audit.jsonl")
    except ImportError:
        pass
    configured = str((settings or {}).get("security_audit_file") or "").strip()
    if configured:
        return configured
    # 共享根回落（dev 姿态）：security_check 的 dev 缺省落点同目录
    shared_root = Path(__file__).resolve().parents[1]
    return str(shared_root / "data" / "security_audit.jsonl")


def read_security_audit(path: str | None) -> dict[str, Any]:
    """读取审计文件（只读）：缺失/不可读 → ``reachable=False`` + 原因（如实降级）。

    JSONL 逐行解析，坏行跳过（security_check ``_query_audit`` 同口径）。
    """
    if not path:
        return {"reachable": False, "reason": "audit path unavailable", "events": []}
    events: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    events.append(record)
    except OSError as exc:
        return {"reachable": False, "reason": f"audit file unreadable: {exc}", "events": []}
    return {"reachable": True, "reason": None, "events": events}


def _count_issue_sources(events: list[dict[str, Any]]) -> dict[str, int]:
    """issue 事件按授予来源计数（context.source；缺 context 归 other）。"""
    counts = {"approval_card_issues": 0, "user_rule_issues": 0, "oauth_broker_issues": 0, "other_issues": 0}
    for event in events:
        if event.get("event_type") != "issue":
            continue
        context = event.get("context")
        source = context.get("source") if isinstance(context, dict) else None
        key = f"{source}_issues" if f"{source}_issues" in counts else "other_issues"
        counts[key] += 1
    return counts


def _audit_component_counts(audit: dict[str, Any]) -> dict[str, int | None]:
    """审计文件弹卡侧计数；不可达 → 三键 None（如实降级，禁造数）。"""
    components: dict[str, int | None] = dict.fromkeys(_AUDIT_EVENT_KEYS)
    if not audit.get("reachable"):
        return components
    auto_allows = 0
    pipeline_grants = 0
    whitelist_adds = 0
    for event in audit.get("events") or []:
        name = event.get("event")
        if name == "grants_auto_allow":
            auto_allows += 1
        elif name == "mode_switch" and event.get("action") == "grant":
            pipeline_grants += 1
        elif name == "whitelist_add":
            whitelist_adds += 1
    components.update(
        {
            "auto_allow_events": auto_allows,
            "pipeline_mode_grants": pipeline_grants,
            "whitelist_adds": whitelist_adds,
        }
    )
    return components


def _post_revoke_consumes(events: list[dict[str, Any]]) -> int:
    """撤销后同 grant 再消费计数（账本面生效异常探测器，恒 0 为健康态）。"""
    revoke_ts: dict[str, str] = {}
    for event in events:
        if event.get("event_type") == "revoke" and event.get("grant_id"):
            grant_id = str(event["grant_id"])
            if grant_id not in revoke_ts:
                revoke_ts[grant_id] = str(event.get("ts") or "")
    stale = 0
    for event in events:
        if event.get("event_type") != "consume":
            continue
        raw_grant_id = event.get("grant_id")
        baseline = revoke_ts.get(str(raw_grant_id)) if raw_grant_id else None
        if baseline is None:
            continue
        revoked_at = _parse_ts(baseline)
        consumed_at = _parse_ts(str(event.get("ts") or ""))
        if revoked_at is not None and consumed_at is not None and consumed_at > revoked_at:
            stale += 1
    return stale


def _metric(
    *,
    label: str,
    caliber: str,
    unit: str,
    value: int | float | None,
    degraded: bool,
    reason: str | None,
    components: dict[str, Any],
) -> dict[str, Any]:
    return {
        "label": label,
        "caliber": caliber,
        "unit": unit,
        "value": value,
        "degraded": degraded,
        "reason": reason,
        "components": components,
    }


def compute_metrics(events: list[dict[str, Any]], audit: dict[str, Any]) -> dict[str, Any]:
    """四指标只读聚合（纯函数：events = ``ledger.events()`` 形态，audit =
    :func:`read_security_audit` 形态）。"""
    issue_counts = _count_issue_sources(events)
    audit_components = _audit_component_counts(audit)
    deny_reasons = {"unclassified": 0, "invalid_entry": 0, "other": 0}
    consumes = 0
    revokes = 0
    for event in events:
        event_type = event.get("event_type")
        if event_type == "consume":
            consumes += 1
        elif event_type == "revoke":
            revokes += 1
        elif event_type == "deny":
            context = event.get("context")
            reason = context.get("reason") if isinstance(context, dict) else None
            key = reason if isinstance(reason, str) and reason in deny_reasons else "other"
            deny_reasons[key] += 1

    popup_reason = (
        "弹卡事件在既有数据面无记录：审批卡弹出不经 grants 账本（账本只记授予结果 issue）"
        "也不经 security_audit.jsonl（只记 auto 放行/白名单/管道档授予）——"
        "可观测弹卡侧计数见 components；执法面补弹卡审计事件后本口径自动闭合"
    )
    revoke_reason = (
        "被拒事件（authorize miss → 弹卡）不经 grants 账本与 security_audit.jsonl，"
        "无法从两源配对计算墙钟差；账本内撤销与状态翻转同事务（撤销后下一次 authorize 即 miss），"
        "账本面生效证明见 components.post_revoke_consumes（恒 0，>0 即生效异常）；"
        "端到端墙钟差由 P3 验收实测承载（执行方案 §9）"
    )

    metrics = {
        "approval_card_popup_rate": _metric(
            label="审批卡弹出率",
            caliber="弹卡次数 / 分级调用总数（§9 审批疲劳族）",
            unit="ratio",
            value=None,
            degraded=True,
            reason=popup_reason,
            components={
                "approval_card_grant_issues": issue_counts["approval_card_issues"],
                "classify_denies": deny_reasons["unclassified"] + deny_reasons["invalid_entry"] + deny_reasons["other"],
                **audit_components,
            },
        ),
        "grant_hit_rate": _metric(
            label="grant 命中率（省弹次数）",
            caliber="authorize 命中数 /（命中数 + 弹卡数）（§9）；省弹次数 = consume 事件数",
            unit="ratio",
            value=None,
            degraded=True,
            reason=popup_reason,
            components={
                "saved_popups": consumes,
                "approval_card_issues": issue_counts["approval_card_issues"],
            },
        ),
        "revoke_to_effect": _metric(
            label="撤销→生效耗时",
            caliber="revoke 返回 → 下一次同类调用被拒的墙钟差（§9；P3 实测 <5s）",
            unit="ms",
            value=None,
            degraded=True,
            reason=revoke_reason,
            components={"revokes": revokes, "post_revoke_consumes": _post_revoke_consumes(events)},
        ),
        "uncategorized_blocked": _metric(
            label="未分类拦截数",
            caliber="classify 未分类 deny 事件数（ADR D2 fail-closed；invalid_entry 分立计数）",
            unit="count",
            value=deny_reasons["unclassified"],
            degraded=False,
            reason=None,
            components=dict(deny_reasons),
        ),
    }
    return {
        "metrics": metrics,
        "sources": {
            "security_audit_jsonl": {
                "reachable": bool(audit.get("reachable")),
                "reason": audit.get("reason"),
            }
        },
    }
