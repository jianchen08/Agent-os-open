#!/usr/bin/env python3
"""内核库只读查询（开发者数据排查面）：临时快照 + 语句白名单，永不触碰活库写面。

与 backup_db.py 同源口径：
  - 库路径解析复用其 resolve_target()（AGENTOS_DB_PATH > config/kernel/storage.yaml
    > 项目根 agentos_kernel.db，driver 非 sqlite 拒绝）；--db 显式指定时绕过解析。
  - 快照 = VACUUM INTO 一致性快照，对运行中的内核安全（不阻塞、不改动源库），
    查询全部落在 tempfile 里的快照副本上，用完即删。

只读执法（多层，缺一即拒绝）：
  1. 语句前缀白名单：select / with / explain / values（前导注释剥除后判定）；
  2. 单语句：sqlite3 execute() 原生拒绝多语句；
  3. 快照连接 PRAGMA query_only=ON；
  4. 源库连接恒 mode=ro（只读 URI，物理上无法写）；
  5. 行数上限（默认 200，--limit）+ 墙钟超时（默认 30s，--timeout-s）。

用法：
  python scripts/kernel_db_query.py "select field_key, value from pipeline_state where pipeline_id like 'd72aa%'"
  python scripts/kernel_db_query.py "select name from sqlite_master where type='table'"
  python scripts/kernel_db_query.py "<SQL>" --db "C:/Users/<u>/AppData/Roaming/agentos/agentos_kernel.db"   # 装机库
  python scripts/kernel_db_query.py "<SQL>" --json --limit 500

定位边界：本脚本是**开发者/脚本侧**工具（任务书派发的插件生产码禁直连内核库，
合法入口 = db-read 契约，见 scripts/check_kernel_db_direct_reads.py 执法口径）。
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import re
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# 进度回调步长（opcode 数）：墙钟超时的检查粒度，测试可调小以快速触发。
_PROGRESS_OPS = 10_000

# 快照单元格渲染上限（字符）：超宽值截断，防单行刷屏。
_CELL_CAP = 60

_DEFAULT_LIMIT = 200
_DEFAULT_TIMEOUT_S = 30.0

# 只读前缀白名单（前导注释/空白剥除后按词判定）。
_READ_PREFIXES = ("select", "with", "explain", "values")


class QueryError(RuntimeError):
    """路径解析/快照/语句守卫/执行任一环失败。"""


def _load_backup_db() -> object:
    """按路径装载同目录 backup_db（scripts/ 非包，直接 import 依赖 CWD 不可靠）。"""
    path = Path(__file__).resolve().parent / "backup_db.py"
    spec = importlib.util.spec_from_file_location("kernel_db_query_backup_db", path)
    if spec is None or spec.loader is None:
        raise QueryError(f"无法装载 backup_db 模块: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass/注解解析要求模块可被 sys.modules 查获
    spec.loader.exec_module(module)
    return module


def resolve_db(explicit: str | None) -> Path:
    """解析目标库路径：--db 显式优先（只验存在），否则走内核同口径解析。"""
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise QueryError(f"--db 指定的库文件不存在: {path}")
        return path
    backup_db = _load_backup_db()
    try:
        return backup_db.resolve_target()  # type: ignore[attr-defined]
    except backup_db.BackupError as e:  # type: ignore[attr-defined]
        raise QueryError(str(e)) from e


def _strip_leading_comments(sql: str) -> str:
    """剥除前导空白与注释（-- 行注释与 /* */ 块注释），返回首个实义字符起的语句。"""
    s = sql.strip()
    while True:
        if s.startswith("--"):
            nl = s.find("\n")
            s = "" if nl < 0 else s[nl + 1 :].strip()
            continue
        if s.startswith("/*"):
            end = s.find("*/")
            if end < 0:
                return ""
            s = s[end + 2 :].strip()
            continue
        return s


def validate_sql(sql: str) -> str:
    """只读守卫：剥注释后取首标识符判定前缀白名单；不合法即拒绝（fail-closed）。"""
    statement = _strip_leading_comments(sql)
    if not statement:
        raise QueryError("空语句")
    match = re.match(r"[A-Za-z_]+", statement)
    first_word = match.group(0).lower() if match else ""
    if first_word not in _READ_PREFIXES:
        raise QueryError(f"只允许只读查询（select/with/explain/values 前缀），收到: {first_word!r}")
    return statement


@dataclass
class QueryOutcome:
    columns: list[str]
    rows: list[list[object]]
    truncated: bool


def run_query(
    db_path: Path,
    sql: str,
    limit: int = _DEFAULT_LIMIT,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> QueryOutcome:
    """快照后在快照上执行只读查询；源库全程只读连接，查询结束快照即删。"""
    statement = validate_sql(sql)
    deadline = time.monotonic() + timeout_s
    with tempfile.TemporaryDirectory(prefix="kernel_db_query_") as tmp:
        snap = Path(tmp) / f"{db_path.stem}-snapshot.db"
        src = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        try:
            src.execute("VACUUM INTO ?1", (str(snap),))
        except sqlite3.Error as e:
            raise QueryError(f"快照失败（源库 {db_path}）: {e}") from e
        finally:
            src.close()

        conn = sqlite3.connect(f"file:{snap.as_posix()}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only=ON")

            def _hit_deadline() -> bool:
                return time.monotonic() > deadline

            conn.set_progress_handler(_hit_deadline, _PROGRESS_OPS)
            try:
                cur = conn.execute(statement)
            except sqlite3.ProgrammingError as e:
                raise QueryError(f"语句被拒绝（含多语句提交也在此拒绝）: {e}") from e
            except sqlite3.OperationalError as e:
                raise QueryError(f"查询未完成（超时 {timeout_s}s 或 SQL 错误）: {e}") from e
            try:
                columns = [d[0] for d in cur.description or []]
                rows: list[list[object]] = []
                truncated = False
                for row in cur:
                    if len(rows) >= limit:
                        truncated = True
                        break
                    rows.append(list(row))
            finally:
                # Windows：截断 break 留下未 finalize 的语句会锁住快照文件，
                # TemporaryDirectory 清理直接 PermissionError——必须显式关游标。
                cur.close()
            return QueryOutcome(columns=columns, rows=rows, truncated=truncated)
        finally:
            conn.close()


def _render_cell(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value)
    if len(text) > _CELL_CAP:
        text = text[:_CELL_CAP] + "…"
    return text.replace("\n", "\\n")


def format_table(outcome: QueryOutcome) -> str:
    """对齐文本表：列宽按渲染后内容收敛，单元格超宽截断。"""
    rendered = [[_render_cell(v) for v in row] for row in outcome.rows]
    if not outcome.columns:
        return "(无结果集)"
    widths = [len(c) for c in outcome.columns]
    for row in rendered:
        for i, cell in enumerate(row):
            widths[i] = min(max(widths[i], len(cell)), _CELL_CAP)
    lines = [
        " | ".join(c.ljust(w) for c, w in zip(outcome.columns, widths, strict=True)),
        "-+-".join("-" * w for w in widths),
    ]
    lines.extend(" | ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)) for row in rendered)
    return "\n".join(lines)


def main() -> int:
    try:
        if isinstance(sys.stdout, io.TextIOWrapper):
            # GBK 控制台免疫：输出层强制 UTF-8（不可用时维持默认，逐值 replace）
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except OSError:
        pass
    parser = argparse.ArgumentParser(description="内核库只读查询（临时快照，详见模块 docstring）")
    parser.add_argument("sql", help="只读 SQL（select/with/explain/values 前缀，单语句）")
    parser.add_argument("--db", default=None, help="目标库路径（缺省=内核同口径解析，见 backup_db）")
    parser.add_argument("--json", action="store_true", help="JSON 输出（默认对齐文本表）")
    parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT, help=f"行数上限（默认 {_DEFAULT_LIMIT}）")
    parser.add_argument(
        "--timeout-s", type=float, default=_DEFAULT_TIMEOUT_S, help=f"查询墙钟超时秒数（默认 {_DEFAULT_TIMEOUT_S:g}）"
    )
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit 必须 ≥ 1")
    try:
        db_path = resolve_db(args.db)
        outcome = run_query(db_path, args.sql, limit=args.limit, timeout_s=args.timeout_s)
    except QueryError as e:
        print(f"kernel_db_query: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(outcome.__dict__, ensure_ascii=False, default=repr))
    else:
        print(format_table(outcome))
        tail = f"（{len(outcome.rows)} 行）"
        if outcome.truncated:
            tail += f" — 已截断至 {args.limit} 行，请用 WHERE/LIMIT 收窄"
        print(tail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
