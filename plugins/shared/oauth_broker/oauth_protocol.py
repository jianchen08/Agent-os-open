"""MCP 授权协议原语（RFC 9728 / RFC 8414 / OIDC Discovery / RFC 7591 / OAuth 2.1 + PKCE / RFC 8707 / RFC 9207）。

纯协议层：只做 HTTP 请求与数据校验/构造，不碰 .env、不持流程状态、不打开浏览器。
所有端点 URL 先过 scheme 白名单（http/https 绝对 URL），其余一律拒（fail-closed）。

受保护资源元数据候选顺序：显式 `resource_metadata_url` 入参 → 无凭据请求 401 的
`WWW-Authenticate: ... resource_metadata="…"` 指针（RFC 9728 §5.1，best-effort：拿不到
不算错）→ well-known 子路径插入形态 → well-known 根形态。某候选「不可用」（非 200 /
非 JSON / 缺 authorization_servers / 传输失败）即继续下一候选，全部不可用才拒
（实测三家真实服务形态不一：Linear 只有根形态可用、Sentry 与 GitHub Copilot MCP
只有子路径形态可用）。

授权服务器元数据候选（同源，仅路径不同）：RFC 8414 路径插入 → OIDC 路径插入 → OIDC
路径追加（规范正文顺序）。文档 `issuer` 与探测 issuer 不符 / 缺必需端点 / 明确不支持
S256 则**立即拒**（不换下一形态——那是绕过 issuer 校验的安全性质）。

客户端身份：动态注册（RFC 7591）为主；operator 可传预注册 `client_id`（+ 可选
`client_secret`，走 client_secret_post）覆盖；`client_id` 为 HTTPS URL 时即
Client ID Metadata Documents 形态（2026-07-28 修订在编机制，需 operator 自托管该文档），
本层只原样透传，不托管也不校验文档。

[来源: modelcontextprotocol.io/specification/2026-07-28/basic/authorization
 与 .../authorization-server-discovery，2026-10-17 取]
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_TIMEOUT_SECS = 20.0
MAX_RESPONSE_BYTES = 1_048_576

#: 错误码（机器可读；工具边界按 code 出失败信封）
URL_INVALID = "URL_INVALID"
REQUEST_FAILED = "REQUEST_FAILED"
DISCOVERY_FAILED = "DISCOVERY_FAILED"
METADATA_INVALID = "METADATA_INVALID"
PKCE_UNSUPPORTED = "PKCE_UNSUPPORTED"
REGISTRATION_FAILED = "REGISTRATION_FAILED"
TOKEN_REQUEST_FAILED = "TOKEN_REQUEST_FAILED"
TOKEN_RESPONSE_INVALID = "TOKEN_RESPONSE_INVALID"


class OAuthError(Exception):
    """协议层失败（错误是值：``code`` 机器可读、文本给人看）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ────────────────────────────────────────────────────────────
# URL 校验与 HTTP
# ────────────────────────────────────────────────────────────


def require_http_url(url: str, what: str) -> str:
    """端点 URL 白名单：http/https 绝对 URL，无 fragment，否则 URL_INVALID。"""
    if not isinstance(url, str) or not url.strip():
        raise OAuthError(URL_INVALID, f"{what} 不能为空")
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise OAuthError(URL_INVALID, f"{what} 仅支持 http/https 绝对 URL：{url}")
    if parts.fragment:
        raise OAuthError(URL_INVALID, f"{what} 不得含 fragment：{url}")
    return url.strip()


def _http_json(
    url: str, *, what: str, method: str = "GET", data: bytes | None = None, timeout: float = DEFAULT_TIMEOUT_SECS
) -> tuple[int, Any]:
    """发一次请求，返回 ``(status, JSON 解析结果或 None)``；传输层失败 → REQUEST_FAILED。"""
    require_http_url(url, what)
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 —— scheme 已白名单
            status, body = response.status, response.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read(MAX_RESPONSE_BYTES)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OAuthError(REQUEST_FAILED, f"{what} 请求失败：{exc}") from exc
    try:
        return status, json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, None


def _post_json(url: str, payload: dict[str, Any], *, what: str, timeout: float) -> tuple[int, Any]:
    body = json.dumps(payload).encode("utf-8")
    return _http_json(url, what=what, method="POST", data=body, timeout=timeout)


def _post_form(url: str, form: dict[str, str], *, what: str, timeout: float) -> tuple[int, Any]:
    return _http_json(url, what=what, method="POST", data=urllib.parse.urlencode(form).encode(), timeout=timeout)


