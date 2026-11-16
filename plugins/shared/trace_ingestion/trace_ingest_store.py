"""E 事件汇聚云端存储（插件自有数据面，SQLite，内核零改动）。

表设计与 §3.2/§3.3 对齐：
- ``trace_events`` 主键 ``(origin_host, ulid)`` —— 幂等键 + 排序存储一体
  （ULID 字典序即时间序，跨源不做全局时序承诺）；已存在即跳过（重传零重复）。
- ``trace_dead_letters`` —— 必需字段缺失/形态非法的事件入死信并继续
  （schema 宽容：坏事件不阻塞后续批次，§3.2）。

导入形态与 ``http_json`` 先例一致：插件侧把 ``plugins/shared`` 推上 sys.path
后裸名导入。sqlite3 同步调用（审批插件先例）——批量为百条量级，无阻塞风险。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trace_events (
    origin_host      TEXT NOT NULL,
    origin_kind      TEXT NOT NULL DEFAULT '',
    ulid             TEXT NOT NULL,
    event            TEXT NOT NULL,
    tags_json        TEXT NOT NULL DEFAULT '{}',
    extra_json       TEXT NOT NULL DEFAULT '{}',
    protocol_version INTEGER NOT NULL,
    emitted_at       TEXT NOT NULL DEFAULT '',
    received_at      TEXT NOT NULL,
    PRIMARY KEY (origin_host, ulid)
);
CREATE TABLE IF NOT EXISTS trace_dead_letters (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_host  TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    received_at  TEXT NOT NULL
);
"""


class TraceEventStore:
    """事件表 + 死信表（幂等插入 / origin 过滤查询 / 游标分页）。"""

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── 事件面 ──────────────────────────────────────────────

    def insert_event(
        self,
        *,
        origin_host: str,
        origin_kind: str,
        ulid: str,
        event: str,
        tags: dict[str, Any],
        extra: dict[str, Any],
        protocol_version: int,
        emitted_at: str,
        received_at: str,
    ) -> bool:
        """插入一条事件；已存在（同 origin_host+ulid）跳过返回 False（幂等）。"""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO trace_events "
            "(origin_host, origin_kind, ulid, event, tags_json, extra_json, "
            "protocol_version, emitted_at, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                origin_host,
                origin_kind,
                ulid,
                event,
                json.dumps(tags, ensure_ascii=False),
                json.dumps(extra, ensure_ascii=False),
                protocol_version,
                emitted_at,
                received_at,
            ),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def query_events(
        self,
        *,
        origin_host: str | None = None,
        limit: int = 100,
        after_ulid: str = "",
    ) -> list[dict[str, Any]]:
        """按 (origin_host, ulid) 序查询；origin_host 缺省查全部来源（§3.3）。

        after_ulid 为游标（同 origin 内的 ULID 续读位）；origin_host 未指定时
        游标无意义（跨源无全局序），忽略。
        """
        if origin_host:
            rows = self._conn.execute(
                "SELECT origin_host, origin_kind, ulid, event, tags_json, extra_json, "
                "protocol_version, emitted_at, received_at "
                "FROM trace_events WHERE origin_host = ? AND ulid > ? "
                "ORDER BY origin_host, ulid LIMIT ?",
                (origin_host, after_ulid, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT origin_host, origin_kind, ulid, event, tags_json, extra_json, "
                "protocol_version, emitted_at, received_at "
                "FROM trace_events ORDER BY origin_host, ulid LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def count_events(self, origin_host: str | None = None) -> int:
        if origin_host:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM trace_events WHERE origin_host = ?", (origin_host,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM trace_events").fetchone()
        return int(row[0]) if row else 0

    # ── 死信面 ──────────────────────────────────────────────

    def insert_dead_letter(
        self, *, origin_host: str, reason: str, payload: dict[str, Any], received_at: str
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO trace_dead_letters (origin_host, reason, payload_json, received_at) "
            "VALUES (?, ?, ?, ?)",
            (origin_host, reason, json.dumps(payload, ensure_ascii=False), received_at),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def list_dead_letters(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, origin_host, reason, payload_json, received_at "
            "FROM trace_dead_letters ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "id": row[0],
                "origin_host": row[1],
                "reason": row[2],
                "payload": json.loads(row[3]),
                "received_at": row[4],
            }
            for row in rows
        ]

    def count_dead_letters(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM trace_dead_letters").fetchone()
        return int(row[0]) if row else 0


def _row_to_event(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "origin_host": row[0],
        "origin_kind": row[1],
        "ulid": row[2],
        "event": row[3],
        "tags": json.loads(row[4]),
        "extra": json.loads(row[5]),
        "protocol_version": row[6],
        "emitted_at": row[7],
        "received_at": row[8],
    }
