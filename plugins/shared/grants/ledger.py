"""grants 授权账本（SQLite）——中心方案「四件固定资产」之授权账本的载体。

设计真值：
- ADR 2026-11-19-grants-ledger-and-tool-classification（D1 账本独立系统插件、D2 未分类 fail-closed）；
- docs/working/grants账本执行方案_20261119.md §1（方法面）/§7（DDL v1）。

本批（P0）不动任何执法路径：账本无调用方（P1 才接线 security_check）。

落点：``<user_root>/data/grants/grants.db``——用户数据根解析复用 security_check
permission_modes 表的同构机制（``_root_api.user_data_dir`` 优先，回落配置声明
``grants_db_file``，再回落共享根 ``data/`` dev 姿态），禁硬编码绝对路径；
单测经 ``db_path`` 显式注入（tmp_path）。

事件审计：issue/consume/deny/revoke/expire 五类，追加写 grant_events。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: 行记录别名（模块级：类内方法名 ``list`` 会遮蔽内建 ``list``，类内注解走此别名）。
GrantRecord = dict[str, Any]
GrantList = list[GrantRecord]
EventRecord = dict[str, Any]
EventList = list[EventRecord]

# ── 闭集枚举（DDL 注释即契约，执行方案 §7）────────────────────────────────────
SUBJECT_KINDS = ("user", "agent", "task", "pipeline")
GRANULARITIES = ("once", "task", "standing", "domain")  # v1 四档（ADR D2）
SOURCES = ("approval_card", "user_rule", "oauth_broker", "proto_projection")
EVENT_TYPES = ("issue", "consume", "deny", "revoke", "expire")
STATUSES = ("active", "consumed", "revoked", "expired")

# ── mandate 契约（P2，执行方案 §4；消费方 = 跨端委派 delegation，尚未立项）──────
#: mandate 四要素校验失败原因闭集（``authorize`` 返回的 ``mandate.reason`` 值域）。
#: 验签（mandate 出处可信性）留给跨端执行面（delegation 插件，本批只落契约位 +
#: 测试桩，不实现远端）；本面只做可本地判定的形态与绑定校验。
MANDATE_REASONS = (
    "mandate_malformed",    # mandate 非对象
    "mandate_absent",       # require_mandate 声明强制但未携带 mandate
    "no_grant",             # 账本无命中 grant（无 grant 不放行——跨端上线顺序约束原文）
    "scope_invalid",        # scope 缺失/非对象/空
    "scope_exceeds_grant",  # mandate scope 超出命中 grant 的 scope（逐键 ⊆ 校验）
    "expires_missing",      # 时限缺失/空
    "expires_unparseable",  # 时限不可解析（fail-closed 视同无效）
    "mandate_expired",      # 时限已过
    "task_id_missing",      # task_id 缺失/空
    "task_id_mismatch",     # 与调用方声明的执行任务 id 不符（绑定校验）
    "not_revocable",        # revocable 非 True（不可撤销的 mandate 一律拒）
)


def validate_mandate(
    mandate: Any,
    *,
    grant: GrantRecord | None,
    task_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """跨端 mandate 四要素校验（scope/时限/task_id/可撤销）——契约面 v1。

    校验序（先到先拒）：形态 → scope（非空对象且逐键 ⊆ grant.scope）→ 时限
    （必填、可解析、未过期）→ task_id（必填；调用方声明 ``task_id`` 时逐字相符）
    → 可撤销（恒须 ``revocable is True``）。

    Returns:
        ``{"valid": bool, "reason": str | None}``（reason ∈ :data:`MANDATE_REASONS`）。
    """
    result: dict[str, Any] = {"valid": False, "reason": None}
    if not isinstance(mandate, dict):
        result["reason"] = "mandate_malformed"
        return result
    scope = mandate.get("scope")
    if not isinstance(scope, dict) or not scope:
        result["reason"] = "scope_invalid"
        return result
    grant_scope = grant.get("scope") if isinstance(grant, dict) else None
    grant_scope = grant_scope if isinstance(grant_scope, dict) else {}
    for key, value in scope.items():
        if key not in grant_scope or grant_scope[key] != value:
            result["reason"] = "scope_exceeds_grant"
            return result
    expires_at = mandate.get("expires_at")
    if not isinstance(expires_at, str) or not expires_at.strip():
        result["reason"] = "expires_missing"
        return result
    parsed = _parse_ts(expires_at)
    if parsed is None:
        result["reason"] = "expires_unparseable"
        return result
    if parsed <= (now or datetime.now(UTC)):
        result["reason"] = "mandate_expired"
        return result
    mandate_task = mandate.get("task_id")
    if not isinstance(mandate_task, str) or not mandate_task.strip():
        result["reason"] = "task_id_missing"
        return result
    if task_id is not None and mandate_task != task_id:
        result["reason"] = "task_id_mismatch"
        return result
    if mandate.get("revocable") is not True:
        result["reason"] = "not_revocable"
        return result
    result["valid"] = True
    return result


# DDL v1（执行方案 §7 原样，IF NOT EXISTS 仅为幂等 ensure_schema）。
_DDL = (
    """
    CREATE TABLE IF NOT EXISTS grants (
      grant_id       TEXT PRIMARY KEY,
      subject_kind   TEXT NOT NULL,     -- user|agent|task|pipeline
      subject_id     TEXT NOT NULL,
      tool           TEXT NOT NULL,     -- '*' 允许
      action         TEXT NOT NULL,     -- '*' 允许
      scope_json     TEXT NOT NULL,     -- {resource_pattern?, domain?, ...}
      granularity    TEXT NOT NULL,     -- once|task|standing|domain（v1 四档）
      status         TEXT NOT NULL,     -- active|consumed|revoked|expired
      max_uses       INTEGER,
      uses           INTEGER NOT NULL DEFAULT 0,
      created_at     TEXT NOT NULL,
      expires_at     TEXT,
      source         TEXT NOT NULL,     -- approval_card|user_rule|oauth_broker|proto_projection
      evidence       TEXT,              -- 审批卡 id / 审计事件指针
      pipeline_id    TEXT,
      parent_task_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS grant_events (
      event_id    TEXT PRIMARY KEY,
      grant_id    TEXT,
      event_type  TEXT NOT NULL,        -- issue|consume|deny|revoke|expire
      ts          TEXT NOT NULL,
      context_json TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_grants_subject ON grants(subject_kind, subject_id, tool, action, status)",
)


class GrantsLedgerError(Exception):
    """账本操作违契约（未知 grant / 闭集枚举外取值 / 状态机非法迁移）。"""


def resolve_db_path(settings: dict[str, Any] | None = None) -> str:
    """账本落点解析——与 security_check ``_resolve_permission_modes_file`` 同构。

    用户数据根优先（``<user_data_dir>/grants/grants.db``）；不可用回落 config
    注入 settings 的 ``grants_db_file``；再回落共享根 ``data/grants/``（dev
    姿态历史缺省形态，装机形态恒有用户数据根）。
    """
    try:
        from _root_api import user_data_dir  # noqa: PLC0415

        data_dir = user_data_dir()
        if data_dir:
            return str(Path(data_dir) / "grants" / "grants.db")
    except ImportError:
        pass
    configured = str((settings or {}).get("grants_db_file") or "").strip()
    if configured:
        return configured
    # 共享根回落（dev 姿态）：plugins/shared/grants/ 的上级共享根 + data/grants/
    shared_root = Path(__file__).resolve().parents[1]
    return str(shared_root / "grants" / "data" / "grants" / "grants.db")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_ts(value: str) -> datetime | None:
    """ISO 8601 解析；'Z' 后缀归一。不可解析返回 None（调用方 fail-closed）。"""
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _is_expired(expires_at: str | None, now: datetime) -> bool:
    """到期判定：空 = 永不过期；不可解析 = fail-closed（视同已过期）。"""
    if not expires_at:
        return False
    parsed = _parse_ts(expires_at)
    if parsed is None:
        return True
    return parsed <= now


class GrantsLedger:
    """授权账本（SQLite）。每个方法独立短连接（跨线程安全；写面 SQLite 自串行）。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = str(db_path) if db_path is not None else resolve_db_path()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            # WAL：sidecar 服务与内核 HTTP 面可能跨进程并发读；写面仍单写者串行。
            conn.execute("PRAGMA journal_mode=WAL")
            for stmt in _DDL:
                conn.execute(stmt)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── 授出面（request = 授出 grant；调用方为授权决定面：P1 审批卡授予 /
    #    用户规则面 / oauth_broker。服务名单无 issue，request 即授出。）──────
    def request(
        self,
        *,
        subject_kind: str,
        subject_id: str,
        tool: str,
        action: str,
        granularity: str,
        source: str,
        scope: dict[str, Any] | None = None,
        max_uses: int | None = None,
        expires_at: str | None = None,
        evidence: str | None = None,
        pipeline_id: str | None = None,
        parent_task_id: str | None = None,
    ) -> str:
        if subject_kind not in SUBJECT_KINDS:
            raise GrantsLedgerError(f"unknown subject_kind: {subject_kind}")
        if granularity not in GRANULARITIES:
            raise GrantsLedgerError(f"unknown granularity: {granularity}")
        if source not in SOURCES:
            raise GrantsLedgerError(f"unknown source: {source}")
        if scope is not None and not isinstance(scope, dict):
            raise GrantsLedgerError("scope must be an object")
        if expires_at is not None and _parse_ts(expires_at) is None:
            raise GrantsLedgerError(f"unparseable expires_at: {expires_at}")
        grant_id = uuid.uuid4().hex
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO grants (grant_id, subject_kind, subject_id, tool, action, scope_json,"
                " granularity, status, max_uses, uses, created_at, expires_at, source, evidence,"
                " pipeline_id, parent_task_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, 0, ?, ?, ?, ?, ?, ?)",
                (
                    grant_id,
                    subject_kind,
                    subject_id,
                    tool,
                    action,
                    json.dumps(scope or {}, ensure_ascii=False),
                    granularity,
                    max_uses,
                    _now_iso(),
                    expires_at,
                    source,
                    evidence,
                    pipeline_id,
                    parent_task_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        self.append_event(event_type="issue", grant_id=grant_id, context={"source": source, "granularity": granularity})
        return grant_id

    # ── 查询命中面（P1 security_check 特权类调用入口）────────────────────────
    def authorize(self, *, subject_kind: str, subject_id: str, tool: str, action: str) -> dict[str, Any] | None:
        """按主体 + 工具 + 动作查活跃 grant；命中返回最具体一条，未命中返回 None。

        匹配：主体精确（kind+id）；grant 侧 tool/action 支持 '*' 通配；精确键
        优先于通配。到期/不可解析时限的行在读时懒惰过期（status→expired + 事件）。

        mandate 契约位（P2）在服务面承载（server.py ``grants.ledger.authorize``
        增 mandate/require_mandate/task_id 三参 + 四要素校验报告）；本 raw 方法
        返回形态保持 P0 原样（grant dict | None），P0/P1 消费方零感知。
        """
        now = datetime.now(UTC)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM grants WHERE subject_kind = ? AND subject_id = ? AND status = 'active'"
                " AND tool IN (?, '*') AND action IN (?, '*')",
                (subject_kind, subject_id, tool, action),
            ).fetchall()
        finally:
            conn.close()
        candidates: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            grant = self._row_to_grant(row)
            if _is_expired(grant["expires_at"], now):
                self._mark_expired(grant["grant_id"], now)
                continue
            specificity = (1 if grant["tool"] == tool else 0) + (1 if grant["action"] == action else 0)
            candidates.append((specificity, grant))
        if not candidates:
            return None
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        return candidates[0][1]

    def consume(self, *, grant_id: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """记账一次使用（uses+1）；达 max_uses 即置 consumed（once 语义）。"""
        new_uses = 0
        new_status = "active"
        grant: dict[str, Any] | None = None
        expired = False
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()
            if row is None:
                raise GrantsLedgerError(f"unknown grant: {grant_id}")
            grant = self._row_to_grant(row)
            if grant["status"] != "active":
                raise GrantsLedgerError(f"grant not active: {grant_id} (status={grant['status']})")
            if _is_expired(grant["expires_at"], datetime.now(UTC)):
                expired = True
                conn.execute("UPDATE grants SET status = 'expired' WHERE grant_id = ?", (grant_id,))
                conn.commit()
            else:
                new_uses = int(grant["uses"]) + 1
                max_uses = grant["max_uses"]
                if max_uses is not None and new_uses >= int(max_uses):
                    new_status = "consumed"
                conn.execute(
                    "UPDATE grants SET uses = ?, status = ? WHERE grant_id = ?", (new_uses, new_status, grant_id)
                )
                conn.commit()
        finally:
            conn.close()
        if expired:
            self.append_event(event_type="expire", grant_id=grant_id)
            raise GrantsLedgerError(f"grant expired: {grant_id}")
        assert grant is not None
        self.append_event(event_type="consume", grant_id=grant_id, context=context)
        return {**grant, "uses": new_uses, "status": new_status}

    def revoke(self, *, grant_id: str, context: dict[str, Any] | None = None) -> bool:
        """撤销活跃 grant（幂等：未知/已撤销 = False）。撤销即时生效（下次 authorize 未命中）。"""
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE grants SET status = 'revoked' WHERE grant_id = ? AND status = 'active'", (grant_id,)
            )
            conn.commit()
            changed = cursor.rowcount > 0
        finally:
            conn.close()
        if changed:
            self.append_event(event_type="revoke", grant_id=grant_id, context=context)
        return changed

    def list(
        self,
        *,
        subject_kind: str | None = None,
        subject_id: str | None = None,
        tool: str | None = None,
        status: str | None = None,
    ) -> GrantList:
        """按过滤器列 grant（查询面先做全表到期清扫，保证 status 过滤准确）。"""
        self.expire_due()
        clauses: list[str] = []
        params: list[str] = []
        if subject_kind is not None:
            clauses.append("subject_kind = ?")
            params.append(subject_kind)
        if subject_id is not None:
            clauses.append("subject_id = ?")
            params.append(subject_id)
        if tool is not None:
            clauses.append("tool = ?")
            params.append(tool)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM grants"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, grant_id"
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [self._row_to_grant(row) for row in rows]

    def expire_due(self, now: datetime | None = None) -> int:
        """到期清扫：active 且 expires_at 已到（或不可解析）→ expired + 事件；返回数量。"""
        now = now or datetime.now(UTC)
        conn = self._connect()
        try:
            rows = conn.execute("SELECT grant_id, expires_at FROM grants WHERE status = 'active'").fetchall()
        finally:
            conn.close()
        count = 0
        for row in rows:
            if _is_expired(row["expires_at"], now):
                self._mark_expired(row["grant_id"], now)
                count += 1
        return count

    def _mark_expired(self, grant_id: str, now: datetime) -> None:
        conn = self._connect()
        try:
            conn.execute("UPDATE grants SET status = 'expired' WHERE grant_id = ? AND status = 'active'", (grant_id,))
            conn.commit()
        finally:
            conn.close()
        self.append_event(event_type="expire", grant_id=grant_id, context={"expired_at": now.isoformat()})

    # ── 事件审计（追加写；deny 由 policy.classify 未分类路径产生）────────────
    def append_event(
        self,
        *,
        event_type: str,
        grant_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> str:
        if event_type not in EVENT_TYPES:
            raise GrantsLedgerError(f"unknown event_type: {event_type}")
        event_id = uuid.uuid4().hex
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO grant_events (event_id, grant_id, event_type, ts, context_json) VALUES (?, ?, ?, ?, ?)",
                (
                    event_id,
                    grant_id,
                    event_type,
                    _now_iso(),
                    json.dumps(context, ensure_ascii=False) if context is not None else None,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return event_id

    def events(
        self, *, grant_id: str | None = None, event_type: str | None = None
    ) -> EventList:
        clauses: list[str] = []
        params: list[str] = []
        if grant_id is not None:
            clauses.append("grant_id = ?")
            params.append(grant_id)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        sql = "SELECT * FROM grant_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts, event_id"
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [
            {
                "event_id": row["event_id"],
                "grant_id": row["grant_id"],
                "event_type": row["event_type"],
                "ts": row["ts"],
                "context": json.loads(row["context_json"]) if row["context_json"] else None,
            }
            for row in rows
        ]

    @staticmethod
    def _row_to_grant(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "grant_id": row["grant_id"],
            "subject_kind": row["subject_kind"],
            "subject_id": row["subject_id"],
            "tool": row["tool"],
            "action": row["action"],
            "scope": json.loads(row["scope_json"]) if row["scope_json"] else {},
            "granularity": row["granularity"],
            "status": row["status"],
            "max_uses": row["max_uses"],
            "uses": row["uses"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "source": row["source"],
            "evidence": row["evidence"],
            "pipeline_id": row["pipeline_id"],
            "parent_task_id": row["parent_task_id"],
        }
