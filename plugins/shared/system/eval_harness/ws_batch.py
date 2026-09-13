# -*- coding: utf-8 -*-
"""WS 直驱批量评测（绕开停摆的任务队列）：user_input 派发 → 产物验证 → scorecard。

判定 = 题集 acceptance_criteria 的 file_check（会话工作空间产物核对）与
bash_check（cwd=会话工作空间跑命令取退出码）——与评估闸门同语义的 harness
侧直判；口径注记：本批不经 AC 闸门（任务队列停摆），闸门路径已另行验证。
"""
from __future__ import annotations
import asyncio, json, os, subprocess, sys, time
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
os.chdir(ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts", "eval_bench"))
sys.path.insert(0, HERE)

from kernel_client import KernelClient, dispatch_and_collect  # noqa: E402
import aggregate  # noqa: E402

WS_ROOT = os.path.join(ROOT, ".ai_workspaces", "sessions")


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


async def main() -> None:
    plan = [
        ("config/self_evolve/suites/dev/smoke.yaml", None),
        ("config/self_evolve/suites/dev/coding.yaml", "tac_policy_lookup,longwof_config_diff_report"),
        ("config/self_evolve/suites/dev/writing.yaml", "write_structured_doc"),
        ("config/self_evolve/suites/dev/research.yaml", "gaia_style_multihop_local"),
        ("config/self_evolve/suites/dev/memory.yaml", "mem_multiround_three_keys"),
        ("config/self_evolve/suites/dev/transfer.yaml", "cap_conflict_resolution,cap_key_carry_multiround"),
    ]
    client = KernelClient("http://localhost:" + os.environ.get("AGENTOS_KERNEL_PORT", "9100"))
    client.login("admin", os.environ.get("AGENTOS_ADMIN_PASSWORD", "admin12345"))
    ws_url = client.ws_chat_url()

    results = []
    for suite_path, only in plan:
        suite = yaml.safe_load(open(suite_path, encoding="utf-8"))
        mode = suite.get("mode", "coding")
        allow = {c.strip() for c in only.split(",")} if only else None
        for case in suite.get("cases") or []:
            if allow and case["id"] not in allow:
                continue
            cid = case["id"]
            # 建会话并绑定模式执行者（绕开 main 的任务派发——任务队列停摆时
            # main 派发只会把工作丢进死队列；绑定 executor 后 WS 消息直达执行者）
            title = f"eval-ws-{cid}"
            agent_id = str(case.get("target") or "general_agent")
            try:
                client.login("admin", os.environ.get("AGENTOS_ADMIN_PASSWORD", "admin12345"))
            except Exception:
                pass  # 旧 token 仍有效时刷新失败不阻断
            # 建会话即带 agent_id（出生绑定）——建后再 PATCH 不影响已建主管道
            import urllib.request as _u2
            creq = _u2.Request(client.base_url + "/api/v1/sessions", method="POST")
            creq.add_header("Content-Type", "application/json")
            creq.add_header("Authorization", f"Bearer {client.token}")
            creq.data = json.dumps({"title": title, "agent_id": agent_id}).encode()
            with _u2.urlopen(creq, timeout=20) as resp:
                sess = json.loads(resp.read().decode())
            thread = str(sess.get("thread_id") or sess.get("id") or "")
            agent_id = str(case.get("target") or "general_agent")
            import urllib.request as _u
            req = _u.Request(client.base_url + f"/api/v1/sessions/{thread}/agent",
                             method="PATCH")
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", f"Bearer {client.token}")
            req.data = json.dumps({"agent_id": agent_id}).encode()
            with _u.urlopen(req, timeout=15) as resp:
                resp.read()
            # WS 建连必须用刷新后的 token（ws_chat_url 拼 token）
            ws_url = client.ws_chat_url()
            ws_dir = session_ws(thread)
            os.makedirs(ws_dir, exist_ok=True)
            print(f"[{cid}] session={thread} agent={agent_id}", flush=True)
            messages = [str(m).replace("{workspace}", ws_dir) for m in case["messages"]]
            # 多轮题：同一线程逐条发，最后一条收流判定
            terminal = ""
            for i, msg in enumerate(messages):
                last = i == len(messages) - 1
                content = msg
                # 前端 admin 连接会周期性重连互踢（replaced_by_new_connection）——
                # 被踢即重连发「继续」走 resume 路由，直到 stream_end 或重试耗尽
                for attempt in range(6):
                    try:
                        r = await dispatch_and_collect(
                            ws_url, thread, content, f"ws-{cid}-{i}-{attempt}",
                            timeout_s=420 if last else 300)
                    except Exception as exc:  # noqa: BLE001 —— 连接被踢/网络抖动
                        if "replaced_by_new_connection" not in str(exc) and attempt >= 5:
                            terminal = "ws_error"
                            break
                        content = "继续"
                        await asyncio.sleep(3)
                        continue
                    terminal = r.terminal
                    if terminal == "stream_end":
                        break
                    if terminal in ("stream_error", "error"):
                        break
                    content = "继续"
                if terminal in ("stream_error", "error"):
                    break
            criteria_cfg = case.get("acceptance_criteria") or {}
            crit = {}
            for name, cfg in criteria_cfg.items():
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
            results.append({"case_id": cid, "task_status": status, "criteria": crit})
            print(f"[{cid}] {status} {crit}", flush=True)

    summary = aggregate.summarize("mixed", results)
    out = os.path.join("reports", "eval", "wsbatch_" + time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    print("")
    print(f"scorecard: {summary['passed']}/{summary['total']} -> {out}")
    for r in summary["results"]:
        print(f"  {r['case_id']}: {'PASS' if r['passed'] else 'FAIL'} {r['behavior_class']}")


if __name__ == "__main__":
    asyncio.run(main())
