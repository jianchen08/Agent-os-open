#!/usr/bin/env python3
"""sync_relay 云端中继插件——真值库 + 设备注册表的端点面（http_endpoints，零内核改动）。

端点面（声明见 plugin.json http_endpoints；鉴权分工按仓内既定口径）::

    POST /ext/sync_relay/events            auth:"none" —— per-device token 验签
                                           自管（device:token 绑定防顶名）；
                                           ULID 幂等入账，schema/HLC/属主执法，
                                           违约死信不阻塞批次。
    GET  /ext/sync_relay/events            auth:"none" —— cursor 增量拉取（拉为主）。
    POST /ext/sync_relay/devices           auth:"none" —— 设备注册。
    POST /ext/sync_relay/devices/heartbeat auth:"none" —— 心跳（在链判定数据源）。
    GET  /ext/sync_relay/bootstrap         auth:"none" —— 入网基线（层级权威①）。
    POST /ext/sync_relay/enroll            auth:"admin" —— 登录即入网（ADR
                                           2026-11-02-login-as-enrollment）：
                                           relay 内核 admin JWT 直签 per-device
                                           token（唯一认证动作 = 连接 IP + 账号
                                           密码登录；入网码机制已退役）。
    GET  /ext/sync_relay/devices           auth:"admin" —— 注册表查看（内核 JWT）。
    GET  /ext/sync_relay/routing_view      auth:"none" —— 2c 路由只读视图
                                           （per-device token 鉴权；位阶+在线+
                                           能力面透传，relay 只供数据不裁决）。
    GET  /ext/sync_relay/registry          auth:"none" —— 全网注册表复制快照
                                           （per-device token 鉴权；含各设备
                                           token_hash 对等信任面，A 档分发通道）。
    POST /ext/sync_relay/exec_tokens       auth:"none" —— 执行凭据签发（发起端
                                           sync token 鉴权；双钥配对流：发起端
                                           签发 → 属主端验证，per-device 短时效）。
    GET  /ext/sync_relay/exec_tokens       auth:"none" —— 属主端验证集拉取
                                           （只回落绑定自己的有效凭据）。
    GET  /ext/sync_relay/dead_letters      auth:"admin" —— 死信查看。

token 来源：env ``AGENTOS_SYNC_RELAY_TOKENS``（``device:token`` 逗号对）+
/enroll 登录直签发（sync_device_tokens，登录即入网）两源合一；两源皆空 =
一律 403 fail-closed（装载不受阻，端点在但全拒）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根（共享裸模块）入 sys.path

from _root_api import (  # noqa: E402
    SyncEvent,  # noqa: E402
    decode_body as _decode_body,
    json_response as _json_response,
    now_iso_utc,  # noqa: E402
    ok as _ok,
    protocol_error as _error,
    user_data_dir,  # noqa: E402
)
from exec_token_store import ExecTokenStore  # noqa: E402
from exec_tokens import (  # noqa: E402
    DEFAULT_TTL_SECS,
    ExecTokenError,
    issue_exec_token,
    verification_set,
)
from relay_core import (  # noqa: E402
    EnvelopeError,
    TokenAuth,
    enroll_device,
    is_online,
    parse_tokens_env,
    process_events,
    select_baseline,
    token_sha256,
)
from relay_store import RelayStore  # noqa: E402
from routing_view import build_routing_view  # noqa: E402

plugin = AgentOSPlugin("sync_relay")
logger = logging.getLogger(__name__)

#: per-device token env（``device:token`` 逗号对；不落配置明文，P6）。
TOKENS_ENV = "AGENTOS_SYNC_RELAY_TOKENS"

_DEFAULT_ONLINE_WINDOW_SECS = 90.0
_DEFAULT_PULL_PAGE_LIMIT = 500

_store: RelayStore | None = None
_auth = TokenAuth({})
_online_window_secs = _DEFAULT_ONLINE_WINDOW_SECS
_pull_page_limit = _DEFAULT_PULL_PAGE_LIMIT
_exec_store: ExecTokenStore | None = None
_exec_token_ttl_secs = DEFAULT_TTL_SECS
_hub_device_id = ""


def _default_db_path() -> str:
    data_root = user_data_dir()
    if data_root is None:
        raise RuntimeError("sync_relay 无法定位用户数据目录（USER_ROOT 未配置）")
    return str(data_root / "sync_relay" / "relay.db")


def _default_exec_db_path() -> str:
    """exec token 分库路径（与事件账本 relay.db 分库——双钥分离下沉到存储层）。"""
    data_root = user_data_dir()
    if data_root is None:
        raise RuntimeError("sync_relay 无法定位用户数据目录（USER_ROOT 未配置）")
    return str(data_root / "sync_relay" / "exec_tokens.db")


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """装配存储与鉴权；token 未配置不拒装载——端点在、全拒（fail-closed + 告警）。"""
    global _store, _auth, _online_window_secs, _pull_page_limit
    global _exec_store, _exec_token_ttl_secs, _hub_device_id
    settings = plugin.get_config().get("settings", {})
    _online_window_secs = _parse_positive(
        settings.get("online_window_secs"), _DEFAULT_ONLINE_WINDOW_SECS
    )
    _hub_device_id = str(settings.get("hub_device_id", "") or "").strip()
    _pull_page_limit = int(
        _parse_positive(settings.get("pull_page_limit"), _DEFAULT_PULL_PAGE_LIMIT)
    )
    _exec_token_ttl_secs = _parse_positive(
        settings.get("exec_token_ttl_secs"), DEFAULT_TTL_SECS
    )
    _store = RelayStore(_default_db_path())
    _exec_store = ExecTokenStore(_default_exec_db_path())
    tokens = parse_tokens_env(os.environ.get(TOKENS_ENV, ""))
    # 直签发 token 表随 store 现查：签发即时生效，无需重载。
    _auth = TokenAuth(tokens, issued_source=_store.list_device_tokens)
    if not tokens:
        logger.warning(
            "[sync_relay] %s 未配置；仅登录直签发（/enroll）的设备可通行（fail-closed 面不变）",
            TOKENS_ENV,
        )
    logger.info(
        "sync_relay 已装载: devices=%s online_window=%ss hub=%s",
        _auth.configured_devices, _online_window_secs,
        _hub_device_id or "-",
    )


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    global _store, _exec_store
    if _store is not None:
        _store.close()
        _store = None
    if _exec_store is not None:
        _exec_store.close()
        _exec_store = None


def _parse_positive(raw: Any, default: float) -> float:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _bearer(headers: dict[str, str] | None) -> str:
    """取 Bearer 凭据（内核 header key 已小写）。"""
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == "authorization" and isinstance(v, str):
            if v.lower().startswith("bearer "):
                return v[7:].strip()
    return ""


def _opt_str(value: Any) -> str | None:
    """可缺省字符串字段：未携带/非串 → None（注册表保留原值）；空串=明确清空。"""
    return value.strip() if isinstance(value, str) else None


def _is_loopback_address(address: str) -> bool:
    """回环地址判定（sync_agent/server.py 同款孪生实现——播种件禁共享根跨包
    import，改动两处必须同刀）。"""
    from urllib.parse import urlparse

    try:
        host = (urlparse(address).hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "::1") or host.startswith("127.")


def _normalized_address(value: Any) -> str | None:
    """自报地址规范化：回环一律不留（None=保留原值语义仅在未携带时生效，
    明确上回环 = 明确清空）。

    回环自报只对上报者本机可达，注册表消费者是全网设备——存了必生成他端
    死连接卡（云端中枢自报 127.0.0.1:9100 的生产实证）；本机找自己不经注册表。
    """
    address = _opt_str(value)
    if address is None:
        return None
    return "" if _is_loopback_address(address) else address


def _opt_dict(value: Any) -> dict[str, Any] | None:
    """可缺省对象字段：未携带/非 dict → None（注册表保留原值）。"""
    return value if isinstance(value, dict) else None


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            "query": {"type": "object", "additionalProperties": {"type": "string"}},
            "query_multi": {
                "type": "object",
                "additionalProperties": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    description="HTTP endpoint handler for /ext/sync_relay/**",
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "integer"},
            "body": {"type": "object"},
        },
    },
    render={"card": "generic"},
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    query_multi: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """/ext/sync_relay/** 分发：设备面 token 自管，观测面内核 admin JWT。"""
    del plugin_id  # dispatcher 已路由到本插件
    if _store is None:
        return _error("sync_relay store not initialized (load pending)", 502)
    store = _store
    parts = [unquote(p) for p in path.split("/") if p]
    # parts[0]="ext", parts[1]="sync_relay"
    rest = parts[2:]
    bearer = _bearer(headers)
    if rest == ["events"] and method == "POST":
        return _handle_receive(store, raw_body, bearer)
    if rest == ["events"] and method == "GET":
        return _handle_pull(store, bearer, query or {})
    if rest == ["devices"] and method == "POST":
        return _handle_register(store, raw_body, bearer)
    if rest == ["devices", "heartbeat"] and method == "POST":
        return _handle_heartbeat(store, raw_body, bearer)
    if rest == ["bootstrap"] and method == "GET":
        return _handle_bootstrap(store, bearer)
    if rest == ["enroll"] and method == "POST":
        return _handle_enroll(store, raw_body, headers or {})
    if rest == ["verify"] and method == "GET":
        return _handle_verify(bearer)
    if rest == ["borrow_auth"] and method == "POST":
        return await _handle_borrow_auth(bearer)
    if rest == ["devices"] and method == "GET":
        return _handle_list_devices(store)
    if rest == ["routing_view"] and method == "GET":
        return _handle_routing_view(store, bearer)
    if rest == ["registry"] and method == "GET":
        return _handle_registry(store, bearer)
    if rest == ["exec_tokens"] and method == "POST":
        return _handle_issue_exec_token(store, raw_body, bearer)
    if rest == ["exec_tokens"] and method == "GET":
        return _handle_exec_verification_set(bearer)
    if rest == ["dead_letters"] and method == "GET":
        return _ok(_json_response({"dead_letters": store.list_dead_letters()}))
    return _error(f"sync_relay: no route for {method} {path}", 404)


def _tenant_of(headers: dict[str, str]) -> str:
    """内核注入的租户标识（x-agentos-tenant，header key 已小写）。"""
    for k, v in headers.items():
        if isinstance(k, str) and k.lower() == "x-agentos-tenant" and isinstance(v, str):
            return v.strip()
    return ""


def _handle_enroll(
    store: RelayStore, raw_body: str, headers: dict[str, str]
) -> dict[str, Any]:
    """登录即入网（auth:admin，内核 JWT 已验）：admin 登录态直签 per-device token。

    唯一认证动作 = 连接 IP + 账号密码登录（ADR 2026-11-02-login-as-enrollment）；
    重复 enroll 幂等换发（upsert 顶旧绑定），旧 token 即时失效。
    """
    body = _decode_envelope(raw_body)
    if body is None:
        return _error("入网体必须是 JSON 对象", 400)
    outcome = enroll_device(
        store,
        device_id=str(body.get("device_id", "") or ""),
        tier=str(body.get("tier", "") or ""),
        app_version=str(body.get("app_version", "") or ""),
        capabilities=body.get("capabilities") if isinstance(body.get("capabilities"), dict) else {},
        tenant_id=_tenant_of(headers),
        now=datetime.now(UTC),
    )
    if isinstance(outcome, EnvelopeError):
        return _error(outcome.message, outcome.status)
    logger.info("[sync_relay] 登录直签发完成: device=%s", outcome.device_id)
    return _ok(_json_response(outcome.to_dict(), 200))


def _decode_envelope(raw_body: str) -> dict[str, Any] | None:
    try:
        envelope = _decode_body(raw_body, strict_object=True)
    except ValueError:
        return None
    return envelope if isinstance(envelope, dict) else None


def _handle_receive(store: RelayStore, raw_body: str, bearer: str) -> dict[str, Any]:
    envelope = _decode_envelope(raw_body)
    if envelope is None:
        return _error("信封必须是 JSON 对象", 400)
    outcome = process_events(
        store,
        envelope.get("events"),
        auth=_auth,
        bearer=bearer,
        received_at=now_iso_utc(),
    )
    if isinstance(outcome, EnvelopeError):
        return _error(outcome.message, outcome.status)
    logger.info(
        "[sync_relay] 批处理完成: accepted=%s duplicates=%s dead_lettered=%s",
        outcome.accepted, outcome.duplicates, len(outcome.dead_lettered),
    )
    return _ok(_json_response(outcome.to_dict(), 200))


def _handle_pull(store: RelayStore, bearer: str, query: dict[str, str]) -> dict[str, Any]:
    device = _auth.device_for(bearer)
    if device is None:
        return _error("unrecognized per-device token", 403)
    try:
        since = int(query.get("since_cursor") or "0")
    except ValueError:
        return _error("since_cursor 必须是整数", 400)
    try:
        limit = int(query.get("limit") or str(_pull_page_limit))
    except ValueError:
        limit = _pull_page_limit
    limit = max(1, min(limit, _pull_page_limit))
    events = store.events.pull(since, limit=limit)
    return _ok(
        _json_response(
            {"events": [_event_wire(e) for e in events], "count": len(events), "device": device},
            200,
        )
    )


def _backfill_token_hash(store: RelayStore, device: str, bearer: str) -> None:
    """对等信任哈希随行自愈（A 档迁移面）：presented token 摘要与在案不符即回填。

    覆盖 env 预置 token（无兑换签发面）与存量行（列新增前注册）；换绑 token
    （重签发）同样一次心跳收敛——注册表哈希恒等 presented 事实。
    """
    digest = token_sha256(bearer)
    registered = store.get_device(device)
    if registered is None or registered.get("token_hash") != digest:
        store.set_device_token_hash(device_id=device, token_hash=digest)


def _handle_register(store: RelayStore, raw_body: str, bearer: str) -> dict[str, Any]:
    device = _auth.device_for(bearer)
    if device is None:
        return _error("unrecognized per-device token", 403)
    body = _decode_envelope(raw_body)
    if body is None:
        return _error("注册体必须是 JSON 对象", 400)
    stated = str(body.get("device_id", "") or "")
    if stated and stated != device:
        return _error("device_id 与 token 绑定不符（防顶名）", 403)
    now = now_iso_utc()
    store.upsert_device(
        device_id=device,
        tier=str(body.get("tier", "") or ""),
        app_version=str(body.get("app_version", "") or ""),
        capabilities=body.get("capabilities") if isinstance(body.get("capabilities"), dict) else {},
        now_iso=now,
        address=_normalized_address(body.get("address")),
        storage=_opt_dict(body.get("storage")),
    )
    _backfill_token_hash(store, device, bearer)
    registered = store.get_device(device)
    return _ok(_json_response({"registered": registered}, 200))


def _handle_heartbeat(store: RelayStore, raw_body: str, bearer: str) -> dict[str, Any]:
    device = _auth.device_for(bearer)
    if device is None:
        return _error("unrecognized per-device token", 403)
    body = _decode_envelope(raw_body) or {}
    stated = str(body.get("device_id", "") or "")
    if stated and stated != device:
        return _error("device_id 与 token 绑定不符（防顶名）", 403)
    now = now_iso_utc()
    capabilities = body.get("capabilities")
    if isinstance(capabilities, dict):
        # 心跳携带能力面（2c D8：exec_channel 子键随心跳刷新，配置漂移无需重自举）。
        store.upsert_device(
            device_id=device,
            tier=str(body.get("tier", "") or ""),
            app_version=str(body.get("app_version", "") or ""),
            capabilities=capabilities,
            now_iso=now,
            address=_normalized_address(body.get("address")),
            storage=_opt_dict(body.get("storage")),
        )
    elif store.get_device(device) is None:
        # 未注册先心跳：按注册处理（幂等收敛，避免两端时序竞争丢档位面）。
        store.upsert_device(
            device_id=device,
            tier=str(body.get("tier", "") or ""),
            app_version=str(body.get("app_version", "") or ""),
            capabilities={},
            now_iso=now,
            address=_normalized_address(body.get("address")),
            storage=_opt_dict(body.get("storage")),
        )
    else:
        store.touch_device(device_id=device, now_iso=now)
    _backfill_token_hash(store, device, bearer)
    return _ok(_json_response({"heartbeat_at": now}, 200))


def _handle_bootstrap(store: RelayStore, bearer: str) -> dict[str, Any]:
    device = _auth.device_for(bearer)
    if device is None:
        return _error("unrecognized per-device token", 403)
    tiers = {d["device_id"]: d["tier"] for d in store.list_devices()}
    all_events = store.events.pull(0, limit=store.events.max_cursor() or 1)
    baseline = select_baseline(all_events, tiers)
    return _ok(
        _json_response(
            {
                "device": device,
                "events": [_event_wire(e) for e in baseline],
                "cursor": store.events.max_cursor(),
                "tiers": tiers,
            },
            200,
        )
    )


def _handle_verify(bearer: str) -> dict[str, Any]:
    """借权验证面（auth:none，bearer=per-device token）：token→绑定设备轻量核验。

    member 目标内核的 borrow_auth 经此验证请求方是否同注册表成员（ADR
    2026-10-22-borrowed-device-auth）；未命中即 403，不泄露存在性。
    """
    device = _auth.device_for(bearer) if _auth else None
    if device is None:
        return _error("unrecognized per-device token", 403)
    return _ok(_json_response({"valid": True, "device_id": device}, 200))


def _http_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict[str, Any] | None]:
    """stdlib HTTP JSON 单发（借权面出站统一出口，零三方依赖——云端 venv 无
    aiohttp 的 2026-10-22 实证后改道）。返回 (status, body)；网络失败 (0, None)。
    """
    import urllib.error
    import urllib.request

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        if _is_loopback_url(url):
            # 回环强制无代理（装机 2026-11-05 §B4）：借权面打本机内核，用户态
            # 系统代理劫持回环会 502；外部 relay 出口不动（部署可能依赖代理出网）。
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            resp_cm = opener.open(req, timeout=timeout)
        else:
            resp_cm = urllib.request.urlopen(req, timeout=timeout)
        with resp_cm as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    except Exception:  # noqa: BLE001 —— 网络失败归一 status=0
        return 0, None
    try:
        body = json.loads(raw.decode("utf-8"))
        return status, body if isinstance(body, dict) else None
    except Exception:  # noqa: BLE001 —— 非 JSON 体按无体处理
        return status, None


def _is_loopback_url(url: str) -> bool:
    """回环 URL 判定（_http_json 强制无代理的分流依据；探活回环判定同口径）。

    sync_agent/server.py 同款孪生实现——改动两处必须同刀。
    """
    from urllib.parse import urlsplit  # noqa: PLC0415

    host = urlsplit(url).hostname or ""
    return host in ("localhost", "::1") or host.startswith("127.")


async def _login_admin(password: str, port: str) -> tuple[int, dict[str, Any] | None]:
    """本机内核 login 出站（测试 monkeypatch 面）；返回 (status, body)。"""
    return _http_json(
        "POST",
        f"http://127.0.0.1:{port}/api/v1/auth/login",
        payload={"username": "admin", "password": password},
    )


async def _mint_admin_session() -> dict[str, Any] | None:
    """env 管理口令 → 本机内核 login 换发 admin 会话（借权签发唯一出口）。

    口令经内核 env 注入（装机/云端部署契约）；缺失或登录失败返回 None，由
    调用方翻译为 503——借权不可用必须显式失败，不静默降级为匿名面。
    """
    password = os.environ.get("AGENTOS_ADMIN_PASSWORD", "").strip()
    port = (os.environ.get("AGENTOS_KERNEL_PORT") or "").strip() or "9100"
    if not password:
        logger.error("[sync_relay] 借权被拒：AGENTOS_ADMIN_PASSWORD 未注入（部署契约缺失）")
        return None
    status, body = await _login_admin(password, port)
    if status < 200 or status >= 300 or not isinstance(body, dict):
        logger.error("[sync_relay] 借权换发登录失败: status=%s", status)
        return None
    return body


async def _handle_borrow_auth(bearer: str) -> dict[str, Any]:
    """配对设备借权换发本内核 admin 会话（auth:none，bearer=per-device token）。

    信任边界=同注册表（ADR 2026-10-22-borrowed-device-auth，用户裁定借权会话
    等同 admin）：per-device token 经 TokenAuth 验明即同信任域成员——与入网码
    物理递手的信任强度一致。响应即内核 login 契约（access/refresh/expires_in）
    附 verified_device 供请求方留痕。
    """
    if _auth is None:
        return _error("sync_relay auth not initialized (load pending)", 503)
    device = _auth.device_for(bearer)
    if device is None:
        return _error("unrecognized per-device token", 403)
    minted = await _mint_admin_session()
    if minted is None:
        return _error("借权换发失败（本机管理口令缺失或登录失败）", 503)
    return _ok(_json_response({**minted, "verified_device": device}, 200))


def _handle_list_devices(store: RelayStore) -> dict[str, Any]:
    now = datetime.now(UTC)
    devices = [
        {**d, "online": is_online(d["last_heartbeat_at"], now, _online_window_secs)}
        for d in store.list_devices()
    ]
    return _ok(
        _json_response(
            {"devices": devices, "online_window_secs": _online_window_secs}, 200
        )
    )


def _handle_routing_view(store: RelayStore, bearer: str) -> dict[str, Any]:
    """路由只读视图（2c D8：relay 是数据源不是决策者；per-device token 鉴权）。

    配置 hub_device_id 时，任何已配对设备的成功拉取即中枢网络面活性的直接
    证据（请求正被中枢服务）——顺带 touch 中枢条目（ADR 2026-10-21：中枢卡
    可达性以拉取证据优先，心跳单源误报「不可达」的中和面）。
    """
    device = _auth.device_for(bearer)
    if device is None:
        return _error("unrecognized per-device token", 403)
    if _hub_device_id:
        store.touch_device(device_id=_hub_device_id, now_iso=now_iso_utc())
    view = build_routing_view(
        store,
        now=datetime.now(UTC),
        window_secs=_online_window_secs,
        requested_by=device,
        hub_device_id=_hub_device_id,
    )
    return _ok(_json_response(view, 200))


def _handle_registry(store: RelayStore, bearer: str) -> dict[str, Any]:
    """全网注册表复制快照（auth:none，bearer=per-device token；A 档分发通道）。

    每台成员经此拉取全网设备表（含各设备 token_hash，ADR
    2026-10-22-distributed-registry-peer-trust）：目标端借权核验改本地比对，
    中枢退化为分发通道——已复制到本机即可核验，非在线依赖。哈希仅对同注册表
    成员出域（入网码物理递手的信任边界），非成员零暴露（403）；明文 token
    恒不出库（P6）。
    """
    device = _auth.device_for(bearer)
    if device is None:
        return _error("unrecognized per-device token", 403)
    now = datetime.now(UTC)
    devices = [
        {**d, "online": is_online(d["last_heartbeat_at"], now, _online_window_secs)}
        for d in store.list_devices()
    ]
    return _ok(
        _json_response(
            {
                "devices": devices,
                "online_window_secs": _online_window_secs,
                "requested_by": device,
                "fetched_at": now_iso_utc(),
            },
            200,
        )
    )


def _handle_issue_exec_token(
    store: RelayStore, raw_body: str, bearer: str
) -> dict[str, Any]:
    """执行凭据签发（双钥配对流：发起端签发 → 属主端验证，ADR D13'）。

    发起端 sync token 鉴权；凭据体由发起端自带（relay 不代生成）；目标设备
    须在注册表；短时效（settings exec_token_ttl_secs 缺省，上限 24h 夹取）。
    """
    issuer = _auth.device_for(bearer)
    if issuer is None:
        return _error("unrecognized per-device token", 403)
    if _exec_store is None:
        return _error("sync_relay exec token store not initialized (load pending)", 502)
    body = _decode_envelope(raw_body)
    if body is None:
        return _error("签发体必须是 JSON 对象", 400)
    requested_ttl = body.get("ttl_secs")
    outcome = issue_exec_token(
        _exec_store,
        issued_by=issuer,
        target_device=str(body.get("target_device", "") or ""),
        token=str(body.get("exec_token", "") or ""),
        ttl_secs=requested_ttl if requested_ttl is not None else _exec_token_ttl_secs,
        now=datetime.now(UTC),
        device_exists=lambda device_id: store.get_device(device_id) is not None,
    )
    if isinstance(outcome, ExecTokenError):
        return _error(outcome.message, outcome.status)
    logger.info(
        "[sync_relay] 执行凭据已签发: target=%s by=%s", outcome.target_device, outcome.issued_by
    )
    return _ok(_json_response(outcome.to_dict(), 200))


def _handle_exec_verification_set(bearer: str) -> dict[str, Any]:
    """属主端验证集拉取：只回落绑定自己的有效执行凭据（per-device token 鉴权）。"""
    device = _auth.device_for(bearer)
    if device is None:
        return _error("unrecognized per-device token", 403)
    if _exec_store is None:
        return _error("sync_relay exec token store not initialized (load pending)", 502)
    tokens = verification_set(_exec_store, device_id=device, now=datetime.now(UTC))
    return _ok(_json_response({"device": device, "tokens": tokens}, 200))


def _event_wire(event: SyncEvent) -> dict[str, Any]:
    wire = event.to_wire()
    wire["cursor"] = event.cursor
    return wire


if __name__ == "__main__":
    plugin.run()
