# -*- coding: utf-8 -*-
"""瘦身后闭环复验：task_submit HTTP 通道（带 acceptance_criteria）→ 轮询终态 → 聚合。

用法（root venv，项目根 cwd）：.venv/Scripts/python.exe reports/verify_round.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))
os.chdir(ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yaml  # noqa: E402

import aggregate  # noqa: E402

BASE = "http://localhost:9100"
USER, PASSWORD = "admin", os.environ.get("AGENTOS_ADMIN_PASSWORD", "admin12345")


def http(method: str, path: str, body: dict | None = None, token: str | None = None,
         timeout: float = 30) -> dict:
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def main() -> None:
    login_resp = http("POST", "/api/v1/auth/login",
                      {"username": USER, "password": PASSWORD})
    login_data = login_resp.get("data") or login_resp
    token = str(login_data.get("token") or login_data.get("access_token") or "")
    print("login OK", flush=True)

    suite = yaml.safe_load(open("config/self_evolve/suites/dev/smoke.yaml", encoding="utf-8"))
    results = []
    for case in suite["cases"]:
        run_tag = time.strftime("%Y%m%d_%H%M%S")
        body = {
            "title": f"eval-{case['id']}-{run_tag}",
            "description": "\n".join(case["messages"]),
            "target_id": case.get("target", "executor/general_agent"),
            "thread_id": f"thread-selfevolve-{case['id']}",
            "acceptance_criteria": case.get("acceptance_criteria") or {},
        }
        created = http("POST", "/ext/task_service/tasks/root", body, token)
        task = created.get("data") or created
        task_id = str(task.get("id") or task.get("task_id") or "")
        print(f"[{case['id']}] task_id={task_id}", flush=True)
        if not task_id:
            print("  派发失败:", json.dumps(created, ensure_ascii=False)[:300])
            results.append({"case_id": case["id"], "task_status": "dispatch_error",
                            "criteria": {}})
            continue

        # 轮询终态（上限 15 分钟）
        status, detail = "", {}
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            detail = http("GET", f"/ext/task_service/tasks/{task_id}", token=token)
            data = detail.get("data") or detail
            status = str(data.get("status") or "")
            if status in ("completed", "failed", "cancelled"):
                break
            time.sleep(20)
        print(f"[{case['id']}] status={status}", flush=True)
        # 验收结果：任务详情/评估面（MVP 取详情内 criteria；缺则置空由聚合按终态判）
        criteria = {}
        for k, v in (data.get("acceptance_criteria") or {}).items():
            criteria[k] = bool(v.get("passed", v)) if isinstance(v, dict) else bool(v)
        if not criteria:
            try:
                state = http("GET", "/api/v1/pipelines/state", token=token)
                for row in (state.get("data") or state) if isinstance((state.get("data") or state), list) else []:
                    if str(row.get("pipeline_id")) == task_id:
                        st = row.get("state") or {}
                        for k, v in (st.get("task", {}).get("acceptance_criteria") or {}).items():
                            criteria[k] = bool(v.get("passed", v)) if isinstance(v, dict) else bool(v)
            except Exception as exc:  # noqa: BLE001
                print("  state 读取失败:", exc)
        print(f"[{case['id']}] criteria={criteria}", flush=True)
        results.append({"case_id": case["id"], "task_id": task_id,
                        "task_status": status or "timeout", "criteria": criteria})

    summary = aggregate.summarize("coding", results)
    out_dir = os.path.join("reports", "eval", f"verify_{time.strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    print(f"\nscorecard: {summary['passed']}/{summary['total']} → {out_dir}/summary.json")
    for r in summary["results"]:
        print(f"  - {r['case_id']}: {'PASS' if r['passed'] else 'FAIL'} "
              f"status={r['task_status']} behavior={r['behavior_class']}")


if __name__ == "__main__":
    main()
