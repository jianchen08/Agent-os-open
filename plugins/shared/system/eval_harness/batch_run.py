"""批量评测：多题集选题 → task_submit 派发 → 轮询 → eval_summary 判定 → 聚合。"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
os.chdir(ROOT)
sys.path.insert(0, HERE)

import aggregate  # noqa: E402
import suite_reader  # noqa: E402

BASE = "http://localhost:" + os.environ.get("AGENTOS_KERNEL_PORT", "9100")
USER, PASSWORD = "admin", os.environ.get("AGENTOS_ADMIN_PASSWORD", "admin12345")


def http(method, path, body=None, token=None, timeout=70):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
        return json.loads(r.read().decode())


def login():
    r = http("POST", "/api/v1/auth/login", {"username": USER, "password": PASSWORD})
    d = r.get("data") or r
    return d.get("token") or d.get("access_token")


def _plan_from_argv(argv: list[str]) -> list[tuple[str, str | None]]:
    """显式套件参数：`batch_run.py 套件.yaml[[:case1,case2] ...]`（跑 external
    档/单题重放用）；缺省走既有代表性选题 plan。"""
    plan = []
    for arg in argv:
        path, _, cases = arg.partition("::")
        plan.append((path, cases or None))
    return plan


def _find_task_id_by_title(token: str, title: str) -> str:
    """按标题查已建任务（504 假拒后幂等恢复用：内核可能已建任务）。"""
    try:
        d = http("GET", "/ext/task_service/tasks?limit=50", token=token)
        data = d.get("data") or d
        items = data.get("tasks") or data.get("items") or (data if isinstance(data, list) else [])
        for t in items:
            if str(t.get("title") or "") == title:
                return str(t.get("id") or t.get("task_id") or "")
    except Exception as e:
        print(f"[batch_run] task lookup failed for {title!r}: {e}",
              file=sys.stderr)
    return ""


def main():
    # 第一批选题：每模式代表性题，避开长耗时（constraint_follow 类未入选）；
    # 显式套件参数（sys.argv）优先——external 档评测入口：
    #   batch_run.py config/self_evolve/suites/external/swe_verified_seed.yaml
    plan = _plan_from_argv(sys.argv[1:]) or [
        ("config/self_evolve/suites/dev/smoke.yaml", None),
        ("config/self_evolve/suites/dev/coding.yaml", "tac_policy_lookup,longwof_config_diff_report"),
        ("config/self_evolve/suites/dev/writing.yaml", "write_structured_doc"),
        ("config/self_evolve/suites/dev/research.yaml", "gaia_style_multihop_local"),
        ("config/self_evolve/suites/dev/memory.yaml", "mem_multiround_three_keys"),
        ("config/self_evolve/suites/dev/transfer.yaml", "cap_conflict_resolution,cap_key_carry_multiround"),
    ]
    token = login()
    last_login = time.monotonic()
    results = []
    for suite_path, cases in plan:
        suite = suite_reader.load_suite(suite_path)
        mode = suite.get("mode", "coding")
        batches = suite_reader.expand_batches(suite, cases, project_root=ROOT)
        for b in batches:
            args = b["task_submit_args"]
            # 工具参数名 → HTTP TaskRootCreate 字段名映射
            args = {
                "title": args["goal_title"],
                "description": args["goal_description"],
                "target_id": args["target_id"],
                "acceptance_criteria": args.get("acceptance_criteria") or {},
                "thread_id": f"thread-batch-{b['case_id']}",
                "workspace": args.get("workspace", ""),
                "workspace_mode": args.get("workspace_mode", "plain"),
            }

            cid = b["case_id"]
            # 派发幂等重试：504 是内核侧假拒（端点超时但任务可能已建，
            # §17.10/17.13 实锤）——重试前先按标题查已建任务，查到即复用
            task_id = ""
            for attempt in range(1, 4):
                try:
                    resp = http("POST", "/ext/task_service/tasks/root", args, token)
                    task_id = str(resp.get("id") or (resp.get("data") or {}).get("id") or "")
                    break
                except urllib.error.HTTPError as e:
                    print(f"[{cid}] dispatch attempt {attempt} FAIL {e.code}: "
                          f"{e.read().decode()[:160]}", flush=True)
                    if attempt < 3:
                        time.sleep(15 * attempt)
                        task_id = _find_task_id_by_title(token, args["title"])
                        if task_id:
                            print(f"[{cid}] recovered existing task {task_id} "
                                  f"(504 假拒已建)", flush=True)
                            break
            if task_id:
                print(f"[{cid}] dispatched {task_id}", flush=True)
            else:
                results.append({"case_id": cid, "task_status": "dispatch_error", "criteria": {}})
                continue
            results.append({"case_id": cid, "task_id": task_id,
                            "task_status": "pending", "criteria": {},
                            "expected": list((args.get("acceptance_criteria") or {}).keys())})
    # 全部派发后统一轮询（并发执行，串行等待）
    pending = [r for r in results if r.get("task_id")]
    deadline = time.monotonic() + 2400  # 40 分钟总窗
    while pending and time.monotonic() < deadline:
        if time.monotonic() - last_login > 480:
            token = login(); last_login = time.monotonic()
        still = []
        for r in pending:
            try:
                d = http("GET", f"/ext/task_service/tasks/{r['task_id']}", token=token)
            except Exception:
                still.append(r); continue
            data = d.get("data") or d
            st = str(data.get("status") or "")
            if st in ("completed", "failed", "cancelled"):
                r["task_status"] = st
                # 判定真值 = state 的 task.eval_summary 存在 → 各预期指标 True
                try:
                    ps = http("GET", "/api/v1/pipelines/state", token=token)
                    rows = ps.get("data") or ps
                    row = next((x for x in rows if str(x.get("pipeline_id")) == r["task_id"]), None)
                    fields = (row or {}).get("state") or {}
                    evaluated = "task.eval_summary" in fields
                except Exception:
                    evaluated = False
                for m in r["expected"]:
                    r["criteria"][m] = evaluated and st == "completed"
                print(f"[{r['case_id']}] {st} evaluated={evaluated}", flush=True)
            else:
                still.append(r)
        pending = still
        if pending:
            time.sleep(30)
    for r in pending:
        r["task_status"] = "timeout"
        for m in r["expected"]:
            r["criteria"][m] = False
    # 补 dispatch_error 的 expected
    for r in results:
        r.setdefault("expected", [])
    summary = aggregate.summarize(os.environ.get("BATCH_MODE", "mixed"), results)
    out = os.path.join("reports", "eval", "batch_" + suite_reader.time_tag())
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    print("")
    print(f"scorecard: {summary['passed']}/{summary['total']} -> {out}/summary.json")
    for r in summary["results"]:
        print(f"  {r['case_id']}: {'PASS' if r['passed'] else 'FAIL'} status={r['task_status']} behavior={r['behavior_class']}")


if __name__ == "__main__":
    main()