def _unauthenticated_get(url: str, *, timeout: float) -> tuple[int, dict[str, str]]:
    """无凭据 GET 一次，返回 ``(status, 小写键头表)``；传输层失败记 ``(0, {})``。

    只服务 `WWW-Authenticate` 指针探测（best-effort 提示面）：失败不构成错误值，
    由调用方按「无指针」继续走 well-known 候选。
    """
    try:
        require_http_url(url, "MCP 服务器 URL")
        request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 —— scheme 已白名单
            return response.status, {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}
    except (OAuthError, urllib.error.URLError, OSError, ValueError):
        return 0, {}


def resource_metadata_pointer(server_url: str, *, timeout: float = DEFAULT_TIMEOUT_SECS) -> str | None:
    """RFC 9728 §5.1：无凭据请求的 401 响应里 `WWW-Authenticate` 的 `resource_metadata` 指针。

    best-effort：拿不到（非 401 / 无该参数 / 请求失败）返回 None——那不是错误，
    well-known 候选才是规范要求的兜底路径。
    """
    status, headers = _unauthenticated_get(server_url, timeout=timeout)
    if status != 401:
        return None
    match = re.search(r'resource_metadata\s*=\s*"([^"]+)"', headers.get("www-authenticate", ""))
    return match.group(1).strip() if match else None


def _origin_and_path(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}", parts.path.rstrip("/")


# ────────────────────────────────────────────────────────────
# 发现（RFC 9728 / RFC 8414 + OIDC Discovery）
# ────────────────────────────────────────────────────────────


def protected_resource_metadata_urls(server_url: str) -> list[str]:
    """well-known 候选（规范顺序：子路径插入形态 → 根形态）。"""
    origin, path = _origin_and_path(server_url)
    urls = [f"{origin}/.well-known/oauth-protected-resource{path}"] if path else []
    urls.append(f"{origin}/.well-known/oauth-protected-resource")
    return urls


def protected_resource_metadata_candidates(
    server_url: str, *, resource_metadata_url: str | None = None, timeout: float = DEFAULT_TIMEOUT_SECS
) -> list[str]:
    """完整候选面（去重保序）：显式指针 → WWW-Authenticate 指针 → 子路径形态 → 根形态。"""
    candidates: list[str] = []
    if resource_metadata_url:
        candidates.append(require_http_url(resource_metadata_url, "resource_metadata 指针"))
    pointer = resource_metadata_pointer(server_url, timeout=timeout)
    if pointer:
        # 指针是提示面：非 http/https 的怪值按「无指针」跳过（不因提示面失败而阻断发现）
        with contextlib.suppress(OAuthError):
            candidates.append(require_http_url(pointer, "WWW-Authenticate 的 resource_metadata"))
    candidates.extend(protected_resource_metadata_urls(server_url))
    deduped: list[str] = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return deduped


def authorization_server_metadata_urls(issuer: str) -> list[str]:
    """授权服务器元数据候选（规范顺序：RFC 8414 路径插入 → OIDC 路径插入 → OIDC 路径追加）。"""
    origin, path = _origin_and_path(issuer)
    candidates = (
        [
            f"{origin}/.well-known/oauth-authorization-server{path}",
            f"{origin}/.well-known/openid-configuration{path}",
            f"{origin}{path}/.well-known/openid-configuration",
        ]
        if path
        else [f"{origin}/.well-known/oauth-authorization-server", f"{origin}/.well-known/openid-configuration"]
    )
    return candidates


def _protected_resource_document(server_url: str, url: str, document: Any) -> dict[str, Any]:
    """校验并归一受保护资源元数据文档（不可用 → METADATA_INVALID）。"""
    if not isinstance(document, dict):
        raise OAuthError(METADATA_INVALID, f"受保护资源元数据非 JSON 对象：{url}")
    servers = document.get("authorization_servers")
    if not isinstance(servers, list) or not servers or not all(isinstance(item, str) for item in servers):
        raise OAuthError(METADATA_INVALID, f"受保护资源元数据缺 authorization_servers：{url}")
    for issuer in servers:
        parts = urllib.parse.urlsplit(issuer)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise OAuthError(METADATA_INVALID, f"authorization_servers 条目非 http/https：{issuer}")
    resource = document.get("resource")
    scopes = document.get("scopes_supported")
    return {
        "resource": resource if isinstance(resource, str) and resource else server_url,
        "authorization_servers": list(servers),
        "scopes_supported": [item for item in scopes if isinstance(item, str)] if isinstance(scopes, list) else [],
    }


