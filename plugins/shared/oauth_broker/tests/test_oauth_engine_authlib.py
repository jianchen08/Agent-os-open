# @feature: FP-0.2.MCP OAuth 授权 | @vision: V3 可嵌入 | @ci: python-coverage
"""``oauth_engine`` Authlib 协议引擎行为测试（CA-P0 件 1）。

设计真值：docs/working/连接面_连接器框架与成熟件优先授权覆盖_20261119.md §4
（协议机退役自研：通用 OAuth2/OIDC 流换 Authlib；自研每接一家都要手工追规范）。

验收面（对本地 mock OAuth server 端到端，真 HTTP，禁打真实外部服务）：
- 授权 URL 生成（Authlib create_authorization_url：必备参数 + S256 + resource）；
- PKCE 对（Authlib rfc7636 实现锚：challenge == create_s256_code_challenge(verifier)）；
- token 兑换（授权码 + PKCE verifier + RFC 8707 resource；公共客户端 client_id 进
  form、机密客户端 client_secret_post）与归一化（expires_at ISO8601 UTC）；
- 刷新（refresh_token grant + 轮换 + secret 同用）；
- 错误翻译（AS 拒 → TOKEN_REQUEST_FAILED 带 error；传输失败 → TOKEN_REQUEST_FAILED；
  200 缺 access_token → TOKEN_RESPONSE_INVALID；非法端点 → URL_INVALID 先拒）；
- 客户端注册请求体（mcp SDK OAuthClientMetadata 承载，RFC 7591 公共客户端字段）；
- 审计锚：引擎与适配层源码无裸 token 兑换 HTTP 手搓（无 urllib 传输面）。

机密客户端的 client_secret_post 路径由「secret 在 form 里」的对面端观测证死：
若 Authlib 走了 client_secret_basic，form 里没有 client_secret，对面端必回
invalid_client——用例间互证，不另 spy HTTP 头。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import oauth_engine
import pytest
import requests
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from fake_auth_server import b64url, fetch_redirect_target, parse_query
from oauth_errors import OAuthError

pytestmark = pytest.mark.unit

CLIENT_REDIRECT = "http://127.0.0.1:9100/ext/oauth_broker/callback"

_PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _register_and_authorize(server) -> tuple[str, str, dict[str, str]]:
    """SDK 注册请求体 → 授权 URL → 用户代理 302 → 回调 query（端到端三步）。"""
    payload = oauth_engine.client_registration_payload(
        client_name="agentos-oauth-broker", redirect_uri=CLIENT_REDIRECT, scope=None
    )
    server.registrations.append(payload)
    client_id = f"client-manual-{len(server.registrations)}"
    server.clients[client_id] = payload
    verifier, challenge = oauth_engine.make_pkce_pair()
    state = oauth_engine.make_state()
    url = oauth_engine.build_authorization_url(
        f"{server.base_url}/authorize",
        client_id=client_id,
        redirect_uri=CLIENT_REDIRECT,
        state=state,
        code_challenge=challenge,
        resource=server.resource_url,
    )
    location = fetch_redirect_target(url)
    return client_id, verifier, parse_query(location)


# ────────────────────────────────────────────────────────────
# 1. PKCE 与 state（Authlib 实现锚）
# ────────────────────────────────────────────────────────────


def test_pkce_pair_matches_authlib_s256_transform() -> None:
    """challenge 必须等于 Authlib create_s256_code_challenge(verifier)（实现锚）。"""
    pairs = [oauth_engine.make_pkce_pair() for _ in range(5)]
    assert len({verifier for verifier, _ in pairs}) == 5, "verifier 必须每次不同"

    for verifier, challenge in pairs:
        assert 43 <= len(verifier) <= 128, f"RFC 7636 长度界：{len(verifier)}"
        assert challenge == create_s256_code_challenge(verifier), "challenge 必须出自 Authlib rfc7636"
        expected = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        assert challenge == expected, "且与 S256 标准变换逐字一致"
        assert "=" not in challenge, "base64url 无填充"


def test_state_is_unpredictable_and_unique() -> None:
    states = {oauth_engine.make_state() for _ in range(8)}
    assert len(states) == 8
    assert all(len(state) >= 24 for state in states), "state 需足够随机（CSRF 防线）"


# ────────────────────────────────────────────────────────────
# 2. 授权 URL（Authlib create_authorization_url）
# ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("scope", "resource"),
    [(None, None), ("files:read", None), ("files:read files:write", "https://mcp.example.com/mcp")],
)
def test_authorization_url_built_by_authlib_carries_oauth_params(scope: str | None, resource: str | None) -> None:
    _, challenge = oauth_engine.make_pkce_pair()

    url = oauth_engine.build_authorization_url(
        "https://as.example/authorize",
        client_id="client-1",
        redirect_uri=CLIENT_REDIRECT,
        state="st-1",
        code_challenge=challenge,
        scope=scope,
        resource=resource,
    )
    params = parse_query(url)

    assert params["response_type"] == "code"
    assert params["client_id"] == "client-1"
    assert params["redirect_uri"] == CLIENT_REDIRECT
    assert params["state"] == "st-1"
    assert params["code_challenge"] == challenge
    assert params["code_challenge_method"] == "S256"
    assert ("scope" in params) == (scope is not None)
    assert ("resource" in params) == (resource is not None)


def test_authorization_url_appends_to_existing_query() -> None:
    url = oauth_engine.build_authorization_url(
        "https://as.example/authorize?tenant=acme",
        client_id="client-1",
        redirect_uri=CLIENT_REDIRECT,
        state="st-2",
        code_challenge="ch",
    )
    params = parse_query(url)
    assert params["tenant"] == "acme", "既有 query 保留（分隔符 &）"
    assert params["response_type"] == "code"


# ────────────────────────────────────────────────────────────
# 3. token 兑换与刷新（Authlib OAuth2Session，对 mock provider 端到端）
# ────────────────────────────────────────────────────────────


def test_exchange_authorization_code_end_to_end_via_authlib(fake_as) -> None:
    client_id, verifier, callback_query = _register_and_authorize(fake_as)
    before = datetime.now(UTC)

    tokens = oauth_engine.exchange_authorization_code(
        f"{fake_as.base_url}/token",
        code=callback_query["code"],
        redirect_uri=CLIENT_REDIRECT,
        client_id=client_id,
        code_verifier=verifier,
        resource=fake_as.resource_url,
    )

    assert tokens["access_token"].startswith("AT-")
    assert tokens["refresh_token"].startswith("RT-")
    assert tokens["token_type"] == "Bearer"
    expires_at = datetime.fromisoformat(tokens["expires_at"])
    assert before + timedelta(seconds=fake_as.expires_in - 60) <= expires_at <= before + timedelta(
        seconds=fake_as.expires_in + 60
    ), "expires_at 归一为 ISO8601 UTC 且贴近 now+expires_in"
    (request,) = fake_as.token_requests
    assert request["grant_type"] == "authorization_code"
    assert request["code_verifier"] == verifier, "PKCE verifier 随请求回带（对面端真校验）"
    assert request["resource"] == fake_as.resource_url, "RFC 8707 resource 进 token 请求"
    assert request["client_id"] == client_id


def test_confidential_client_uses_client_secret_post(fake_as) -> None:
    """预注册机密客户端：secret 随 form 提交（client_secret_post），非 Basic 头。"""
    fake_as.require_client_secret = "s3cr3t"
    client_id, verifier, callback_query = _register_and_authorize(fake_as)

    tokens = oauth_engine.exchange_authorization_code(
        f"{fake_as.base_url}/token",
        code=callback_query["code"],
        redirect_uri=CLIENT_REDIRECT,
        client_id=client_id,
        client_secret="s3cr3t",
        code_verifier=verifier,
        resource=fake_as.resource_url,
    )

    assert tokens["access_token"].startswith("AT-")
    (request,) = fake_as.token_requests
    assert request["client_secret"] == "s3cr3t", "client_secret_post：secret 在 form 里（Basic 头路线到不了这里）"


def test_refresh_rotates_and_old_token_dies(fake_as) -> None:
    client_id, verifier, callback_query = _register_and_authorize(fake_as)
    first = oauth_engine.exchange_authorization_code(
        f"{fake_as.base_url}/token",
        code=callback_query["code"],
        redirect_uri=CLIENT_REDIRECT,
        client_id=client_id,
        code_verifier=verifier,
        resource=fake_as.resource_url,
    )

    refreshed = oauth_engine.refresh_access_token(
        f"{fake_as.base_url}/token",
        refresh_token=first["refresh_token"],
        client_id=client_id,
        resource=fake_as.resource_url,
    )

    assert refreshed["access_token"] != first["access_token"]
    assert refreshed["refresh_token"] != first["refresh_token"], "公共客户端轮换"
    assert len(fake_as.token_requests) == 2, "一次兑换 + 一次刷新"
    (exchange_request, refresh_request) = fake_as.token_requests
    assert exchange_request["grant_type"] == "authorization_code"
    assert refresh_request["grant_type"] == "refresh_token"
    assert refresh_request["resource"] == fake_as.resource_url
    # 轮换是真撤销：旧 refresh 不可再用，新 refresh 可继续
    with pytest.raises(OAuthError) as excinfo:
        oauth_engine.refresh_access_token(
            f"{fake_as.base_url}/token",
            refresh_token=first["refresh_token"],
            client_id=client_id,
            resource=fake_as.resource_url,
        )
    assert excinfo.value.code == "TOKEN_REQUEST_FAILED"
    again = oauth_engine.refresh_access_token(
        f"{fake_as.base_url}/token",
        refresh_token=refreshed["refresh_token"],
        client_id=client_id,
        resource=fake_as.resource_url,
    )
    assert again["access_token"] != refreshed["access_token"]


def test_refresh_carries_client_secret_post(fake_as) -> None:
    fake_as.require_client_secret = "s3cr3t"
    client_id, verifier, callback_query = _register_and_authorize(fake_as)
    first = oauth_engine.exchange_authorization_code(
        f"{fake_as.base_url}/token",
        code=callback_query["code"],
        redirect_uri=CLIENT_REDIRECT,
        client_id=client_id,
        client_secret="s3cr3t",
        code_verifier=verifier,
        resource=fake_as.resource_url,
    )

    refreshed = oauth_engine.refresh_access_token(
        f"{fake_as.base_url}/token",
        refresh_token=first["refresh_token"],
        client_id=client_id,
        client_secret="s3cr3t",
        resource=fake_as.resource_url,
    )

    assert refreshed["access_token"].startswith("AT-")
    assert fake_as.token_requests[-1]["client_secret"] == "s3cr3t", "刷新同用 client_secret_post"


# ────────────────────────────────────────────────────────────
# 4. 错误翻译（错误是值）
# ────────────────────────────────────────────────────────────


def test_exchange_wrong_verifier_translated_as_token_request_failed(fake_as) -> None:
    client_id, _, callback_query = _register_and_authorize(fake_as)
    other, _ = oauth_engine.make_pkce_pair()

    with pytest.raises(OAuthError) as excinfo:
        oauth_engine.exchange_authorization_code(
            f"{fake_as.base_url}/token",
            code=callback_query["code"],
            redirect_uri=CLIENT_REDIRECT,
            client_id=client_id,
            code_verifier=other,
            resource=fake_as.resource_url,
        )

    assert excinfo.value.code == "TOKEN_REQUEST_FAILED"
    assert "invalid_grant" in str(excinfo.value)
    assert fake_as.refresh_tokens == {}, "校验失败不得签发 refresh token"


def test_exchange_without_required_secret_translated(fake_as) -> None:
    fake_as.require_client_secret = "s3cr3t"
    client_id, verifier, callback_query = _register_and_authorize(fake_as)

    with pytest.raises(OAuthError) as excinfo:
        oauth_engine.exchange_authorization_code(
            f"{fake_as.base_url}/token",
            code=callback_query["code"],
            redirect_uri=CLIENT_REDIRECT,
            client_id=client_id,
            code_verifier=verifier,
            resource=fake_as.resource_url,
        )

    assert excinfo.value.code == "TOKEN_REQUEST_FAILED"
    assert "invalid_client" in str(excinfo.value)


def test_token_endpoint_5xx_translated_with_reason(fake_as) -> None:
    fake_as.token_error = (500, {"error": "server_error"})

    with pytest.raises(OAuthError) as excinfo:
        oauth_engine.refresh_access_token(
            f"{fake_as.base_url}/token", refresh_token="RT-x", client_id="client-1", resource=fake_as.resource_url
        )

    assert excinfo.value.code == "TOKEN_REQUEST_FAILED"
    assert "server_error" in str(excinfo.value)


def test_token_response_missing_access_token_rejected(fake_as) -> None:
    fake_as.token_response_override = {"token_type": "Bearer"}

    with pytest.raises(OAuthError) as excinfo:
        oauth_engine.refresh_access_token(
            f"{fake_as.base_url}/token", refresh_token="RT-x", client_id="client-1", resource=fake_as.resource_url
        )

    assert excinfo.value.code == "TOKEN_RESPONSE_INVALID"


def test_token_transport_failure_translated() -> None:
    with pytest.raises(OAuthError) as excinfo:
        oauth_engine.refresh_access_token(
            "http://127.0.0.1:1/token", refresh_token="RT-x", client_id="client-1", resource="https://x/mcp"
        )

    assert excinfo.value.code == "TOKEN_REQUEST_FAILED"


@pytest.mark.parametrize("endpoint", ["", "ftp://as.example/token", "not-a-url", "https://as.example/x#frag"])
def test_non_http_token_endpoint_rejected_before_request(endpoint: str) -> None:
    """非法端点在校验期即拒（URL_INVALID 先于任何请求）。"""
    with pytest.raises(OAuthError) as excinfo:
        oauth_engine.exchange_authorization_code(
            endpoint, code="c", redirect_uri=CLIENT_REDIRECT, client_id="c", code_verifier="v"
        )

    assert excinfo.value.code == "URL_INVALID"


# ────────────────────────────────────────────────────────────
# 5. 客户端注册请求体（mcp SDK OAuthClientMetadata 承载）
# ────────────────────────────────────────────────────────────


def test_client_registration_payload_is_public_client() -> None:
    payload = oauth_engine.client_registration_payload(
        client_name="agentos-oauth-broker", redirect_uri=CLIENT_REDIRECT, scope="files:read"
    )

    assert payload["redirect_uris"] == [CLIENT_REDIRECT]
    assert payload["token_endpoint_auth_method"] == "none"
    assert set(payload["grant_types"]) == {"authorization_code", "refresh_token"}
    assert payload["response_types"] == ["code"]
    assert payload["client_name"] == "agentos-oauth-broker"
    assert payload["scope"] == "files:read"


def test_client_registration_payload_omits_empty_scope() -> None:
    payload = oauth_engine.client_registration_payload(client_name="x", redirect_uri=CLIENT_REDIRECT, scope=None)
    assert "scope" not in payload


def test_registration_payload_end_to_end_accepted(fake_as) -> None:
    """注册请求体对面端可消费（RFC 7591 字段面真值由对面端观测）。"""
    payload = oauth_engine.client_registration_payload(
        client_name="agentos-oauth-broker", redirect_uri=CLIENT_REDIRECT, scope=None
    )
    response = requests.post(
        f"{fake_as.base_url}/register", json=payload, headers={"Accept": "application/json"}, timeout=5
    )
    assert response.status_code == 201
    document = json.loads(response.content)
    assert document["client_id"].startswith("client-")
    (observed,) = fake_as.registrations
    assert observed["token_endpoint_auth_method"] == "none"
    assert observed["redirect_uris"] == [CLIENT_REDIRECT]


# ────────────────────────────────────────────────────────────
# 6. 审计锚：无裸 token 兑换 HTTP 手搓（验收判据 1 的机械化）
# ────────────────────────────────────────────────────────────


def test_engine_and_adapter_have_no_raw_http_transport() -> None:
    """协议机退役自研的验收锚：引擎与适配层源码不得引入 urllib 传输面（token
    兑换/刷新一律经 Authlib OAuth2Session；元数据 GET 走 requests）。"""
    for name in ("oauth_engine.py", "oauth_protocol.py"):
        source = (_PLUGIN_DIR / name).read_text(encoding="utf-8")
        assert "urllib.request" not in source, f"{name} 不得手搓 HTTP 传输"
        assert "urlopen" not in source, f"{name} 不得手搓 HTTP 传输"
