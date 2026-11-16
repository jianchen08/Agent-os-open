#!/usr/bin/env python3
"""统一管道标题存量数据迁移（ADR 2026-10-27-pipeline-title-field）。

一次性回填 pipeline_state 的 ``title`` 键（统一管道标题，无回退腿设计的
存量数据补齐）：

- 任务管道：``title := 自身 task.goal``（非空）——任务标题即管道标题；
- 会话主管道：``title := 会话标题``——``sessions.pipeline_ids[0]`` 承载
  主管道身份（ADR 2026-08-21 保序契约，主管道恒在前）；
- 已有 title 的行不动（幂等，保住启用方新写的值）；两者皆缺的管道不造名
  （面板走短 ID 兜底）。

用法：
    python scripts/migrate_pipeline_titles.py                # dry-run（只读预览）
    python scripts/migrate_pipeline_titles.py --apply        # 写入（默认装机库）
    python scripts/migrate_pipeline_titles.py --db <path> --apply

⚠️ 活库铁律：内核运行中禁止直连写库。--apply 前先关闭灵汐助手（装机版），
脚本检测到 agentos-kernel 进程会拒绝执行（--force 可越过，仅限明确知道
自己在做什么的场景）。--dry-run 只读（mode=ro），任意时刻可安全运行。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PREVIEW_LIMIT = 10
KERNEL_PROCESS_NAME = "agentos-kernel.exe"

# 任务管道：自身 task.goal 非空即回填
SQL_TASK_GOAL = """
INSERT INTO pipeline_state (pipeline_id, field_key, value, value_kind, tenant_id, updated_at)
SELECT ps.pipeline_id, 'title', ps.value, 'str', ps.tenant_id, :now
FROM pipeline_state ps
WHERE ps.field_key = 'task.goal' AND ps.value != ''
  AND NOT EXISTS (
      SELECT 1 FROM pipeline_state t
      WHERE t.pipeline_id = ps.pipeline_id AND t.field_key = 'title'
        AND t.tenant_id = ps.tenant_id)
"""

# 会话主管道：会话标题回填（pipeline_ids[0] = 主管道）
SQL_SESSION_MAIN = """
INSERT INTO pipeline_state (pipeline_id, field_key, value, value_kind, tenant_id, updated_at)
SELECT json_extract(s.pipeline_ids, '$[0]'), 'title', s.title, 'str', s.tenant_id, :now
FROM sessions s
WHERE s.title IS NOT NULL AND s.title != ''
  AND s.pipeline_ids IS NOT NULL AND json_extract(s.pipeline_ids, '$[0]') IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM pipeline_state t
      WHERE t.pipeline_id = json_extract(s.pipeline_ids, '$[0]') AND t.field_key = 'title'
        AND t.tenant_id = s.tenant_id)
"""

SQL_PREVIEW_TASK = """
SELECT ps.pipeline_id, ps.tenant_id, substr(ps.value, 1, 30) FROM pipeline_state ps
WHERE ps.field_key = 'task.goal' AND ps.value != ''
  AND NOT EXISTS (
      SELECT 1 FROM pipeline_state t
      WHERE t.pipeline_id = ps.pipeline_id AND t.field_key = 'title'
        AND t.tenant_id = ps.tenant_id)
LIMIT :limit
"""

SQL_PREVIEW_SESSION = """
SELECT json_extract(s.pipeline_ids, '$[0]'), s.tenant_id, substr(s.title, 1, 30) FROM sessions s
WHERE s.title IS NOT NULL AND s.title != ''
  AND s.pipeline_ids IS NOT NULL AND json_extract(s.pipeline_ids, '$[0]') IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM pipeline_state t
      WHERE t.pipeline_id = json_extract(s.pipeline_ids, '$[0]') AND t.field_key = 'title'
        AND t.tenant_id = s.tenant_id)
LIMIT :limit
"""

SQL_COUNT_TASK = """
SELECT COUNT(*) FROM pipeline_state ps
WHERE ps.field_key = 'task.goal' AND ps.value != ''
  AND NOT EXISTS (
      SELECT 1 FROM pipeline_state t
      WHERE t.pipeline_id = ps.pipeline_id AND t.field_key = 'title'
        AND t.tenant_id = ps.tenant_id)
