"""OAuth 协议引擎——Authlib 唯一实现面（CA-P0 协议机退役自研，2026-11-19）。

设计真值：``docs/working/连接面_连接器框架与成熟件优先授权覆盖_20261119.md`` §4
（淘汰判据：自研协议机不再新增任何平台适配代码；规范演进由官方库扛）。

本模块是 oauth_broker 内唯一允许出现 OAuth 协议机制（PKCE / state / 授权 URL /
token 兑换 / 刷新 / 客户端注册请求体）的地方；上层 ``oauth_protocol.py`` 只做
MCP 特有协商的薄适配（发现候选编排 / 元数据文档校验 / WWW-Authenticate 指针 /
注册 HTTP 编排）。token 兑换与刷新的 HTTP 一律经 Authlib ``OAuth2Session``，
无手搓 form POST（审计锚：``tests/test_oauth_engine_authlib.py`` 源码审计用例）。

客户端会话两档（:func:`_client_session`）：机密客户端 = ``client_secret_post``
（secret 进 form，RFC 6749 §2.3.1，与既有 ``.env`` 落库契约同语义）；公共客户端 =
``none``（client_id 进 form）。**显式设档**防 Authlib 缺省
``client_secret_basic``（Basic 头路线与对面端 form 契约不符）。

客户端注册请求体经 mcp SDK ``OAuthClientMetadata`` 单源构造（RFC 7591 公共
客户端字段面：grant_types / response_types / token_endpoint_auth_method=none 由
SDK 模型给出，不自拼）。

[来源: Authlib https://docs.authlib.org/en/latest/client/oauth2.html ；
 mcp python-sdk OAuthClientMetadata；行为实证（fetch_token body 形态 / 错误分层 /
 200 缺 access_token 不抛需自查）见 docs/working/连接面执行_台账.md R1，2026-11-19 取]
"""

from __future__ import annotations

import dataclasses
import urllib.parse
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import requests
from authlib.common.security import generate_token
from authlib.integrations.base_client.errors import OAuthError as AuthlibOAuthError
from authlib.integrations.requests_client import OAuth2Session
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from mcp.shared.auth import OAuthClientMetadata
from oauth_errors import (
    DEFAULT_TIMEOUT_SECS,
    TOKEN_REQUEST_FAILED,
    TOKEN_RESPONSE_INVALID,
    URL_INVALID,
    OAuthError,
)
from pydantic import AnyUrl

__all__ = [
    "DEFAULT_TIMEOUT_SECS",
    "TOKEN_REQUEST_FAILED",
    "TOKEN_RESPONSE_INVALID",
    "URL_INVALID",
    "OFFLINE_ACCESS_SCOPE",
    "OAuthError",
    "ScopeTiers",
    "build_authorization_url",
    "client_registration_payload",
    "exchange_authorization_code",
    "make_pkce_pair",
    "make_state",
    "refresh_access_token",
    "require_http_url",
    "select_request_scopes",
]

#: PKCE 方法（本客户端只做 S256；RFC 7636）
PKCE_METHOD = "S256"

#: N8：拿 refresh token 的 scope（公告了才自动追加，见 flow._negotiate_request_scope）
OFFLINE_ACCESS_SCOPE = "offline_access"


# ────────────────────────────────────────────────────────────
# scope 分档骨架（N13/N14，设计真值 §4：写进引擎而非 yaml）
# ────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class ScopeTiers:
    """provider scope 分档声明（N13 分层 scope 渐进请求的最小数据结构）。

    ``base`` = 只读档（连接默认授予）；``write`` = 写/发送档（**独立审批动作**
    才启用，N14——写档是 base 的 superset，启用时合并请求避免重复弹窗）。
    后续批次 provider yaml 落地时，yaml 的 scopes 段直接映射到本结构。
    """

    base: tuple[str, ...]
    write: tuple[str, ...] = ()


def select_request_scopes(
    tiers: ScopeTiers,
    *,
    include_write: bool = False,
    advertised: Iterable[str] = (),
) -> str | None:
    """按分档选出本次授权请求的 scope 串（N14：base 默认；write 仅显式批准）。

    去重保序合并（superset 语义）；``advertised`` 公告含 ``offline_access`` 且未
    选中时自动追加（N8 同款口径）。空档且无追加 → ``None``（请求不带 scope）。
    """
    selected = list(dict.fromkeys([*tiers.base, *(tiers.write if include_write else ())]))
    if OFFLINE_ACCESS_SCOPE in set(advertised) and OFFLINE_ACCESS_SCOPE not in selected:
        selected.append(OFFLINE_ACCESS_SCOPE)
    return " ".join(selected) or None


# ────────────────────────────────────────────────────────────
# URL 校验（安全门：http/https 白名单）
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


# ────────────────────────────────────────────────────────────
# PKCE / state（Authlib 实现）
# ────────────────────────────────────────────────────────────


def make_pkce_pair() -> tuple[str, str]:
    """生成 PKCE(S256) 对：``(code_verifier, code_challenge)``（Authlib rfc7636）。"""
    verifier = generate_token(64)
    return verifier, create_s256_code_challenge(verifier)


def make_state() -> str:
    """CSRF 状态值（回调必须原样带回且命中挂起表）。"""
    return generate_token(32)


# ────────────────────────────────────────────────────────────
# 授权 URL（Authlib create_authorization_url）
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
    """构造授权请求 URL（PKCE(S256) + state + RFC 8707 resource）。

    经 Authlib ``create_authorization_url``：response_type/client_id/redirect_uri/
    state 由 Authlib 单源给出；``code_challenge``/``code_challenge_method``/
    ``resource`` 走 kwargs 直通 query（challenge 由上层从 :func:`make_pkce_pair`
    持有——Authlib 的 S256 变换已在 challenge 计算处发生，此处不重算）。
    """
    require_http_url(authorization_endpoint, "授权端点")
    client = OAuth2Session(client_id=client_id, redirect_uri=redirect_uri)
    url, _state = client.create_authorization_url(
        authorization_endpoint,
        state=state,
        scope=scope,
        resource=resource,
        code_challenge=code_challenge,
        code_challenge_method=PKCE_METHOD,
    )
    return url


