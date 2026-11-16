"""sync_event 事件模型 + 同步账本存储 —— 多端同步的三键契约单点实现。

契约：ADR ``docs/decisions/2026-10-15-multi-device-sync-hlc-lww.md`` D3（E 通道
模式泛化），规格 = ``tests/test_mobile_gm1_contracts.py::TestSyncEventContract``
（G-M1.4，9 条）。三键分工：

- **ULID** 主键——幂等去重（at-least-once 传输收敛为 exactly-once 效果）；
- **HLC** 时间戳（可选字段，裁决依据）——"两件事谁后"的全序可比戳，落败旧值
  留账本可查；缺 HLC 的事件过不了 merge 层（裸墙钟 LWW 已被 ADR 否）；
- **cursor**（存储层按到账序发号）——拉取窗口与断点续传序，"我拉到哪了"。

schema 五必填（缺一即拒，fail-closed）：``ulid / device_id / asset_type /
version / updated_at``。

导入形态与 ``ulid_keys`` 先例一致：插件侧把 ``plugins/shared`` 推上 sys.path
后裸名导入。存储为插件自有 SQLite（pim/trace_ingestion data/ 自持先例，
内核零改动）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hlc_clock import HlcTimestamp
from time_iso import now_iso_utc

__all__ = [
    "ASSET_CHANNELS",
    "CHANNEL_OWNER_SCOPED",
    "REQUIRED_FIELDS",
    "SyncEvent",
    "SyncStore",
    "asset_channel",
    "asset_key_of",
    "is_owner_scoped",
    "validate_event",
]

#: 五必填（G-M1.4 契约：缺一即拒）。
REQUIRED_FIELDS = ("ulid", "device_id", "asset_type", "version", "updated_at")

#: 通道注册表（ADR 2026-10-17-sync-two-channel-routing D2，路由中间件单点）：
#: 每个可同步资产类型声明所属通道；agent（capture/apply 路由）与 relay（属主
#: 执法）同源消费，不许两端各写词表。未注册类型按共享面处理（2b 既有行为）。
#: 通道语义（通道层通用属性，ADR 同文 D1）：file = 共享面（HLC-LWW；T2 起
#: plugins/skills/artifact_index 以 git 仓库化为 file 通道实现形态，拍板③b，
#: config yaml 面为 git 化前过渡形态）；db = 属主面（只追加、他端只读镜像）。
#: 预留：``backup`` 类型入口（T6 手动备份，ADR 同文"裁定补充"④）——通道
#: 归属随 T6 范围裁定后入表，本刀不占名不实现。
ASSET_CHANNELS: dict[str, str] = {
    "config": "file",
    "plugins": "file",
    "skills": "file",
    "artifact_index": "file",
    "session": "db",
    "message": "db",
    "task": "db",
}

#: 通道语义（通道层通用属性，不随资产类型增殖）：
#: file = 共享面（无属主，HLC-LWW 后改赢）；db = 属主面（只追加、他端只读镜像）。
CHANNEL_OWNER_SCOPED: dict[str, bool] = {"file": False, "db": True}


def asset_channel(asset_type: str) -> str:
    """资产类型 → 通道名；未注册返回空串（按共享面处理）。"""
    return ASSET_CHANNELS.get(asset_type, "")


def is_owner_scoped(asset_type: str) -> bool:
    """资产类型是否属主 scoped（relay 属主执法与端侧镜像语义的唯一判定源）。"""
    return CHANNEL_OWNER_SCOPED.get(asset_channel(asset_type), False)

#: ULID 词表（Crockford base32，去易混字符）。
_CROCKFORD = frozenset("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


@dataclass(frozen=True)
class SyncEvent:
    """一条同步事件（不可变值对象；cursor 由存储层发号）。"""

    ulid: str
    device_id: str
    asset_type: str
    version: int
    updated_at: str
    asset_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    hlc: HlcTimestamp | None = None
    cursor: int = 0

    @property
    def asset_key(self) -> str:
        """资产身份键（裁决与属主登记的粒度）。"""
        return asset_key_of(self.asset_type, self.asset_id)

    def to_wire(self) -> dict[str, Any]:
        """线格式（上行信封 events[] 成员；不含存储层 cursor）。"""
        wire: dict[str, Any] = {
            "ulid": self.ulid,
            "device_id": self.device_id,
            "asset_type": self.asset_type,
            "version": self.version,
            "updated_at": self.updated_at,
            "payload": self.payload,
        }
        if self.asset_id:
            wire["asset_id"] = self.asset_id
        if self.hlc is not None:
            wire["hlc"] = self.hlc.to_dict()
        return wire


def asset_key_of(asset_type: str, asset_id: str) -> str:
    return f"{asset_type}/{asset_id}" if asset_id else asset_type


def validate_event(raw: Mapping[str, Any]) -> SyncEvent:
    """schema 校验：五必填缺一即拒 + 字段类型违约即拒（ValueError 族）。

    hlc 为可选字段（协议宽容，trace_ingestion 先例）：携带时结构校验，缺失时
    事件仍可入账本——但 merge/裁决层拒收无 HLC 事件（裸墙钟 LWW 被否，ADR D2），
    现役唯一生产者 sync_agent 对每条上行事件恒打 HLC。
    """
    if not isinstance(raw, Mapping):
        raise ValueError("sync_event 必须是对象")
    missing = [name for name in REQUIRED_FIELDS if raw.get(name) is None]
    if missing:
        raise ValueError(f"sync_event 缺少必填字段: {', '.join(missing)}")

    ulid = raw["ulid"]
    if not isinstance(ulid, str) or len(ulid) != 26 or not set(ulid) <= _CROCKFORD:
        raise ValueError("ulid 必须是 26 位大写 Crockford Base32")

    device_id = raw["device_id"]
    if not isinstance(device_id, str) or not device_id:
        raise ValueError("device_id 必须是非空字符串")

    asset_type = raw["asset_type"]
    if not isinstance(asset_type, str) or not asset_type:
        raise ValueError("asset_type 必须是非空字符串")

    version = raw["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ValueError("version 必须是非负整数")

    updated_at = raw["updated_at"]
    if not isinstance(updated_at, str) or not updated_at:
        raise ValueError("updated_at 必须是非空字符串")

    asset_id = raw.get("asset_id", "")
    if not isinstance(asset_id, str):
        raise ValueError("asset_id 必须是字符串")

    payload = raw.get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是对象")

    raw_hlc = raw.get("hlc")
    hlc = None if raw_hlc is None else HlcTimestamp.from_dict(raw_hlc)

    cursor = raw.get("cursor", 0)
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise ValueError("cursor 必须是非负整数")

    return SyncEvent(
        ulid=ulid,
        device_id=device_id,
        asset_type=asset_type,
        asset_id=asset_id,
        version=version,
        updated_at=updated_at,
        payload=payload,
        hlc=hlc,
        cursor=cursor,
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    ulid         TEXT NOT NULL UNIQUE,
    device_id    TEXT NOT NULL,
    asset_type   TEXT NOT NULL,
    asset_id     TEXT NOT NULL DEFAULT '',
    version      INTEGER NOT NULL,
    updated_at   TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    hlc_physical INTEGER,
    hlc_logical  INTEGER,
    hlc_device   TEXT,
    received_at  TEXT NOT NULL DEFAULT ''
);
"""