"""

SQL_COUNT_SESSION = """
SELECT COUNT(*) FROM sessions s
WHERE s.title IS NOT NULL AND s.title != ''
  AND s.pipeline_ids IS NOT NULL AND json_extract(s.pipeline_ids, '$[0]') IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM pipeline_state t
      WHERE t.pipeline_id = json_extract(s.pipeline_ids, '$[0]') AND t.field_key = 'title'
        AND t.tenant_id = s.tenant_id)
"""


def default_db_path() -> Path:
    """装机库缺省路径（%APPDATA%\\agentos\\agentos_kernel.db）。"""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        sys.exit("APPDATA 未设置（非 Windows 环境？）——请用 --db 显式指定库路径")
    return Path(appdata) / "agentos" / "agentos_kernel.db"


def kernel_process_running() -> bool:
    """探测 agentos-kernel 进程（Windows tasklist / POSIX pgrep）。

    探测失败按「可能在运行」处理（fail-closed：要求人工确认/--force），
    不静默放行直连写活库。"""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {KERNEL_PROCESS_NAME}"],
                capture_output=True,
                timeout=15,
                check=False,
            ).stdout.decode("utf-8", errors="replace")
            return KERNEL_PROCESS_NAME.lower() in out.lower()
        out = subprocess.run(
            ["pgrep", "-f", "agentos-kernel"],
            capture_output=True,
            timeout=15,
            check=False,
        ).stdout.decode("utf-8", errors="replace")
        return bool(out.strip())
    except Exception as exc:  # noqa: BLE001 — 探测失败不静默放行，提示人工确认
        print(f"[WARN] 内核进程探测失败（{exc}）——请人工确认内核已关闭", file=sys.stderr)
        return True


def preview(conn: sqlite3.Connection, label: str, sql: str) -> None:
    rows = conn.execute(sql, {"limit": PREVIEW_LIMIT}).fetchall()
    print(f"  [{label}] 待回填样例（≤{PREVIEW_LIMIT} 条）：")
    for pid, tenant, value in rows:
        print(f"    - {pid} (tenant={tenant[:12]}…) ← {value}")
    if not rows:
        print("    - （无）")


def run(conn: sqlite3.Connection, apply: bool) -> int:
    now = datetime.now(UTC).isoformat()
    if not apply:
        task_rows = conn.execute(SQL_COUNT_TASK).fetchone()[0]
        session_rows = conn.execute(SQL_COUNT_SESSION).fetchone()[0]
        print(
            f"[DRY-RUN] 待回填：任务管道 {task_rows} 条 + 会话主管道 {session_rows} 条"
            f"（共 {task_rows + session_rows} 条；--apply 写入）"
        )
        preview(conn, "任务管道 task.goal", SQL_PREVIEW_TASK)
        preview(conn, "会话主管道 title", SQL_PREVIEW_SESSION)
        return 0

    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        cur.execute(SQL_TASK_GOAL, {"now": now})
        task_rows = cur.rowcount
        cur.execute(SQL_SESSION_MAIN, {"now": now})
        session_rows = cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(
        f"[APPLY] 回填完成：任务管道 {task_rows} 条 + 会话主管道 {session_rows} 条（共 {task_rows + session_rows} 条）"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="统一管道标题存量数据迁移（dry-run 默认）")
    parser.add_argument("--db", type=Path, default=default_db_path(), help="内核库路径")
    parser.add_argument("--apply", action="store_true", help="实际写入（缺省只读预览）")
    parser.add_argument("--force", action="store_true", help="越过内核进程守卫（明确知道内核已停时才用）")
    args = parser.parse_args()

    if not args.db.exists():
        sys.exit(f"库不存在: {args.db}")

    if args.apply:
        if kernel_process_running() and not args.force:
            sys.exit(
                "检测到 agentos-kernel 进程在运行——活库禁止直连写。\n"
                "请先关闭灵汐助手（装机版），确认进程退出后再执行 --apply。"
            )
        uri = f"file:{args.db.as_posix()}"
        conn = sqlite3.connect(uri, timeout=5, uri=True)
        conn.execute("PRAGMA busy_timeout = 5000")
    else:
        # dry-run：只读连接（WAL 库同进程外只读安全，不触发恢复不落锁）
        uri = f"file:{args.db.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, timeout=5, uri=True)

    print(f"库: {args.db}（{'APPLY' if args.apply else 'DRY-RUN'}）")
    try:
        return run(conn, apply=args.apply)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
