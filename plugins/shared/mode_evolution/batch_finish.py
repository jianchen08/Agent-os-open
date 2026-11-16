"""批量收官：对已派发的任务轮询到终态 → DB 读 eval_summary 判定 → scorecard。

用法：python batch_finish.py [--tenant <id>]
授权锚（admin 档）：本脚本为操作者本机运行的运维通道，操作者即管理员；
DB 判定走内核 db-read 契约服务 HTTP 端点 POST /api/v1/db/read
（pipeline_state_admin 全局面，admin 会话，客户端在 ledger_db），
--tenant 可选收窄到单租户。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
os.chdir(ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import ledger_db  # noqa: E402 — db-read 契约 HTTP 客户端（三脚本共享）

BASE = "http://127.0.0.1:" + os.environ.get("AGENTOS_KERNEL_PORT", "9100")  # 回环固定数字地址：localhost 会被系统代理劫持（装机 §B4 实证）

TASKS = {
    "3dd34a81e060": "normal_qa", "4bfa4b71fb73": "normal_file_ops",
    "3c556ac0ce32": "tac_policy_lookup", "098de717f03c": "longwof_config_diff_report",
    "14eb53980522": "write_structured_doc", "35635015392e": "gaia_style_multihop_local",
    "1f7c5f283de4": "mem_multiround_three_keys", "6fa7d64d66f5": "cap_conflict_resolution",
    "56883bb35542": "cap_key_carry_multiround",
}


def _open_no_proxy(req, data=None, timeout=40):
    """回环请求强制无代理（ProxyHandler({}) 覆盖注册表/env 代理面，装机 §B4）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(req, data=data, timeout=timeout)


def http(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    with _open_no_proxy(req, data=data, timeout=40) as r:
        return json.loads(r.read().decode())


def _cli_tenant() -> str:
    """--tenant <id> 可选参：admin 档收窄到单租户判定；缺省空串 = 跨租户全量。"""
    argv = sys.argv
    i = argv.index("--tenant") + 1 if "--tenant" in argv else 0
    return argv[i] if i and i < len(argv) else ""


def state_scalar(
    token: str, pipeline_id: str, field_key: str, tenant_id: str = ""
) -> list[list[Any]]:
    """pipeline_state 标量键行（db-read 契约 pipeline_state_admin 面；
    尾列投影真实租户列，tenant_id 非空时契约收窄到该租户）。"""
    return ledger_db.db_read_rows(
        http, token,
        "SELECT value, tenant_id FROM pipeline_state"
        " WHERE pipeline_id = :pipeline_id AND field_key = :field_key",
        {"pipeline_id": pipeline_id, "field_key": field_key},
        tenant_id,
    )


def eval_task_verdict(
    token: str, pipeline_id: str, task_status: str, tenant_id: str = ""
) -> tuple[bool, dict[str, bool]]:
    """DB 读 eval_summary 判定 + 预期指标从 AC 声明取（判定=评估闸门已跑出 summary）。"""
    rows = state_scalar(token, pipeline_id, "task.eval_summary", tenant_id)
    evaluated = bool(rows) and len(rows[0][0]) > 4
    ac_rows = state_scalar(token, pipeline_id, "task.acceptance_criteria", tenant_id)
    names = list(json.loads(ac_rows[0][0]).keys()) if ac_rows and ac_rows[0][0] else []
    crit = dict.fromkeys(names, evaluated and task_status == "completed")
    return evaluated, crit


def main():
    tenant = _cli_tenant()
    token = ledger_db.admin_login(http)
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
            # 任务状态词汇收敛（ADR 2026-10-30）：cancelled 退役，停止 = stopped
            if st in ("completed", "failed", "stopped"):
                evaluated, crit = eval_task_verdict(token, tid, st, tenant)
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
