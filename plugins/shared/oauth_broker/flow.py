"""OAuth 授权流程编排：发现 → 注册 → PKCE → 授权 URL → 本机回调 → 落库 → 刷新。

流程状态（``state`` ↔ code_verifier / issuer / 端点 / 回调 URI）只活在插件进程
内存：工具调用与回调请求同进程同事件循环（SDK 对入站请求逐消息起独立 task），
挂起表以 ``state`` 为键。sidecar 重启会丢挂起表——回调届时 fail-closed 拒并
提示重新调用；verifier 不落盘（一次性秘密，落盘即扩大泄漏面）。

回调入口由内核 ``/ext/<plugin_id>/**`` 分发到本插件 ``http.handle``
（见 ``plugin.json`` 的 ``http_endpoints``，``auth=none``：浏览器重定向不带凭据，
``state`` 命中 + ``iss`` 逐字相符 + PKCE 是三道闸门）。

刷新语义：``oauth_authorize`` 是「确保已授权」——有效 token 直接回已授权；
过期且有 refresh token 先走刷新（公共客户端轮换，新 refresh 落库）；刷新被拒
如实上报 ``refresh_error`` 后回落全量授权。

grants 账本接线（P2，ADR 2026-11-19）：授权成立（existing/refresh/authorization_code
任一路径到达 authorized）即经 ``grants_link`` 幂等落 domain grant（source=oauth_broker）；
刷新被拒（凭据失效信号）联动 revoke 该服务器既有 grant。账本是记录面：缺席/异常
只降级记录，不阻断授权主流程。
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import grants_link
import guidance as guidance_module
import oauth_protocol as protocol
from env_store import (
    ENV_KEY_SUFFIXES,
    derive_env_prefix,
    env_file_path,
    key_name,
    merge_env_updates,
    read_env,
    status_entries,
)
from oauth_protocol import OAuthError

__all__ = [
    "AUTHORIZE_MODES",
    "CALLBACK_PATH",
    "DEFAULT_WAIT_SECONDS",
    "MAX_WAIT_SECONDS",
    "ENV_KEY_SUFFIXES",
    "GUIDANCE_ACTIONS",
    "GUIDANCE_ACTION_CREATE_PAT",
    "GUIDANCE_ACTION_REGISTER_APP",
    "MODE_BROWSER",
    "MODE_PASTE_CALLBACK",
    "authorize",
    "callback_url",
    "complete_pasted_callback",
    "derive_env_prefix",
    "handle_callback",
    "normalize_server_url",
    "open_browser",
    "resolve_callback_base",
    "status",
]

#: 本机回调路由（plugin.json http_endpoints 声明的同一路径）
CALLBACK_PATH = "/ext/oauth_broker/callback"
DEFAULT_WAIT_SECONDS = 180.0
MAX_WAIT_SECONDS = 900.0
#: 挂起流程保活上限（回调晚到仍可完成；超期条目在新流程启动时清理）
PENDING_TTL_SECONDS = 900.0
DEFAULT_KERNEL_PORT = 9100
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

#: 引导分支本次打开哪个页面（注册 OAuth App / 创建 PAT）
GUIDANCE_ACTION_REGISTER_APP = "register_app"
GUIDANCE_ACTION_CREATE_PAT = "create_pat"
GUIDANCE_ACTIONS = (GUIDANCE_ACTION_REGISTER_APP, GUIDANCE_ACTION_CREATE_PAT)

# ── N3 登录模式（Codex McpLoginMode::{Browser, PasteCallback} 同款，2026-11-19）──
#: browser = 拉本机浏览器 + 本机回调收码（RFC 8252 loopback）；paste_callback =
#: 不拉浏览器出授权 URL，用户手动完成后把回调 URL 粘回（无浏览器/远程兜底，
#: Device Flow 之外的再一层兜底，设计真值 §5 表）。
MODE_BROWSER = "browser"
MODE_PASTE_CALLBACK = "paste_callback"
AUTHORIZE_MODES = (MODE_BROWSER, MODE_PASTE_CALLBACK)

# ── N8 引擎内建（设计真值 §4：写进引擎而非 yaml）──
#: scope 被拒（授权服务器 error=invalid_scope）→ 去 scope 自动重试一次（Codex
#: ``perform_oauth_login_retry_without_scopes`` 同款）；重试用尽即终态。
SCOPE_DOWNGRADE_ERROR = "invalid_scope"
#: offline_access 公告自动追加（Claude 同款：服务器公告了才追加，拿 refresh token）。
OFFLINE_ACCESS_SCOPE = "offline_access"
_SCOPE_RETRY_NOTE = (
    "授权服务器拒绝了申请的 scope（invalid_scope）：已自动去掉 scope 生成新授权 URL，"
    "请在新的 authorization_url 完成授权（仅自动重试一次）"
)


# ────────────────────────────────────────────────────────────
# 地址面
# ────────────────────────────────────────────────────────────


def normalize_server_url(server_url: str) -> str:
    """校验并规范化服务器 URL（http/https 绝对 URL；只剥尾斜杠，大小写照抄）。"""
    if not isinstance(server_url, str) or not server_url.strip():
        raise OAuthError(protocol.URL_INVALID, "server_url 不能为空")
    url = protocol.require_http_url(server_url.strip(), "MCP 服务器 URL")
    return url.rstrip("/") or url


def resolve_callback_base(explicit: str | None) -> str:
    """回调基址：显式参数须为回环 http/https；缺省取内核回环端口（默认 9100）。"""
    if explicit:
        base = explicit.strip().rstrip("/")
        parts = urllib.parse.urlsplit(base)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise OAuthError("CALLBACK_BASE_INVALID", f"回调基址须为 http/https 绝对 URL：{explicit}")
        if parts.hostname not in _LOOPBACK_HOSTS:
            raise OAuthError("CALLBACK_BASE_INVALID", f"回调只允许回环地址（{'/'.join(_LOOPBACK_HOSTS)}）：{explicit}")
        return base
    raw_port = os.environ.get("AGENTOS_KERNEL_PORT", "").strip()
    try:
        port = int(raw_port) if raw_port else DEFAULT_KERNEL_PORT
    except ValueError:
        port = DEFAULT_KERNEL_PORT
    return f"http://127.0.0.1:{port}"


def callback_url(base: str) -> str:
    """回调完整 URL（注册给授权服务器的 redirect_uri 同值）。"""
    return f"{base}{CALLBACK_PATH}"


def open_browser(url: str) -> bool:
    """默认浏览器打开面；无浏览器/无显示环境返回 False（调用方转「待授权」）。

    测试经模块命名空间 patch 本函数注入替身（同 pim_scheduler ``_now`` 先例）。
    """
    try:
        return bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001 —— 后端缺失时 webbrowser 抛 OSError 族，非致命
        return False


# ────────────────────────────────────────────────────────────
# .env 读写边界（内核同源语义；错误在边界翻译）
# ────────────────────────────────────────────────────────────


def _read_env() -> dict[str, str]:
    try:
        return read_env(env_file_path())
    except OSError as exc:
        raise OAuthError("ENV_READ_FAILED", f"用户根 .env 读取失败：{exc}") from exc


def _write_env(updates: list[tuple[str, str]]) -> None:
    try:
        merge_env_updates(env_file_path(), updates)
    except OSError as exc:
        raise OAuthError("ENV_WRITE_FAILED", f"用户根 .env 写入失败：{exc}") from exc


def _write_tokens(
    prefix: str, tokens: dict[str, Any], *, token_endpoint: str, client_id: str, resource: str | None
) -> None:
    """token 落库面（只有这一条写路径；未知字段不落）。"""
    updates = [
        (key_name(prefix, "ACCESS_TOKEN"), tokens["access_token"]),
        (key_name(prefix, "TOKEN_ENDPOINT"), token_endpoint),
        (key_name(prefix, "CLIENT_ID"), client_id),
    ]
    if tokens.get("refresh_token"):
        updates.append((key_name(prefix, "REFRESH_TOKEN"), tokens["refresh_token"]))
    if resource:
        updates.append((key_name(prefix, "RESOURCE"), resource))
    # 无 expires_in → 空值删键（避免旧到期时间与刚换来的 token 错配）
    updates.append((key_name(prefix, "EXPIRES_AT"), tokens.get("expires_at") or ""))
    _write_env(updates)


def _entry_for(variables: dict[str, str], prefix: str) -> dict[str, Any] | None:
    entries = status_entries(variables, prefix=prefix)
    return entries[0] if entries else None


def _env_keys(prefix: str) -> list[str]:
    return [key_name(prefix, suffix) for suffix in ENV_KEY_SUFFIXES]


# ────────────────────────────────────────────────────────────
# 挂起表（同进程同事件循环；state 为键）
# ────────────────────────────────────────────────────────────


@dataclass
class _PendingFlow:
    state: str
    prefix: str
    server_url: str
    resource: str | None
    issuer: str
    iss_required: bool
    token_endpoint: str
    client_id: str
    redirect_uri: str
    code_verifier: str
    created_at: float
    authorization_endpoint: str = ""
    scope: str | None = None
    retried_without_scope: bool = False
    client_secret: str | None = None
    future: asyncio.Future[dict[str, Any]] | None = field(default=None)


_PENDING: dict[str, _PendingFlow] = {}
_PENDING_LOCK = threading.Lock()


def _register_pending(flow_state: _PendingFlow) -> None:
    now = time.monotonic()
    with _PENDING_LOCK:
        for state in [s for s, item in _PENDING.items() if now - item.created_at > PENDING_TTL_SECONDS]:
            _PENDING.pop(state, None)
        _PENDING[flow_state.state] = flow_state


def _peek_pending(state: str) -> _PendingFlow | None:
    with _PENDING_LOCK:
        return _PENDING.get(state)


def _consume_pending(state: str) -> None:
    """消费（不移交返回值）：调用方已持有 peeker 得到的同一对象。"""
    with _PENDING_LOCK:
        _PENDING.pop(state, None)


def _publish(flow_state: _PendingFlow, payload: dict[str, Any]) -> None:
    future = flow_state.future
    if future is not None and not future.done():
        future.set_result(payload)


# ────────────────────────────────────────────────────────────
# 引导分支（自助注册不可行：自动开注册页/PAT 页 + 逐条列值）
# ────────────────────────────────────────────────────────────


def _guidance_notes(action: str, payload: dict[str, Any], reason: str) -> list[str]:
    """引导文案：指明下一步动作（点哪个、贴什么、贴到哪里）。"""
    notes = [f"进入人工引导：{reason}"]
    if action == GUIDANCE_ACTION_CREATE_PAT:
        notes.append("已打开 PAT 创建页（pat_page_url；预填为 best-effort [未验证]，失效就按下列值手填）")
        notes.append(
            f"生成 token 后写入用户根 .env 的 {payload['pat_env_key']}"
            "（设置页密钥面或直接编辑该文件），MCP 服务器鉴权用 "
            f"{payload['auth_config_snippet']}；随后重新调用 oauth_authorize 即会识别为已授权"
        )
    else:
        notes.append("请在刚打开的页面创建 OAuth App（预填为 best-effort [未验证]，失效就按下列值手填）")
        notes.append(
            "把页面给出的 client_id（若同时给了 client_secret 一并）作为参数重新调用 oauth_authorize"
            "（client_id=…[, client_secret=…]）：插件会把它落库到用户根 .env 并继续标准授权流程"
        )
    if payload.get("registration_url"):
        notes.append(f"OAuth App 注册页：{payload['registration_url']}")
    if payload.get("pat_page_url"):
        notes.append(f"PAT 创建页：{payload['pat_page_url']}")
    notes.append(f"要填的 app 名：{payload['app_name']}")
    notes.append(f"Homepage URL：{payload['homepage_url']}")
    notes.append(f"Callback / Redirect URL：{payload['callback_url']}")
    if payload.get("scopes"):
        notes.append("要勾的 scope：" + ", ".join(payload["scopes"]))
    notes.append("我们不代点、不抓取页面结果（不碰你的浏览器会话与账号）：注册与同意必须由人完成")
    return notes


async def _guidance_result(
    *,
    url: str,
    prefix: str,
    redirect_uri: str,
    resource_metadata_url: str | None,
    opener: Callable[[str], bool] | None,
    timeout: float,
    guidance: Any,
    guidance_action: str,
    scopes: list[str],
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """自助注册不可行时的引导返回面（自动开页 best-effort + 值逐条列出）。"""
    hosts = [guidance_module.host_of(url)]
    if metadata is not None:
        hosts.append(guidance_module.host_of(metadata.get("issuer")))
    provider, guide = guidance_module.match_provider(guidance_module.load_providers(guidance), hosts=hosts)
    homepage = resolve_callback_base(None)
    payload = guidance_module.build_guide(
        provider=provider,
        guide=guide,
        callback_url=redirect_uri,
        homepage_url=homepage,
        scopes=scopes,
        action=guidance_action,
        access_token_key=key_name(prefix, "ACCESS_TOKEN"),
    )
    target = payload["pat_page_url"] if guidance_action == GUIDANCE_ACTION_CREATE_PAT else payload["registration_url"]
    browser_opened = bool(target) and bool((opener or open_browser)(target))
    notes = _guidance_notes(guidance_action, payload, reason)
    if metadata is not None and metadata.get("client_id_metadata_document_supported"):
        notes.append(
            "该服务声明支持客户端元数据文档（CIMD）：若你自托管 HTTPS 元数据文档，"
            "可把该文档 URL 当 client_id 传入（可选路径，需你自行托管该文档）"
        )
    return {
        "status": "registration_required",
        "method": None,
        "server_url": url,
        "resource": url,
        "env_prefix": prefix,
        "env_keys": _env_keys(prefix),
        "expires_at": None,
        "authorization_url": None,
        "browser_opened": browser_opened,
        "refresh_error": None,
        "notes": notes,
        "guidance": payload,
    }


# ────────────────────────────────────────────────────────────
# grants 账本接线（P2，ADR 2026-11-19）：授权成立 → issue（幂等）；
# 刷新被拒 → 联动 revoke。账本是记录面（无执法消费方）：缺席/异常只降级记录，
# 绝不阻断授权主流程（授权是用户在浏览器当面完成的独立授权行为）。
# ────────────────────────────────────────────────────────────


def _split_scopes(scope: str | None) -> list[str]:
    """OAuth scope 串（空格分隔）→ 清单（空串剔除）。"""
    return [item for item in (scope or "").split() if item]


def _negotiate_request_scope(
    explicit: str | None, discovered: dict[str, Any], metadata: dict[str, Any]
) -> str | None:
    """N8 引擎内建：scope 协商 discovery 优先 + offline_access 公告自动追加。

    - 显式 ``scope`` 传参原样使用（不丢弃 operator 意图）；
    - 缺省 = PRM ``scopes_supported`` 优先，空则 AS ``scopes_supported``
      （Codex 同款：服务器公告 ``scopes_supported`` 则优先用之）；
    - ``offline_access``：**公告了才自动追加**（Claude 同款，拿 refresh token）——
      [假设] R1-2：未公告时盲发会被部分 AS 以 invalid_scope 拒，反而触发无谓降级。
    """
    advertised = [*discovered.get("scopes_supported", []), *metadata.get("scopes_supported", [])]
    if explicit:
        selected = _split_scopes(explicit)
    else:
        selected = list(discovered.get("scopes_supported") or []) or list(metadata.get("scopes_supported") or [])
    if OFFLINE_ACCESS_SCOPE in advertised and OFFLINE_ACCESS_SCOPE not in selected:
        selected.append(OFFLINE_ACCESS_SCOPE)
    return " ".join(selected) or None


def _cimd_client_id(metadata: dict[str, Any]) -> str | None:
    """N2 客户端注册协商的 CIMD 分支（**桩**，本批不启用自动 CIMD）。

    [假设] R1-4：CIMD 的 client_id = 我方自托管且 AS 可公开抓取的 HTTPS 客户端
    元数据文档 URL；本仓当前没有该文档托管面，auto-CIMD 无从发起——即使 AS 公告
    ``client_id_metadata_document_supported`` 也返回 None（协商顺序落到 DCR →
    预置）。operator 自托管了文档时，直接把文档 URL 作为 ``client_id`` 参数传入
    即可（透传路径已有，既有测试钉死：test_cimd_client_id_url_passed_through）。
    """
    del metadata  # 桩：探测位保留（client_id_metadata_document_supported），启用时在此返回文档 URL
    return None


def _normalize_mode(mode: str) -> str:
    """N3 登录模式归一边界：白名单外 → OPTION_INVALID。"""
    if mode not in AUTHORIZE_MODES:
        raise OAuthError("OPTION_INVALID", f"mode 只接受 {'/'.join(AUTHORIZE_MODES)}")
    return mode


def _start_retry_without_scope(flow_state: _PendingFlow) -> tuple[_PendingFlow, str]:
    """N8：scope 被拒 → 去 scope 重新发起一次（全新 state/PKCE，仅一次）。

    复用同一客户端身份（client_id/secret/redirect_uri），新 state 新 verifier；
    重试流程同样进挂起表（晚到的回调仍能完成落库），等待方经
    ``retry_authorization_url`` 事件拿到新授权 URL。
    """
    verifier, challenge = protocol.make_pkce_pair()
    state = protocol.make_state()
    retry_url = protocol.build_authorization_url(
        flow_state.authorization_endpoint,
        client_id=flow_state.client_id,
        redirect_uri=flow_state.redirect_uri,
        state=state,
        code_challenge=challenge,
        scope=None,
        resource=flow_state.resource,
    )
    retry_state = dataclasses.replace(
        flow_state,
        state=state,
        code_verifier=verifier,
        scope=None,
        retried_without_scope=True,
        created_at=time.monotonic(),
        future=None,
    )
    _register_pending(retry_state)
    return retry_state, retry_url


async def _ensure_grant_record(
    *, method: str, url: str, prefix: str, resource: str | None, scope: str | None
) -> None:
    """授权成立即确保账本有本服务器 domain grant（幂等，grants_link 内降级）。"""
    await grants_link.ensure_server_grant(
        server_url=url,
        prefix=prefix,
        resource=resource,
        method=method,
        scopes=_split_scopes(scope),
    )


async def authorize(
    *,
    server_url: str,
    client_id: str | None = None,
    client_secret: str | None = None,
    scope: str | None = None,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    callback_base_url: str | None = None,
    resource_metadata_url: str | None = None,
    guidance: Any = None,
    guidance_action: str = GUIDANCE_ACTION_REGISTER_APP,
    opener: Callable[[str], bool] | None = None,
    timeout: float = protocol.DEFAULT_TIMEOUT_SECS,
    mode: str = MODE_BROWSER,
) -> dict[str, Any]:
    """确保 ``server_url`` 已授权（既有有效 token / 刷新 / 全量授权 / 引导注册四条路径）。

    自助注册不可行（元数据不可得 / 无 registration_endpoint / 注册被拒）且未传入
    ``client_id`` 时走**引导分支**（``status="registration_required"``）：自动打开该服务
    的 OAuth App 注册页或 PAT 页（``guidance_action`` 选），并逐条列出用户要手填的值。

    ``client_secret`` 只在预注册机密客户端形态使用（client_secret_post），落用户根 .env。

    ``mode``（N3 双模式）：``browser`` = 拉本机浏览器 + 本机回调收码；
    ``paste_callback`` = 不拉浏览器、立即返回授权 URL，用户手动完成后把回调 URL
    经 :func:`complete_pasted_callback` 粘回完成兑换（``wait_seconds`` 不适用）。

    scope 缺省走 discovery 协商（N8：PRM ``scopes_supported`` 优先，空则 AS；
    ``offline_access`` 公告了才自动追加）；授权响应 ``error=invalid_scope`` 时
    自动去 scope 重试一次（Codex 同款），再次被拒即终态。

    Raises:
        OAuthError: URL_INVALID / CALLBACK_BASE_INVALID / DISCOVERY_FAILED /
            METADATA_INVALID / PKCE_UNSUPPORTED / REGISTRATION_FAILED /
            TOKEN_REQUEST_FAILED / AUTHORIZATION_FAILED / OPTION_INVALID /
            ENV_READ_FAILED / ENV_WRITE_FAILED。
    """
    mode = _normalize_mode(mode)
    url = normalize_server_url(server_url)
    redirect_uri = callback_url(resolve_callback_base(callback_base_url))
    prefix = derive_env_prefix(url)
    variables = _read_env()
    entry = _entry_for(variables, prefix)
    if entry is not None and entry["has_access_token"] and entry["expired"] is not True:
        await _ensure_grant_record(
            method="existing", url=url, prefix=prefix, resource=entry.get("resource"), scope=scope
        )
        return _authorized_result("existing", url, prefix, variables)

    refresh_error: str | None = None
    if entry is not None and entry["has_refresh_token"]:
        refreshed, refresh_error = await _try_refresh(url, prefix, variables, timeout)
        if refreshed is not None:
            await _ensure_grant_record(
                method="refresh",
                url=url,
                prefix=prefix,
                resource=variables.get(key_name(prefix, "RESOURCE")),
                scope=scope,
            )
            return _authorized_result("refresh", url, prefix, _read_env())
        # 刷新被拒 = 凭据失效信号 → 联动撤销该服务器既有 grant（幂等）后回落全量
        # 授权（凭据被吊销时的唯一恢复路径；重新授权成功会落新 grant）
        await grants_link.revoke_server_grants(
            server_url=url, context={"reason": refresh_error, "stage": "refresh_rejected"}
        )

    client = client_id or variables.get(key_name(prefix, "CLIENT_ID"))
    client_secret = client_secret or variables.get(key_name(prefix, "CLIENT_SECRET"))
    try:
        discovered = await asyncio.to_thread(
            protocol.discover_protected_resource,
            url,
            resource_metadata_url=resource_metadata_url,
            timeout=timeout,
        )
    except OAuthError as exc:
        if exc.code != protocol.DISCOVERY_FAILED or client:
            raise
        # PRM 不可得（老式端点）：进引导分支，别只报「不支持」
        return await _guidance_result(
            url=url,
            prefix=prefix,
            redirect_uri=redirect_uri,
            resource_metadata_url=resource_metadata_url,
            opener=opener,
            timeout=timeout,
            guidance=guidance,
            guidance_action=guidance_action,
            scopes=[],
            reason=f"受保护资源元数据不可得（{exc}）",
        )
    try:
        metadata = await asyncio.to_thread(
            protocol.discover_authorization_server, discovered["authorization_servers"][0], timeout=timeout
        )
    except OAuthError as exc:
        if exc.code != protocol.DISCOVERY_FAILED or client:
            raise
        # AS 无元数据（如 GitHub）：仍进引导分支；scope 清单取 PRM 的 scopes_supported
        return await _guidance_result(
            url=url,
            prefix=prefix,
            redirect_uri=redirect_uri,
            resource_metadata_url=resource_metadata_url,
            opener=opener,
            timeout=timeout,
            guidance=guidance,
            guidance_action=guidance_action,
            scopes=list(discovered["scopes_supported"]),
            reason=f"授权服务器元数据不可得（{exc}）",
        )
    resource = discovered["resource"]
    scopes = list(discovered["scopes_supported"])
    # N8 引擎内建：scope 协商 discovery 优先 + offline_access 公告追加（写进引擎而非 yaml）
    negotiated_scope = _negotiate_request_scope(scope, discovered, metadata)
    if not client:
        # N2 客户端注册协商顺序：Auto(CIMD 优先)→DCR→预置 client_id——预置/缓存
        # client_id 已在上方命中则整段跳过；CIMD 分支当前为桩（_cimd_client_id，
        # [假设] 见其 docstring），协商落到 DCR。
        cimd_client = _cimd_client_id(metadata)
        if cimd_client:
            client = cimd_client
    if not client:
        if not metadata["registration_endpoint"]:
            return await _guidance_result(
                url=url,
                prefix=prefix,
                redirect_uri=redirect_uri,
                resource_metadata_url=resource_metadata_url,
                opener=opener,
                timeout=timeout,
                guidance=guidance,
                guidance_action=guidance_action,
                scopes=scopes,
                reason=f"该服务不提供动态客户端注册端点（{metadata['issuer']}）",
                metadata=metadata,
            )
        try:
            registered = await asyncio.to_thread(
                protocol.register_client,
                metadata["registration_endpoint"],
                redirect_uri=redirect_uri,
                client_name="agentos-oauth-broker",
                scope=negotiated_scope,
                timeout=timeout,
            )
            client = registered["client_id"]
        except OAuthError as exc:
            if exc.code != protocol.REGISTRATION_FAILED:
                raise
            return await _guidance_result(
                url=url,
                prefix=prefix,
                redirect_uri=redirect_uri,
                resource_metadata_url=resource_metadata_url,
                opener=opener,
                timeout=timeout,
                guidance=guidance,
                guidance_action=guidance_action,
                scopes=scopes,
                reason=f"动态客户端注册被拒（{exc}）",
                metadata=metadata,
            )
    if client_secret:
        # 预注册机密客户端：secret 落用户根 .env（唯一落库面），后续刷新同用
        _write_env([(key_name(prefix, "CLIENT_SECRET"), client_secret), (key_name(prefix, "CLIENT_ID"), client)])
    verifier, challenge = protocol.make_pkce_pair()
    state = protocol.make_state()
    authorization_url = protocol.build_authorization_url(
        metadata["authorization_endpoint"],
        client_id=client,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=challenge,
        scope=negotiated_scope,
        resource=resource,
    )
    flow_state = _PendingFlow(
        state=state,
        prefix=prefix,
        server_url=url,
        resource=resource,
        issuer=metadata["issuer"],
        iss_required=metadata["iss_parameter_supported"],
        token_endpoint=metadata["token_endpoint"],
        client_id=client,
        redirect_uri=redirect_uri,
        code_verifier=verifier,
        created_at=time.monotonic(),
        authorization_endpoint=metadata["authorization_endpoint"],
        scope=negotiated_scope,
        client_secret=client_secret,
    )
    waiter: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    flow_state.future = waiter
    _register_pending(flow_state)

    if mode == MODE_PASTE_CALLBACK:
        # N3 粘贴回调双模式：不拉浏览器、不等待；用户粘回的回调 URL 经
        # complete_pasted_callback 走与 http 回调同一闸门核完成兑换
        return _pending_result(
            url,
            prefix,
            resource,
            authorization_url,
            browser_opened=False,
            refresh_error=refresh_error,
            notes=[
                "粘贴回调模式（未拉起浏览器）：请在任意浏览器打开 authorization_url 完成授权，"
                f"然后把浏览器最终跳转到的回调 URL（形如 {redirect_uri}?code=…&state=…）"
                "经 oauth(action='paste_callback', callback_url='<回调URL>') 提交完成兑换"
            ],
        )

    browser_opened = bool((opener or open_browser)(authorization_url))
    if not browser_opened:
        return _pending_result(
            url,
            prefix,
            resource,
            authorization_url,
            browser_opened=False,
            refresh_error=refresh_error,
            notes=[
                "本机浏览器未打开：请手动访问 authorization_url 完成授权，随后重新调用 oauth_authorize 查询结果",
                f"粘贴模式兜底：在任意浏览器打开 authorization_url，完成后把回调 URL（形如 {redirect_uri}?code=…&state=…）"
                "经 oauth(action='paste_callback', callback_url=…) 粘回完成兑换",
            ],
        )
    if wait_seconds <= 0:
        return _pending_result(
            url,
            prefix,
            resource,
            authorization_url,
            browser_opened=True,
            refresh_error=refresh_error,
            notes=["已按 wait_seconds=0 立即返回：完成浏览器授权后重新调用 oauth_authorize 查询结果"],
        )

    try:
        outcome = await asyncio.wait_for(waiter, timeout=wait_seconds)
    except TimeoutError:
        return _pending_result(
            url,
            prefix,
            resource,
            authorization_url,
            browser_opened=True,
            refresh_error=refresh_error,
            notes=["回调未在等待上限内到达：完成浏览器授权后重新调用 oauth_authorize 查询结果（晚到的回调仍会落库）"],
        )
    if outcome.get("retry_authorization_url"):
        # N8：scope 被拒 → 已自动去 scope 重试一次，返回新授权 URL（重试流程在挂起表）
        return _pending_result(
            url,
            prefix,
            resource,
            str(outcome["retry_authorization_url"]),
            browser_opened=browser_opened,
            refresh_error=refresh_error,
            notes=[_SCOPE_RETRY_NOTE],
        )
    if outcome.get("error"):
        raise _outcome_error(str(outcome["error"]))
    await _ensure_grant_record(method="authorization_code", url=url, prefix=prefix, resource=resource, scope=negotiated_scope)
    return _authorized_result(
        "authorization_code", url, prefix, _read_env(), refresh_error=refresh_error, browser_opened=True
    )


#: 回调路径可原样上抛的本地错误码（其余 outcome error 视为授权服务器错误码）
_PASSTHROUGH_ERRORS = frozenset(
    {"ENV_READ_FAILED", "ENV_WRITE_FAILED", protocol.TOKEN_REQUEST_FAILED, protocol.TOKEN_RESPONSE_INVALID}
)


def _outcome_error(error_code: str) -> OAuthError:
    if error_code in _PASSTHROUGH_ERRORS:
        return OAuthError(error_code, f"授权失败（{error_code}）")
    return OAuthError("AUTHORIZATION_FAILED", f"授权失败（{error_code}）")


async def _try_refresh(
    url: str, prefix: str, variables: dict[str, str], timeout: float
) -> tuple[dict[str, Any] | None, str | None]:
    """过期时先走刷新；返回 ``(成功标记, 失败错误码)``（刷新不可行也归失败码）。"""
    token_endpoint = variables.get(key_name(prefix, "TOKEN_ENDPOINT"))
    client = variables.get(key_name(prefix, "CLIENT_ID"))
    client_secret = variables.get(key_name(prefix, "CLIENT_SECRET"))
    refresh_token = variables.get(key_name(prefix, "REFRESH_TOKEN"))
    if not token_endpoint or not client or not refresh_token:
        return None, "REFRESH_CONTEXT_MISSING"
    resource = variables.get(key_name(prefix, "RESOURCE")) or url
    try:
        tokens = await asyncio.to_thread(
            protocol.refresh_access_token,
            token_endpoint,
            refresh_token=refresh_token,
            client_id=client,
            client_secret=client_secret,
            resource=resource,
            timeout=timeout,
        )
    except OAuthError as exc:
        return None, exc.code
    _write_tokens(prefix, tokens, token_endpoint=token_endpoint, client_id=client, resource=resource)
    return {"refreshed": True}, None


def _authorized_result(
    method: str,
    url: str,
    prefix: str,
    variables: dict[str, str],
    *,
    refresh_error: str | None = None,
    browser_opened: bool = False,
) -> dict[str, Any]:
    entry = _entry_for(variables, prefix) or {}
    return {
        "status": "authorized",
        "method": method,
        "server_url": url,
        "resource": entry.get("resource") or url,
        "env_prefix": prefix,
        "env_keys": _env_keys(prefix),
        "expires_at": entry.get("expires_at"),
        "authorization_url": None,
        "browser_opened": browser_opened,
        "refresh_error": refresh_error,
        "notes": [],
    }


def _pending_result(
    url: str,
    prefix: str,
    resource: str | None,
    authorization_url: str,
    *,
    browser_opened: bool,
    refresh_error: str | None,
    notes: list[str],
) -> dict[str, Any]:
    return {
        "status": "pending",
        "method": None,
        "server_url": url,
        "resource": resource,
        "env_prefix": prefix,
        "env_keys": _env_keys(prefix),
        "expires_at": None,
        "authorization_url": authorization_url,
        "browser_opened": browser_opened,
        "refresh_error": refresh_error,
        "notes": notes,
    }


# ────────────────────────────────────────────────────────────
# 本机回调（浏览器面；内核 /ext 分发到 http.handle）
# ────────────────────────────────────────────────────────────


def _page(title: str, message: str) -> str:
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f"<title>{title}</title></head><body><h1>{title}</h1><p>{message}</p></body></html>"
    )


@dataclass
class _SettledCallback:
    """回调核结果（http 回调面与粘贴模式共用）。"""

    outcome: str  # "ok" | "retry" | "reject"
    flow_state: _PendingFlow | None = None
    tokens: dict[str, Any] | None = None
    retry_authorization_url: str | None = None
    reject_status: int = 400
    reject_page: str = ""
    reject_code: str = "AUTHORIZATION_FAILED"


async def _settle_callback(query: dict[str, str], *, timeout: float) -> _SettledCallback:
    """回调闸门 + 授权码换 token + 落库（共享核，N3 双模式同源）。

    闸门顺序（fail-closed）：state 命中挂起表 → iss 校验（RFC 9207）→ AS error
    参数（``invalid_scope`` 触发 N8 去 scope 自动重试一次）→ code → 换 token →
    落库。被拒时零 token 请求、零落库；iss 闸门被拒不消费挂起流程（合法回调仍可
    重试），也不唤醒等待中的工具调用。
    """
    state = (query.get("state") or "").strip()
    if not state:
        return _SettledCallback("reject", reject_page=_reject_page_text("回调缺少 state"))
    flow_state = _peek_pending(state)
    if flow_state is None:
        return _SettledCallback("reject", reject_page=_reject_page_text("state 无效或已过期（可能插件已重启）"))

    issuer = query.get("iss")
    if flow_state.iss_required and not issuer:
        return _SettledCallback(
            "reject", flow_state=flow_state, reject_page=_reject_page_text("授权响应缺少 iss 参数")
        )
    if issuer and issuer != flow_state.issuer:
        # RFC 9207 §2.4：iss 不符时不得据该响应采取任何行动（含展示 error 内容）
        return _SettledCallback(
            "reject", flow_state=flow_state, reject_page=_reject_page_text("iss 与已记录 issuer 不符")
        )

    # iss 闸门之后消费（同步段内无 await：单事件循环下不存在双消费窗口）
    _consume_pending(state)
    error = query.get("error")
    if error:
        if error == SCOPE_DOWNGRADE_ERROR and not flow_state.retried_without_scope:
            _retry_state, retry_url = _start_retry_without_scope(flow_state)
            _publish(flow_state, {"retry_authorization_url": retry_url})
            return _SettledCallback("retry", flow_state=flow_state, retry_authorization_url=retry_url)
        _publish(flow_state, {"error": error})
        return _SettledCallback(
            "reject",
            flow_state=flow_state,
            reject_page=_page("授权失败", f"授权服务器返回错误：{error}。请重新调用 oauth_authorize 发起授权。"),
        )

    code = query.get("code")
    if not code:
        _publish(flow_state, {"error": "missing_code"})
        return _SettledCallback(
            "reject", flow_state=flow_state, reject_page=_page("授权失败", "回调缺少 code。请重新调用 oauth_authorize 发起授权。")
        )

    try:
        tokens = await asyncio.to_thread(
            protocol.exchange_authorization_code,
            flow_state.token_endpoint,
            code=code,
            redirect_uri=flow_state.redirect_uri,
            client_id=flow_state.client_id,
            client_secret=flow_state.client_secret,
            code_verifier=flow_state.code_verifier,
            resource=flow_state.resource,
            timeout=timeout,
        )
    except OAuthError as exc:
        _publish(flow_state, {"error": exc.code})
        return _SettledCallback(
            "reject",
            flow_state=flow_state,
            reject_code=exc.code,
            reject_page=_page("授权失败", f"换 token 失败（{exc.code}）。请重新调用 oauth_authorize 发起授权。"),
        )

    try:
        _write_tokens(
            flow_state.prefix,
            tokens,
            token_endpoint=flow_state.token_endpoint,
            client_id=flow_state.client_id,
            resource=flow_state.resource,
        )
    except OAuthError as exc:
        _publish(flow_state, {"error": exc.code})
        return _SettledCallback(
            "reject",
            flow_state=flow_state,
            reject_code=exc.code,
            reject_page=_page("授权失败", f"凭据落库失败（{exc.code}）"),
        )

    _publish(flow_state, {"expires_at": tokens.get("expires_at")})
    return _SettledCallback("ok", flow_state=flow_state, tokens=tokens)


def _reject_page_text(reason: str) -> str:
    return _page("授权失败", f"{reason}。请重新调用 oauth_authorize 发起授权。")


async def handle_callback(query: dict[str, str], *, timeout: float = protocol.DEFAULT_TIMEOUT_SECS) -> tuple[int, str]:
    """回调闸门 + 授权码换 token + 落库：返回 ``(HTTP 状态, HTML 页面)``。

    与 :func:`complete_pasted_callback` 共享 :func:`_settle_callback` 核（N3 双模式
    同一闸门）；本函数是浏览器面（内核 ``/ext`` 分发）的薄壳。
    """
    settled = await _settle_callback(query, timeout=timeout)
    if settled.outcome == "retry":
        return 200, _page(
            "需要继续授权",
            "授权服务器拒绝了申请的 scope：已自动去掉 scope 重新发起授权（仅一次）。"
            "请回到 AgentOS 获取新的授权链接并完成授权。",
        )
    if settled.outcome == "reject":
        return settled.reject_status, settled.reject_page
    return 200, _page("授权成功", "凭据已保存到本机，可以关闭此页面。")


async def complete_pasted_callback(
    pasted_url: str, *, timeout: float = protocol.DEFAULT_TIMEOUT_SECS
) -> dict[str, Any]:
    """N3 粘贴回调双模式的兑换端：解析用户粘回的回调 URL，走共享回调核完成兑换。

    成功 → ``authorized`` 结果 + grants issue（与 http 回调同账）；触发 N8 scope
    降级重试 → ``pending`` 结果（带去 scope 的新授权 URL）；闸门/兑换失败 →
    ``OAuthError``（错误是值：闸门拒=AUTHORIZATION_FAILED，换 token 失败透传
    机器码如 TOKEN_REQUEST_FAILED）。
    """
    text = (pasted_url or "").strip()
    parts = urllib.parse.urlsplit(text)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    settled = await _settle_callback(query, timeout=timeout)
    if settled.outcome == "retry" and settled.flow_state is not None and settled.retry_authorization_url:
        return _pending_result(
            settled.flow_state.server_url,
            settled.flow_state.prefix,
            settled.flow_state.resource,
            settled.retry_authorization_url,
            browser_opened=False,
            refresh_error=None,
            notes=[_SCOPE_RETRY_NOTE],
        )
    if settled.outcome == "reject":
        raise OAuthError(settled.reject_code, f"回调处理被拒：{settled.reject_page or '回调参数无效'}")
    flow_state = settled.flow_state
    assert flow_state is not None, "ok 分支必带 flow_state（_settle_callback 契约）"
    await _ensure_grant_record(
        method="authorization_code",
        url=flow_state.server_url,
        prefix=flow_state.prefix,
        resource=flow_state.resource,
        scope=flow_state.scope,
    )
    return _authorized_result("authorization_code", flow_state.server_url, flow_state.prefix, _read_env())


# ────────────────────────────────────────────────────────────
# 状态查询
# ────────────────────────────────────────────────────────────


def status(*, server_url: str | None = None) -> dict[str, Any]:
    """列出已授权服务器状态（``server_url`` 给定时只报该服务器）。

    Raises:
        OAuthError: URL_INVALID / ENV_READ_FAILED。
    """
    variables = _read_env()
    prefix = derive_env_prefix(normalize_server_url(server_url)) if server_url is not None else None
    return {"servers": status_entries(variables, prefix=prefix, now=datetime.now(UTC))}
