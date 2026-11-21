"""MCP 授权协商薄适配（RFC 9728 / RFC 8414 / OIDC Discovery / RFC 7591 / RFC 9207）。

CA-P0 协议机退役自研（2026-11-19）：OAuth 通用机制（PKCE / state / 授权 URL /
token 兑换 / 刷新 / 客户端注册请求体）已整体迁往 :mod:`oauth_engine`（Authlib
唯一实现面），本模块按签名原样 re-export 保持调用面（``flow.py`` 与既有测试的
import 面不变）；自身退化为 **MCP 特有协商的薄适配**——发现候选编排、元数据
文档校验、WWW-Authenticate 指针、动态注册的 HTTP 编排与错误码翻译。token 兑换
/刷新零手搓 HTTP（审计锚：``tests/test_oauth_engine_authlib.py``）。

纯协议层：只做 HTTP 请求与数据校验/构造，不碰 .env、不持流程状态、不打开浏览器。
所有端点 URL 先过 scheme 白名单（http/https 绝对 URL，:func:`oauth_engine.require_http_url`），
其余一律拒（fail-closed）。

受保护资源元数据候选顺序：显式 `resource_metadata_url` 入参 → 无凭据请求 401 的
`WWW-Authenticate: ... resource_metadata="…"` 指针（RFC 9728 §5.1，best-effort：拿不到
不算错）→ well-known 子路径插入形态 → well-known 根形态。某候选「不可用」（非 200 /
非 JSON / 缺 authorization_servers / 传输失败）即继续下一候选，全部不可用才拒
（实测三家真实服务形态不一：Linear 只有根形态可用、Sentry 与 GitHub Copilot MCP
只有子路径形态可用）。候选构造保留仓内实现：mcp SDK 的
``build_protected_resource_metadata_discovery_urls`` 走 urljoin，丢失「子路径插入」
形态（RFC 9728 正确形态、Sentry/GitHub Copilot MCP 真实可用形态，行为被测试钉死）。

授权服务器元数据候选经 mcp SDK
``build_oauth_authorization_server_metadata_discovery_urls``（auth_server_url=issuer：
带路径三候选 RFC 8414 插入 → OIDC 插入 → OIDC 追加；根两候选——与仓内既有顺序逐项
一致，实证见台账 R1）。文档 `issuer` 与探测 issuer 不符 / 缺必需端点 / 明确不支持
S256 则**立即拒**（不换下一形态——那是绕过 issuer 校验的安全性质）。

客户端身份：动态注册（RFC 7591）为主，请求体经 mcp SDK ``OAuthClientMetadata``
单源构造（:func:`oauth_engine.client_registration_payload`）；operator 可传预注册
`client_id`（+ 可选 `client_secret`，走 client_secret_post）覆盖；`client_id` 为
HTTPS URL 时即 Client ID Metadata Documents 形态（2026-07-28 修订在编机制，需
operator 自托管该文档），本层只原样透传，不托管也不校验文档。

[来源: modelcontextprotocol.io/specification/2026-07-28/basic/authorization
 与 .../authorization-server-discovery，2026-10-17 取]
"""

from __future__ import annotations

import contextlib
import json
import urllib.parse
from typing import Any

import oauth_engine as engine
import requests
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    extract_field_from_www_auth,
)
from oauth_engine import (  # noqa: F401 —— re-export：调用面（flow.py / 既有测试）签名不变
    build_authorization_url,
    exchange_authorization_code,
    make_pkce_pair,
    make_state,
    refresh_access_token,
    require_http_url,
)
from oauth_errors import (  # noqa: F401 —— 同上，错误码与 OAuthError re-export
    DEFAULT_TIMEOUT_SECS,
    DISCOVERY_FAILED,
    METADATA_INVALID,
    PKCE_UNSUPPORTED,
    REGISTRATION_FAILED,
    REQUEST_FAILED,
    TOKEN_REQUEST_FAILED,
    TOKEN_RESPONSE_INVALID,
    URL_INVALID,
    OAuthError,
)

MAX_RESPONSE_BYTES = 1_048_576

