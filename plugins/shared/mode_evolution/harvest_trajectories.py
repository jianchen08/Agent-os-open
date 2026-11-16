"""真实轨迹采集脚本（运维通道）：db-read 契约 HTTP 读 → 筛选 → harvested 套件 + 轨迹样本库。

用法（root venv，项目根 cwd；内核在线 = 既有前置——登录与任务端点同源）：
  .venv/Scripts/python.exe plugins/shared/mode_evolution/harvest_trajectories.py [--dry-run] [--tenant <id>]

授权锚（admin 档）：本脚本为操作者本机运行的运维通道，操作者即管理员；
查询走内核 db-read 契约服务 HTTP 端点 POST /api/v1/db/read
（pipeline_state_admin 全局面，admin 会话，客户端在 ledger_db；登录凭据 =
AGENTOS_ADMIN_PASSWORD），跨租户全量收割即采集设计语义
（ADR 2026-09-16-trajectory-harvest-sourcing）；可带 --tenant 收窄到单租户采集。

产物（B 部类，仅本脚本/用户通道写入，ADR 2026-09-16-trajectory-harvest-sourcing）：
  config/self_evolve/suites/harvested/{mode}.yaml   判定题集（评估主样本集）
  reports/eval/trajectory_library/{tag}.json        轨迹样本库（成功+失败全量+筛选审计）
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from typing import Any

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
os.chdir(ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import ledger_db  # noqa: E402 — db-read 契约 HTTP 客户端（三脚本共享）
import trajectory_harvest  # noqa: E402

BASE = "http://127.0.0.1:" + os.environ.get("AGENTOS_KERNEL_PORT", "9100")  # 回环固定数字地址：localhost 会被系统代理劫持（装机 §B4 实证）

_SUITES_OUT = os.path.join(ROOT, "config", "self_evolve", "suites", "harvested")
_LIBRARY_OUT = os.path.join(ROOT, "reports", "eval", "trajectory_library")


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
    """--tenant <id> 可选参：admin 档收窄到单租户采集；缺省空串 = 跨租户全量。"""
    argv = sys.argv
    i = argv.index("--tenant") + 1 if "--tenant" in argv else 0
    return argv[i] if i and i < len(argv) else ""


def _decode(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return value


def load_tasks(token: str, tenant_id: str = "") -> list[dict[str, Any]]:
    """全量任务行（db-read 契约 pipeline_state_admin 面；尾列投影真实租户列，
    tenant_id 非空时契约收窄到该租户）。"""
    tasks = []
    for (pid, goal, desc, status, ac, evs, model, agent, tokens,
         ended_at, _tenant) in ledger_db.db_read_rows(
            http, token,
            "SELECT s.pipeline_id,"
            "  MAX(CASE WHEN field_key='task.goal' THEN s.value END),"
            "  MAX(CASE WHEN field_key='task.description' THEN s.value END),"
            "  MAX(CASE WHEN field_key='task.status' THEN s.value END),"
            "  MAX(CASE WHEN field_key='task.acceptance_criteria' THEN s.value END),"
            "  MAX(CASE WHEN field_key='task.eval_summary' THEN s.value END),"
            "  MAX(CASE WHEN field_key='llm_model' THEN s.value END),"
            "  MAX(CASE WHEN field_key='agent.id' THEN s.value END),"
            "  MAX(CASE WHEN field_key='track.total_tokens' THEN s.value END),"
            "  MAX(CASE WHEN field_key='task.ended_at' THEN s.value END),"
            "  s.tenant_id"
            " FROM pipeline_state s"
            " WHERE s.pipeline_id IN"
            "  (SELECT pipeline_id FROM pipeline_state WHERE field_key='task.goal')"
            " GROUP BY s.pipeline_id",
            {},
            tenant_id,
    ):
        tasks.append({
            "pipeline_id": pid,
            "goal_title": _decode(goal) or "",
            "description": _decode(desc) or "",
            "status": _decode(status),
            "acceptance_criteria": _decode(ac),
            "eval_summary": _decode(evs),
            "model": _decode(model),
            "agent_id": _decode(agent),
            "total_tokens": _decode(tokens),
            "ended_at": _decode(ended_at),
            "run_ids": _run_ids(token, pid, tenant_id),
        })
    return tasks


def _run_ids(token: str, pipeline_id: str, tenant_id: str = "") -> list[str]:
    # runs 表退役（ADR 2026-09-18）：当前 run_id 是 state 标量键
    rows = ledger_db.db_read_rows(
        http, token,
        "SELECT value, tenant_id FROM pipeline_state"
        " WHERE pipeline_id = :pipeline_id AND field_key = :field_key",
        {"pipeline_id": pipeline_id, "field_key": "run_id"},
        tenant_id,
    )
    r = rows[0] if rows else None
    return [r[0]] if r and r[0] else []


def write_outputs(result: dict[str, Any], tag: str, suites_out: str,
                  library_out: str) -> list[str]:
    written = []
    os.makedirs(suites_out, exist_ok=True)
    counts = result["library"]["counts"]
    for mode, suite in sorted(result["suites"].items()):
        path = os.path.join(suites_out, f"{mode}.yaml")
        body = yaml.safe_dump(suite, allow_unicode=True, sort_keys=False)
        header = (
            f"# harvested 判定题集（{mode}）——真实轨迹筛选采集生成物，勿手改\n"
            f"# 生成: {tag} | 判定 case 数: {len(suite['cases'])} | 全局审计: "
            f"{json.dumps(counts, ensure_ascii=False)}\n"
            f"# 溯源: 每 case 的 harvested_from 记原任务 pipeline_id；轨"
            f"迹全量与筛选审计见 reports/eval/trajectory_library/{tag}.json\n")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(header + body)
        written.append(path)
    os.makedirs(os.path.dirname(library_out), exist_ok=True)
    with open(library_out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(result["library"], fh, ensure_ascii=False, indent=1)
    written.append(library_out)
    return written


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    tenant = _cli_tenant()
    tag = time.strftime("%Y%m%d_%H%M%S")
    token = ledger_db.admin_login(http)
    tasks = load_tasks(token, tenant)
    result = trajectory_harvest.harvest(tasks, generated_tag=tag)
    counts = result["library"]["counts"]
    print(f"采集审计: {json.dumps(counts, ensure_ascii=False)}")
    for mode, suite in sorted(result["suites"].items()):
        print(f"  suite harvested/{mode}.yaml: {len(suite['cases'])} judged cases")
    if dry_run:
        print("(dry-run：不落盘)")
        return
    library_path = os.path.join(_LIBRARY_OUT, f"{tag}.json")
    written = write_outputs(result, tag, _SUITES_OUT, library_path)
    for path in written:
        print(f"写出: {path}")


if __name__ == "__main__":
    main()
