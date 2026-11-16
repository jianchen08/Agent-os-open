"""终局收集：按题目标题取最新任务实例，等终态 → DB eval_summary 判定 → scorecard。

用法：python final_collect.py [最大等待秒，缺省 3600] [--tenant <id>]
授权锚（admin 档）：本脚本为操作者本机运行的运维通道，操作者即管理员；
DB 判定走内核 db-read 契约服务 HTTP 端点 POST /api/v1/db/read
（pipeline_state_admin 全局面，admin 会话，客户端在 ledger_db），
--tenant 可选收窄到单租户。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
os.chdir(ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import aggregate  # noqa: E402
import ledger_db  # noqa: E402 — db-read 契约 HTTP 客户端（三脚本共享）

BASE = "http://127.0.0.1:" + os.environ.get("AGENTOS_KERNEL_PORT", "9100")  # 回环固定数字地址：localhost 会被系统代理劫持（装机 §B4 实证）
CASE_IDS = ["normal_qa", "normal_file_ops", "tac_policy_lookup",
            "longwof_config_diff_report", "write_structured_doc",
            "gaia_style_multihop_local", "mem_multiround_three_keys",
            "cap_conflict_resolution", "cap_key_carry_multiround"]


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


def main():
    tenant = _cli_tenant()
    rest = list(sys.argv[1:])
    if "--tenant" in rest:  # 剔除 --tenant 及其值，剩余首个位置参数 = 最大等待秒
        i = rest.index("--tenant")
        del rest[i : i + 2]
    max_wait = int(rest[0]) if rest else 3600
    token = ledger_db.admin_login(http)
    deadline = time.monotonic() + max_wait
    verdicts = {}
    last_login = time.monotonic()
    while time.monotonic() < deadline:
        if time.monotonic() - last_login > 480:  # token TTL 防护
            try:
                token = ledger_db.admin_login(http)
                last_login = time.monotonic()
            except Exception as e:
                # 刷新失败不更新 last_login，下轮重试；stderr 留痕便于诊断
                print(f"[final_collect] token refresh failed: {e}",
                      file=sys.stderr)
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
            # 任务状态词汇收敛（ADR 2026-10-30）：cancelled 退役，停止 = stopped
            if status in ("completed", "failed", "stopped"):
                rows = state_scalar(token, tid, "task.eval_summary", tenant)
                evaluated = bool(rows) and len(rows[0][0]) > 4
                ac_rows = state_scalar(token, tid, "task.acceptance_criteria", tenant)
                names = list(json.loads(ac_rows[0][0]).keys()) if ac_rows and ac_rows[0][0] else []
                crit = dict.fromkeys(names, evaluated and status == "completed")
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
