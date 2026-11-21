# @feature: FP-0.2.MCP OAuth 授权 | @vision: V3 可嵌入 | @ci: python-coverage
"""桌面 loopback 弹窗流 + 粘贴回调双模式 + 引擎内建行为测试（CA-P0 件 2）。

设计真值：docs/working/连接面_连接器框架与成熟件优先授权覆盖_20261119.md §4/§5
（N2/N3/N8/N13/N14）+ docs/working/连接面外部参照_Codex与个人AI实现调研_20261119.md
（Codex McpLoginMode::{Browser, PasteCallback} 与 scope 降级重试同款语义）。

验收面（对本地 mock OAuth server，真 HTTP，禁打真实外部服务）：
- loopback 回调端到端：拉浏览器（替身 opener）→ 真 302 → **真 127.0.0.1 HTTP
  监听器**收码（RFC 8252 loopback 收码面）→ PKCE 兑换 → grants issue → 刷新；
- 粘贴回调双模式（N3）：mode=paste_callback 不拉浏览器出授权 URL，用户把真 302
  的最终回调 URL 粘回（complete_pasted_callback）→ 同一闸门+兑换+落库+grants；
  篡改/外物 URL/重复粘贴 fail-closed；
- scope 协商降级重试（N8/Codex 同款）：回调 error=invalid_scope → 自动去 scope
  重新发起一次（新 state/PKCE），再次被拒即终态；
- offline_access 自动追加（N8/Claude 同款：公告了才追加）；
- discovery 优先 scope 协商（N8：PRM scopes_supported 优先，空则 AS）；
- scope 分档骨架（N13/N14）：ScopeTiers base 恒请求、write 仅显式批准（superset
  合并）、offline_access 公告追加。
"""

from __future__ import annotations

import asyncio
import base64
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from sys import path as sys_path
from typing import Any

import flow
import pytest
from env_store import key_name, merge_env_updates, read_env
from fake_auth_server import fetch_redirect_target, parse_query
from oauth_engine import ScopeTiers, select_request_scopes
from oauth_errors import OAuthError
from oauth_fixtures import load_server_module

_THIS_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _THIS_DIR.parent
_SHARED_DIR = _PLUGIN_DIR.parent
_GRANTS_DIR = _SHARED_DIR / "grants"
if str(_GRANTS_DIR) not in sys_path:
    sys_path.insert(0, str(_GRANTS_DIR))

from ledger import GrantsLedger, GrantsLedgerError  # noqa: E402 —— grants 平铺模块

pytestmark = pytest.mark.unit


# ────────────────────────────────────────────────────────────
# 助手：opener 替身 / 真 loopback HTTP 回调接收器 / grants 假 cap
# ────────────────────────────────────────────────────────────


def _capturing_opener(captured: list[str], *, opened: bool = True) -> Any:
    def opener(url: str) -> bool:
        captured.append(url)
        return opened

    return opener


