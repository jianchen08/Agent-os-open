"""经验轨迹数据集构建入口（P0）。

子命令：
  harvest   读内核 traces + 复盘索引 → raw bundles（JSONL）
  normalize raw → `agentos.experience.v1` episodes（credit 暂空）
  credit    复盘 step_credits 对齐 → episodes（含 G_t）
  validate  质量闸（schema/覆盖/去重/泄漏/校准）
  build     harvest + normalize + credit + validate
  ingest    P1 预留（当前显式失败，不伪造成功）

只读库：经 scripts/kernel_db_query.py 临时快照；不直连活库。
所有产物落 --root（默认 reports/eval/experience_ops）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import experience_lib as lib  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
DB_QUERY = REPO_ROOT / "scripts" / "kernel_db_query.py"
DEFAULT_ROOT = REPO_ROOT / "reports" / "eval" / "experience_ops"
SUCCESS_STATUSES = lib.SUCCESS_STATUSES


def _now_tag() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _resolve_root(raw: str | None) -> Path:
    path = Path(raw) if raw else DEFAULT_ROOT
    if not path.is_absolute():
        path = REPO_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not path.exists():
        return items
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line:
            items.append(json.loads(line))
    return items


def _write_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8", newline="\n")


def _db_query(sql: str, db: str | None, limit: int) -> list[list[Any]]:
    command = [sys.executable, str(DB_QUERY), sql, "--json", "--limit", str(limit)]
    if db:
        command.extend(["--db", db])
    result = subprocess.run(command, capture_output=True, text=True,
                            encoding="utf-8", cwd=str(REPO_ROOT), check=False)
    if result.returncode != 0:
        raise RuntimeError(f"kernel_db_query 失败: {result.stderr.strip()[:500]}")
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError(f"kernel_db_query 输出非 JSON: {result.stdout[:300]}") from exc
    return payload.get("rows") or []


def _chunks(items: list[str], size: int = 200):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _quote_ids(ids: list[str]) -> str:
    safe = [item for item in ids if item and all(ch.isalnum() or ch in "_-" for ch in item)]
    if len(safe) != len(ids):
        raise ValueError("pipeline_id 含非法字符，拒绝拼 SQL")
    return ",".join(f"'{item}'" for item in safe)


def _decode(value: Any) -> Any:
    return lib.decode_state_value(value)


def _load_tasks(db: str | None, since: str | None) -> list[dict[str, Any]]:
    sql = (
        "SELECT s.pipeline_id,"
        " MAX(CASE WHEN s.field_key='task.goal' THEN s.value END),"
        " MAX(CASE WHEN s.field_key='task.description' THEN s.value END),"
        " MAX(CASE WHEN s.field_key='task.status' THEN s.value END),"
        " MAX(CASE WHEN s.field_key='task.acceptance_criteria' THEN s.value END),"
        " MAX(CASE WHEN s.field_key='task.eval_summary' THEN s.value END),"
        " MAX(CASE WHEN s.field_key='llm_model' THEN s.value END),"
        " MAX(CASE WHEN s.field_key='agent.id' THEN s.value END),"
        " MAX(CASE WHEN s.field_key='task.ended_at' THEN s.value END),"
        " MAX(CASE WHEN s.field_key='task.ws_meta.path' THEN s.value END)"
        " FROM pipeline_state s"
        " WHERE s.pipeline_id IN"
        "  (SELECT pipeline_id FROM pipeline_state WHERE field_key='task.goal')"
        " GROUP BY s.pipeline_id"
    )
    tasks = []
    for row in _db_query(sql, db, 50000):
        ended_at = _decode(row[8])
        if since and isinstance(ended_at, str) and ended_at[:10] < since:
            continue
        tasks.append({
            "pipeline_id": row[0],
            "goal": _decode(row[1]) or "",
            "description": _decode(row[2]) or "",
            "status": _decode(row[3]) or "",
            "acceptance_criteria": _decode(row[4]) or {},
            "score": _extract_score(_decode(row[5])),
            "model": _decode(row[6]) or "",
            "agent_id": _decode(row[7]) or "",
            "ended_at": ended_at or "",
            "ws_path": _decode(row[9]) or "",
        })
    return tasks


def _extract_score(eval_summary: Any) -> float | None:
    if isinstance(eval_summary, dict):
        for key in ("score", "overall_score", "pass_rate"):
            value = eval_summary.get(key)
            if isinstance(value, (int, float)):
                return float(value)
    return None


def _load_traces(db: str | None, pipeline_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {pid: [] for pid in pipeline_ids}
    for chunk in _chunks(pipeline_ids):
        ids = _quote_ids(chunk)
        sql = (
            "SELECT pipeline_id, seq, plugin_id, patch_data FROM traces"
            f" WHERE pipeline_id IN ({ids}) ORDER BY pipeline_id, seq"
        )
        for row in _db_query(sql, db, 200000):
            grouped.setdefault(row[0], []).append({
                "seq": row[1], "plugin_id": row[2], "patch_data": row[3],
            })
    return grouped


def _load_review_index(db: str | None,
                       pipeline_ids: list[str]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, dict[str, Any]]] = {pid: {} for pid in pipeline_ids}
    for chunk in _chunks(pipeline_ids):
        ids = _quote_ids(chunk)
        sql = (
            "SELECT pipeline_id, field_key, value FROM pipeline_state"
            f" WHERE pipeline_id IN ({ids}) AND field_key LIKE 'task.review.%'"
        )
        for row in _db_query(sql, db, 200000):
            parts = str(row[1]).split(".")
            if len(parts) < 4 or parts[0] != "task" or parts[1] != "review":
                continue
            review_id, field = parts[2], ".".join(parts[3:])
            entry = grouped.setdefault(row[0], {}).setdefault(review_id, {"review_id": review_id})
            entry[field] = _decode(row[2])
    return grouped


def _select_review(reviews: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if not reviews:
        return None
    completed = [item for item in reviews.values() if item.get("status") == "completed"]
    pool = completed or list(reviews.values())
    return sorted(pool, key=lambda item: str(item.get("created_at") or ""), reverse=True)[0]


def _read_report(task: dict[str, Any], review: dict[str, Any] | None) -> dict[str, Any] | None:
    if not review:
        return None
    ws_path = str(task.get("ws_path") or "")
    raw_path = str(review.get("path") or "")
    candidates: list[Path] = []
    if raw_path:
        if raw_path.startswith("ws:"):
            raw_path = raw_path[3:]
        candidate = Path(raw_path)
        if candidate.is_absolute():
            candidates.append(candidate)
        elif ws_path:
            candidates.append(Path(ws_path) / candidate)
    if ws_path:
        candidates.append(Path(ws_path) / "docs" / "working" / "reviews"
                          / f"{review.get('review_id')}.json")
    for candidate in candidates:
        if candidate.is_file():
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict):
                payload.setdefault("review_id", review.get("review_id"))
                payload.setdefault("status", review.get("status"))
                payload["report_ref"] = f"ws:{candidate.relative_to(ws_path).as_posix()}" \
                    if ws_path and candidate.is_relative_to(Path(ws_path)) else str(candidate)
                return payload
    return {"review_id": review.get("review_id"), "status": review.get("status"),
            "overall_status": None, "lessons": [], "step_credits": [],
            "report_ref": None}


def harvest_from_db(db: str | None, since: str | None) -> list[dict[str, Any]]:
    tasks = _load_tasks(db, since)
    if not tasks:
        return []
    ids = [task["pipeline_id"] for task in tasks]
    traces = _load_traces(db, ids)
    reviews = _load_review_index(db, ids)
    bundles = []
    for task in tasks:
        selected = _select_review(reviews.get(task["pipeline_id"], {}))
        report = _read_report(task, selected)
        bundles.append({
            "task": task,
            "traces": traces.get(task["pipeline_id"], []),
            "review": report,
        })
    return bundles


def harvest_from_json(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".jsonl":
        return _read_jsonl(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "bundles" in payload:
        payload = payload["bundles"]
    if not isinstance(payload, list):
        raise ValueError("--from-json 需要 bundle 数组或 {bundles: [...]}")
    return payload


def normalize_bundles(bundles: list[dict[str, Any]], suite: str,
                      split: str, gamma: float) -> list[dict[str, Any]]:
    episodes = []
    for bundle in bundles:
        episode = lib.build_episode(
            bundle.get("task") or {},
            bundle.get("traces") or [],
            None,
            suite=suite,
            split=bundle.get("task", {}).get("split") or split,
            gamma=gamma,
        )
        if episode:
            episodes.append(episode)
    return episodes


def apply_reviews(episodes: list[dict[str, Any]], bundles: list[dict[str, Any]],
                  gamma: float) -> list[dict[str, Any]]:
    reviews = {}
    for bundle in bundles:
        task_id = (bundle.get("task") or {}).get("pipeline_id")
        if task_id:
            reviews[str(task_id)] = bundle.get("review")
    for episode in episodes:
        task_id = str((episode.get("source") or {}).get("pipeline_id") or "")
        review = reviews.get(task_id) or {}
        lib.apply_credits(episode.get("steps") or [],
                          review.get("step_credits") or [],
                          review.get("review_id"), gamma=gamma)
        episode.setdefault("review", {})
        episode["review"].update({
            "review_id": review.get("review_id"),
            "report_ref": review.get("report_ref"),
            "status": review.get("status"),
            "overall_status": review.get("overall_status"),
            "credit_policy": review.get("credit_policy") or "review_credit_v1",
            "lessons_count": len(review.get("lessons") or []),
        })
        episode.setdefault("returns", {})["G"] = [
            step.get("credit", {}).get("discounted_return") for step in episode.get("steps") or []
        ]
    return episodes


def _run_dir(root: Path, suite: str, run_id: str) -> Path:
    return root / "datasets" / suite / run_id


def cmd_harvest(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    run_id = args.run_id or _now_tag()
    if args.from_json:
        bundles = harvest_from_json(Path(args.from_json))
    else:
        bundles = harvest_from_db(args.db, args.since)
    out = _run_dir(root, args.suite, run_id) / "raw.jsonl"
    _write_jsonl(out, bundles)
    print(json.dumps({"ok": True, "raw": str(out), "bundles": len(bundles)},
                     ensure_ascii=False))
    return 0


def cmd_normalize(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    run_dir = _run_dir(root, args.suite, args.run_id)
    bundles = _read_jsonl(run_dir / "raw.jsonl")
    episodes = normalize_bundles(bundles, args.suite, args.split, args.gamma)
    out = run_dir / "normalized.jsonl"
    _write_jsonl(out, episodes)
    print(json.dumps({"ok": True, "normalized": str(out), "episodes": len(episodes)},
                     ensure_ascii=False))
    return 0


def cmd_credit(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    run_dir = _run_dir(root, args.suite, args.run_id)
    bundles = _read_jsonl(run_dir / "raw.jsonl")
    episodes = _read_jsonl(run_dir / "normalized.jsonl")
    episodes = apply_reviews(episodes, bundles, args.gamma)
    out = run_dir / "episodes.jsonl"
    _write_jsonl(out, episodes)
    reviewed = sum(1 for ep in episodes
                   if (ep.get("review") or {}).get("review_id"))
    print(json.dumps({"ok": True, "episodes": str(out), "count": len(episodes),
                      "with_review": reviewed}, ensure_ascii=False))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    run_dir = _run_dir(root, args.suite, args.run_id)
    episodes = _read_jsonl(run_dir / "episodes.jsonl")
    heldout = None
    if args.heldout:
        heldout = {line.strip() for line in Path(args.heldout).read_text(
            encoding="utf-8").splitlines() if line.strip()}
    gates = lib.quality_gates(episodes, heldout_case_ids=heldout)
    manifest = {
        "suite": args.suite,
        "run_id": args.run_id,
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "counts": {"episodes": len(episodes),
                   "steps": gates.get("steps", 0)},
        "quality_gates": gates,
        "code_version": _code_version(),
    }
    _write_json(run_dir / "manifest.json", manifest)
    print(json.dumps(gates, ensure_ascii=False, indent=2))
    return 0 if gates.get("all_passed") else 2


def cmd_build(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    run_id = args.run_id or _now_tag()
    args.run_id = run_id
    if args.from_json:
        bundles = harvest_from_json(Path(args.from_json))
    else:
        bundles = harvest_from_db(args.db, args.since)
    run_dir = _run_dir(root, args.suite, run_id)
    _write_jsonl(run_dir / "raw.jsonl", bundles)
    episodes = normalize_bundles(bundles, args.suite, args.split, args.gamma)
    _write_jsonl(run_dir / "normalized.jsonl", episodes)
    episodes = apply_reviews(episodes, bundles, args.gamma)
    _write_jsonl(run_dir / "episodes.jsonl", episodes)
    heldout = None
    if args.heldout:
        heldout = {line.strip() for line in Path(args.heldout).read_text(
            encoding="utf-8").splitlines() if line.strip()}
    gates = lib.quality_gates(episodes, heldout_case_ids=heldout)
    manifest = {
        "suite": args.suite,
        "run_id": run_id,
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "counts": {"bundles": len(bundles), "episodes": len(episodes),
                   "steps": gates.get("steps", 0)},
        "quality_gates": gates,
        "code_version": _code_version(),
    }
    _write_json(run_dir / "manifest.json", manifest)
    (root / "datasets" / args.suite / "latest").write_text(run_id + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "run_dir": str(run_dir), "gates": gates},
                     ensure_ascii=False, indent=2))
    return 0 if gates.get("all_passed") else 2


def cmd_ingest(args: argparse.Namespace) -> int:  # noqa: ARG001
    print(json.dumps({"ok": False,
                      "error": "P1 未实现：入库走 hindsight_memory + memory 工具；P0 只产 JSONL"},
                     ensure_ascii=False))
    return 4


def _code_version() -> str:
    try:
        result = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, encoding="utf-8", check=False)
        return result.stdout.strip() if result.returncode == 0 else ""
    except OSError:
        return ""


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=None,
                        help="固定 ops 空间（默认 reports/eval/experience_ops）")
    parser.add_argument("--suite", required=True, help="题集/数据分片名")
    parser.add_argument("--run-id", default=None, help="运行 ID（默认时间戳）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AgentOS 经验轨迹数据集构建")
    sub = parser.add_subparsers(dest="command", required=True)

    harvest = sub.add_parser("harvest", help="traces + 复盘索引 → raw bundles")
    _add_common(harvest)
    harvest.add_argument("--since", default=None, help="task.ended_at >= YYYY-MM-DD")
    harvest.add_argument("--db", default=None, help="显式内核库路径（默认解析）")
    harvest.add_argument("--from-json", default=None, help="从导出 bundle JSON/JSONL 读取")
    harvest.set_defaults(func=cmd_harvest)

    normalize = sub.add_parser("normalize", help="raw → episodes（credit 暂空）")
    _add_common(normalize)
    normalize.add_argument("--split", default="train", choices=["train", "heldout", "ood"])
    normalize.add_argument("--gamma", type=float, default=0.95)
    normalize.set_defaults(func=cmd_normalize)

    credit = sub.add_parser("credit", help="复盘 step_credits 对齐")
    _add_common(credit)
    credit.add_argument("--gamma", type=float, default=0.95)
    credit.set_defaults(func=cmd_credit)

    validate = sub.add_parser("validate", help="质量闸")
    _add_common(validate)
    validate.add_argument("--heldout", default=None,
                          help="held-out case_id 清单（每行一个；缺省不检查泄漏）")
    validate.set_defaults(func=cmd_validate)

    build = sub.add_parser("build", help="harvest + normalize + credit + validate")
    _add_common(build)
    build.add_argument("--since", default=None)
    build.add_argument("--db", default=None)
    build.add_argument("--from-json", default=None)
    build.add_argument("--split", default="train", choices=["train", "heldout", "ood"])
    build.add_argument("--gamma", type=float, default=0.95)
    build.add_argument("--heldout", default=None)
    build.set_defaults(func=cmd_build)

    ingest = sub.add_parser("ingest", help="P1 预留：经验入 hindsight")
    _add_common(ingest)
    ingest.set_defaults(func=cmd_ingest)
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
