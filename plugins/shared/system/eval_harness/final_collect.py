# -*- coding: utf-8 -*-
"""终局收集：按题目标题取最新任务实例，等终态 → DB eval_summary 判定 → scorecard。

用法：python final_collect.py [最大等待秒，缺省 3600]
"""
from __future__ import annotations
import json, os, re, sqlite3, sys, time, urllib.request
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
os.chdir(ROOT)
sys.path.insert(0, HERE)

import yaml  # noqa: E402
import aggregate  # noqa: E402

BASE = "http://localhost:" + os.environ.get("AGENTOS_KERNEL_PORT", "9100")
CASE_IDS = ["normal_qa", "normal_file_ops", "tac_policy_lookup",
            "longwof_config_diff_report", "write_structured_doc",
            "gaia_style_multihop_local", "mem_multiround_three_keys",
            "cap_conflict_resolution", "cap_key_carry_multiround"]


def http(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data=data, timeout=40) as r:
        return json.loads(r.read().decode())


def latest_by_case(token):
    d = http("GET", "/ext/task_service/tasks", token=token)
    items = (d.get("data") or d)
    items = items if isinstance(items, list) else (items.get("tasks") or items.get("items") or [])
    latest: dict[str, Any] = {}
    for t in items:
        m = re.match(r"eval-([a-z_0-9]+)-(\d{8}_\d{6})$", str(t.get("title") or ""))
        if not m:
            continue
        cid, tag = m.groups()
        if cid in CASE_IDS and (cid not in latest or tag > latest[cid][1]):
            latest[cid] = (t.get("id"), tag, t.get("status"))
    return latest


def main():
    max_wait = int(sys.argv[1]) if len(sys.argv) > 1 else 3600
    token = http("POST", "/api/v1/auth/login",
                 {"username": "admin",
                  "password": os.environ.get("AGENTOS_ADMIN_PASSWORD", "admin12345")})
    token = ((token.get("data") or token).get("token")
             or (token.get("data") or token).get("access_token"))
    conn = sqlite3.connect(os.path.join(ROOT, "agentos_kernel.db"))
    cur = conn.cursor()
    deadline = time.monotonic() + max_wait
    verdicts = {}
    last_login = time.monotonic()
    while time.monotonic() < deadline:
        if time.monotonic() - last_login > 480:  # token TTL 防护
            try:
                token = http("POST", "/api/v1/auth/login",
                             {"username": "admin",
                              "password": os.environ.get("AGENTOS_ADMIN_PASSWORD", "admin12345")})
                token = ((token.get("data") or token).get("token")
                         or (token.get("data") or token).get("access_token"))
                last_login = time.monotonic()
            except Exception:
                pass
        try:
            latest = latest_by_case(token)
        except Exception:
            time.sleep(30)
            continue
        pending_count = 0
        for cid in CASE_IDS:
            if cid in verdicts:
                continue
            ent = latest.get(cid)
            if not ent:
                continue
            tid, tag, status = ent
            if status in ("completed", "failed", "cancelled"):
                cur.execute(
                    "SELECT value FROM pipeline_state WHERE pipeline_id=? "
                    "AND field_key='task.eval_summary'", (tid,))
                row = cur.fetchone()
                evaluated = row is not None and len(row[0]) > 4
                cur.execute(
                    "SELECT value FROM pipeline_state WHERE pipeline_id=? "
                    "AND field_key='task.acceptance_criteria'", (tid,))
                ac = cur.fetchone()
                names = list(json.loads(ac[0]).keys()) if ac and ac[0] else []
                crit = {m: (evaluated and status == "completed") for m in names}
                verdicts[cid] = {"task_id": tid, "task_status": status,
                                 "criteria": crit}
                print(f"[{cid}] {status} evaluated={evaluated}", flush=True)
            else:
                pending_count += 1
        if not pending_count:
            break
        print(f"  ... {pending_count} 题未终态，30s 后再查", flush=True)
        time.sleep(30)

    results = []
    for cid in CASE_IDS:
        v = verdicts.get(cid)
        if v:
            results.append({"case_id": cid, "task_status": v["task_status"],
                            "criteria": v["criteria"]})
        else:
            results.append({"case_id": cid, "task_status": "no_run", "criteria": {}})
    summary = aggregate.summarize("mixed", results)
    out = os.path.join(ROOT, "reports", "eval",
                       "final_" + time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    print(f"\n终局 scorecard: {summary['passed']}/{summary['total']} -> {out}/summary.json")
    for r in summary["results"]:
        print(f"  {r['case_id']}: {'PASS' if r['passed'] else 'FAIL'} "
              f"status={r['task_status']} behavior={r['behavior_class']}")


if __name__ == "__main__":
    main()