def discover_protected_resource(
    server_url: str, *, resource_metadata_url: str | None = None, timeout: float = DEFAULT_TIMEOUT_SECS
) -> dict[str, Any]:
    """受保护资源元数据发现：返回 ``{"resource", "authorization_servers", "scopes_supported"}``。

    任一候选「不可用」即继续下一候选；全部不可用才拒。若曾有候选 200 但文档不可用，
    最终错误码为 METADATA_INVALID（区别于全 404/不可达的 DISCOVERY_FAILED）。

    Raises:
        OAuthError: URL_INVALID / METADATA_INVALID / DISCOVERY_FAILED。
    """
    server_url = require_http_url(server_url, "MCP 服务器 URL")
    last = ""
    saw_invalid_document = False
    for url in protected_resource_metadata_candidates(
        server_url, resource_metadata_url=resource_metadata_url, timeout=timeout
    ):
        try:
            status, document = _http_json(url, what="受保护资源元数据", timeout=timeout)
        except OAuthError as exc:
            last = f"{url} → {exc}"
            continue
        if status != 200:
            last = f"{url} → HTTP {status}"
            continue
        try:
            return _protected_resource_document(server_url, url, document)
        except OAuthError as exc:
            last = f"{url} → {exc}"
            saw_invalid_document = True
            continue
    if saw_invalid_document:
        raise OAuthError(METADATA_INVALID, f"受保护资源元数据均不可用（{server_url}）：{last}")
    raise OAuthError(DISCOVERY_FAILED, f"未发现受保护资源元数据（{server_url}）：{last}")
    raise OAuthError(DISCOVERY_FAILED, f"未发现受保护资源元数据（{server_url}）：{last}")