class SyncStore:
    """同步事件账本（SQLite；ULID 幂等 + 到账序 cursor 发号）。

    Args:
        db_path: 库文件路径；``None`` = 内存库（测试/一次性账本）。
        connection: 既有连接注入（宿主自有库共享单连接，避免同文件多连接
            写锁竞争——relay_store 组合消费用）；给出时忽略 ``db_path``。
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if connection is not None:
            self._conn = connection
        elif db_path is None:
            self._conn = sqlite3.connect(":memory:")
        else:
            path = Path(db_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def upsert(self, raw: Mapping[str, Any], *, received_at: str = "") -> bool:
        """写入一条事件；schema 违约抛 ValueError（fail-closed）。

        同 ULID 重放跳过返回 False（幂等，断网续传重发零重复）；新入库分配
        到账序 cursor（AUTOINCREMENT，严格递增）返回 True。
        """
        event = validate_event(raw)
        hlc = event.hlc
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO sync_events "
            "(ulid, device_id, asset_type, asset_id, version, updated_at, "
            " payload_json, hlc_physical, hlc_logical, hlc_device, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.ulid,
                event.device_id,
                event.asset_type,
                event.asset_id,
                event.version,
                event.updated_at,
                _dumps(event.payload),
                None if hlc is None else hlc.physical_ms,
                None if hlc is None else hlc.logical,
                None if hlc is None else hlc.device_id,
                received_at or now_iso_utc(),
            ),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def pull(self, since_cursor: int, limit: int = 1000) -> list[SyncEvent]:
        """拉取 cursor 之后的事件：严格大于 since、按 cursor 升序（断点续传序）。"""
        rows = self._conn.execute(
            "SELECT seq, ulid, device_id, asset_type, asset_id, version, updated_at, "
            "payload_json, hlc_physical, hlc_logical, hlc_device "
            "FROM sync_events WHERE seq > ? ORDER BY seq LIMIT ?",
            (since_cursor, limit),
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def count_events(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM sync_events").fetchone()
        return int(row[0]) if row else 0

    def max_cursor(self) -> int:
        """当前账本头（游标发放位；空账本为 0）。"""
        row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM sync_events").fetchone()
        return int(row[0]) if row else 0

    def events_for_asset(self, asset_type: str, asset_id: str = "") -> list[SyncEvent]:
        """单资产的全部历史事件（到账序）——落败旧值可查（ADR D2）。"""
        rows = self._conn.execute(
            "SELECT seq, ulid, device_id, asset_type, asset_id, version, updated_at, "
            "payload_json, hlc_physical, hlc_logical, hlc_device "
            "FROM sync_events WHERE asset_type = ? AND asset_id = ? ORDER BY seq",
            (asset_type, asset_id),
        ).fetchall()
        return [_row_to_event(row) for row in rows]


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _row_to_event(row: tuple[Any, ...]) -> SyncEvent:
    hlc = None
    if row[8] is not None and row[9] is not None and row[10] is not None:
        hlc = HlcTimestamp(physical_ms=row[8], logical=row[9], device_id=row[10])
    return SyncEvent(
        cursor=row[0],
        ulid=row[1],
        device_id=row[2],
        asset_type=row[3],
        asset_id=row[4],
        version=row[5],
        updated_at=row[6],
        payload=json.loads(row[7]),
        hlc=hlc,
    )
