"""sync_relay 执行凭据分库 —— exec token 账户（双钥分离的存储承载，ADR D13'）。

双钥分离下沉到存储层：exec token（执行面凭据）与 sync token/事件账本
（relay.db）**分库**——单钥泄露不连带；token 明文只在本库与属主端验证集
（sync_agent 侧 data/ 永不同步层）两处落地，不进注册表、不进同步事件面（P6）。

表设计：
- ``sync_exec_tokens`` —— 发起端签发的执行凭据绑定（token 主键 + 属主端
  device_id + 发起端 issued_by + 时效；短时效由签发面夹取，过期行由
  :meth:`ExecTokenStore.prune` 物理清理，验证集查询恒按注入时钟过滤）。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_exec_tokens (
    token      TEXT PRIMARY KEY,
    device_id  TEXT NOT NULL,
    issued_by  TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sync_exec_tokens_device
    ON sync_exec_tokens(device_id);
"""


class ExecTokenStore:
    """exec token 账户（独立 SQLite：签发登记 / 属主凭据清单 / 过期清理）。"""

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def insert(
        self,
        *,
        token: str,
        device_id: str,
        issued_by: str,
        created_at: str,
        expires_at: str,
    ) -> None:
        """登记一条签发绑定（同 token 重签幂等覆盖——发起端重试安全）。"""
        self._conn.execute(
            "INSERT INTO sync_exec_tokens "
            "(token, device_id, issued_by, created_at, expires_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(token) DO UPDATE SET "
            "device_id=excluded.device_id, issued_by=excluded.issued_by, "
            "created_at=excluded.created_at, expires_at=excluded.expires_at",
            (token, device_id, issued_by, created_at, expires_at),
        )
        self._conn.commit()

    def list_for(self, device_id: str) -> list[dict[str, str]]:
        """属主端名下全部凭据绑定（时效语义归逻辑面按注入时钟裁决）。"""
        rows = self._conn.execute(
            "SELECT token, issued_by, expires_at FROM sync_exec_tokens "
            "WHERE device_id = ? ORDER BY expires_at, token",
            (device_id,),
        ).fetchall()
        return [
            {"token": str(row[0]), "issued_by": str(row[1]), "expires_at": str(row[2])}
            for row in rows
        ]

    def prune(self, now: datetime) -> int:
        """过期凭据物理清理（畸形时效行保留——fail-closed 可见，不静默销毁）。

        返回清理行数；验证集拉取时顺带执行（housekeeping）。
        """
        rows = self._conn.execute(
            "SELECT token, expires_at FROM sync_exec_tokens"
        ).fetchall()
        expired: list[str] = []
        for token, expires_at_raw in rows:
            try:
                expires_at = datetime.fromisoformat(str(expires_at_raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if now > expires_at:
                expired.append(str(token))
        if not expired:
            return 0
        cur = self._conn.executemany(
            "DELETE FROM sync_exec_tokens WHERE token = ?", [(t,) for t in expired]
        )
        self._conn.commit()
        return cur.rowcount
