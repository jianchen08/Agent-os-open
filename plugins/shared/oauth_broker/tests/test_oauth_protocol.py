# @feature: FP-0.2.MCP OAuth 授权 | @vision: V3 可嵌入 | @ci: python-coverage
"""``oauth_protocol`` 协议面行为测试（真 HTTP；假授权服务器为对面端）。

验收面：受保护资源元数据发现（子路径优先 → 根回落 → 全缺拒）、授权服务器
元数据发现（RFC 8414 与 OIDC 两形态 + 路径 issuer 三形态候选、issuer 必须
逐字相符、缺 token_endpoint / 无 S256 拒）、动态客户端注册（字段面真值由
对面端观测）、PKCE 生成与 verifier↔challenge 对应性、授权 URL 必备参数、
授权码交换与 refresh（含轮换与旧 token 失效）、非 http/https 端点拒收。

断言口径：字面值只锁对面端可观测的真值（issued token / 注册请求体），
其余用性质断言（URL 集合、键集合不变、区间、轮换幂等性）。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import oauth_protocol
import pytest
from fake_auth_server import b64url, drive_authorization, fetch_redirect_target, parse_query
from oauth_protocol import OAuthError

pytestmark = pytest.mark.unit

CLIENT_REDIRECT = "http://127.0.0.1:9100/ext/oauth_broker/callback"


def _register(server, **kwargs):
    meta = oauth_protocol.discover_authorization_server(server.issuer)
    return oauth_protocol.register_client(
        meta["registration_endpoint"], redirect_uri=CLIENT_REDIRECT, client_name="agentos-oauth-broker", **kwargs
    )


def _authorize(server, client_id, *, scope=None, resource=None):
    verifier, challenge = oauth_protocol.make_pkce_pair()
    state = oauth_protocol.make_state()
    url = oauth_protocol.build_authorization_url(
        f"{server.base_url}/authorize",
        client_id=client_id,
        redirect_uri=CLIENT_REDIRECT,
        state=state,
        code_challenge=challenge,
        scope=scope,
        resource=resource if resource is not None else server.resource_url,
    )
    location = fetch_redirect_target(url)
    return verifier, state, parse_query(location)


# ────────────────────────────────────────────────────────────
# 1. 受保护资源元数据发现（RFC 9728）
# ────────────────────────────────────────────────────────────


def test_protected_resource_discovery_prefers_subpath_form(auth_server_factory) -> None:
    """两种形态都在时取子路径插入形态（根形态故意放坏件证明未走根）。"""
    server = auth_server_factory(prm_root_override={"resource": "x", "authorization_servers": []})

    doc = oauth_protocol.discover_protected_resource(server.resource_url)

    assert doc["authorization_servers"] == [server.issuer]
    assert doc["resource"] == server.resource_url


def test_protected_resource_discovery_falls_back_to_root(auth_server_factory) -> None:
    """子路径形态 404 → 回落根形态（真实形态一例：Linear 只有根形态可用）。"""
    server = auth_server_factory(omit_prm_subpath=True)

    doc = oauth_protocol.discover_protected_resource(server.resource_url)

    assert doc["authorization_servers"] == [server.issuer]


def test_protected_resource_discovery_subpath_only(auth_server_factory) -> None:
    """根形态 404 → 子路径形态仍可发现（真实形态一例：Sentry / GitHub Copilot MCP 只有子路径可用）。"""
    server = auth_server_factory(omit_prm_root=True)

    doc = oauth_protocol.discover_protected_resource(server.resource_url)

    assert doc["authorization_servers"] == [server.issuer]
    assert doc["resource"] == server.resource_url


def test_protected_resource_discovery_uses_explicit_pointer_first(auth_server_factory) -> None:
    """显式 resource_metadata_url 指针优先（回指到与 well-known 不同的文档）。"""
    server = auth_server_factory()
    server.custom_prm = {"resource": f"{server.base_url}/custom", "authorization_servers": [f"{server.base_url}/as"]}

    doc = oauth_protocol.discover_protected_resource(
        server.resource_url, resource_metadata_url=f"{server.base_url}/custom-prm"
    )

    assert doc["resource"] == f"{server.base_url}/custom"
    assert doc["authorization_servers"] == [f"{server.base_url}/as"]


def test_protected_resource_discovery_uses_www_authenticate_pointer(auth_server_factory) -> None:
    """无凭据 401 的 WWW-Authenticate 指针被采纳（RFC 9728 §5.1，规范首选机制）。"""
    server = auth_server_factory()
    server.custom_prm = {"resource": f"{server.base_url}/via-header", "authorization_servers": [server.issuer]}
    server.www_authenticate_pointer = f"{server.base_url}/custom-prm"

    doc = oauth_protocol.discover_protected_resource(server.resource_url)

    assert doc["resource"] == f"{server.base_url}/via-header"


def test_protected_resource_discovery_ignores_bad_pointer(auth_server_factory) -> None:
    """指针是提示面：非 http/https 的怪值按「无指针」跳过，继续走 well-known 候选。"""
    server = auth_server_factory()
    server.www_authenticate_pointer = "/relative-path"

    doc = oauth_protocol.discover_protected_resource(server.resource_url)

    assert doc["authorization_servers"] == [server.issuer]


def test_protected_resource_discovery_pointer_404_falls_back(auth_server_factory) -> None:
    """指针地址不可用（404）→ 继续走 well-known 候选（提示面失败不阻断发现）。"""
    server = auth_server_factory()
    server.www_authenticate_pointer = f"{server.base_url}/gone"

    doc = oauth_protocol.discover_protected_resource(server.resource_url)

    assert doc["authorization_servers"] == [server.issuer]


def test_protected_resource_discovery_pointer_probe_ignores_non_401(auth_server_factory) -> None:
    """无凭据 GET 直接得到非 401 应答（服务器应答正常）→ 无指针，白名单候选仍能发现。"""
    server = auth_server_factory(mcp_get_status=200)

    doc = oauth_protocol.discover_protected_resource(server.resource_url)

    assert doc["authorization_servers"] == [server.issuer]


def test_protected_resource_discovery_all_missing_fails_closed(auth_server_factory) -> None:
    """两种形态全 404 → DISCOVERY_FAILED（不静默回落直连 AS）。"""
    server = auth_server_factory(omit_prm_subpath=True, omit_prm_root=True)

    with pytest.raises(OAuthError) as excinfo:
        oauth_protocol.discover_protected_resource(server.resource_url)

    assert excinfo.value.code == "DISCOVERY_FAILED"


def test_protected_resource_discovery_captures_scopes(auth_server_factory) -> None:
    """scopes_supported 进发现结果（引导面取它列给用户）；非字符串条目丢弃。"""
    server = auth_server_factory()
    server.prm_document_override = {
        "resource": server.resource_url,
        "authorization_servers": [server.issuer],
        "scopes_supported": ["files:read", 7, "files:write"],
    }

    doc = oauth_protocol.discover_protected_resource(server.resource_url)

    assert doc["scopes_supported"] == ["files:read", "files:write"]


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({}, id="missing-key"),
        pytest.param({"authorization_servers": []}, id="empty-list"),
        pytest.param({"authorization_servers": "not-a-list"}, id="wrong-type"),
        pytest.param({"authorization_servers": ["ftp://as.example"]}, id="non-http-server"),
    ],
)
def test_protected_resource_document_invalid_rejected(auth_server_factory, document) -> None:
    """坏件（缺 authorization_servers / 空 / 非列表 / 非 http 服务器）一律 METADATA_INVALID。"""
    server = auth_server_factory(prm_document_override=document)

    with pytest.raises(OAuthError) as excinfo:
        oauth_protocol.discover_protected_resource(server.resource_url)

    assert excinfo.value.code == "METADATA_INVALID"


# ────────────────────────────────────────────────────────────
# 2. 授权服务器元数据发现（RFC 8414 / OIDC Discovery）
# ────────────────────────────────────────────────────────────


def test_authorization_server_metadata_rfc8414_root_issuer(fake_as) -> None:
    meta = oauth_protocol.discover_authorization_server(fake_as.issuer)

    assert meta["issuer"] == fake_as.issuer
    assert meta["token_endpoint"] == f"{fake_as.base_url}/token"
    assert meta["authorization_endpoint"] == f"{fake_as.base_url}/authorize"
    assert meta["registration_endpoint"] == f"{fake_as.base_url}/register"
    assert meta["iss_parameter_supported"] is True


@pytest.mark.parametrize(
    ("form", "issuer_path"),
    [
        ("rfc8414", ""),
        ("rfc8414", "/tenant1"),
        ("oidc", ""),
        ("oidc", "/tenant1"),
        ("oidc_append", "/tenant1"),
    ],
)
def test_authorization_server_metadata_all_discovery_forms(auth_server_factory, form, issuer_path) -> None:
    """issuer 带路径/不带路径 × 三种 well-known 形态全部可发现（候选顺序语义）。"""
    server = auth_server_factory(as_metadata_form=form, issuer_path=issuer_path)

    meta = oauth_protocol.discover_authorization_server(server.issuer)

    assert meta["issuer"] == server.issuer
    assert meta["token_endpoint"] == f"{server.base_url}/token"


def test_authorization_server_metadata_all_missing_fails_closed(auth_server_factory) -> None:
    server = auth_server_factory(as_metadata_form="none")

    with pytest.raises(OAuthError) as excinfo:
        oauth_protocol.discover_authorization_server(server.issuer)

    assert excinfo.value.code == "DISCOVERY_FAILED"


def test_authorization_server_metadata_issuer_mismatch_rejected(auth_server_factory) -> None:
    """文档 issuer 与探测用 issuer 不一致 → 拒（RFC 8414 §3.3 / RFC 9207 混流攻击面）。"""
    server = auth_server_factory(issuer_override="https://evil.example")

    with pytest.raises(OAuthError) as excinfo:
        oauth_protocol.discover_authorization_server(server.issuer)

    assert excinfo.value.code == "METADATA_INVALID"


def test_authorization_server_metadata_missing_token_endpoint_rejected(auth_server_factory) -> None:
    """缺 token_endpoint 的坏件 → METADATA_INVALID（不带着半张元数据往下走）。"""
    server = auth_server_factory(omit_token_endpoint=True)

    with pytest.raises(OAuthError) as excinfo:
        oauth_protocol.discover_authorization_server(server.issuer)

    assert excinfo.value.code == "METADATA_INVALID"


def test_authorization_server_metadata_pkce_unsupported_rejected(auth_server_factory) -> None:
    """对面端明确不支持 S256（只列 plain）→ PKCE_UNSUPPORTED（本客户端只做 S256）。"""
    server = auth_server_factory(code_challenge_methods=("plain",))

    with pytest.raises(OAuthError) as excinfo:
        oauth_protocol.discover_authorization_server(server.issuer)

    assert excinfo.value.code == "PKCE_UNSUPPORTED"


# ────────────────────────────────────────────────────────────
# 3. 动态客户端注册（RFC 7591）
# ────────────────────────────────────────────────────────────


def test_register_client_declares_public_client_and_returns_id(fake_as) -> None:
    meta = oauth_protocol.discover_authorization_server(fake_as.issuer)

    result = oauth_protocol.register_client(
        meta["registration_endpoint"], redirect_uri=CLIENT_REDIRECT, client_name="agentos-oauth-broker"
    )

    assert result["client_id"].startswith("client-")
    (payload,) = fake_as.registrations
    assert payload["redirect_uris"] == [CLIENT_REDIRECT]
    assert payload["token_endpoint_auth_method"] == "none"
    assert payload["response_types"] == ["code"]
    assert set(payload["grant_types"]) == {"authorization_code", "refresh_token"}


def test_register_client_rejected_propagates_reason(auth_server_factory) -> None:
    server = auth_server_factory(dcr_error=(400, {"error": "invalid_redirect_uri"}))

    with pytest.raises(OAuthError) as excinfo:
        oauth_protocol.register_client(
            f"{server.base_url}/register", redirect_uri=CLIENT_REDIRECT, client_name="agentos-oauth-broker"
        )

    assert excinfo.value.code == "REGISTRATION_FAILED"
    assert "invalid_redirect_uri" in str(excinfo.value)


# ────────────────────────────────────────────────────────────
# 4. PKCE 与授权 URL
# ────────────────────────────────────────────────────────────


def test_pkce_pair_correspondence_and_entropy() -> None:
    """verifier↔challenge 对应性（S256）与随机性：多组输入 + 性质断言。"""
    pairs = [oauth_protocol.make_pkce_pair() for _ in range(5)]
    assert len({verifier for verifier, _ in pairs}) == 5, "verifier 必须每次不同"

    for verifier, challenge in pairs:
        assert 43 <= len(verifier) <= 128, f"RFC 7636 长度界：{verifier}"
        assert challenge == b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        assert "=" not in challenge, "base64url 无填充"


@pytest.mark.parametrize("scope", [None, "files:read", "files:read files:write"])
def test_authorization_url_carries_required_params(scope) -> None:
    verifier, challenge = oauth_protocol.make_pkce_pair()
    url = oauth_protocol.build_authorization_url(
        "https://as.example/authorize",
        client_id="client-1",
        redirect_uri=CLIENT_REDIRECT,
        state="st-1",
        code_challenge=challenge,
        scope=scope,
        resource="https://mcp.example.com/mcp",
    )
    params = parse_query(url)

    assert params["response_type"] == "code"
    assert params["client_id"] == "client-1"
    assert params["redirect_uri"] == CLIENT_REDIRECT
    assert params["state"] == "st-1"
    assert params["code_challenge"] == challenge
    assert params["code_challenge_method"] == "S256"
    assert params["resource"] == "https://mcp.example.com/mcp"
    assert ("scope" in params) == (scope is not None)


# ────────────────────────────────────────────────────────────
# 5. 授权码交换与 refresh
# ────────────────────────────────────────────────────────────


def test_exchange_authorization_code_issues_tokens(fake_as) -> None:
    client_id = _register(fake_as)["client_id"]
    verifier, state, callback_query = _authorize(fake_as, client_id)
    before = datetime.now(UTC)

    tokens = oauth_protocol.exchange_authorization_code(
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
    assert (
        before + timedelta(seconds=fake_as.expires_in - 60)
        <= expires_at
        <= before + timedelta(seconds=fake_as.expires_in + 60)
    )
    assert len(fake_as.token_requests) == 1
    (request,) = fake_as.token_requests
    assert request["grant_type"] == "authorization_code"
    assert request["resource"] == fake_as.resource_url, "RFC 8707：token 请求必须带 resource"
    assert request["code_verifier"] == verifier


def test_exchange_rejects_wrong_verifier(fake_as) -> None:
    """PKCE 校验在对面端真实执行：错 verifier 换不出 token。"""
    client_id = _register(fake_as)["client_id"]
    _, _, callback_query = _authorize(fake_as, client_id)
    other_verifier, _ = oauth_protocol.make_pkce_pair()

    with pytest.raises(OAuthError) as excinfo:
        oauth_protocol.exchange_authorization_code(
            f"{fake_as.base_url}/token",
            code=callback_query["code"],
            redirect_uri=CLIENT_REDIRECT,
            client_id=client_id,
            code_verifier=other_verifier,
            resource=fake_as.resource_url,
        )

    assert excinfo.value.code == "TOKEN_REQUEST_FAILED"
    assert fake_as.refresh_tokens == {}, "校验失败不得签发 refresh token"


def test_exchange_rejects_wrong_redirect_uri(fake_as) -> None:
    client_id = _register(fake_as)["client_id"]
    verifier, _, callback_query = _authorize(fake_as, client_id)

    with pytest.raises(OAuthError) as excinfo:
        oauth_protocol.exchange_authorization_code(
            f"{fake_as.base_url}/token",
            code=callback_query["code"],
            redirect_uri="http://127.0.0.1:9999/ext/oauth_broker/callback",
            client_id=client_id,
            code_verifier=verifier,
            resource=fake_as.resource_url,
        )

    assert excinfo.value.code == "TOKEN_REQUEST_FAILED"


def test_exchange_without_expires_in_yields_null_expires_at(auth_server_factory) -> None:
    server = auth_server_factory(include_expires_in=False)
    client_id = _register(server)["client_id"]
    verifier, _, callback_query = _authorize(server, client_id)

    tokens = oauth_protocol.exchange_authorization_code(
        f"{server.base_url}/token",
        code=callback_query["code"],
        redirect_uri=CLIENT_REDIRECT,
        client_id=client_id,
        code_verifier=verifier,
        resource=server.resource_url,
    )

    assert tokens["expires_at"] is None


def test_exchange_with_client_secret_uses_client_secret_post(auth_server_factory) -> None:
    """预注册机密客户端：client_secret 随令牌请求提交（client_secret_post，RFC 6749 §2.3.1）。"""
    server = auth_server_factory(require_client_secret="s3cr3t")
    client_id = _register(server)["client_id"]
    verifier, _, callback_query = _authorize(server, client_id)

    tokens = oauth_protocol.exchange_authorization_code(
        f"{server.base_url}/token",
        code=callback_query["code"],
        redirect_uri=CLIENT_REDIRECT,
        client_id=client_id,
        client_secret="s3cr3t",
        code_verifier=verifier,
        resource=server.resource_url,
    )

    assert tokens["access_token"].startswith("AT-")
    (record,) = server.token_requests
    assert record["client_secret"] == "s3cr3t"


def test_exchange_without_required_client_secret_rejected(auth_server_factory) -> None:
    """对面端要求机密客户端：不带 client_secret 换不出 token（证明该参数真被使用）。"""
    server = auth_server_factory(require_client_secret="s3cr3t")
    client_id = _register(server)["client_id"]
    verifier, _, callback_query = _authorize(server, client_id)

    with pytest.raises(OAuthError) as excinfo:
        oauth_protocol.exchange_authorization_code(
            f"{server.base_url}/token",
            code=callback_query["code"],
            redirect_uri=CLIENT_REDIRECT,
            client_id=client_id,
            code_verifier=verifier,
            resource=server.resource_url,
        )

    assert excinfo.value.code == "TOKEN_REQUEST_FAILED"
    assert "invalid_client" in str(excinfo.value)


def test_refresh_carries_client_secret(auth_server_factory) -> None:
    server = auth_server_factory(require_client_secret="s3cr3t")
    client_id = _register(server)["client_id"]
    verifier, _, callback_query = _authorize(server, client_id)
    first = oauth_protocol.exchange_authorization_code(
        f"{server.base_url}/token",
        code=callback_query["code"],
        redirect_uri=CLIENT_REDIRECT,
        client_id=client_id,
        client_secret="s3cr3t",
        code_verifier=verifier,
        resource=server.resource_url,
    )

    refreshed = oauth_protocol.refresh_access_token(
        f"{server.base_url}/token",
        refresh_token=first["refresh_token"],
        client_id=client_id,
        client_secret="s3cr3t",
        resource=server.resource_url,
    )

    assert refreshed["access_token"] != first["access_token"]
    assert server.token_requests[-1]["client_secret"] == "s3cr3t"


def test_refresh_rotates_refresh_token_and_revokes_old(fake_as) -> None:
    client_id = _register(fake_as)["client_id"]
    verifier, _, callback_query = _authorize(fake_as, client_id)
    first = oauth_protocol.exchange_authorization_code(
        f"{fake_as.base_url}/token",
        code=callback_query["code"],
        redirect_uri=CLIENT_REDIRECT,
        client_id=client_id,
        code_verifier=verifier,
        resource=fake_as.resource_url,
    )

    refreshed = oauth_protocol.refresh_access_token(
        f"{fake_as.base_url}/token",
        refresh_token=first["refresh_token"],
        client_id=client_id,
        resource=fake_as.resource_url,
    )

    assert refreshed["access_token"] != first["access_token"]
    assert refreshed["refresh_token"] != first["refresh_token"], "公共客户端 refresh 轮换"
    # 旧 refresh 已失效（性质：轮换是真撤销，不是换皮）
    with pytest.raises(OAuthError) as excinfo:
        oauth_protocol.refresh_access_token(
            f"{fake_as.base_url}/token",
            refresh_token=first["refresh_token"],
            client_id=client_id,
            resource=fake_as.resource_url,
        )
    assert excinfo.value.code == "TOKEN_REQUEST_FAILED"
    # 新 refresh 可继续用（幂等可用性）
    again = oauth_protocol.refresh_access_token(
        f"{fake_as.base_url}/token",
        refresh_token=refreshed["refresh_token"],
        client_id=client_id,
        resource=fake_as.resource_url,
    )
    assert again["access_token"] != refreshed["access_token"]


# ────────────────────────────────────────────────────────────
# 6. 安全门：非 http/https 端点与错误翻译
# ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "endpoint",
    ["", "   ", "ftp://as.example/token", "file:///etc/passwd", "not-a-url", "https://as.example/x#frag"],
)
def test_non_http_endpoint_rejected(endpoint) -> None:
    with pytest.raises(OAuthError) as excinfo:
        oauth_protocol.exchange_authorization_code(
            endpoint,
            code="c",
            redirect_uri=CLIENT_REDIRECT,
            client_id="client-1",
            code_verifier="v",
            resource="https://mcp.example.com",
        )

    assert excinfo.value.code == "URL_INVALID"


def test_token_endpoint_http_error_translated(auth_server_factory) -> None:
    server = auth_server_factory(token_error=(500, {"error": "server_error"}))

    with pytest.raises(OAuthError) as excinfo:
        oauth_protocol.refresh_access_token(
            f"{server.base_url}/token", refresh_token="RT-x", client_id="client-1", resource=server.resource_url
        )

    assert excinfo.value.code == "TOKEN_REQUEST_FAILED"
    assert "server_error" in str(excinfo.value)


def test_token_response_without_access_token_rejected(auth_server_factory) -> None:
    """200 但响应缺 access_token 的坏件 → TOKEN_RESPONSE_INVALID（不当成功）。"""
    server = auth_server_factory(token_response_override={"token_type": "Bearer"})

    with pytest.raises(OAuthError) as excinfo:
        oauth_protocol.refresh_access_token(
            f"{server.base_url}/token", refresh_token="RT-x", client_id="client-1", resource=server.resource_url
        )

    assert excinfo.value.code == "TOKEN_RESPONSE_INVALID"


def test_registration_response_without_client_id_rejected(auth_server_factory) -> None:
    server = auth_server_factory(dcr_response_override={"redirect_uris": [CLIENT_REDIRECT]})

    with pytest.raises(OAuthError) as excinfo:
        oauth_protocol.register_client(
            f"{server.base_url}/register", redirect_uri=CLIENT_REDIRECT, client_name="agentos-oauth-broker"
        )

    assert excinfo.value.code == "REGISTRATION_FAILED"
    assert "client_id" in str(excinfo.value)


def test_registration_transport_failure_translated() -> None:
    with pytest.raises(OAuthError) as excinfo:
        oauth_protocol.register_client(
            "http://127.0.0.1:1/register", redirect_uri=CLIENT_REDIRECT, client_name="agentos-oauth-broker"
        )

    assert excinfo.value.code == "REGISTRATION_FAILED"


def test_registration_carries_requested_scope(fake_as) -> None:
    oauth_protocol.register_client(
        f"{fake_as.base_url}/register",
        redirect_uri=CLIENT_REDIRECT,
        client_name="agentos-oauth-broker",
        scope="files:read",
    )

    (payload,) = fake_as.registrations
    assert payload["scope"] == "files:read"


def test_protected_resource_non_json_body_fails_closed(auth_server_factory) -> None:
    """200 但正文非 JSON（坏件形态）：所有形态都不可用 → METADATA_INVALID（不当成功）。"""
    server = auth_server_factory(prm_raw_body=b"<html>not json</html>")

    with pytest.raises(OAuthError) as excinfo:
        oauth_protocol.discover_protected_resource(server.resource_url)

    assert excinfo.value.code == "METADATA_INVALID"


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"authorization_endpoint": "ftp://evil.example/authorize"}, id="non-http-authorize-endpoint"),
        pytest.param({"authorization_endpoint": 12345}, id="non-string-authorize-endpoint"),
        pytest.param({"registration_endpoint": 12345}, id="non-string-registration-endpoint"),
    ],
)
def test_authorization_server_metadata_bad_endpoint_fields_rejected(auth_server_factory, document) -> None:
    """端点字段非法（非 http/https / 非字符串）→ METADATA_INVALID（不带坏端点往下走）。"""
    server = auth_server_factory()
    server.as_metadata_override = server.authorization_server_document() | document

    with pytest.raises(OAuthError) as excinfo:
        oauth_protocol.discover_authorization_server(server.issuer)

    assert excinfo.value.code == "METADATA_INVALID"


def test_authorization_server_transport_failure_translated() -> None:
    with pytest.raises(OAuthError) as excinfo:
        oauth_protocol.discover_authorization_server("http://127.0.0.1:1")

    assert excinfo.value.code == "DISCOVERY_FAILED"


def test_token_transport_failure_translated() -> None:
    """令牌端点不可达 → TOKEN_REQUEST_FAILED（错误值，不是裸网络异常）。"""
    with pytest.raises(OAuthError) as excinfo:
        oauth_protocol.refresh_access_token(
            "http://127.0.0.1:1/token", refresh_token="RT-x", client_id="client-1", resource="https://x/mcp"
        )

    assert excinfo.value.code == "TOKEN_REQUEST_FAILED"


def test_transport_failure_translated() -> None:
    """连接层失败（端口无监听）→ DISCOVERY_FAILED（同源候选不再继续试，早失败）。"""
    with pytest.raises(OAuthError) as excinfo:
        oauth_protocol.discover_protected_resource("http://127.0.0.1:1/mcp")

    assert excinfo.value.code == "DISCOVERY_FAILED"


def test_drive_authorization_helper_is_real_protocol_roundtrip(fake_as) -> None:
    """用户代理助手自身走真 302（防「助手悄悄直连内部状态」的自欺）。"""
    client_id = _register(fake_as)["client_id"]
    verifier, challenge = oauth_protocol.make_pkce_pair()
    state = oauth_protocol.make_state()
    url = oauth_protocol.build_authorization_url(
        f"{fake_as.base_url}/authorize",
        client_id=client_id,
        redirect_uri=CLIENT_REDIRECT,
        state=state,
        code_challenge=challenge,
        resource=fake_as.resource_url,
    )

    drive = drive_authorization(url, lambda query: query)

    assert drive["query"]["state"] == state
    assert drive["query"]["code"].startswith("code-")
    assert drive["query"]["iss"] == fake_as.issuer
