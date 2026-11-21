"""学习曲线报告与离线留一验证入口（P0）。

在线 trial 派发由主 agent 的 task_submit 完成；本脚本只做：
  report   读 campaign.json → Avg/Final/Δ/slope/cost 报告
  offline  读 episodes.jsonl → 邻居回报预测动作好/坏的 AUC

不派发任务、不改题集、不写经验 bank。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import experience_lib as lib  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ROOT = REPO_ROOT / "reports" / "eval" / "experience_ops"


def _resolve_root(raw: str | None) -> Path:
    path = Path(raw) if raw else DEFAULT_ROOT
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _latest_run(root: Path, suite: str, run_id: str | None) -> str:
    if run_id:
        return run_id
    latest = root / "datasets" / suite / "latest"
    if not latest.is_file():
        raise FileNotFoundError(f"缺少 latest 指针: {latest}")
    return latest.read_text(encoding="utf-8").strip()


def _slope(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    n = len(values)
    xs = list(range(1, n + 1))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return 0.0
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values, strict=True))
    return round(numerator / denominator, 4)


def _arm_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_case: dict[str, dict[int, bool]] = {}
    for row in rows:
        case_id = str(row.get("case_id") or "")
        trial = row.get("trial")
        if not case_id or not isinstance(trial, int) or trial < 1:
            continue
        by_case.setdefault(case_id, {})[trial] = bool(row.get("passed"))

    per_case = []
    all_values: list[bool] = []
    final_values: list[bool] = []
    for case_id in sorted(by_case):
        trials = by_case[case_id]
        last = max(trials)
        y = [trials.get(index) for index in range(1, last + 1)]
        numeric = [bool(v) for v in y if v is not None]
        all_values.extend(numeric)
        final_values.append(bool(trials[last]))
        per_case.append({"case_id": case_id, "y": y,
                         "avg": round(sum(numeric) / len(numeric), 4) if numeric else 0.0,
                         "final": 1 if trials[last] else 0})

    by_trial: dict[int, list[bool]] = {}
    for trials in by_case.values():
        for trial, passed in trials.items():
            by_trial.setdefault(trial, []).append(passed)
    curve = [round(sum(by_trial[t]) / len(by_trial[t]), 4) for t in sorted(by_trial)]

    total = len(rows)
    successes = sum(1 for row in rows if row.get("passed"))
    cost = sum(float(row.get("cost_usd") or 0) for row in rows)
    tokens = sum(int(row.get("tokens") or 0) for row in rows)
    avg = round(sum(all_values) / len(all_values), 4) if all_values else 0.0
    final = round(sum(final_values) / len(final_values), 4) if final_values else 0.0
    return {
        "per_case": per_case,
        "aggregate": {
            "avg": avg,
            "final": final,
            "delta": round(final - avg, 4),
            "slope": _slope(curve),
            "curve": curve,
            "runs": total,
            "successes": successes,
            "cost_usd": round(cost, 6),
            "tokens": tokens,
            "cost_per_success": round(cost / successes, 6) if successes else None,
        },
        "retrieval": {},
    }


def cmd_report(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    campaign_dir = root / "campaigns" / args.run
    source = campaign_dir / "campaign.json"
    if not source.is_file():
        raise FileNotFoundError(f"缺少 campaign.json: {source}")
    payload = _read_json(source)
    results = payload.get("results") or []
    arms = payload.get("protocol", {}).get("arms") or sorted(
        {str(row.get("arm") or "default") for row in results})
    report = {
        "campaign_id": payload.get("campaign_id") or args.run,
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "suite": payload.get("suite") or {},
        "protocol": payload.get("protocol") or {},
        "arms": [],
    }
    for arm in arms:
        rows = [row for row in results if str(row.get("arm") or "default") == arm]
        report["arms"].append({"name": arm, **_arm_summary(rows)})

    output = campaign_dir / "report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8", newline="\n")
    _write_markdown(campaign_dir / "report.md", report)
    print(json.dumps({"ok": True, "report": str(output),
                      "arms": [a["name"] for a in report["arms"]]}, ensure_ascii=False))
    if args.dry_run:
        print("(dry-run：未派发任何任务；本命令本就只读)")
    return 0


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [f"# 学习曲线报告 {report['campaign_id']}", ""]
    suite = report.get("suite") or {}
    lines += [f"- suite: {suite.get('id', '')} v{suite.get('version', '')}",
              f"- protocol: {report.get('protocol', {}).get('type', '')} "
              f"L={report.get('protocol', {}).get('L', '')}", ""]
    lines += ["| arm | Avg | Final | Δ | slope | cost/success |",
              "|---|---|---|---|---|---|"]
    for arm in report["arms"]:
        agg = arm["aggregate"]
        lines.append(f"| {arm['name']} | {agg['avg']:.3f} | {agg['final']:.3f} | "
                     f"{agg['delta']:.3f} | {agg['slope']:.3f} | "
                     f"{agg['cost_per_success'] if agg['cost_per_success'] is not None else '-'} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def cmd_offline(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    run_id = _latest_run(root, args.suite, args.run_id)
    episodes_path = root / "datasets" / args.suite / run_id / "episodes.jsonl"
    episodes = _read_jsonl(episodes_path)
    result = lib.leave_one_out_auc(episodes, k=args.k)
    output = root / "datasets" / args.suite / run_id / "offline_auc.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                      encoding="utf-8", newline="\n")
    print(json.dumps({"ok": True, "run_id": run_id, "episodes": len(episodes),
                      "auc": result}, ensure_ascii=False))
    if args.min_auc is not None and (result.get("auc") is None
                                     or result["auc"] < args.min_auc):
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="学习曲线报告 / 离线留一验证")
    sub = parser.add_subparsers(dest="command", required=True)

    report = sub.add_parser("report", help="campaign.json → report.json/md")
    report.add_argument("--root", default=None)
    report.add_argument("--run", required=True, help="campaign 运行 ID")
    report.add_argument("--dry-run", action="store_true", help="只读复算（本命令恒只读）")
    report.set_defaults(func=cmd_report)

    offline = sub.add_parser("offline", help="episodes.jsonl → 留一 AUC")
    offline.add_argument("--root", default=None)
    offline.add_argument("--suite", required=True)
    offline.add_argument("--run-id", default=None)
    offline.add_argument("--k", type=int, default=8)
    offline.add_argument("--min-auc", type=float, default=None)
    offline.set_defaults(func=cmd_offline)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:500]}, ensure_ascii=False))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
