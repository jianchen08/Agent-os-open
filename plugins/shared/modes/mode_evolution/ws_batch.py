"""WS 直驱批量评测（轮次重试版）。

绕开任务队列：每题建会话（出生绑定执行者）→ WS user_input 派发 →
韧性续跑（连接互踢即重连）→ 产物按 acceptance_criteria 在会话工作空间
直判（file_check 语义 + bash_check 退出码）。失败题自动进入下一轮重试
（供应商间歇性超时的对策），最多 MAX_ROUNDS 轮。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from typing import Any

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
os.chdir(ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts", "eval_bench"))
sys.path.insert(0, HERE)

import aggregate  # noqa: E402
from kernel_client import KernelClient, dispatch_and_collect  # noqa: E402

WS_ROOT = os.path.join(ROOT, ".ai_workspaces", "sessions")
MAX_ROUNDS = 3

PLAN = [
    ("config/self_evolve/suites/dev/smoke.yaml", None),
    ("config/self_evolve/suites/dev/coding.yaml",
     "tac_policy_lookup,longwof_config_diff_report"),
    ("config/self_evolve/suites/dev/writing.yaml", "write_structured_doc"),
    ("config/self_evolve/suites/dev/research.yaml", "gaia_style_multihop_local"),
    ("config/self_evolve/suites/dev/memory.yaml", "mem_multiround_three_keys"),
    ("config/self_evolve/suites/dev/transfer.yaml",
     "cap_conflict_resolution,cap_key_carry_multiround"),
]


def session_ws(thread_id: str) -> str:
    return os.path.join(WS_ROOT, thread_id)


def verify_file_check(ws_dir: str, params: dict) -> bool:
    path = os.path.join(ws_dir, str(params.get("path") or ""))
    if not os.path.isfile(path):
        return False
    check = params.get("check", "exists")
    content = open(path, encoding="utf-8", errors="replace").read()
    if check == "exists":
        return True
    if check == "not_empty":
        return len(content.strip()) > 0
    if check == "contains":
        return str(params.get("pattern") or "") in content
    return False


def verify_bash_check(ws_dir: str, params: dict) -> bool:
    cmd = str(params.get("command") or "")
    if not cmd:
        return False
    proc = subprocess.run(cmd, cwd=ws_dir, shell=True, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=60)
    return proc.returncode == 0


async def run_one_case(client, ws_url, case, results) -> bool:
    cid = case["id"]
    agent_id = str(case.get("target") or "general_agent")
    try:
        client.login("admin",
                     os.environ.get("AGENTOS_ADMIN_PASSWORD", "admin12345"))
    except Exception as e:
        print(f"[ws_batch] login failed for case {cid}: {e}", file=sys.stderr)
    import urllib.request as u2
    creq = u2.Request(client.base_url + "/api/v1/sessions", method="POST")
    creq.add_header("Content-Type", "application/json")
    creq.add_header("Authorization", f"Bearer {client.token}")
    creq.data = json.dumps({"title": f"eval-ws-{cid}-{time.strftime('%H%M%S')}",
                            "agent_id": agent_id}).encode()
    with u2.urlopen(creq, timeout=20) as resp:
        sess = json.loads(resp.read().decode())
    thread = str(sess.get("thread_id") or "")
    ws_dir = session_ws(thread)
    os.makedirs(ws_dir, exist_ok=True)
    print(f"[{cid}] session={thread[:24]} agent={agent_id}", flush=True)

    messages = [str(m).replace("{workspace}", ws_dir) for m in case["messages"]]
    # 消息级执行上下文（router.route_user_input 原生消费）——按 anchor 分流：
    # materials（缺省）= 评测物料区 plain；repo = 主仓库根 worktree（读仓库物料）
    anchor = str(case.get("anchor") or "materials")
    if anchor == "repo":
        exec_ctx = {"workspace": {"source_path": ROOT, "mode": "worktree"}}
    else:
        exec_ctx = {"workspace": {"source_path": ws_dir, "mode": "plain"}}
    terminal = ""
    for i, msg in enumerate(messages):
        content = msg
        for attempt in range(6):
            try:
                r = await dispatch_and_collect(
                    client.ws_chat_url(), thread, content,
                    f"ws-{cid}-{i}-{attempt}", timeout_s=420,
                    execution_context=exec_ctx)
            except Exception:
                if attempt >= 5:
                    terminal = "ws_error"
                    break
                content = "继续"
                await asyncio.sleep(3)
                continue
            terminal = r.terminal
            if terminal in ("stream_end", "stream_error", "error"):
                break
            content = "继续"
        if terminal in ("stream_error", "error"):
            break

    crit = {}
    for name, cfg in (case.get("acceptance_criteria") or {}).items():
        params = (cfg or {}).get("input_params") or {}
        try:
            if name == "file_check":
                ok = verify_file_check(ws_dir, params)
            elif name == "bash_check":
                ok = verify_bash_check(ws_dir, params)
            else:
                ok = False
        except Exception:
            ok = False
        crit[name] = ok
    status = "completed" if terminal == "stream_end" else f"ws_{terminal or 'none'}"
    ok = status == "completed" and all(crit.values())
    results.append({"case_id": cid, "task_status": status, "criteria": crit})
    verdict = "PASS" if ok else "FAIL"
    print(f"[{cid}] {status} {crit} -> {verdict}", flush=True)
    return ok


async def main() -> None:
    client = KernelClient("http://localhost:"
                          + os.environ.get("AGENTOS_KERNEL_PORT", "9100"))
    client.login("admin", os.environ.get("AGENTOS_ADMIN_PASSWORD", "admin12345"))
    ws_url = client.ws_chat_url()

    all_cases = []
    for suite_path, only in PLAN:
        suite = yaml.safe_load(open(suite_path, encoding="utf-8"))
        allow = {c.strip() for c in only.split(",")} if only else None
        for case in suite.get("cases") or []:
            if allow and case["id"] not in allow:
                continue
            all_cases.append(case)

    results: list[dict[str, Any]] = []
    passed = set()
    for rnd in range(1, MAX_ROUNDS + 1):
        round_cases = all_cases if rnd == 1 else [
            c for c in all_cases if c["id"] not in passed]
        if not round_cases:
            break
        if rnd > 1:
            print(f"===== 第 {rnd} 轮：重试 {len(round_cases)} 道失败题 =====",
                  flush=True)
            await asyncio.sleep(15)
        for case in round_cases:
            if await run_one_case(client, ws_url, case, results):
                passed.add(case["id"])

    summary = aggregate.summarize("mixed", results)
    out = os.path.join("reports", "eval",
                       "wsbatch_" + time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    print("")
    print(f"scorecard: {summary['passed']}/{summary['total']} -> {out}")
    for r in summary["results"]:
        verdict = "PASS" if r["passed"] else "FAIL"
        print(f"  {r['case_id']}: {verdict} {r['behavior_class']}")


if __name__ == "__main__":
    asyncio.run(main())