__all__ = [
    "DEFAULT_TIMEOUT_SECS",
    "DISCOVERY_FAILED",
    "MAX_RESPONSE_BYTES",
    "METADATA_INVALID",
    "OAuthError",
    "PKCE_UNSUPPORTED",
    "REGISTRATION_FAILED",
    "REQUEST_FAILED",
    "TOKEN_REQUEST_FAILED",
    "TOKEN_RESPONSE_INVALID",
    "URL_INVALID",
    "authorization_server_metadata_urls",
    "build_authorization_url",
    "discover_authorization_server",
    "discover_protected_resource",
    "exchange_authorization_code",
    "make_pkce_pair",
    "make_state",
    "protected_resource_metadata_candidates",
    "protected_resource_metadata_urls",
    "refresh_access_token",
    "register_client",
    "require_http_url",
    "resource_metadata_pointer",
]

# mcp SDK 指针提取器形参标注 httpx.Response，实现只消费 ``headers.get(...)``——
# requests Response 鸭子同形。桥接为 Any 以过静态检查（仅此一处 SDK 签名面）。
_www_auth_field_extractor: Any = extract_field_from_www_auth


# ────────────────────────────────────────────────────────────
# HTTP（元数据 GET 面；token/注册不在本模块）
# ────────────────────────────────────────────────────────────


def _http_json(url: str, *, what: str, timeout: float = DEFAULT_TIMEOUT_SECS) -> tuple[int, Any]:
    """GET 一次 JSON 面，返回 ``(status, JSON 解析结果或 None)``；传输层失败 → REQUEST_FAILED。

    响应体读取封顶 :data:`MAX_RESPONSE_BYTES`（超限按解析失败处理——文档面 DoS 卫生）。
    """
    require_http_url(url, what)
    try:
        with requests.get(url, headers={"Accept": "application/json"}, timeout=timeout, stream=True) as response:
            status = response.status_code
            body = b""
            for chunk in response.iter_content(chunk_size=65536):
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    return status, None
    except requests.exceptions.RequestException as exc:
        raise OAuthError(REQUEST_FAILED, f"{what} 请求失败：{exc}") from exc
    try:
        return status, json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, None


def _origin_and_path(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}", parts.path.rstrip("/")


# ────────────────────────────────────────────────────────────
# 发现（RFC 9728 / RFC 8414 + OIDC Discovery）
# ────────────────────────────────────────────────────────────


def resource_metadata_pointer(server_url: str, *, timeout: float = DEFAULT_TIMEOUT_SECS) -> str | None:
    """RFC 9728 §5.1：无凭据请求的 401 响应里 `WWW-Authenticate` 的 `resource_metadata` 指针。

    best-effort：拿不到（非 401 / 无该参数 / 请求失败）返回 None——那不是错误，
    well-known 候选才是规范要求的兜底路径。指针值解析经 mcp SDK
    ``extract_field_from_www_auth``。
    """
    try:
        require_http_url(server_url, "MCP 服务器 URL")
        with requests.get(server_url, headers={"Accept": "application/json"}, timeout=timeout, stream=True) as response:
            status = response.status_code
    except (OAuthError, requests.exceptions.RequestException):
        return None
    if status != 401:
        return None
    value = _www_auth_field_extractor(response, "resource_metadata")
    return value.strip() if isinstance(value, str) and value.strip() else None


def protected_resource_metadata_urls(server_url: str) -> list[str]:
    """well-known 候选（规范顺序：子路径插入形态 → 根形态；仓内构造，理由见模块 docstring）。"""
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
    """授权服务器元数据候选（mcp SDK 工具承载；RFC 8414 路径插入 → OIDC 插入 → OIDC 追加）。

    第二形参只服务 SDK 的 legacy 兜底分支（auth_server_url 恒非 None 时不可达），同传 issuer。
    """
    return build_oauth_authorization_server_metadata_discovery_urls(issuer, issuer)


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
# 动态客户端注册（RFC 7591）——HTTP 编排；请求体单源在 oauth_engine（mcp SDK 模型）
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
    payload = engine.client_registration_payload(client_name=client_name, redirect_uri=redirect_uri, scope=scope)
    try:
        response = requests.post(
            registration_endpoint, json=payload, headers={"Accept": "application/json"}, timeout=timeout
        )
    except requests.exceptions.RequestException as exc:
        raise OAuthError(REGISTRATION_FAILED, f"动态客户端注册失败：{exc}") from exc
    try:
        document = response.json() if response.content else None
    except ValueError:
        document = None
    if not 200 <= response.status_code < 300 or not isinstance(document, dict):
        reason = document.get("error") if isinstance(document, dict) else f"HTTP {response.status_code}"
        raise OAuthError(REGISTRATION_FAILED, f"动态客户端注册被拒（{reason}）")
    client_id = document.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise OAuthError(REGISTRATION_FAILED, "注册响应缺 client_id")
    return {"client_id": client_id}
