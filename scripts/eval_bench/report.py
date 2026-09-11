"""评测报告渲染与落盘（JSON + Markdown）。

输出目录结构：{out_dir}/{run_id}/report.json + report.md + cases/{case_id}.json
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _pct(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator * 100, 1) if denominator else None


def summarize(suite_name: str, case_results: list[dict[str, Any]]) -> dict[str, Any]:
    """case 结果列表 → 汇总指标（成功率/平均轮次/重试/审批/token）。"""
    cases = len(case_results)
    passed = sum(1 for c in case_results if c["passed"])
    approval_ledgers = [c["approval"] for c in case_results if c.get("approval")]
    requests_seen = sum(l.get("requests_seen", 0) for l in approval_ledgers)
    approved = sum(l.get("approved", 0) for l in approval_ledgers)
    denied = sum(l.get("denied", 0) for l in approval_ledgers)

    by_model: dict[str, dict[str, int]] = {}
    for c in case_results:
        for model, bucket in (c.get("metrics", {}).get("by_model") or {}).items():
            target = by_model.setdefault(model, {"input": 0, "output": 0, "cached": 0, "total": 0})
            for key in target:
                target[key] += bucket.get(key, 0)

    def _avg(key: str) -> float | None:
        values = [c["metrics"][key] for c in case_results
                  if isinstance(c.get("metrics"), dict) and key in c["metrics"]]
        return round(sum(values) / len(values), 1) if values else None

    return {
        "suite": suite_name,
        "cases": cases,
        "passed": passed,
        "pass_rate": _pct(passed, cases),
        "avg_iterations": _avg("iterations_total"),
        "avg_tool_retries": _avg("tool_retries"),
        "avg_error_steps": _avg("error_steps"),
        "approval": {
            "requests_seen": requests_seen,
            "approved": approved,
            "denied": denied,
            "intercept_rate": _pct(denied, requests_seen),
        },
        "tokens": {
            "input": sum(b["input"] for b in by_model.values()),
            "output": sum(b["output"] for b in by_model.values()),
            "cached": sum(b["cached"] for b in by_model.values()),
            "total": sum(b["total"] for b in by_model.values()),
            "by_model": by_model,
        },
    }


def render_markdown(summary: dict[str, Any], case_results: list[dict[str, Any]]) -> str:
    """汇总 + 逐 case 表格的 Markdown 报告。"""
    approval = summary["approval"]
    tokens = summary["tokens"]
    lines = [
        f"# 评测报告：{summary['suite']}",
        "",
        f"- case 数：{summary['cases']}　通过：{summary['passed']}　"
        f"成功率：{summary['pass_rate']}%",
        f"- 平均轮次：{summary['avg_iterations']}　平均工具重试：{summary['avg_tool_retries']}"
        f"　平均失败步：{summary['avg_error_steps']}",
        f"- 审批：请求 {approval['requests_seen']}（批准 {approval['approved']} / "
        f"拒绝 {approval['denied']}），拦截率 {approval['intercept_rate']}%",
        f"- Token：输入 {tokens['input']:,} / 输出 {tokens['output']:,} / "
        f"缓存 {tokens['cached']:,} / 合计 {tokens['total']:,}",
        "",
        "## 逐 case 结果",
        "",
        "| case | 类别 | 结果 | 管道 | 轮次 | 重试 | 失败步 | 超时步 | Token | 审批 | 失败断言 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in case_results:
        m = c.get("metrics", {})
        appr = c.get("approval") or {}
        approval_text = (
            f"{appr.get('denied', 0)}拒/{appr.get('approved', 0)}准"
            if appr.get("requests_seen")
            else "-"
        )
        lines.append(
            f"| {c['case_id']} | {c.get('category', '')} | "
            f"{'✅' if c['passed'] else '❌'} | {m.get('pipelines', '-')} | "
            f"{m.get('iterations_total', '-')} | {m.get('tool_retries', '-')} | "
            f"{m.get('error_steps', '-')} | {m.get('timeout_steps', '-')} | "
            f"{m.get('tokens_total', 0):,} | {approval_text} | "
            f"{'; '.join(c.get('failed_assertions', [])) or '-'} |"
        )
    lines += ["", "## 逐 case 明细", ""]
    for c in case_results:
        lines.append(f"### {c['case_id']}（{'通过' if c['passed'] else '未通过'}）")
        lines.append("")
        lines.append(f"- 管道：{c.get('pipeline_ids') or '未观察到新管道'}")
        if c.get("notes"):
            lines.append(f"- 备注：{c['notes']}")
        for a in c.get("assertions", []):
            mark = "✅" if a["passed"] else "❌"
            lines.append(f"- {mark} {a['type']}: {a['detail']}")
        lines.append("")
    return "\n".join(lines)


def write_outputs(out_root: Path, suite_name: str,
                  summary: dict[str, Any], case_results: list[dict[str, Any]]) -> Path:
    """写 report.json / report.md / cases/*.json，返回运行目录。"""
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_dir = out_root / run_id
    (run_dir / "cases").mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "cases": case_results,
    }
    (run_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "report.md").write_text(
        render_markdown(summary, case_results), encoding="utf-8"
    )
    for c in case_results:
        (run_dir / "cases" / f"{c['case_id']}.json").write_text(
            json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return run_dir
