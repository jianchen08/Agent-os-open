# -*- coding: utf-8 -*-
"""真实轨迹采集脚本（运维通道）：DB 只读直查 → 筛选 → harvested 套件 + 轨迹样本库。

用法（root venv，项目根 cwd）：
  .venv/Scripts/python.exe plugins/shared/system/eval_harness/harvest_trajectories.py [--dry-run]

产物（B 部类，仅本脚本/用户通道写入，ADR 2026-09-16-trajectory-harvest-sourcing）：
  config/self_evolve/suites/harvested/{mode}.yaml   判定题集（评估主样本集）
  reports/eval/trajectory_library/{tag}.json        轨迹样本库（成功+失败全量+筛选审计）
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from typing import Any

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
os.chdir(ROOT)
sys.path.insert(0, HERE)

import trajectory_harvest  # noqa: E402

_SUITES_OUT = os.path.join(ROOT, "config", "self_evolve", "suites", "harvested")
_LIBRARY_OUT = os.path.join(ROOT, "reports", "eval", "trajectory_library")

_TASK_QUERY = """
SELECT s.pipeline_id,
  MAX(CASE WHEN field_key='task.goal' THEN s.value END),
  MAX(CASE WHEN field_key='task.description' THEN s.value END),
  MAX(CASE WHEN field_key='task.status' THEN s.value END),
  MAX(CASE WHEN field_key='task.acceptance_criteria' THEN s.value END),
  MAX(CASE WHEN field_key='task.eval_summary' THEN s.value END),
  MAX(CASE WHEN field_key='llm_model' THEN s.value END),
  MAX(CASE WHEN field_key='agent.id' THEN s.value END),
  MAX(CASE WHEN field_key='track.total_tokens' THEN s.value END),
  MAX(CASE WHEN field_key='task.ended_at' THEN s.value END)
FROM pipeline_state s
WHERE s.pipeline_id IN
  (SELECT pipeline_id FROM pipeline_state WHERE field_key='task.goal')
GROUP BY s.pipeline_id"""  # state 值域标量化（ADR 2026-09-18）：value 列原文即标量


def _decode(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return value


def load_tasks(db_path: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cur.execute(_TASK_QUERY)
        tasks = []
        for (pid, goal, desc, status, ac, evs, model, agent, tokens,
             ended_at) in cur.fetchall():
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
                "run_ids": _run_ids(cur, pid),
            })
        return tasks
    finally:
        conn.close()


def _run_ids(cur: sqlite3.Cursor, pipeline_id: str) -> list[str]:
    # runs 表退役（ADR 2026-09-18）：当前 run_id 是 state 标量键
    cur.execute("SELECT value FROM pipeline_state WHERE pipeline_id=? "
                "AND field_key='run_id'", (pipeline_id,))
    r = cur.fetchone()
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
    tag = time.strftime("%Y%m%d_%H%M%S")
    db_path = os.environ.get("AGENTOS_DB_PATH") or os.path.join(ROOT, "agentos_kernel.db")
    tasks = load_tasks(db_path)
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