def _metadata_endpoint(document: dict[str, Any], field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value:
        raise OAuthError(METADATA_INVALID, f"授权服务器元数据缺 {field}")
    try:
        return require_http_url(value, field)
    except OAuthError as exc:
        raise OAuthError(METADATA_INVALID, f"授权服务器元数据的 {field} 非法：{value}") from exc


def discover_authorization_server(issuer: str, *, timeout: float = DEFAULT_TIMEOUT_SECS) -> dict[str, Any]:
    """授权服务器元数据发现（RFC 8414 / OIDC Discovery 按候选顺序）。

    ``issuer`` 必须与文档 ``issuer`` 逐字相符（RFC 8414 §3.3）；缺
    authorization_endpoint / token_endpoint、或明确只支持非 S256 的
    code_challenge_methods → 拒。

    Raises:
        OAuthError: URL_INVALID / METADATA_INVALID / PKCE_UNSUPPORTED / DISCOVERY_FAILED。
    """
    issuer = require_http_url(issuer, "授权服务器 issuer")
    last = ""
    for url in authorization_server_metadata_urls(issuer):
        # 候选同源（仅路径不同）：传输层失败 = 源不可达，直接判发现失败，不再试其余候选
        try:
            status, document = _http_json(url, what="授权服务器元数据", timeout=timeout)
        except OAuthError as exc:
            raise OAuthError(DISCOVERY_FAILED, f"授权服务器元数据不可达（{url}）：{exc}") from exc
        if status != 200 or not isinstance(document, dict):
            last = f"{url} → HTTP {status}"
            continue
        document_issuer = document.get("issuer")
        if not isinstance(document_issuer, str) or document_issuer != issuer:
            raise OAuthError(
                METADATA_INVALID, f"元数据 issuer 与探测 issuer 不符（{document_issuer!r} != {issuer!r}）：{url}"
            )
        methods = document.get("code_challenge_methods_supported")
        if isinstance(methods, list) and methods and "S256" not in methods:
            raise OAuthError(PKCE_UNSUPPORTED, f"授权服务器未声明支持 S256：{methods}")
        registration = document.get("registration_endpoint")
        if registration is not None and not isinstance(registration, str):
            raise OAuthError(METADATA_INVALID, "授权服务器元数据的 registration_endpoint 非法")
        scopes = document.get("scopes_supported")
        return {
            "issuer": document_issuer,
            "authorization_endpoint": _metadata_endpoint(document, "authorization_endpoint"),
            "token_endpoint": _metadata_endpoint(document, "token_endpoint"),
            "registration_endpoint": registration,
            "iss_parameter_supported": bool(document.get("authorization_response_iss_parameter_supported")),
            "client_id_metadata_document_supported": bool(document.get("client_id_metadata_document_supported")),
            "scopes_supported": [s for s in scopes if isinstance(s, str)] if isinstance(scopes, list) else [],
        }
    raise OAuthError(
        DISCOVERY_FAILED,
        f"未发现授权服务器元数据（{issuer}）：{last}。"
        "该服务可能不提供 OAuth 元数据（老式预注册端点，如 GitHub）："
        "本插件无法自动完成授权，请改用它自己的 OAuth App（预注册 client_id/client_secret）"
        "或在服务侧配置 PAT/API key 直连",
    )


# ────────────────────────────────────────────────────────────
# 动态客户端注册（RFC 7591）与 PKCE
# ────────────────────────────────────────────────────────────


def register_client(
    registration_endpoint: str,
    *,
    redirect_uri: str,
    client_name: str,
    scope: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECS,
) -> dict[str, Any]:
    """注册公共客户端（``token_endpoint_auth_method: none``，回调 URI 锁定本机）。

    Raises:
        OAuthError: URL_INVALID / REGISTRATION_FAILED。
    """
    require_http_url(registration_endpoint, "动态客户端注册端点")
    payload: dict[str, Any] = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if scope:
        payload["scope"] = scope
    try:
        status, document = _post_json(registration_endpoint, payload, what="动态客户端注册", timeout=timeout)
    except OAuthError as exc:
        raise OAuthError(REGISTRATION_FAILED, f"动态客户端注册失败：{exc}") from exc
    if not 200 <= status < 300 or not isinstance(document, dict):
        reason = document.get("error") if isinstance(document, dict) else f"HTTP {status}"
        raise OAuthError(REGISTRATION_FAILED, f"动态客户端注册被拒（{reason}）")
    client_id = document.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise OAuthError(REGISTRATION_FAILED, "注册响应缺 client_id")
    return {"client_id": client_id}


def make_pkce_pair() -> tuple[str, str]:
    """生成 PKCE(S256) 对：``(code_verifier, code_challenge)``。"""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def make_state() -> str:
    """CSRF 状态值（回调必须原样带回且命中挂起表）。"""
    return secrets.token_urlsafe(32)


# ────────────────────────────────────────────────────────────
# 授权 URL 与令牌端点
# ────────────────────────────────────────────────────────────


def build_authorization_url(
    authorization_endpoint: str,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    scope: str | None = None,
    resource: str | None = None,
) -> str:
    """构造授权请求 URL（PKCE(S256) + state + RFC 8707 resource）。"""
    require_http_url(authorization_endpoint, "授权端点")
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if scope:
        params["scope"] = scope
    if resource:
        params["resource"] = resource
    separator = "&" if urllib.parse.urlsplit(authorization_endpoint).query else "?"
    return f"{authorization_endpoint}{separator}{urllib.parse.urlencode(params)}"


def _parse_token_response(document: dict[str, Any]) -> dict[str, Any]:
    """令牌响应归一（只带已知字段；未知字段丢弃）。"""
    access_token = document.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthError(TOKEN_RESPONSE_INVALID, "令牌响应缺 access_token")
    expires_at = None
    expires_in = document.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool) and expires_in >= 0:
        expires_at = (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat()
    refresh_token = document.get("refresh_token")
    scope = document.get("scope")
    token_type = document.get("token_type")
    return {
        "access_token": access_token,
        "refresh_token": refresh_token if isinstance(refresh_token, str) and refresh_token else None,
        "token_type": token_type if isinstance(token_type, str) else None,
        "scope": scope if isinstance(scope, str) else None,
        "expires_at": expires_at,
    }


def _token_request(token_endpoint: str, form: dict[str, str], timeout: float) -> dict[str, Any]:
    require_http_url(token_endpoint, "令牌端点")
    try:
        status, document = _post_form(token_endpoint, form, what="令牌请求", timeout=timeout)
    except OAuthError as exc:
        raise OAuthError(TOKEN_REQUEST_FAILED, f"令牌请求失败：{exc}") from exc
    if not 200 <= status < 300 or not isinstance(document, dict):
        reason = document.get("error") if isinstance(document, dict) else f"HTTP {status}"
        raise OAuthError(TOKEN_REQUEST_FAILED, f"令牌请求被拒（{reason}）")
    return _parse_token_response(document)


def exchange_authorization_code(
    token_endpoint: str,
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    code_verifier: str,
    client_secret: str | None = None,
    resource: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECS,
) -> dict[str, Any]:
    """授权码换令牌（PKCE verifier 随请求回带；RFC 8707 resource 同带）。

    `client_secret` 给定时按 client_secret_post（RFC 6749 §2.3.1）随表单提交——
    预注册机密客户端形态；缺省 = 公共客户端（DCR `token_endpoint_auth_method: none`）。
    """
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        form["client_secret"] = client_secret
    if resource:
        form["resource"] = resource
    return _token_request(token_endpoint, form, timeout)


def refresh_access_token(
    token_endpoint: str,
    *,
    refresh_token: str,
    client_id: str,
    client_secret: str | None = None,
    resource: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECS,
) -> dict[str, Any]:
    """refresh_token 授权（公共客户端轮换：响应里的新 refresh 由调用方落库）。"""
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id}
    if client_secret:
        form["client_secret"] = client_secret
    if resource:
        form["resource"] = resource
    return _token_request(token_endpoint, form, timeout)
