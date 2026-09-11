#!/usr/bin/env python3
"""管道监控 watch 脚本：识别 human_interaction 等待态，杜绝 STUCK 假阳性。

长稳测试（2026-09-04）暴露：任务升级 human_interaction 等人工时，run 长时间
无 trace 增长，监控侧按"无进展"误报 STUCK（卡死），触发无效运维干预。

判定模型（优先级从高到低，读到即短路）：
- WAITING_HUMAN   run=Suspended 且 metadata 含 pending_interaction_request_id
                  ——human_interaction 工具进程内阻塞的权威痕迹（内核
                  store 层写入），等待用户作答/审批，绝不判 STUCK；
- SUSPENDED       run=Suspended（停泊挂起：子任务挂号等待唤醒/DSL wait/
                  G8 排空）——合法等待，不判 STUCK；
- RUNNING_STALE   run=Running 且最近 trace 停滞超过阈值——唯一 STUCK 嫌疑；
- RUNNING         run=Running 且 trace 活跃。

只读脚本（幂等）：仅 SELECT，供人工巡检或定时器循环调用。
退出码：0=无 STUCK；2=存在 STUCK 嫌疑；3=DB 不可达。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any

DEFAULT_STUCK_MINS = 30


def _parse_ts(raw: str | None) -> datetime | None:
    """解析 RFC3339/SQLite 时间戳（容错 Z 后缀与缺失）。"""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_active_runs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT r.run_id, r.pipeline_id, r.status, r.created_at, r.ended_at, r.metadata,
               (SELECT MAX(t.created_at) FROM traces t WHERE t.run_id = r.run_id) AS last_trace_at,
               (SELECT ps.field_value FROM pipeline_state ps
                 WHERE ps.pipeline_id = r.pipeline_id AND ps.field_key = 'task.status'
                 LIMIT 1) AS task_status
        FROM runs r
        WHERE r.status IN ('running', 'suspended')
        ORDER BY r.created_at
        """
    ).fetchall()
    active = []
    for run_id, pipeline_id, status, created_at, _ended, metadata, last_trace_at, task_status in rows:
        interaction_request_id = None
        if metadata:
            try:
                interaction_request_id = (json.loads(metadata) or {}).get(
                    "pending_interaction_request_id"
                )
            except (json.JSONDecodeError, TypeError):
                interaction_request_id = None
        active.append(
            {
                "run_id": run_id,
                "pipeline_id": pipeline_id,
                "status": status,
                "created_at": _parse_ts(created_at),
                "last_trace_at": _parse_ts(last_trace_at),
                "task_status": task_status,
                "interaction_request_id": interaction_request_id,
            }
        )
    return active


def classify(run: dict, stuck_cutoff: datetime) -> str:
    if run["interaction_request_id"]:
        return "WAITING_HUMAN"
    if run["status"] == "suspended":
        return "SUSPENDED"
    anchor = run["last_trace_at"] or run["created_at"]
    if anchor and anchor < stuck_cutoff:
        return "RUNNING_STALE"
    return "RUNNING"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db",
        default=os.environ.get("AGENTOS_DB_PATH", "agentos_kernel.db"),
        help="内核 SQLite 路径（默认 AGENTOS_DB_PATH 或项目根 agentos_kernel.db）",
    )
    parser.add_argument(
        "--stuck-mins",
        type=float,
        default=DEFAULT_STUCK_MINS,
        help=f"Running 无 trace 停滞阈值分钟（默认 {DEFAULT_STUCK_MINS}）",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0,
        help=">0 时按秒循环监控（watch 模式）",
    )
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"[watch] DB 不存在: {args.db}", file=sys.stderr)
        return 3

    while True:
        stuck = False
        conn = sqlite3.connect(args.db)
        try:
            now = _now()
            cutoff = datetime.fromtimestamp(
                now.timestamp() - args.stuck_mins * 60, tz=timezone.utc
            )
            for run in _load_active_runs(conn):
                verdict = classify(run, cutoff)
                if verdict == "RUNNING_STALE":
                    stuck = True
                last_trace = (
                    run["last_trace_at"].isoformat() if run["last_trace_at"] else "-"
                )
                extra = (
                    f" request_id={run['interaction_request_id']}"
                    if run["interaction_request_id"]
                    else ""
                )
                print(
                    f"[{verdict:>13}] pipeline={run['pipeline_id']} run={run['run_id']}"
                    f" task={run['task_status'] or '-'} last_trace={last_trace}{extra}"
                )
        finally:
            conn.close()

        if args.interval <= 0:
            return 2 if stuck else 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
