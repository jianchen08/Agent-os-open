"""sync_relay 云端中继存储 —— 事件账本 + 设备注册表 + 属主登记 + 死信。

数据面归属（ADR 2026-10-15-multi-device-sync-hlc-lww D6）：relay 同步账本 =
真值库（D1），为插件自有 SQLite（pim/trace_ingestion data/ 自持先例），
**不镜像、不复用内核 SQLite 主账本文件**；设备注册表 = 2c 路由唯一输入（D7）
+ 在链存在性判定数据源（D1）。

表设计：
- ``sync_events`` —— 经共享单点 :class:`~sync_event.SyncStore`（ULID 幂等 +
  到账序 cursor），同文件单连接（写锁竞争面收敛为一个连接）。
- ``sync_devices`` —— device_id 主键 + tier/版本/能力面 + 心跳时刻（在线 =
  心跳窗口内，D1 在链存在性）。
- ``sync_asset_owners`` —— 属主面登记（会话等属主 scoped 资产：首个创建
  事件的 device 即属主，他端写被拒，D2"属主设备只追加"的执法数据源）。
- ``sync_dead_letters`` —— 协议违约/顶名/属主违规事件的留痕面（不阻塞批次，
  trace_ingestion 先例；拒绝可审计，非静默销毁）。
- ``sync_device_tokens`` —— 登录直签发（ADR 2026-11-02-login-as-enrollment）
  的 per-device token 绑定（P6：与 env 预置绑定表同为鉴权数据源）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from _root_api import SyncStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_devices (
    device_id         TEXT PRIMARY KEY,
    tier              TEXT NOT NULL DEFAULT '',
    app_version       TEXT NOT NULL DEFAULT '',
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    registered_at     TEXT NOT NULL,
    last_heartbeat_at TEXT NOT NULL,
    address           TEXT NOT NULL DEFAULT '',
    storage_json      TEXT NOT NULL DEFAULT '',
    token_hash        TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sync_asset_owners (
    asset_key    TEXT PRIMARY KEY,
    owner_device TEXT NOT NULL,
    claimed_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_dead_letters (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    reason       TEXT NOT NULL,
    ulid         TEXT NOT NULL DEFAULT '',
    device_id    TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    received_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_device_tokens (
    device_id   TEXT PRIMARY KEY,
    token       TEXT NOT NULL,
    tenant_id   TEXT NOT NULL DEFAULT '',
    issued_at   TEXT NOT NULL
);
"""


