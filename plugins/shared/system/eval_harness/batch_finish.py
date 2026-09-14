# -*- coding: utf-8 -*-
"""批量收官：对已派发的任务轮询到终态 → DB 读 eval_summary 判定 → scorecard。"""
from __future__ import annotations
import json, os, sqlite3, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
os.chdir(ROOT)
sys_path = HERE

BASE = "http://localhost:" + os.environ.get("AGENTOS_KERNEL_PORT", "9100")

TASKS = {
    "3dd34a81e060": "normal_qa", "4bfa4b71fb73": "normal_file_ops",
    "3c556ac0ce32": "tac_policy_lookup", "098de717f03c": "longwof_config_diff_report",
    "14eb53980522": "write_structured_doc", "35635015392e": "gaia_style_multihop_local",
    "1f7c5f283de4": "mem_multiround_three_keys", "6fa7d64d66f5": "cap_conflict_resolution",
    "56883bb35542": "cap_key_carry_multiround",
}


def http(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data=data, timeout=40) as r:
        return json.loads(r.read().decode())


def main():
    token = http("POST", "/api/v1/auth/login",
                 {"username": "admin",
                  "password": os.environ.get("AGENTOS_ADMIN_PASSWORD", "admin12345")})
    token = ((token.get("data") or token).get("token")
             or (token.get("data") or token).get("access_token"))
    conn = sqlite3.connect(os.path.join(ROOT, "agentos_kernel.db"))
    cur = conn.cursor()
    results = []
    remaining = dict(TASKS)
    deadline = time.monotonic() + 2700  # 45 分钟
    while remaining and time.monotonic() < deadline:
        done_now = []
        for tid, cid in remaining.items():
            try:
                d = http("GET", f"/ext/task_service/tasks/{tid}", token=token)
            except Exception:
                continue
            st = str((d.get("data") or d).get("status") or "")
            if st in ("completed", "failed", "cancelled"):
                cur.execute(
                    "SELECT field_value FROM pipeline_state WHERE pipeline_id=? "
                    "AND field_key='task.eval_summary'", (tid,))
                row = cur.fetchone()
                evaluated = row is not None and len(row[0]) > 4
                crit = {}
                # 预期指标从 AC 声明取（判定=评估闸门已跑出 summary）
                cur.execute(
                    "SELECT field_value FROM pipeline_state WHERE pipeline_id=? "
                    "AND field_key='task.acceptance_criteria'", (tid,))
                ac_row = cur.fetchone()
                names = list(json.loads(ac_row[0]).keys()) if ac_row and ac_row[0] else []
                for m in names:
                    crit[m] = evaluated and st == "completed"
                results.append({"case_id": cid, "task_id": tid, "task_status": st,
                                "criteria": crit})
                print(f"[{cid}] {st} evaluated={evaluated} {crit}", flush=True)
                done_now.append(tid)
        for tid in done_now:
            remaining.pop(tid, None)
        if remaining:
            time.sleep(30)
    for tid, cid in remaining.items():
        results.append({"case_id": cid, "task_id": tid, "task_status": "timeout",
                        "criteria": {}})
        print(f"[{cid}] timeout", flush=True)

    passed = 0
    for r in results:
        if r["task_status"] == "completed" and r["criteria"] and all(
            bool(v) for v in r["criteria"].values()
        ):
            passed += 1
    out = os.path.join(ROOT, "reports", "eval",
                       "final_" + time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"passed": passed, "total": len(results), "results": results},
                  fh, ensure_ascii=False, indent=1)
    print(f"\n终局 scorecard: {passed}/{len(results)} -> {out}/summary.json", flush=True)


if __name__ == "__main__":
    main()
