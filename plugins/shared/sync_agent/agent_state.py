"""sync_agent 端侧状态库 —— HLC 状态持久化 + 资产裁决状态 + 已应用账本 + 游标
+ 配对凭据 + 捕获指纹账 + 他端只读镜像（T1 DB 逻辑记录）。

每端 sync_agent 自持（ADR 2026-10-15-multi-device-sync-hlc-lww D6"本地 HLC
状态维护"），插件自有 SQLite（data/ 自持先例）：

- ``agent_clock`` —— HLC 状态单行持久化：跨重启单调续接（HLC 只进不退的
  硬前提，重启丢状态 = 时钟倒退 = LWW 误裁）。
- ``agent_assets`` —— 每资产键的裁决状态（本端已知最新 HLC/版本/内容指纹）：
  下行合并（HLC-LWW）的比较基准；本端捕获变更时同刀前移（他端更旧的
  事件到达即判 stale，本地新值不被覆盖）。
- ``agent_applied`` —— 已处理事件 ULID 账本：at-least-once 下行重放幂等。
- ``agent_cursor`` —— 拉取游标水位（"我拉到哪了"，断点续传序）。
- ``agent_credential`` —— 入网码配对流落下的凭据（relay_url/device_token/
  tenant）：**永不同步层**（D4——per-device token 不入同步面，P6），随本端
  data/ 私有；游标/资产/已应用账本按 relay 账本记账，换 relay 重配对即作废。
- ``capture_seen`` / ``capture_hot`` —— 捕获步的已捕获消息指纹账与热路径
  快照（T1：重启续接防重捕；热路径 = 长度+尾指纹未变即零扫描）。
- ``mirror_records`` —— 他端 DB 逻辑记录的**单一通用镜像表**（T1，两通道
  架构 ADR 2026-10-17-sync-two-channel-routing D3）：独立命名空间，与本端
  内核写面零冲突（不接管）；唯一写入者 = relay 属主执法后的 pull-apply
  路径；墓碑打标记不物理删除（"不删除"字面兑现）。行形态通用：
  asset_key 主键 + table/pk/row_json + 呈现维度（view_group/view_seq，消息
  改写按此收敛单行）+ HLC 三元组。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from _root_api import HlcTimestamp

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_clock (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    device_id   TEXT NOT NULL,
    physical_ms INTEGER NOT NULL,
    logical     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_cursor (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    watermark INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_assets (
    asset_key   TEXT PRIMARY KEY,
    hlc_physical INTEGER NOT NULL,
    hlc_logical  INTEGER NOT NULL,
    hlc_device   TEXT NOT NULL,
    version      INTEGER NOT NULL,
    content_hash TEXT NOT NULL DEFAULT '',
    ulid         TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS agent_applied (
    ulid       TEXT PRIMARY KEY,
    outcome    TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_credential (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capture_seen (
    pipeline_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    PRIMARY KEY (pipeline_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS capture_hot (
    pipeline_id TEXT PRIMARY KEY,
    msg_count   INTEGER NOT NULL,
    last_fp     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mirror_records (
    asset_key    TEXT PRIMARY KEY,
    table_name   TEXT NOT NULL,
    record_pk    TEXT NOT NULL,
    row_json     TEXT NOT NULL DEFAULT '{}',
    owner_device TEXT NOT NULL,
    deleted      INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL DEFAULT '',
    view_group   TEXT,
    view_seq     INTEGER,
    hlc_physical INTEGER NOT NULL,
    hlc_logical  INTEGER NOT NULL,
    hlc_device   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mirror_records_view
    ON mirror_records(view_group, view_seq);
CREATE INDEX IF NOT EXISTS idx_mirror_records_table
    ON mirror_records(table_name, view_group, view_seq);
"""