class _LoopbackCallbackServer:
    """真 127.0.0.1 HTTP 回调接收器（RFC 8252 loopback 收码面的替身载体）。

    GET ``<CALLBACK_PATH>?code=…&state=…`` → 经 run_coroutine_threadsafe 调
    ``flow.handle_callback``（与内核 /ext 分发同一入口函数）→ 回浏览器面 HTML。
    回调到达、兑换、落库全部走真 HTTP socket，不是函数直调。
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0
        self.hits: list[dict[str, str]] = []

    def start(self) -> _LoopbackCallbackServer:
        server = self
        loop = self._loop

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query, keep_blank_values=True))
                server.hits.append(query)
                future = asyncio.run_coroutine_threadsafe(flow.handle_callback(query), loop)
                status, body = future.result(timeout=15)
                payload = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


@pytest.fixture
async def loopback_callback() -> Any:
    loop = asyncio.get_running_loop()
    server = _LoopbackCallbackServer(loop).start()
    try:
        yield server
    finally:
        server.stop()


class _FakeLedgerCap:
    """grants.ledger 假 capability（真实 GrantsLedger 承载，同 grants wiring 测试）。"""

    def __init__(self, ledger: GrantsLedger) -> None:
        self.ledger = ledger

    async def call(self, method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
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


@pytest.fixture
def grants_ledger(tmp_path: Path) -> GrantsLedger:
    return GrantsLedger(db_path=str(tmp_path / "grants" / "grants.db"))


@pytest.fixture(autouse=True)
def _wire_and_reset_grants(grants_ledger: GrantsLedger) -> Any:
    import grants_link

    grants_link.set_ledger_cap(_FakeLedgerCap(grants_ledger))
    yield
    grants_link.reset_link()


def _oauth_grant_rows(ledger: GrantsLedger, server_url: str) -> list[dict[str, Any]]:
    return [
        row
        for row in ledger.list(status="active")
        if row["source"] == "oauth_broker" and row["action"] == server_url
    ]


def _expire_token(env_path: Path, prefix: str) -> None:
    merge_env_updates(env_path, [(key_name(prefix, "EXPIRES_AT"), "2020-01-01T00:00:00+00:00")])


async def _wait_for_capture(captured: list[str], timeout: float = 10.0) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    while not captured and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert captured, "授权 URL 未被捕获（流程未走到浏览器/出 URL 步骤）"
    return captured[0]


async def _paste_real_redirect(authorization_url: str) -> str:
    """用户代理（粘贴模式）：真 302 拿浏览器最终跳转的回调 URL（真 HTTP）。"""
    return await asyncio.to_thread(fetch_redirect_target, authorization_url)


async def _browser_callback(query_dict: dict[str, str]) -> tuple[int, str]:
    """经 server.py 注册的同一 ``http.handle`` 入口回传回调（与内核 /ext 分发同源）。"""
    server_module = load_server_module()
    envelope = await server_module.http_handle(
        path=flow.CALLBACK_PATH, method="GET", plugin_id="oauth_broker", raw_body="", headers={}, query=query_dict
    )
    data = envelope["data"]
    return int(data["status"]), base64.b64decode(data["body"]).decode("utf-8")


# ────────────────────────────────────────────────────────────
# 1. loopback 回调端到端（真 127.0.0.1 HTTP 监听器收码 → 兑换 → issue grant → 刷新）
# ────────────────────────────────────────────────────────────


async def test_loopback_browser_flow_end_to_end_grant_and_refresh(
    fake_as, env_path, loopback_callback, grants_ledger
) -> None:
    captured: list[str]

    async def drive(url: str) -> None:
        location = await _paste_real_redirect(url)
        await asyncio.to_thread(_http_get, location)

    def opener(url: str) -> bool:
        captured.append(url)
        asyncio.get_running_loop().create_task(drive(url))
        return True

    captured = []
    result = await flow.authorize(
        server_url=fake_as.resource_url,
        opener=opener,
        callback_base_url=loopback_callback.base_url,
        wait_seconds=10,
    )

    assert result["status"] == "authorized"
    assert result["method"] == "authorization_code"
    assert loopback_callback.hits, "回调必须经真 loopback HTTP socket 到达"
    assert loopback_callback.hits[0].get("code"), "loopback 收到的是带 code 的回调"
    rows = _oauth_grant_rows(grants_ledger, fake_as.resource_url)
    assert len(rows) == 1, "授权成立恰好落一条 domain grant"
    prefix = result["env_prefix"]
    assert re.fullmatch(r"AT-\d+", read_env(env_path)[key_name(prefix, "ACCESS_TOKEN")])

    # 刷新：token 过期 → authorize 先走刷新 → grant 不重复
    _expire_token(env_path, prefix)
    second = await flow.authorize(
        server_url=fake_as.resource_url,
        opener=lambda _url: True,
        callback_base_url=loopback_callback.base_url,
        wait_seconds=0,
    )
    assert second["status"] == "authorized"
    assert second["method"] == "refresh"
    assert len(_oauth_grant_rows(grants_ledger, fake_as.resource_url)) == 1, "刷新不重复 issue"


def _http_get(url: str) -> int:
    """真 loopback GET（用户代理把浏览器重定向打回 loopback 监听器）。"""
    import urllib.request

    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 —— 测试内 127.0.0.1
        return int(response.status)


# ────────────────────────────────────────────────────────────
# 2. 粘贴回调双模式（N3）
# ────────────────────────────────────────────────────────────


async def test_paste_callback_mode_completes_authorization_and_grant(fake_as, env_path, grants_ledger) -> None:
    captured: list[str] = []
    result = await flow.authorize(
        server_url=fake_as.resource_url,
        opener=_capturing_opener(captured),
        mode=flow.MODE_PASTE_CALLBACK,
        wait_seconds=0,
    )

    assert result["status"] == "pending"
    assert result["browser_opened"] is False, "粘贴模式不拉浏览器"
    assert captured == [], "粘贴模式不得触发 opener"
    assert any("paste_callback" in note for note in result["notes"]), "notes 给出粘回动作"
    assert not env_path.exists(), "未完成前不落库"

    pasted = await _paste_real_redirect(result["authorization_url"])
    assert "code=" in pasted, "真 302 的回调 URL 带 code"
    assert "state=" in pasted

    done = await flow.complete_pasted_callback(pasted)

    assert done["status"] == "authorized"
    assert done["method"] == "authorization_code"
    assert done["browser_opened"] is False
    prefix = done["env_prefix"]
    assert re.fullmatch(r"AT-\d+", read_env(env_path)[key_name(prefix, "ACCESS_TOKEN")])
    assert len(_oauth_grant_rows(grants_ledger, fake_as.resource_url)) == 1, "粘贴完成同样落 grants"


async def test_paste_callback_fails_closed_on_tampered_foreign_and_replayed_urls(fake_as, env_path) -> None:
    result = await flow.authorize(
        server_url=fake_as.resource_url, opener=_capturing_opener([]), mode=flow.MODE_PASTE_CALLBACK
    )
    pasted = await _paste_real_redirect(result["authorization_url"])
    query = parse_query(pasted)
    no_state = f"{flow.callback_url('http://127.0.0.1:9100')}?code={query['code']}"
    unknown_state = f"{flow.callback_url('http://127.0.0.1:9100')}?code=x&state=never-registered"

    for bad in (no_state, unknown_state):
        with pytest.raises(OAuthError):
            await flow.complete_pasted_callback(bad)
    assert not env_path.exists(), "被拒的粘贴零落库"

    done = await flow.complete_pasted_callback(pasted)
    assert done["status"] == "authorized"

    # 同一回调 URL 重复粘贴：state 已消费 → 拒（防重放）
    with pytest.raises(OAuthError):
        await flow.complete_pasted_callback(pasted)


# ────────────────────────────────────────────────────────────
# 3. scope 协商降级重试（N8/Codex 同款：invalid_scope → 去 scope 重试一次）
# ────────────────────────────────────────────────────────────


async def _start_pending(fake_as, captured: list[str], wait_seconds: float = 5.0) -> Any:
    task = asyncio.create_task(
        flow.authorize(server_url=fake_as.resource_url, opener=_capturing_opener(captured), wait_seconds=wait_seconds)
    )
    await _wait_for_capture(captured)
    return task


async def test_invalid_scope_retries_once_without_scope(fake_as, env_path) -> None:
    captured: list[str] = []
    task = await _start_pending(fake_as, captured)
    first_url = captured[0]
    assert parse_query(first_url).get("scope"), "首次授权请求带 discovery 协商出的 scope"
    query = parse_query(await _paste_real_redirect(first_url))

    def tamper(q: dict[str, str]) -> dict[str, str]:
        return {"state": q["state"], "iss": q.get("iss", ""), "error": "invalid_scope"}

    status, body = await _browser_callback(tamper(query))
    assert status == 200, "invalid_scope 触发自动重试，浏览器面回继续页"

    result = await task
    assert result["status"] == "pending"
    retry_url = result["authorization_url"]
    assert retry_url, "返回去 scope 的新授权 URL"
    assert retry_url != first_url
    assert "scope" not in parse_query(retry_url), "重试请求必须不带 scope"
    assert any("重试" in note or "scope" in note for note in result["notes"])
    assert not env_path.exists()

    # 完成重试授权 → token 落库 → 复调用即已授权（token 已在，走 existing 短路）
    retry_query = parse_query(await _paste_real_redirect(retry_url))
    status2, _body2 = await _browser_callback(retry_query)
    assert status2 == 200
    again = await flow.authorize(server_url=fake_as.resource_url, opener=_capturing_opener([]), wait_seconds=0)
    assert again["status"] == "authorized"
    assert again["method"] == "existing", "重试授权的 token 已落库"


async def test_invalid_scope_retry_is_once_only(fake_as, env_path) -> None:
    captured: list[str] = []
    task = await _start_pending(fake_as, captured)
    query = parse_query(await _paste_real_redirect(captured[0]))

    await _browser_callback({"state": query["state"], "iss": query.get("iss", ""), "error": "invalid_scope"})
    result = await task
    retry_url = result["authorization_url"]
    assert retry_url

    retry_query = parse_query(await _paste_real_redirect(retry_url))
    status, body = await _browser_callback(
        {"state": retry_query["state"], "iss": retry_query.get("iss", ""), "error": "invalid_scope"}
    )

    assert status == 400, "第二次 invalid_scope 不再重试（终态）"
    assert "invalid_scope" in body
    assert not env_path.exists(), "重试用尽后零落库"


# ────────────────────────────────────────────────────────────
# 4. offline_access 自动追加（N8/Claude 同款：公告了才追加）
# ────────────────────────────────────────────────────────────


async def test_offline_access_appended_when_advertised(auth_server_factory, env_path) -> None:
    server = auth_server_factory(prm_scopes=["files:read", "offline_access"])
    captured: list[str] = []
    result = await flow.authorize(
        server_url=server.resource_url, opener=_capturing_opener(captured), mode=flow.MODE_PASTE_CALLBACK
    )
    assert parse_query(result["authorization_url"])["scope"] == "files:read offline_access"


async def test_offline_access_appended_to_explicit_scope_when_advertised(auth_server_factory, env_path) -> None:
    server = auth_server_factory(prm_scopes=["files:read", "offline_access"])
    captured: list[str] = []
    result = await flow.authorize(
        server_url=server.resource_url,
        opener=_capturing_opener(captured),
        mode=flow.MODE_PASTE_CALLBACK,
        scope="custom:only",
    )
    assert parse_query(result["authorization_url"])["scope"] == "custom:only offline_access"


async def test_offline_access_not_appended_when_not_advertised(fake_as, env_path) -> None:
    captured: list[str] = []
    result = await flow.authorize(
        server_url=fake_as.resource_url,
        opener=_capturing_opener(captured),
        mode=flow.MODE_PASTE_CALLBACK,
        scope="custom:only",
    )
    assert parse_query(result["authorization_url"])["scope"] == "custom:only", "未公告 offline_access 不盲追加"


# ────────────────────────────────────────────────────────────
# 5. scope 协商 discovery 优先（N8/Codex 同款）
# ────────────────────────────────────────────────────────────


async def test_scope_defaults_to_discovery_scopes(fake_as, env_path) -> None:
    captured: list[str] = []
    result = await flow.authorize(
        server_url=fake_as.resource_url, opener=_capturing_opener(captured), mode=flow.MODE_PASTE_CALLBACK
    )
    assert parse_query(result["authorization_url"])["scope"] == "files:read files:write", "PRM scopes_supported 优先"


async def test_scope_falls_back_to_as_scopes_when_prm_empty(auth_server_factory, env_path) -> None:
    server = auth_server_factory(prm_scopes=[])
    captured: list[str] = []
    result = await flow.authorize(
        server_url=server.resource_url, opener=_capturing_opener(captured), mode=flow.MODE_PASTE_CALLBACK
    )
    assert parse_query(result["authorization_url"])["scope"] == "files:read files:write", "PRM 空 → AS scopes_supported"


# ────────────────────────────────────────────────────────────
# 6. scope 分档骨架（N13/N14：base 恒请求、write 仅显式批准、superset 合并）
# ────────────────────────────────────────────────────────────


def test_scope_tiers_base_is_default_request() -> None:
    tiers = ScopeTiers(base=("mail.read", "contacts.read"), write=("mail.send",))

    assert select_request_scopes(tiers) == "mail.read contacts.read", "连接默认只读（N14 base 默认）"


def test_scope_tiers_write_only_when_explicitly_approved() -> None:
    tiers = ScopeTiers(base=("mail.read",), write=("mail.send", "calendar.write"))

    assert select_request_scopes(tiers, include_write=True) == "mail.read mail.send calendar.write", (
        "write 是 base 的 superset 合并（免重复弹窗，N13）"
    )


def test_scope_tiers_offline_access_appended_when_advertised() -> None:
    tiers = ScopeTiers(base=("mail.read",), write=("mail.send",))

    assert select_request_scopes(tiers, advertised=["mail.read", "offline_access"]) == "mail.read offline_access"
    assert select_request_scopes(tiers, advertised=["mail.read"]) == "mail.read"


def test_scope_tiers_dedupes_and_empty_yields_none() -> None:
    tiers = ScopeTiers(base=("mail.read", "mail.read"), write=("mail.read",))
    assert select_request_scopes(tiers, include_write=True) == "mail.read", "去重保序"

    empty = ScopeTiers(base=(), write=())
    assert select_request_scopes(empty) is None
    assert select_request_scopes(empty, include_write=True) is None


# ────────────────────────────────────────────────────────────
# 7. 面上卫生：mode 白名单、粘贴模式的 scope 重试分支与 N2 骨架
# ────────────────────────────────────────────────────────────


async def test_unknown_mode_rejected(fake_as, env_path) -> None:
    with pytest.raises(OAuthError) as excinfo:
        await flow.authorize(server_url=fake_as.resource_url, mode="clipboard")
    assert excinfo.value.code == "OPTION_INVALID"


async def test_paste_callback_mode_scope_retry_returns_new_url(fake_as, env_path, grants_ledger) -> None:
    """粘贴模式同样吃到 N8 重试：粘回 error=invalid_scope 的回调 → pending+去 scope 新 URL。"""
    result = await flow.authorize(
        server_url=fake_as.resource_url, opener=_capturing_opener([]), mode=flow.MODE_PASTE_CALLBACK
    )
    first = parse_query(result["authorization_url"])
    pasted_error = (
        f"{flow.callback_url('http://127.0.0.1:9100')}?"
        + urllib.parse.urlencode({"state": first["state"], "iss": fake_as.issuer, "error": "invalid_scope"})
    )

    retried = await flow.complete_pasted_callback(pasted_error)

    assert retried["status"] == "pending", "粘贴模式触发去 scope 重试 → 返回新授权 URL"
    retry_url = retried["authorization_url"]
    assert retry_url is not None
    assert "scope" not in parse_query(retry_url)
    assert any("重试" in note or "scope" in note for note in retried["notes"])

    pasted = await _paste_real_redirect(retry_url)
    done = await flow.complete_pasted_callback(pasted)
    assert done["status"] == "authorized"
    assert len(_oauth_grant_rows(grants_ledger, fake_as.resource_url)) == 1


async def test_cimd_skeleton_is_wired(monkeypatch: pytest.MonkeyPatch, auth_server_factory, env_path) -> None:
    """N2 骨架接线证明：CIMD 分支产出 client_id 时跳过 DCR、原样进授权请求。"""
    server = auth_server_factory()
    monkeypatch.setattr(flow, "_cimd_client_id", lambda _metadata: "https://client.example/client-metadata.json")

    result = await flow.authorize(
        server_url=server.resource_url, opener=_capturing_opener([]), mode=flow.MODE_PASTE_CALLBACK
    )

    assert result["status"] == "pending"
    assert server.registrations == [], "CIMD 分支命中时不走动态注册"
    assert result["authorization_url"] is not None
    assert parse_query(result["authorization_url"])["client_id"] == "https://client.example/client-metadata.json"


def test_cimd_resolution_stub_documented() -> None:
    """N2 骨架在位：CIMD 分支缺省为桩（返回 None → 协商落到 DCR），假设有标注。"""
    assert flow._cimd_client_id({"client_id_metadata_document_supported": True}) is None
    source = (_PLUGIN_DIR / "flow.py").read_text(encoding="utf-8")
    assert "_cimd_client_id" in source
    assert "[假设]" in source, "CIMD 桩的假设标注必须在源码里"
