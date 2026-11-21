# @feature: FP-0.2.grants_ledger 授权账本 | @ci: python-coverage
"""grants 授权账本（ledger.py）测试——API 契约由本文件定义（TDD Red 先行）。

完成判据 2（执行方案 §1）：五断言流程
issue → query 命中 → consume 计数 +1 → revoke 后 query 未命中 → expire 后 query 未命中。
另覆盖：事件审计、DDL 形状、用户数据根解析、通配/主体匹配、服务端点（server.py）。

设计真值：docs/decisions/2026-11-19-grants-ledger-and-tool-classification.md（D1/D2）
+ docs/working/grants账本执行方案_20261119.md §1/§7。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_ROOT = str(Path(__file__).resolve().parents[1])
for _p in (_SHARED_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ledger as grants_ledger  # noqa: E402 — 平铺插件模块（插件目录在 sys.path 首位）

if TYPE_CHECKING:
    from ledger import GrantsLedger  # 仅注解面类型名；运行时类对象经 grants_ledger 命名空间取

GrantsLedgerError = grants_ledger.GrantsLedgerError
resolve_db_path = grants_ledger.resolve_db_path


def _make_ledger(tmp_path: Path) -> GrantsLedger:
    return grants_ledger.GrantsLedger(db_path=Path(tmp_path) / "grants.db")


def _issue(led: GrantsLedger, **overrides: object) -> str:
    kwargs: dict = {
        "subject_kind": "agent",
        "subject_id": "agent-1",
        "tool": "memory",
        "action": "store",
        "granularity": "task",
        "source": "approval_card",
    }
    kwargs.update(overrides)
    return led.request(**kwargs)  # type: ignore[arg-type]


class TestGrantLifecycleFiveSteps:
    """完成判据 2：五断言流程（issue→命中→consume+1→revoke 未命中→expire 未命中）。"""

    def test_grant_lifecycle_five_steps(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)

        # ① issue → ② query 命中
        grant_id = _issue(led)
        hit = led.authorize(subject_kind="agent", subject_id="agent-1", tool="memory", action="store")
        assert hit is not None
        assert hit["grant_id"] == grant_id

        # ③ consume 计数 +1
        consumed = led.consume(grant_id=grant_id)
        assert consumed["uses"] == 1

        # ④ revoke 后 query 未命中
        assert led.revoke(grant_id=grant_id) is True
        assert led.authorize(subject_kind="agent", subject_id="agent-1", tool="memory", action="store") is None

        # ⑤ expire 后 query 未命中（过去时限 grant 经公开 API 直发，读时懒惰过期）
        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        _issue(led, expires_at=past)
        assert led.authorize(subject_kind="agent", subject_id="agent-1", tool="memory", action="store") is None


class TestAuthorizeMatching:
    def test_subject_mismatch_misses(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        _issue(led, subject_id="agent-1")
        assert led.authorize(subject_kind="agent", subject_id="agent-2", tool="memory", action="store") is None

    def test_tool_action_mismatch_misses(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        _issue(led, tool="memory", action="store")
        assert led.authorize(subject_kind="agent", subject_id="agent-1", tool="memory", action="delete") is None
        assert led.authorize(subject_kind="agent", subject_id="agent-1", tool="file_write", action="store") is None

    def test_wildcard_tool_and_action_match(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        _issue(led, tool="*", action="*")
        hit = led.authorize(subject_kind="agent", subject_id="agent-1", tool="anything", action="whatever")
        assert hit is not None
        assert hit["tool"] == "*"

    def test_exact_match_beats_wildcard(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        wildcard_id = _issue(led, tool="*", action="*")
        exact_id = _issue(led, tool="memory", action="store")
        hit = led.authorize(subject_kind="agent", subject_id="agent-1", tool="memory", action="store")
        assert hit is not None
        assert hit["grant_id"] == exact_id
        assert hit["grant_id"] != wildcard_id

    def test_action_wildcard_matches_all_actions(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        _issue(led, tool="memory", action="*")
        assert led.authorize(subject_kind="agent", subject_id="agent-1", tool="memory", action="store") is not None
        assert led.authorize(subject_kind="agent", subject_id="agent-1", tool="memory", action="delete") is not None

    def test_scope_round_trips_as_dict(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        _issue(led, scope={"resource_pattern": "workspace/**"})
        hit = led.authorize(subject_kind="agent", subject_id="agent-1", tool="memory", action="store")
        assert hit is not None
        assert hit["scope"] == {"resource_pattern": "workspace/**"}

    def test_missing_grant_authorize_misses_for_other_subject_kind(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        _issue(led, subject_kind="task", subject_id="t-9")
        assert led.authorize(subject_kind="agent", subject_id="t-9", tool="memory", action="store") is None


class TestConsume:
    def test_consume_unknown_grant_raises(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        with pytest.raises(GrantsLedgerError):
            led.consume(grant_id="no-such-grant")

    def test_consume_revoked_grant_raises(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        grant_id = _issue(led)
        led.revoke(grant_id=grant_id)
        with pytest.raises(GrantsLedgerError):
            led.consume(grant_id=grant_id)

    def test_consume_marks_consumed_when_max_uses_reached(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        grant_id = _issue(led, max_uses=1)
        led.consume(grant_id=grant_id)
        row = led.list(status="consumed")
        assert [g["grant_id"] for g in row] == [grant_id]
        with pytest.raises(GrantsLedgerError):
            led.consume(grant_id=grant_id)

    def test_consume_unlimited_uses_never_consumes(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        grant_id = _issue(led, max_uses=None)
        for _ in range(3):
            led.consume(grant_id=grant_id)
        rows = led.list(status="active")
        assert [g["grant_id"] for g in rows] == [grant_id]
        assert rows[0]["uses"] == 3

    def test_consume_expired_grant_marks_expired_and_raises(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        grant_id = _issue(led, expires_at=past)  # 直接过期，不经 authorize 懒惰过期
        with pytest.raises(GrantsLedgerError, match="expired"):
            led.consume(grant_id=grant_id)
        assert [g["grant_id"] for g in led.list(status="expired")] == [grant_id]
        assert [e["event_type"] for e in led.events(grant_id=grant_id)] == ["issue", "expire"]


class TestRevoke:
    def test_revoke_idempotent(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        grant_id = _issue(led)
        assert led.revoke(grant_id=grant_id) is True
        assert led.revoke(grant_id=grant_id) is False

    def test_revoke_unknown_grant_is_noop(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        assert led.revoke(grant_id="no-such-grant") is False


class TestExpiry:
    def test_expire_due_sweeps_and_counts(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        g1 = _issue(led, expires_at=future)
        g2 = _issue(led, expires_at=future)
        _issue(led, expires_at=None)
        now = datetime.now(UTC) + timedelta(hours=2)
        assert led.expire_due(now=now) == 2
        rows = led.list(status="expired")
        assert {g["grant_id"] for g in rows} == {g1, g2}
        # 幂等：再扫为 0
        assert led.expire_due(now=now) == 0

    def test_unparseable_expires_at_is_fail_closed(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        grant_id = _issue(led)
        # 直接改库模拟历史/外部写入的坏时限（写入口 request 已拒绝不可解析值）
        conn = sqlite3.connect(str(led.db_path))
        try:
            conn.execute("update grants set expires_at = 'not-a-timestamp' where grant_id = ?", (grant_id,))
            conn.commit()
        finally:
            conn.close()
        assert led.authorize(subject_kind="agent", subject_id="agent-1", tool="memory", action="store") is None
        assert [g["grant_id"] for g in led.list(status="expired")] == [grant_id]

    def test_request_rejects_unparseable_expires_at(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        with pytest.raises(GrantsLedgerError):
            _issue(led, expires_at="not-a-timestamp")


class TestRequestValidation:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("subject_kind", "alien"),
            ("granularity", "session"),  # v1 四档：once/task/standing/domain
            ("source", "self_declared"),
        ],
    )
    def test_closed_enum_fields_reject_unknown_values(self, tmp_path: Path, field: str, value: str) -> None:
        led = _make_ledger(tmp_path)
        with pytest.raises(GrantsLedgerError):
            _issue(led, **{field: value})

    def test_non_object_scope_rejected(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        with pytest.raises(GrantsLedgerError):
            _issue(led, scope="workspace/**")


class TestEvents:
    def test_lifecycle_events_recorded(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        grant_id = _issue(led)
        led.consume(grant_id=grant_id)
        led.revoke(grant_id=grant_id)
        types = [e["event_type"] for e in led.events(grant_id=grant_id)]
        assert types == ["issue", "consume", "revoke"]

    def test_expire_event_recorded(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        _issue(led)
        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        _issue(led, expires_at=past)
        led.authorize(subject_kind="agent", subject_id="agent-1", tool="memory", action="store")
        # 过去时限 grant 读时懒惰过期 → expire 事件（grant_id 取该过期行）
        expired_id = [g["grant_id"] for g in led.list(status="expired") if g["expires_at"] == past][0]
        assert [e["event_type"] for e in led.events(grant_id=expired_id)] == ["issue", "expire"]

    def test_append_event_with_context(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        event_id = led.append_event(event_type="deny", context={"tool": "x", "action": "y", "reason": "unclassified"})
        rows = led.events(event_type="deny")
        assert [e["event_id"] for e in rows] == [event_id]
        assert rows[0]["context"] == {"tool": "x", "action": "y", "reason": "unclassified"}
        assert rows[0]["grant_id"] is None

    def test_append_event_rejects_unknown_type(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        with pytest.raises(GrantsLedgerError):
            led.append_event(event_type="invented")


class TestListFilters:
    def test_filter_by_subject_and_tool(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        _issue(led, subject_id="agent-1", tool="memory")
        _issue(led, subject_id="agent-2", tool="memory")
        _issue(led, subject_id="agent-1", tool="file_write")
        rows = led.list(subject_kind="agent", subject_id="agent-1", tool="memory")
        assert len(rows) == 1
        assert rows[0]["tool"] == "memory"

    def test_filter_by_status(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        g1 = _issue(led)
        _issue(led)
        led.revoke(grant_id=g1)
        assert len(led.list(status="active")) == 1
        assert len(led.list(status="revoked")) == 1
        assert len(led.list()) == 2


class TestDdl:
    """DDL 严格执行执行方案 §7（两表 + 一个索引，列集一致）。"""

    def test_tables_index_and_columns(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        conn = sqlite3.connect(str(led.db_path))
        try:
            tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
            indexes = {r[0] for r in conn.execute("select name from sqlite_master where type='index'")}
            grant_cols = [r[1] for r in conn.execute("pragma table_info(grants)")]
            event_cols = [r[1] for r in conn.execute("pragma table_info(grant_events)")]
            idx_cols = [r[2] for r in conn.execute("pragma index_info('idx_grants_subject')")]
        finally:
            conn.close()
        assert {"grants", "grant_events"} <= tables
        assert "idx_grants_subject" in indexes
        assert grant_cols == [
            "grant_id",
            "subject_kind",
            "subject_id",
            "tool",
            "action",
            "scope_json",
            "granularity",
            "status",
            "max_uses",
            "uses",
            "created_at",
            "expires_at",
            "source",
            "evidence",
            "pipeline_id",
            "parent_task_id",
        ]
        assert event_cols == ["event_id", "grant_id", "event_type", "ts", "context_json"]
        assert idx_cols == ["subject_kind", "subject_id", "tool", "action", "status"]

    def test_schema_persists_across_reopen(self, tmp_path: Path) -> None:
        db = Path(tmp_path) / "grants.db"
        led1 = grants_ledger.GrantsLedger(db_path=db)
        grant_id = _issue(led1)
        led2 = grants_ledger.GrantsLedger(db_path=db)
        hit = led2.authorize(subject_kind="agent", subject_id="agent-1", tool="memory", action="store")
        assert hit is not None
        assert hit["grant_id"] == grant_id


class TestResolveDbPath:
    """落点复用 security_check 同构机制：user_data_dir 优先，回落配置/共享根。"""

    def test_user_root_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path))
        monkeypatch.delenv("AGENTOS_DATA_DIR", raising=False)
        assert resolve_db_path() == str(Path(tmp_path) / "data" / "grants" / "grants.db")

    def test_data_dir_env_takes_precedence(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path / "custom-data"))
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user"))
        assert resolve_db_path() == str(Path(tmp_path) / "custom-data" / "grants" / "grants.db")

    def test_settings_override_when_no_user_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENTOS_USER_ROOT", raising=False)
        monkeypatch.delenv("AGENTOS_DATA_DIR", raising=False)
        override = tmp_path / "elsewhere" / "grants.db"
        assert resolve_db_path({"grants_db_file": str(override)}) == str(override)

    def test_fallback_shared_root_dev_posture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENTOS_USER_ROOT", raising=False)
        monkeypatch.delenv("AGENTOS_DATA_DIR", raising=False)
        got = Path(resolve_db_path())
        assert got == Path(_SHARED_ROOT) / "grants" / "data" / "grants" / "grants.db"

    def test_root_api_import_failure_skips_user_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # _root_api 不可达（import 期失败）→ 跳过用户数据根，走 settings/共享根回落
        monkeypatch.setitem(sys.modules, "_root_api", None)
        override = tmp_path / "via-settings" / "grants.db"
        assert resolve_db_path({"grants_db_file": str(override)}) == str(override)


class TestExpiresAtParsing:
    """expires_at 解析形态：Z 后缀归一（UTC）与 naive 时间戳（视作 UTC）。"""

    def test_z_suffix_timestamp_is_expired_when_past(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        past_z = (datetime.now(UTC) - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        grant_id = _issue(led, expires_at=past_z)
        assert led.authorize(subject_kind="agent", subject_id="agent-1", tool="memory", action="store") is None
        assert [g["grant_id"] for g in led.list(status="expired")] == [grant_id]

    def test_naive_timestamp_treated_as_utc(self, tmp_path: Path) -> None:
        led = _make_ledger(tmp_path)
        future_naive = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        _issue(led, expires_at=future_naive)
        assert led.authorize(subject_kind="agent", subject_id="agent-1", tool="memory", action="store") is not None


class TestServerLifecycle:
    """server.py 单例链：懒构建 / settings 覆盖 / 生命周期钩子 / 配置变更感知。"""

    @pytest.fixture
    def server_mod(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path))  # 懒构建落点进 tmp，不碰共享根
        import importlib.util  # noqa: PLC0415

        spec = importlib.util.spec_from_file_location(
            "grants_p0_server_lifecycle", str(Path(_THIS_DIR) / "server.py")
        )
        assert spec is not None
        assert spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["grants_p0_server_lifecycle"] = mod
        spec.loader.exec_module(mod)
        mod.reset_singletons()
        return mod

    @pytest.mark.asyncio
    async def test_lazy_singletons_build_from_env_and_shipped_table(self, server_mod: object) -> None:
        led = server_mod.get_ledger()
        assert led.db_path.endswith("grants.db")
        policy = server_mod.get_policy()
        # 仓库初稿策略表（只读消费）：memory.retrieve 已分级 auto
        assert policy.classify("memory", "retrieve")["verdict"] == "auto"

    def test_build_policy_injected_dict_beats_file(self, server_mod: object) -> None:
        injected = server_mod._build_policy(
            {"tool_policy": {"tools": {"x.y": {"effect": "read", "verdict": "auto"}}}}
        )
        assert injected.classify("x", "y")["verdict"] == "auto"
        assert injected.classify("totally_unknown", "run")["verdict"] == "deny"

    @pytest.mark.asyncio
    async def test_apply_settings_and_hooks(self, server_mod: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # settings.grants_db_file 覆盖仅在无用户数据根时生效（resolve_db_path 回落链，
        # 与 security_check 同构）——delenv 后覆盖路径生效
        monkeypatch.delenv("AGENTOS_USER_ROOT", raising=False)
        monkeypatch.delenv("AGENTOS_DATA_DIR", raising=False)
        db = tmp_path / "custom" / "g.db"
        server_mod.apply_settings({"settings": {"grants_db_file": str(db)}})
        assert server_mod._LEDGER is not None
        assert server_mod._LEDGER.db_path == str(db)
        # on_load 经 plugin.get_config() 拿注入配置——SDK 注入视图直接设（内核注入形态）
        server_mod.plugin._injected_config = {"settings": {"grants_db_file": str(db)}}
        await server_mod._on_load({})
        assert server_mod._LEDGER is not None  # on_load 重建单例（同落点），不抛即绿
        server_mod._on_config_changed({"settings": {"grants_db_file": str(db)}})
        assert server_mod._LEDGER is not None
        await server_mod._on_unload({})
        assert server_mod._LEDGER is None
        assert server_mod._POLICY is None


class TestHttpEndpoints:
    """管理端点（server.py http.handle）：list / revoke / audit 直驱。"""

    @pytest.fixture
    def server(self, tmp_path: Path) -> object:
        import importlib.util  # noqa: PLC0415

        # 平铺 88 插件同名 server.py：以唯一模块名装载本插件 server（防裸名串台）；
        # 其内部对 ledger/policy/proto_adapters 的裸名导入与测试文件共享同一实例。
        spec = importlib.util.spec_from_file_location(
            "grants_p0_server", str(Path(_THIS_DIR) / "server.py")
        )
        assert spec is not None
        assert spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["grants_p0_server"] = mod
        spec.loader.exec_module(mod)
        mod.reset_singletons()
        mod._LEDGER = _make_ledger(tmp_path)
        return mod

    @pytest.mark.asyncio
    async def test_list_and_revoke_and_audit(self, server: object) -> None:
        led = server._LEDGER
        grant_id = _issue(led)
        other_id = _issue(led)
        led.revoke(grant_id=other_id)

        resp = await server.http_handle(path="/ext/grants/list", method="GET")
        assert resp["data"]["status"] == 200
        body = json.loads(_decode_body(resp))
        ids = {g["grant_id"] for g in body["grants"]}
        assert {grant_id, other_id} <= ids

        resp = await server.http_handle(path=f"/ext/grants/{grant_id}", method="DELETE")
        assert resp["data"]["status"] == 200

        resp = await server.http_handle(path="/ext/grants/audit", method="GET")
        body = json.loads(_decode_body(resp))
        types = {e["event_type"] for e in body["events"]}
        assert {"issue", "revoke"} <= types

    @pytest.mark.asyncio
    async def test_list_query_filters(self, server: object) -> None:
        led = server._LEDGER
        _issue(led, subject_id="agent-1", tool="memory")
        _issue(led, subject_id="agent-2", tool="memory")
        resp = await server.http_handle(
            path="/ext/grants/list", method="GET", query={"subject_id": "agent-2"}
        )
        body = json.loads(_decode_body(resp))
        assert {g["subject_id"] for g in body["grants"]} == {"agent-2"}

    @pytest.mark.asyncio
    async def test_query_tuple_list_and_malformed_fallback(self, server: object) -> None:
        led = server._LEDGER
        _issue(led, subject_id="agent-1", tool="memory")
        # 内核 query_multi 派生形态（[key, value] 元组列表）同语义
        resp = await server.http_handle(
            path="/ext/grants/list", method="GET", query=[["subject_id", "agent-1"]]
        )
        body = json.loads(_decode_body(resp))
        assert {g["subject_id"] for g in body["grants"]} == {"agent-1"}
        # 异形 query（非 dict 非列表对）→ 按缺参走全量，不 500
        resp = await server.http_handle(path="/ext/grants/list", method="GET", query=42)
        body = json.loads(_decode_body(resp))
        assert len(body["grants"]) == 1

    @pytest.mark.asyncio
    async def test_revoke_malformed_grant_id_404(self, server: object) -> None:
        resp = await server.http_handle(path="/ext/grants/", method="DELETE")
        assert resp["data"]["status"] == 404
        resp = await server.http_handle(path="/ext/grants/a/b", method="DELETE")
        assert resp["data"]["status"] == 404

    @pytest.mark.asyncio
    async def test_unknown_route_404(self, server: object) -> None:
        resp = await server.http_handle(path="/ext/grants/nope", method="GET")
        assert resp["data"]["status"] == 404


def _decode_body(resp: dict) -> str:
    import base64  # noqa: PLC0415

    return base64.b64decode(resp["data"]["body"]).decode("utf-8")