# ────────────────────────────────────────────────────────────
# 令牌端点（Authlib OAuth2Session）
# ────────────────────────────────────────────────────────────


def _client_session(client_id: str, client_secret: str | None, redirect_uri: str | None) -> OAuth2Session:
    """Authlib 会话两档（见模块 docstring：显式设档防缺省 Basic）。"""
    if client_secret:
        return OAuth2Session(
            client_id=client_id,
            client_secret=client_secret,
            token_endpoint_auth_method="client_secret_post",
            redirect_uri=redirect_uri,
        )
    return OAuth2Session(client_id=client_id, token_endpoint_auth_method="none", redirect_uri=redirect_uri)


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
    """授权码换令牌（PKCE verifier 随请求回带；RFC 8707 resource 同带）。"""
    require_http_url(token_endpoint, "令牌端点")
    session = _client_session(client_id, client_secret, redirect_uri)
    document = _token_request(
        lambda: session.fetch_token(
            token_endpoint,
            code=code,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
            resource=resource,
            state=None,
            timeout=timeout,
        )
    )
    return _normalize_token(document)


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
    require_http_url(token_endpoint, "令牌端点")
    session = _client_session(client_id, client_secret, None)
    document = _token_request(
        lambda: session.refresh_token(
            token_endpoint, refresh_token=refresh_token, resource=resource, timeout=timeout
        )
    )
    return _normalize_token(document)


def _token_request(call: Any) -> dict[str, Any]:
    """跑一次令牌请求并把 Authlib/requests 异常翻译成 OAuthError（错误是值）。

    翻译表（行为实证见台账 R1）：
    - Authlib ``OAuthError``（4xx JSON 错误，``str`` 形如 ``invalid_grant: …``）
      → TOKEN_REQUEST_FAILED（error 字样进消息，供人读与测试锚）；
    - requests ``HTTPError``（5xx，Authlib raise_for_status）→ 从响应体提取
      ``error`` 作 reason，回退 ``str(exc)`` → TOKEN_REQUEST_FAILED；
    - 其余 requests 异常（连接/超时）→ TOKEN_REQUEST_FAILED（传输失败）；
    - ``ValueError``（200 非 JSON 响应体）→ TOKEN_RESPONSE_INVALID。
    """
    try:
        document = call()
    except AuthlibOAuthError as exc:
        raise OAuthError(TOKEN_REQUEST_FAILED, f"令牌请求被拒（{exc}）") from exc
    except requests.exceptions.HTTPError as exc:
        raise OAuthError(TOKEN_REQUEST_FAILED, f"令牌请求被拒（{_response_error_reason(exc)}）") from exc
    except requests.exceptions.RequestException as exc:
        raise OAuthError(TOKEN_REQUEST_FAILED, f"令牌请求失败：{exc}") from exc
    except ValueError as exc:
        raise OAuthError(TOKEN_RESPONSE_INVALID, f"令牌响应非 JSON：{exc}") from exc
    return document


def _response_error_reason(exc: requests.exceptions.HTTPError) -> str:
    """从 5xx 响应体提取 OAuth error 字段（对面端语义优先于 HTTP 文案）。"""
    response = exc.response
    if response is not None:
        try:
            document = response.json()
        except ValueError:
            document = None
        if isinstance(document, dict):
            error = document.get("error")
            if isinstance(error, str) and error:
                return error
    return str(exc)


def _normalize_token(document: Any) -> dict[str, Any]:
    """令牌响应归一（只带已知字段；未知字段丢弃；expires_at 归一 ISO8601 UTC）。"""
    if not isinstance(document, dict):
        raise OAuthError(TOKEN_RESPONSE_INVALID, "令牌响应非 JSON 对象")
    access_token = document.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthError(TOKEN_RESPONSE_INVALID, "令牌响应缺 access_token")
    refresh_token = document.get("refresh_token")
    scope = document.get("scope")
    token_type = document.get("token_type")
    return {
        "access_token": access_token,
        "refresh_token": refresh_token if isinstance(refresh_token, str) and refresh_token else None,
        "token_type": token_type if isinstance(token_type, str) else None,
        "scope": scope if isinstance(scope, str) else None,
        "expires_at": _expires_at_iso(document),
    }


def _expires_at_iso(document: dict[str, Any]) -> str | None:
    """``expires_at``：Authlib 给 epoch 秒则直转；回退 ``expires_in`` 自算；缺席 → None。"""
    expires_at = document.get("expires_at")
    if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
        return datetime.fromtimestamp(expires_at, tz=UTC).isoformat()
    expires_in = document.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool) and expires_in >= 0:
        return (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat()
    return None


# ────────────────────────────────────────────────────────────
# 客户端注册请求体（mcp SDK OAuthClientMetadata 单源）
# ────────────────────────────────────────────────────────────


def client_registration_payload(*, client_name: str, redirect_uri: str, scope: str | None) -> dict[str, Any]:
    """RFC 7591 注册请求体（mcp SDK 模型单源：grant_types/response_types/认证方法）。"""
    metadata = OAuthClientMetadata(
        client_name=client_name,
        redirect_uris=[AnyUrl(redirect_uri)],
        scope=scope or None,
        token_endpoint_auth_method="none",
    )
    return metadata.model_dump(by_alias=True, mode="json", exclude_none=True)