class AgentStateStore:
    """端侧同步状态（单文件 SQLite）。"""

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── HLC 状态（跨重启单调）────────────────────────────────

    def load_clock(self) -> tuple[str, tuple[int, int]] | None:
        """恢复 ``(device_id, (physical_ms, logical))``；无状态返回 None。"""
        row = self._conn.execute(
            "SELECT device_id, physical_ms, logical FROM agent_clock WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return (str(row[0]), (int(row[1]), int(row[2])))

    def save_clock(self, stamp: HlcTimestamp) -> None:
        self._conn.execute(
            "INSERT INTO agent_clock (id, device_id, physical_ms, logical) "
            "VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "device_id=excluded.device_id, physical_ms=excluded.physical_ms, "
            "logical=excluded.logical",
            (stamp.device_id, stamp.physical_ms, stamp.logical),
        )
        self._conn.commit()

    # ── 拉取游标水位 ─────────────────────────────────────────

    def load_cursor(self) -> int | None:
        """游标水位；未初始化（需要自举）返回 None。"""
        row = self._conn.execute("SELECT watermark FROM agent_cursor WHERE id = 1").fetchone()
        return None if row is None else int(row[0])

    def save_cursor(self, watermark: int) -> None:
        self._conn.execute(
            "INSERT INTO agent_cursor (id, watermark) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET watermark=excluded.watermark",
            (watermark,),
        )
        self._conn.commit()

    # ── 资产裁决状态 ─────────────────────────────────────────

    def get_asset(self, asset_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT hlc_physical, hlc_logical, hlc_device, version, content_hash, ulid "
            "FROM agent_assets WHERE asset_key = ?",
            (asset_key,),
        ).fetchone()
        if row is None:
            return None
        return {
            "hlc": HlcTimestamp(physical_ms=row[0], logical=row[1], device_id=row[2]),
            "version": row[3],
            "content_hash": row[4],
            "ulid": row[5],
        }

    def put_asset(
        self,
        *,
        asset_key: str,
        hlc: HlcTimestamp,
        version: int,
        content_hash: str,
        ulid: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO agent_assets "
            "(asset_key, hlc_physical, hlc_logical, hlc_device, version, content_hash, ulid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(asset_key) DO UPDATE SET "
            "hlc_physical=excluded.hlc_physical, hlc_logical=excluded.hlc_logical, "
            "hlc_device=excluded.hlc_device, version=excluded.version, "
            "content_hash=excluded.content_hash, ulid=excluded.ulid",
            (asset_key, hlc.physical_ms, hlc.logical, hlc.device_id, version, content_hash, ulid),
        )
        self._conn.commit()

    def asset_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM agent_assets").fetchone()
        return int(row[0]) if row else 0

    # ── 已应用账本（下行重放幂等）────────────────────────────

    def mark_applied(self, *, ulid: str, outcome: str, applied_at: str) -> bool:
        """登记已处理；首次登记返回 True（重复返回 False）。"""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO agent_applied (ulid, outcome, applied_at) VALUES (?, ?, ?)",
            (ulid, outcome, applied_at),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def applied_outcome(self, ulid: str) -> str | None:
        row = self._conn.execute(
            "SELECT outcome FROM agent_applied WHERE ulid = ?", (ulid,)
        ).fetchone()
        return None if row is None else str(row[0])

    # ── 配对凭据（入网码兑换落下；永不同步层）────────────────

    def save_credential(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO agent_credential (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    def load_credential(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM agent_credential WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row[0])

    def reset_pairing_state(self) -> None:
        """重配对清账：游标/资产/已应用/捕获账/镜像按 relay 账本记账，换账本即作废。

        HLC 时钟**不清**——单调性跨配对存续（清了 = 时钟倒退 = LWW 误裁）。
        捕获账清空后下次运行整管道重捕上行（新账本补历史；同资产内容寻址
        幂等，下游 HLC 收敛）；镜像清空后按新账本 cursor 重拉重建。
        作废后 needs_bootstrap() 转真，下拍走入网自举（注册+基线+游标定位）。
        """
        self._conn.execute("DELETE FROM agent_cursor")
        self._conn.execute("DELETE FROM agent_assets")
        self._conn.execute("DELETE FROM agent_applied")
        self._conn.execute("DELETE FROM capture_seen")
        self._conn.execute("DELETE FROM capture_hot")
        self._conn.execute("DELETE FROM mirror_records")
        self._conn.commit()

    # ── 捕获指纹账（T1 捕获步：重启续接防重捕）────────────────

    def seen_fingerprints(self, pipeline_id: str) -> set[str]:
        """管道已捕获消息指纹集（捕获 diff 基准）。"""
        rows = self._conn.execute(
            "SELECT fingerprint FROM capture_seen WHERE pipeline_id = ?", (pipeline_id,)
        ).fetchall()
        return {str(r[0]) for r in rows}

    def mark_seen_fingerprints(self, pipeline_id: str, fingerprints: set[str]) -> None:
        """登记已捕获指纹（幂等，INSERT OR IGNORE）。"""
        if not fingerprints:
            return
        self._conn.executemany(
            "INSERT OR IGNORE INTO capture_seen (pipeline_id, fingerprint) VALUES (?, ?)",
            [(pipeline_id, fp) for fp in fingerprints],
        )
        self._conn.commit()

    def capture_hot(self, pipeline_id: str) -> tuple[int, str] | None:
        """热路径快照 ``(消息条数, 尾指纹)``；无记录返回 None（首见须全扫）。"""
        row = self._conn.execute(
            "SELECT msg_count, last_fp FROM capture_hot WHERE pipeline_id = ?", (pipeline_id,)
        ).fetchone()
        return None if row is None else (int(row[0]), str(row[1]))

    def save_capture_hot(self, pipeline_id: str, msg_count: int, last_fp: str) -> None:
        self._conn.execute(
            "INSERT INTO capture_hot (pipeline_id, msg_count, last_fp) VALUES (?, ?, ?) "
            "ON CONFLICT(pipeline_id) DO UPDATE SET "
            "msg_count=excluded.msg_count, last_fp=excluded.last_fp",
            (pipeline_id, msg_count, last_fp),
        )
        self._conn.commit()

    # ── 他端只读镜像（T1：单一通用表；唯一写入者 = pull-apply 路径）──

    def upsert_mirror_record(
        self,
        *,
        asset_key: str,
        table_name: str,
        record_pk: str,
        row: dict[str, Any],
        owner_device: str,
        deleted: bool,
        updated_at: str,
        view: tuple[str, int] | None,
        hlc: HlcTimestamp,
    ) -> None:
        """通用镜像落地（apply 唯一写入口，无 per-type 分支）。

        - 同 ``asset_key``（会话/任务追加、墓碑）→ 整行更新；
        - 消息改写 = 新 asset_key 同呈现维度 (view_group, view_seq) → 先清
          该维度旧行再插入（呈现层零重复；事件账本仍全量可查）。
        """
        import json as _json  # noqa: PLC0415 —— 局部导入：行快照序列化

        row_json = _json.dumps(row, ensure_ascii=False)
        params = (
            asset_key, table_name, record_pk, row_json, owner_device,
            int(deleted), updated_at,
            None if view is None else view[0],
            None if view is None else view[1],
            hlc.physical_ms, hlc.logical, hlc.device_id,
        )
        if view is not None:
            self._conn.execute(
                "DELETE FROM mirror_records WHERE view_group = ? AND view_seq = ? "
                "AND asset_key != ?",
                (view[0], view[1], asset_key),
            )
        self._conn.execute(
            "INSERT OR REPLACE INTO mirror_records "
            "(asset_key, table_name, record_pk, row_json, owner_device, deleted, "
            " updated_at, view_group, view_seq, hlc_physical, hlc_logical, hlc_device) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            params,
        )
        self._conn.commit()

    def _mirror_hlc(self, row: tuple[Any, ...], offset: int) -> dict[str, Any]:
        return {
            "physical_ms": row[offset],
            "logical": row[offset + 1],
            "device_id": row[offset + 2],
        }

    def get_mirror_view_hlc(self, view_group: str, view_seq: int) -> HlcTimestamp | None:
        """呈现维度现存的镜像 HLC（view 收敛路径的乱序重放门槛——不同
        asset_key 的改写事件绕过资产级门槛，须按维度判）。"""
        row = self._conn.execute(
            "SELECT hlc_physical, hlc_logical, hlc_device FROM mirror_records "
            "WHERE view_group = ? AND view_seq = ?",
            (view_group, view_seq),
        ).fetchone()
        if row is None:
            return None
        return HlcTimestamp(physical_ms=row[0], logical=row[1], device_id=row[2])

    def list_mirror_records(self, table_name: str | None = None) -> list[dict[str, Any]]:
        """镜像记录（呈现序：表名 → 分组 → 序）；table_name 给定时按表过滤。"""
        import json as _json  # noqa: PLC0415 —— 局部导入：行快照解析

        sql = (
            "SELECT asset_key, table_name, record_pk, row_json, owner_device, deleted, "
            "updated_at, view_group, view_seq, hlc_physical, hlc_logical, hlc_device "
            "FROM mirror_records "
        )
        params: tuple[Any, ...] = ()
        if table_name:
            sql += "WHERE table_name = ? "
            params = (table_name,)
        sql += "ORDER BY table_name, view_group, view_seq"
        rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "asset_key": r[0],
                "table": r[1],
                "pk": r[2],
                "row": _json.loads(r[3]),
                "owner_device": r[4],
                "deleted": bool(r[5]),
                "updated_at": r[6],
                "view": None if r[7] is None else {"group": r[7], "seq": r[8]},
                "hlc": self._mirror_hlc(r, 9),
            }
            for r in rows
        ]
