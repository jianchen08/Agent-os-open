# @feature: FP-0.2.grants_ledger P2 oauth_broker 接线 | @ci: python-coverage
"""oauth_broker × grants 账本接线行为锁（P2 覆盖面补全，执行方案 §4）。

契约（ADR 2026-11-19-grants-ledger-and-tool-classification + ADR 2026-10-17-mcp-oauth-broker）：
1. 授权成立 → grants.ledger.request 落 grant：granularity=domain、source=oauth_broker、
   subject=user/*、tool=mcp、action=规范化 server_url（scope 携 domain/resource/env_prefix）；
2. 撤销联动：刷新被拒（凭据失效信号）→ 该服务器既有活跃 oauth_broker grant 全部 revoke；
3. 幂等：刷新 / 复用（existing）路径不重复 issue（同一 grant_id 复用，issue 事件不增）；
4. 账本缺席/异常 = 记录面降级：OAuth 授权是用户当面完成的独立授权行为，账本失败
   只留日志，授权主流程零阻断。

账本 = 真实 GrantsLedger（tmp 隔离库）；OAuth 流程 = 真 HTTP 假授权服务器（conftest）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from env_store import key_name, merge_env_updates
from fake_auth_server import FakeAuthServer

_THIS_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _THIS_DIR.parent
_SHARED_DIR = _PLUGIN_DIR.parent
_GRANTS_DIR = _SHARED_DIR / "grants"
for _p in (str(_GRANTS_DIR),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import flow  # noqa: E402 — oauth_broker 平铺模块（conftest 已晋升 sys.path）
import grants_link  # noqa: E402 — P2 接线缝（本批新增）
from ledger import GrantsLedger, GrantsLedgerError  # noqa: E402 — grants 平铺模块

pytestmark = pytest.mark.unit


# ── 假 capability：形态与 grants server.py 服务返回结构一致（真 GrantsLedger 承载）──


class FakeLedgerCap:
    """grants.ledger 假 capability：真实 GrantsLedger 承载；可注入异常形态。"""

    def __init__(self, ledger: GrantsLedger, *, fail: bool = False) -> None:
        self.ledger = ledger
        self.fail = fail

    async def call(self, method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        if self.fail:
            raise RuntimeError("grants ledger service boom")
        if method == "request":
            try:
                grant_id = self.ledger.request(**params)
            except GrantsLedgerError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "grant_id": grant_id}
        if method == "list":
            return {"ok": True, "grants": self.ledger.list(**params)}
        if method == "revoke":
            return {"ok": True, "revoked": self.ledger.revoke(**params)}
        raise AssertionError(f"unexpected ledger method: {method}")


class RefWithCap:
    """生产解析缝形态的 plugin ref 替身：get_capability 按 namespace 返回注入 cap。"""

    def __init__(self, cap: Any) -> None:
        self._cap = cap

    def get_capability(self, name: str) -> Any:
        if name != "grants.ledger":
            raise KeyError(f"capability not injected: {name}")
        return self._cap


@pytest.fixture
def grants_ledger(tmp_path: Path) -> GrantsLedger:
    return GrantsLedger(db_path=str(tmp_path / "grants" / "grants.db"))


@pytest.fixture(autouse=True)
def _reset_grants_link():
    """每用例前后清空接线缝（模块级缝不复位 = 跨用例污染，R3 教训）。"""
    grants_link.reset_link()
    yield
    grants_link.reset_link()


def _seed_cap(ledger: GrantsLedger, *, fail: bool = False) -> FakeLedgerCap:
    cap = FakeLedgerCap(ledger, fail=fail)
    grants_link.set_ledger_cap(cap)
    return cap


def _expire_token(env_path: Path, prefix: str) -> None:
    """把 .env 里该服务器的 EXPIRES_AT 改成过去（走刷新路径）。"""
    merge_env_updates(env_path, [(key_name(prefix, "EXPIRES_AT"), "2020-01-01T00:00:00+00:00")])


async def _run_full_flow(fake_as: Any, callback_endpoint: Any, **kwargs: Any) -> dict[str, Any]:
    """全量授权码流程（真 302 → 真回调 → 真 .env 落库），沿 test_oauth_flow 同款驱动。"""
    captured: list[str] = []

    async def drive(url: str) -> None:
        from fake_auth_server import fetch_redirect_target, parse_query

        query = await __import__("asyncio").to_thread(lambda: parse_query(fetch_redirect_target(url)))
        await callback_endpoint(query)

    def opener(url: str) -> bool:
        import asyncio

        captured.append(url)
        asyncio.get_running_loop().create_task(drive(url))
        return True

    return await flow.authorize(
        server_url=fake_as.resource_url, opener=opener, wait_seconds=10, **kwargs
    )


def _oauth_grant_rows(ledger: GrantsLedger, server_url: str) -> list[dict[str, Any]]:
    return [
        row
        for row in ledger.list(status="active")
        if row["source"] == "oauth_broker" and row["action"] == server_url
    ]


# ────────────────────────────────────────────────────────────
# 1. 授权成立 → issue（domain / source=oauth_broker）
# ────────────────────────────────────────────────────────────


async def test_full_flow_issues_domain_grant(
    fake_as, callback_endpoint, grants_ledger
) -> None:
    _seed_cap(grants_ledger)

    result = await _run_full_flow(fake_as, callback_endpoint)

    assert result["status"] == "authorized"
    rows = _oauth_grant_rows(grants_ledger, fake_as.resource_url)
    assert len(rows) == 1, "一次授权成立恰好落一条 grant"
    grant = rows[0]
    assert grant["granularity"] == "domain"
    assert grant["source"] == "oauth_broker"
    assert grant["subject_kind"] == "user"
    assert grant["subject_id"] == "*"
    assert grant["tool"] == "mcp"
    assert grant["action"] == fake_as.resource_url
    assert grant["scope"]["domain"] == fake_as.resource_url
    assert grant["scope"]["env_prefix"] == result["env_prefix"]
    assert grant["scope"]["resource"] == fake_as.resource_url
    assert grant["evidence"].startswith("oauth_broker:authorization_code:")
    assert len(grants_ledger.events(grant_id=grant["grant_id"], event_type="issue")) == 1


# ────────────────────────────────────────────────────────────
# 2. 幂等：刷新 / 复用路径不重复 issue
# ────────────────────────────────────────────────────────────


async def test_refresh_path_reuses_grant_without_duplicate_issue(
    fake_as, env_path, callback_endpoint, grants_ledger
) -> None:
    _seed_cap(grants_ledger)
    first = await _run_full_flow(fake_as, callback_endpoint)
    rows_before = _oauth_grant_rows(grants_ledger, fake_as.resource_url)
    assert len(rows_before) == 1

    _expire_token(env_path, first["env_prefix"])
    second = await flow.authorize(
        server_url=fake_as.resource_url,
        opener=lambda _url: True,
        wait_seconds=0,
    )

    assert second["status"] == "authorized"
    assert second["method"] == "refresh"
    rows_after = _oauth_grant_rows(grants_ledger, fake_as.resource_url)
    assert len(rows_after) == 1, "刷新路径不重复 issue"
    assert rows_after[0]["grant_id"] == rows_before[0]["grant_id"], "复用同一 grant"
    assert len(grants_ledger.events(event_type="issue")) == 1, "issue 事件不增"


async def test_existing_path_reuses_grant_without_duplicate_issue(
    fake_as, callback_endpoint, grants_ledger
) -> None:
    _seed_cap(grants_ledger)
    await _run_full_flow(fake_as, callback_endpoint)
    rows_before = _oauth_grant_rows(grants_ledger, fake_as.resource_url)
    assert len(rows_before) == 1

    second = await flow.authorize(server_url=fake_as.resource_url, opener=lambda _url: True)

    assert second["status"] == "authorized"
    assert second["method"] == "existing"
    rows_after = _oauth_grant_rows(grants_ledger, fake_as.resource_url)
    assert len(rows_after) == 1, "复用（existing）路径不重复 issue"
    assert rows_after[0]["grant_id"] == rows_before[0]["grant_id"], "复用同一 grant"
    assert len(grants_ledger.events(event_type="issue")) == 1


# ────────────────────────────────────────────────────────────
# 3. 撤销联动：刷新被拒 → revoke；重新授权 → 新 grant
# ────────────────────────────────────────────────────────────


async def test_refresh_rejection_revokes_linked_grant_then_reissue_on_new_consent(
    auth_server_factory, env_path, callback_endpoint, grants_ledger
) -> None:
    server: FakeAuthServer = auth_server_factory()
    _seed_cap(grants_ledger)
    first = await _run_full_flow(server, callback_endpoint)
    rows = _oauth_grant_rows(grants_ledger, server.resource_url)
    assert len(rows) == 1
    old_grant_id = rows[0]["grant_id"]

    # 凭据失效：token 过期 + 假授权服务器拒绝刷新（invalid_grant）
    _expire_token(env_path, first["env_prefix"])
    server.token_error = (400, {"error": "invalid_grant"})
    rejected = await flow.authorize(
        server_url=server.resource_url, opener=lambda _url: True, wait_seconds=0
    )

    assert rejected["status"] == "pending", "刷新被拒后回落全量授权（回调未达 → pending）"
    assert rejected["refresh_error"] is not None
    assert _oauth_grant_rows(grants_ledger, server.resource_url) == [], "刷新被拒 → 联动撤销，无活跃 grant 残留"
    assert grants_ledger.events(grant_id=old_grant_id, event_type="revoke"), "撤销事件在账"
    revoked_row = [row for row in grants_ledger.list() if row["grant_id"] == old_grant_id]
    assert len(revoked_row) == 1
    assert revoked_row[0]["status"] == "revoked"

    # 用户重新授权成功 → 新 grant（新 id），旧 grant 不复活
    server.token_error = None
    second = await _run_full_flow(server, callback_endpoint)
    assert second["status"] == "authorized"
    rows_now = _oauth_grant_rows(grants_ledger, server.resource_url)
    assert len(rows_now) == 1
    assert rows_now[0]["grant_id"] != old_grant_id, "重新授权落新 grant"
    assert len(grants_ledger.events(event_type="issue")) == 2, "issue 两次（两次授权成立）"


# ────────────────────────────────────────────────────────────
# 4. 记录面降级：账本缺席/异常不阻断授权主流程
# ────────────────────────────────────────────────────────────


async def test_ledger_absent_authorization_still_succeeds(
    fake_as, callback_endpoint
) -> None:
    # 不注入 cap、不注入 resolver → get_ledger_cap() = None（记录面缺席）
    result = await _run_full_flow(fake_as, callback_endpoint)
    assert result["status"] == "authorized"
    assert result["method"] == "authorization_code"


async def test_ledger_service_error_authorization_still_succeeds(
    fake_as, callback_endpoint, grants_ledger
) -> None:
    _seed_cap(grants_ledger, fail=True)
    result = await _run_full_flow(fake_as, callback_endpoint)
    assert result["status"] == "authorized", "账本异常只降级记录面，授权不受影响"


# ────────────────────────────────────────────────────────────
# 5. grants_link 单元面：幂等复用 / 撤销只触及本服务器 / 生产 ref 解析缝
# ────────────────────────────────────────────────────────────


async def test_ensure_grant_idempotent_same_server_same_id(grants_ledger) -> None:
    _seed_cap(grants_ledger)
    first = await grants_link.ensure_server_grant(
        server_url="https://mcp.example.com/mcp", prefix="MCP_OAUTH_X", resource=None, method="authorization_code"
    )
    second = await grants_link.ensure_server_grant(
        server_url="https://mcp.example.com/mcp", prefix="MCP_OAUTH_X", resource=None, method="refresh"
    )
    assert first is not None
    assert second == first, "同服务器幂等复用同一 grant_id"
    assert len(grants_ledger.list(status="active")) == 1
    assert len(grants_ledger.events(event_type="issue")) == 1


async def test_revoke_server_grants_only_touches_matching_server(grants_ledger) -> None:
    _seed_cap(grants_ledger)
    await grants_link.ensure_server_grant(
        server_url="https://a.example.com/mcp", prefix="MCP_OAUTH_A", resource=None, method="authorization_code"
    )
    await grants_link.ensure_server_grant(
        server_url="https://b.example.com/mcp", prefix="MCP_OAUTH_B", resource=None, method="authorization_code"
    )
    revoked = await grants_link.revoke_server_grants(
        server_url="https://a.example.com/mcp", context={"reason": "refresh_rejected"}
    )
    assert revoked == 1
    assert grants_ledger.list(status="active")[0]["action"] == "https://b.example.com/mcp"


async def test_resolver_seam_resolves_ledger_via_plugin_ref(grants_ledger) -> None:
    """生产形态：无显式注入，经 ref resolver 按 grants.ledger namespace 解析。"""
    cap = FakeLedgerCap(grants_ledger)
    grants_link.set_ref_resolver(lambda: RefWithCap(cap))
    grant_id = await grants_link.ensure_server_grant(
        server_url="https://mcp.example.com/mcp", prefix="MCP_OAUTH_X", resource=None, method="authorization_code"
    )
    assert grant_id
    assert len(grants_ledger.list(status="active")) == 1


async def test_resolver_absent_or_failing_degrades_to_none(grants_ledger) -> None:
    grants_link.set_ref_resolver(lambda: None)
    assert await grants_link.ensure_server_grant(
        server_url="https://mcp.example.com/mcp", prefix="MCP_OAUTH_X", resource=None, method="authorization_code"
    ) is None
    assert await grants_link.revoke_server_grants(server_url="https://mcp.example.com/mcp") == 0

    def _boom() -> Any:
        raise RuntimeError("ref gone")

    grants_link.set_ref_resolver(_boom)
    assert await grants_link.ensure_server_grant(
        server_url="https://mcp.example.com/mcp", prefix="MCP_OAUTH_X", resource=None, method="authorization_code"
    ) is None
    assert grants_link.get_ledger_cap() is None


async def test_ref_without_capability_degrades_to_none() -> None:
    grants_link.set_ref_resolver(lambda: RefWithCap(None))
    assert await grants_link.ensure_server_grant(
        server_url="https://mcp.example.com/mcp", prefix="MCP_OAUTH_X", resource=None, method="authorization_code"
    ) is None


async def test_other_plugin_grants_are_never_matched(grants_ledger) -> None:
    """非 oauth_broker 来源 / 非本服务器的 grant 不参与幂等复用匹配。"""
    _seed_cap(grants_ledger)
    grants_ledger.request(
        subject_kind="user", subject_id="*", tool="bash", action="https://mcp.example.com/mcp",
        granularity="standing", source="user_rule",
    )
    grants_ledger.request(
        subject_kind="user", subject_id="*", tool="mcp", action="https://other.example.com/mcp",
        granularity="domain", source="oauth_broker",
    )
    grant_id = await grants_link.ensure_server_grant(
        server_url="https://mcp.example.com/mcp", prefix="MCP_OAUTH_X", resource=None, method="authorization_code"
    )
    assert grant_id
    mcp_rows = [row for row in grants_ledger.list(status="active") if row["tool"] == "mcp"]
    assert len(mcp_rows) == 2, "既有他服务器 grant 不被复用，新建本服务器 grant"
    assert {row["action"] for row in mcp_rows} == {
        "https://other.example.com/mcp",
        "https://mcp.example.com/mcp",
    }


# ── 服务响应畸形/被拒面（记录面降级的细分路径）──────────────────────────────


class ScriptedLedgerCap:
    """脚本化假 capability：按方法名回放预置响应/异常（畸形面专用）。"""

    def __init__(self, behaviors: dict[str, Any]) -> None:
        self._behaviors = behaviors
        self.calls: list[str] = []

    async def call(self, method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        self.calls.append(method)
        behavior = self._behaviors[method]
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


async def test_malformed_list_response_falls_through_to_issue(tmp_path: Path) -> None:
    """list 响应形态畸形（grants 非列表）→ 视作无既有 grant，照常 issue（幂等不失效）。"""
    ledger = GrantsLedger(db_path=str(tmp_path / "malformed" / "grants.db"))
    cap = ScriptedLedgerCap({"list": {"ok": True, "grants": "not-a-list"}, "request": {"ok": True, "grant_id": "g1"}})
    grants_link.set_ledger_cap(cap)
    grant_id = await grants_link.ensure_server_grant(
        server_url="https://mcp.example.com/mcp", prefix="MCP_OAUTH_X", resource=None, method="authorization_code"
    )
    assert grant_id == "g1"
    assert cap.calls == ["list", "request"]


async def test_request_rejected_returns_none(tmp_path: Path) -> None:
    """issue 被账本拒绝（{"ok": false}）→ 记录面降级返回 None，不抛。"""
    cap = ScriptedLedgerCap(
        {"list": {"ok": True, "grants": []}, "request": {"ok": False, "error": "unknown granularity"}}
    )
    grants_link.set_ledger_cap(cap)
    assert await grants_link.ensure_server_grant(
        server_url="https://mcp.example.com/mcp", prefix="MCP_OAUTH_X", resource=None, method="authorization_code"
    ) is None


async def test_revoke_skips_rows_without_grant_id(tmp_path: Path) -> None:
    """list 行缺 grant_id（畸形行）→ 跳过不崩，撤销计数 0。"""
    cap = ScriptedLedgerCap(
        {"list": {"ok": True, "grants": [{"source": "oauth_broker", "action": "https://mcp.example.com/mcp"}]}}
    )
    grants_link.set_ledger_cap(cap)
    assert await grants_link.revoke_server_grants(server_url="https://mcp.example.com/mcp") == 0
    assert "revoke" not in cap.calls


async def test_revoke_service_error_degrades_to_zero(tmp_path: Path) -> None:
    """revoke 调用异常 → 记录面降级返回已成功数（0），不抛、不阻断授权流程。"""
    ledger = GrantsLedger(db_path=str(tmp_path / "revoke_err" / "grants.db"))
    ledger.request(
        subject_kind="user", subject_id="*", tool="mcp", action="https://mcp.example.com/mcp",
        granularity="domain", source="oauth_broker",
    )
    cap = ScriptedLedgerCap(
        {
            "list": {"ok": True, "grants": ledger.list(status="active")},
            "revoke": RuntimeError("grants ledger service boom"),
        }
    )
    grants_link.set_ledger_cap(cap)
    assert await grants_link.revoke_server_grants(server_url="https://mcp.example.com/mcp") == 0