class RelayStore:
    """relay 自有库：事件账本（SyncStore 组合）+ 注册表 + 属主 + 死信。"""

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()
        self.events = SyncStore(connection=self._conn)

    def _migrate(self) -> None:
        """存量库补列（CREATE TABLE IF NOT EXISTS 不改旧表）；新库为空操作。"""
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(sync_devices)")}
        if "address" not in cols:
            self._conn.execute(
                "ALTER TABLE sync_devices ADD COLUMN address TEXT NOT NULL DEFAULT ''"
            )
        if "storage_json" not in cols:
            self._conn.execute(
                "ALTER TABLE sync_devices ADD COLUMN storage_json TEXT NOT NULL DEFAULT ''"
            )
        if "token_hash" not in cols:
            self._conn.execute(
                "ALTER TABLE sync_devices ADD COLUMN token_hash TEXT NOT NULL DEFAULT ''"
            )

    def close(self) -> None:
        self._conn.close()

    # ── 设备注册表（D7：2c 路由唯一输入；D1：在链存在性数据源）────

    def upsert_device(
        self,
        *,
        device_id: str,
        tier: str,
        app_version: str,
        capabilities: dict[str, Any],
        now_iso: str,
        address: str | None = None,
        storage: dict[str, Any] | None = None,
    ) -> None:
        """注册/更新设备（注册与心跳共用 upsert；address/storage 随上报刷新）。

        address/storage 为 None（上报方未携带，如版本偏斜的旧 agent）时保留
        原值——字段只增不删，老端心跳不清空新端地址。
        """
        if address is None or storage is None:
            existing = self.get_device(device_id) or {}
            if address is None:
                address = str(existing.get("address") or "")
            if storage is None:
                storage = existing.get("storage")
        self._conn.execute(
            "INSERT INTO sync_devices "
            "(device_id, tier, app_version, capabilities_json, registered_at, last_heartbeat_at, "
            "address, storage_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET "
            "tier=excluded.tier, app_version=excluded.app_version, "
            "capabilities_json=excluded.capabilities_json, last_heartbeat_at=excluded.last_heartbeat_at, "
            "address=excluded.address, storage_json=excluded.storage_json",
            (
                device_id,
                tier,
                app_version,
                json.dumps(capabilities, ensure_ascii=False),
                now_iso,
                now_iso,
                address,
                json.dumps(storage, ensure_ascii=False) if storage is not None else "",
            ),
        )
        self._conn.commit()

    def touch_device(self, *, device_id: str, now_iso: str) -> bool:
        """心跳 touch；设备未注册返回 False（fail-closed，须先注册）。"""
        cur = self._conn.execute(
            "UPDATE sync_devices SET last_heartbeat_at = ? WHERE device_id = ?",
            (now_iso, device_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def set_device_token_hash(self, *, device_id: str, token_hash: str) -> None:
        """登记对等信任哈希（注册表复制的分发面，ADR
        2026-10-22-distributed-registry-peer-trust）：兑换签发即落 + 心跳/注册
        随行自愈回填（存量行/换绑 token 均收敛）。"""
        self._conn.execute(
            "UPDATE sync_devices SET token_hash = ? WHERE device_id = ?",
            (token_hash, device_id),
        )
        self._conn.commit()

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT device_id, tier, app_version, capabilities_json, registered_at, "
            "last_heartbeat_at, address, storage_json, token_hash FROM sync_devices "
            "WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        return None if row is None else _device_row(row)

    def list_devices(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT device_id, tier, app_version, capabilities_json, registered_at, "
            "last_heartbeat_at, address, storage_json, token_hash FROM sync_devices "
            "ORDER BY device_id"
        ).fetchall()
        return [_device_row(row) for row in rows]

    # ── 登录直签发 token 绑定（登录即入网，ADR 2026-11-02-login-as-enrollment）──

    def upsert_device_token(
        self, *, device_id: str, token: str, tenant_id: str, issued_at: str
    ) -> None:
        """登记签发的 per-device token 绑定（同设备重签顶旧，重入网即换发）。"""
        self._conn.execute(
            "INSERT INTO sync_device_tokens (device_id, token, tenant_id, issued_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET "
            "token=excluded.token, tenant_id=excluded.tenant_id, issued_at=excluded.issued_at",
            (device_id, token, tenant_id, issued_at),
        )
        self._conn.commit()

    def list_device_tokens(self) -> dict[str, str]:
        """device→token 绑定表（TokenAuth 动态源；env 预置表之外的签发面）。"""
        rows = self._conn.execute(
            "SELECT device_id, token FROM sync_device_tokens"
        ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    # ── 属主登记（D2：属主面只追加的执法数据源）────────────────

    def claim_owner(self, *, asset_key: str, device_id: str, now_iso: str) -> bool:
        """首个事件设备成为属主；已被他端占有的键返回 False。"""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO sync_asset_owners (asset_key, owner_device, claimed_at) "
            "VALUES (?, ?, ?)",
            (asset_key, device_id, now_iso),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get_owner(self, asset_key: str) -> str | None:
        row = self._conn.execute(
            "SELECT owner_device FROM sync_asset_owners WHERE asset_key = ?", (asset_key,)
        ).fetchone()
        return None if row is None else str(row[0])

    # ── 死信（协议违约/顶名/属主违规留痕，不阻塞批次）──────────

    def dead_letter(
        self, *, reason: str, event: dict[str, Any], received_at: str
    ) -> None:
        self._conn.execute(
            "INSERT INTO sync_dead_letters (reason, ulid, device_id, payload_json, received_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                reason,
                str(event.get("ulid", "")),
                str(event.get("device_id", "")),
                json.dumps(event, ensure_ascii=False),
                received_at,
            ),
        )
        self._conn.commit()

    def list_dead_letters(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, reason, ulid, device_id, payload_json, received_at "
            "FROM sync_dead_letters ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "id": row[0],
                "reason": row[1],
                "ulid": row[2],
                "device_id": row[3],
                "payload": json.loads(row[4]),
                "received_at": row[5],
            }
            for row in rows
        ]


def _device_row(row: tuple[Any, ...]) -> dict[str, Any]:
    storage_json = str(row[7] or "")
    return {
        "device_id": row[0],
        "tier": row[1],
        "app_version": row[2],
        "capabilities": json.loads(row[3]),
        "registered_at": row[4],
        "last_heartbeat_at": row[5],
        "address": str(row[6] or ""),
        "storage": json.loads(storage_json) if storage_json else None,
        "token_hash": str(row[8] or ""),
    }
