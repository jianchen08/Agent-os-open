#!/usr/bin/env python3
"""sync_agent 端侧常驻插件——多端同步的每端执行面（零内核改动）。

装配（on_load）→ 设备内生配置缺失拒载（fail-fast，trace_exporter 先例）→
配对解析（env > 配对凭据 > settings.yaml，relay_url/token 必须同源成对）→
已配对则引擎自举（端侧状态全新：注册 + 入网基线 + 游标定位；否则断点续传）
→ 后台单拍循环（capture → flush → pull → heartbeat，relay 不可达只退避不阻塞
本机执行）。

入网流（ADR 2026-11-02-login-as-enrollment）：未配对**不拒载**——引擎停转、
enroll 端点可用、状态面 paired=false 可查；`POST /ext/sync_agent/enroll` 携
relay 基址 + relay 内核 admin JWT → 向 relay 直签正式 per-device token →
凭据落 data/（永不同步层，P6：token 不入同步面）→ 重置配对账（游标/资产/
已应用按 relay 账本记账，换账本即作废；HLC 时钟保留）→ 引擎即时按新凭据
重建。重启后凭据续效（解析序 env > 凭据 > settings）。

配置源（settings.yaml 字段 + env 覆盖；入网凭据 = enroll 落下的本端工件）：
- relay_url    env AGENTOS_SYNC_RELAY_URL > 配对凭据 > settings.sync_agent.relay_url
- token        env AGENTOS_SYNC_TOKEN > 配对凭据（settings 不承载 token，P6）
- device_id    env AGENTOS_DEVICE_ID（缺省 platform.node()）
- device_tier  env AGENTOS_DEVICE_TIER（缺省 desktop；词表 cloud/desktop/phone）
- watch_root   env AGENTOS_SYNC_WATCH_DIR（缺省用户配置层 USER_CONFIG_DIR）
- file_channel env AGENTOS_SYNC_FILE_OWNER_SCOPES/_READER_SCOPES（缺省纯读者；
  scope 源目录 settings file_root_*，缺省 plugins/shared、skills、data 内
  artifact_index/；git 缺失 = 通道停用，config/db 面不受影响）
- exec_channel env AGENTOS_EXEC_MCP_URL/_ALLOWED_ROOTS > settings.exec_channel 块
  （2c D8 能力子键：注册/心跳上报 + 属主端验证集周期拉取；未配 mcp_url = 面
  全关，注册表零噪音）
- backup       settings backup_repo_root（env AGENTOS_SYNC_BACKUP_ROOT 优先；T6
  手动备份仓库根；空 = 备份能力停用，端点诚实报未配置；git 可执行缺失同停用）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根（共享裸模块）入 sys.path

from _root_api import (  # noqa: E402  # noqa: E402
    OutboxStore,  # noqa: E402
    decode_body as _decode_body,
    json_response as _json_response,
    now_iso_utc,  # noqa: E402
    ok as _ok,
    protocol_error as _error,
    user_config_dir,
    user_data_dir,
)
from agent_core import SUPPORTED_ASSET_TYPES, AgentConfig, SyncAgentEngine  # noqa: E402
from agent_state import AgentStateStore  # noqa: E402
from backup import BackupConfig, BackupError, BackupService  # noqa: E402
from exec_channel import build_capability, exec_channel_from_settings  # noqa: E402
from file_channel import (  # noqa: E402
    FILE_CHANNEL_SCOPES,
    FileChannelConfig,
)

plugin = AgentOSPlugin("sync_agent")
logger = logging.getLogger(__name__)

_RELAY_URL_ENV = "AGENTOS_SYNC_RELAY_URL"
_TOKEN_ENV = "AGENTOS_SYNC_TOKEN"
_DEVICE_ID_ENV = "AGENTOS_DEVICE_ID"
_DEVICE_TIER_ENV = "AGENTOS_DEVICE_TIER"
_APP_VERSION_ENV = "AGENTOS_APP_VERSION"
_WATCH_DIR_ENV = "AGENTOS_SYNC_WATCH_DIR"
_POLL_INTERVAL_ENV = "AGENTOS_SYNC_POLL_INTERVAL"
_HEARTBEAT_INTERVAL_ENV = "AGENTOS_SYNC_HEARTBEAT_SECS"
_FILE_OWNER_SCOPES_ENV = "AGENTOS_SYNC_FILE_OWNER_SCOPES"
_FILE_READER_SCOPES_ENV = "AGENTOS_SYNC_FILE_READER_SCOPES"
_BACKUP_ROOT_ENV = "AGENTOS_SYNC_BACKUP_ROOT"

#: db-admin table_query 单页行数（内核 clamp 上限 500，整表分页拉平）。
_BACKUP_TABLE_PAGE = 500

_TIER_VOCABULARY = {"cloud", "desktop", "phone"}

#: file 通道 scope 源目录的 settings 键（缺省值伴生；相对路径按进程 cwd 解析）。
_SCOPE_ROOT_SETTINGS: dict[str, tuple[str, str]] = {
    "plugins": ("file_root_plugins", "plugins/shared"),
    "skills": ("file_root_skills", "skills"),
}
_ARTIFACT_INDEX_SETTING = "file_root_artifact_index"

#: 配对凭据键（agent_credential 表；data/ 永不同步层）。
_CRED_RELAY_URL = "relay_url"
_CRED_DEVICE_TOKEN = "device_token"
_CRED_TENANT = "tenant"


@dataclass
class Assembly:
    """装载期落定的设备内生面（引擎重建共用，enroll 重入网不重算）。"""

    state: AgentStateStore
    outbox: OutboxStore
    watch_root: Path
    device_id: str
    device_tier: str
    app_version: str
    settings: dict[str, Any]
    data_root: Path
    backup: BackupService | None = None


_assembly: Assembly | None = None
_engine: SyncAgentEngine | None = None

#: 云端注册表透传短 TTL 缓存（秒）：心跳态要新鲜，只缓存成功面（降级不缓存，
#: 恢复即见）；重配对/卸载即清。
_REGISTRY_CACHE_TTL_SECS = 5.0
_registry_cache: dict[str, Any] | None = None
_registry_cached_at: float = 0.0
_registry_mono: Callable[[], float] = time.monotonic


def _resolve_pairing(settings: dict[str, Any], state: AgentStateStore) -> tuple[str, str]:
    """配对解析（env > 配对凭据 > settings.yaml；返回 (relay_url, token)，可为空串）。"""
    relay_url = (
        os.environ.get(_RELAY_URL_ENV, "").strip()
        or state.load_credential(_CRED_RELAY_URL) or ""
        or str(settings.get("relay_url", "") or "").strip()
    )
    token = os.environ.get(_TOKEN_ENV, "").strip() or state.load_credential(_CRED_DEVICE_TOKEN) or ""
    return relay_url.rstrip("/"), token


def _parse_scope_list(raw: str) -> list[str]:
    """scope 列表解析（逗号/空白分隔；词表外告警剔除——同步面不因配置笔误拒载）。"""
    names: list[str] = []
    for token in raw.replace(",", " ").split():
        if token in FILE_CHANNEL_SCOPES:
            if token not in names:
                names.append(token)
        else:
            logger.warning(
                "[sync_agent] file scope %r 不在词表 %s，剔除",
                token,
                "/".join(FILE_CHANNEL_SCOPES),
            )
    return names


def _resolve_scope_root(settings: dict[str, Any], scope: str, data_root: Path) -> Path:
    """scope 内容源解析：settings 覆盖 > 缺省（相对路径按进程 cwd 解析）。"""
    if scope in _SCOPE_ROOT_SETTINGS:
        key, default = _SCOPE_ROOT_SETTINGS[scope]
        raw = str(settings.get(key, "") or "").strip() or default
        root = Path(raw)
    else:
        raw = str(settings.get(_ARTIFACT_INDEX_SETTING, "") or "").strip()
        root = Path(raw) if raw else data_root / "sync_agent" / "artifact_index"
    return root if root.is_absolute() else Path.cwd() / root


def _build_file_channel_config(
    settings: dict[str, Any], data_root: Path, device_id: str
) -> FileChannelConfig:
    """file 通道装配（T2 拍板③b）：属主域=配置声明（缺省纯读者）。

    缺省 reader 域 = 词表全集减属主域（transport 接线后自动镜像；无传输面
    前读者臂空转零副作用）。属主源目录缺失 = 该域不捕获（告警一行）。
    """
    owner_raw = (
        os.environ.get(_FILE_OWNER_SCOPES_ENV, "").strip()
        or str(settings.get("file_owner_scopes", "") or "").strip()
    )
    reader_raw = (
        os.environ.get(_FILE_READER_SCOPES_ENV, "").strip()
        or str(settings.get("file_reader_scopes", "") or "").strip()
    )
    owner_names = _parse_scope_list(owner_raw)
    if reader_raw:
        reader_names = [s for s in _parse_scope_list(reader_raw) if s not in owner_names]
    else:
        reader_names = [s for s in FILE_CHANNEL_SCOPES if s not in owner_names]
    owner_roots: dict[str, Path] = {}
    for scope in owner_names:
        root = _resolve_scope_root(settings, scope, data_root)
        if root.is_dir():
            owner_roots[scope] = root
        else:
            logger.warning("[sync_agent] file scope %s 源目录缺失，本域不捕获: %s", scope, root)
    return FileChannelConfig(
        repo_root=data_root / "sync_agent" / "file_channel",
        owner_roots=owner_roots,
        reader_scopes=frozenset(reader_names),
        device_id=device_id,
    )


def _exec_capability_of(settings: dict[str, Any]) -> dict[str, Any]:
    """exec_channel 能力子键（env > settings.exec_channel 块；未启用 = 空 dict）。"""
    return build_capability(exec_channel_from_settings(settings))


def _authorization(headers: dict[str, str] | None) -> str:
    """取入站请求的原始 Authorization 头值（内核 header key 已小写，大小写防御）。

    db-admin/service-registry 契约身份链：插件只透传凭证，角色校验在内核
    handler 执行（信任锚点，插件无法伪造角色）。
    """
    for key, value in (headers or {}).items():
        if isinstance(key, str) and key.lower() == "authorization" and value:
            return value
    return ""


def _unwrap_status_envelope(envelope: Any, what: str) -> dict[str, Any]:
    """``{status, body}``/``{status, error}`` 信封解包（db-admin 形态）。"""
    if isinstance(envelope, dict) and isinstance(envelope.get("body"), dict):
        return envelope["body"]
    if isinstance(envelope, dict) and "error" in envelope:
        err = envelope["error"]
        message = err.get("message") if isinstance(err, dict) else str(err)
        raise RuntimeError(f"{what} 失败: {message or envelope}")
    raise RuntimeError(f"{what} 返回异常信封: {envelope!r}")


class _BackupKernelReads:
    """备份导出的内核读端口（每请求构造：身份链随当次请求 Authorization）。

    db-admin table_query 整表分页拉平（租户收窄随身份链）+ service-registry
    messages.list 全文行。失败上抛 RuntimeError（run_backup 统一翻译为
    BackupError，导出面零 commit 保证）。
    """

    def __init__(self, authorization: str) -> None:
        self._authorization = authorization

    async def list_table_rows(self, table: str) -> list[dict[str, Any]]:
        handle = plugin.get_capability("db-admin")
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            envelope = await handle.call(
                "table_query",
                {
                    "table": table,
                    "limit": _BACKUP_TABLE_PAGE,
                    "offset": offset,
                    "_authorization": self._authorization,
                },
            )
            body = _unwrap_status_envelope(envelope, f"db-admin.table_query({table})")
            page = body.get("rows")
            if not isinstance(page, list):
                raise RuntimeError(f"db-admin.table_query({table}) 信封缺 rows 列表")
            rows.extend(row for row in page if isinstance(row, dict))
            offset += len(page)
            total = int(body.get("total") or 0)
            if not page or offset >= total:
                return rows

    async def list_pipeline_messages(self, pipeline_id: str) -> list[dict[str, Any]]:
        handle = plugin.get_capability("service-registry")
        result = await handle.call("messages.list", {"pipeline_id": pipeline_id})
        if not isinstance(result, list):
            raise RuntimeError(f"messages.list({pipeline_id}) 返回非数组信封")
        return [row for row in result if isinstance(row, dict)]


def _build_backup(settings: dict[str, Any]) -> BackupService | None:
    """备份能力装配（settings backup_repo_root / env 覆盖；未配置 = 停用）。"""
    raw = (
        os.environ.get(_BACKUP_ROOT_ENV, "").strip()
        or str(settings.get("backup_repo_root", "") or "").strip()
    )
    if not raw:
        return None
    root = Path(raw)
    if not root.is_absolute():
        root = Path.cwd() / root
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("[sync_agent] 备份仓库根目录不可建，备份能力停用: %s (%s)", root, exc)
        return None
    return BackupService(BackupConfig(repo_root=root))


def _build_engine(assembly: Assembly) -> SyncAgentEngine | None:
    """按当前配对解析装配引擎；配对不全（缺 relay_url 或 token）返回 None。"""
    relay_url, token = _resolve_pairing(assembly.settings, assembly.state)
    if not relay_url or not token:
        return None
    config = AgentConfig(
        relay_url=relay_url,
        token=token,
        device_id=assembly.device_id,
        device_tier=assembly.device_tier,
        app_version=assembly.app_version,
        watch_root=assembly.watch_root,
        pattern=str(assembly.settings.get("watch_pattern", "") or "").strip() or "**/*.yaml",
        poll_interval_secs=_positive(
            os.environ.get(_POLL_INTERVAL_ENV), assembly.settings.get("poll_interval_secs"), 5.0
        ),
        heartbeat_interval_secs=_positive(
            os.environ.get(_HEARTBEAT_INTERVAL_ENV),
            assembly.settings.get("heartbeat_interval_secs"),
            30.0,
        ),
        file_channel=_build_file_channel_config(
            assembly.settings, assembly.data_root, assembly.device_id
        ),
        exec_channel=exec_channel_from_settings(assembly.settings),
        exec_verification_path=assembly.data_root / "sync_agent" / "exec_verification.json",
        registry_replica_path=assembly.data_root / "sync_agent" / "registry_replica.json",
        data_root=assembly.data_root,
    )
    return SyncAgentEngine(config, state=assembly.state, outbox=assembly.outbox)


def _start_engine(engine: SyncAgentEngine) -> None:
    restored = engine.load_pending()
    engine.start()
    logger.info(
        "sync_agent 引擎已启动: device=%s/%s relay=%s 恢复待上行=%s 自举=%s",
        engine.config.device_id,
        engine.config.device_tier,
        engine.config.relay_url,
        restored,
        "续传" if not engine.needs_bootstrap() else "待入网",
    )


def _reset_registry_cache() -> None:
    """清云端注册表透传缓存（重配对换中枢/卸载时：旧中枢视图不得泄漏进新答）。"""
    global _registry_cache, _registry_cached_at
    _registry_cache = None
    _registry_cached_at = 0.0


async def _restart_engine() -> SyncAgentEngine:
    """停旧引擎并按当前凭据重建（唯一调用点 _handle_enroll：凭据已落盘）。

    装配必成不变量：凭据落盘后 relay_url/token 解析双非空（env 或凭据至少
    一源供给），_build_engine 不可能返回 None。
    """
    global _engine
    if _engine is not None:
        await _engine.stop()
        _engine = None
    _reset_registry_cache()
    assert _assembly is not None, "restart 前提：装载已完成"
    engine = _build_engine(_assembly)
    assert engine is not None, "凭据落盘后解析必非空"
    _engine = engine
    _start_engine(engine)
    return engine


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """装配设备内生面；配对不全不拒载（enroll 端点必须可达——登录入网入口）。"""
    global _assembly, _engine
    settings = plugin.get_config().get("settings", {})
    device_id = os.environ.get(_DEVICE_ID_ENV, "").strip() or platform.node()
    tier = os.environ.get(_DEVICE_TIER_ENV, "").strip() or str(
        settings.get("device_tier", "") or ""
    ).strip() or "desktop"
    app_version = os.environ.get(_APP_VERSION_ENV, "").strip() or str(
        settings.get("app_version", "") or ""
    ).strip() or "0.0.0-dev"
    watch_raw = os.environ.get(_WATCH_DIR_ENV, "").strip()
    configured_watch: Path | None = Path(watch_raw) if watch_raw else user_config_dir()
    if configured_watch is None:
        raise RuntimeError(
            "sync_agent 缺少必填配置: 用户配置层（USER_ROOT 未配置且未设 "
            + _WATCH_DIR_ENV
            + "）"
        )
    if tier not in _TIER_VOCABULARY:
        raise RuntimeError(
            f"sync_agent device_tier 必须在 {'/'.join(sorted(_TIER_VOCABULARY))}，得到 {tier!r}"
        )
    data_root = user_data_dir()
    if data_root is None:
        raise RuntimeError("sync_agent 无法定位用户数据目录（USER_ROOT 未配置）")
    state = AgentStateStore(data_root / "sync_agent" / "state.db")
    outbox = OutboxStore(data_root / "sync_agent" / "outbox.jsonl")
    _assembly = Assembly(
        state=state,
        outbox=outbox,
        watch_root=configured_watch,
        device_id=device_id,
        device_tier=tier,
        app_version=app_version,
        settings=settings,
        data_root=data_root,
        backup=_build_backup(settings),
    )
    _engine = _build_engine(_assembly)
    if _engine is None:
        logger.warning(
            "[sync_agent] 未配对（无 relay_url/token 配置且无入网凭据）——引擎停转，"
            "等待 /ext/sync_agent/enroll 登录入网"
        )
        return
    _start_engine(_engine)


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    global _engine
    if _engine is not None:
        await _engine.stop()
        _engine = None
    _reset_registry_cache()


def _positive(env_raw: str | None, setting_raw: Any, default: float) -> float:
    for raw in (env_raw, setting_raw):
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return default


def _identity_payload() -> dict[str, Any]:
    assert _assembly is not None
    capabilities: dict[str, Any] = {"asset_types": sorted(SUPPORTED_ASSET_TYPES)}
    exec_capability = _exec_capability_of(_assembly.settings)
    if exec_capability:
        capabilities["exec_channel"] = exec_capability
    return {
        "device_id": _assembly.device_id,
        "tier": _assembly.device_tier,
        "app_version": _assembly.app_version,
        "capabilities": capabilities,
    }


def _post_enroll(
    endpoint: str, admin_jwt: str, payload: dict[str, Any]
) -> tuple[int, dict[str, Any] | None]:
    """登录直签发出站（stdlib urllib，_relay_verify 同款零三方依赖口径；admin JWT 即凭据）。"""
    return _http_json("POST", endpoint, token=admin_jwt, payload=payload, timeout=30.0)


def _relay_error_message(body: dict[str, Any] | None) -> str:
    """relay 错误体（{"error": {"code","message"}}）→ 用户可读消息。"""
    inner = (body or {}).get("error")
    if isinstance(inner, dict):
        return str(inner.get("message", "") or "")
    if isinstance(inner, str):
        return inner
    return ""


async def _handle_enroll(raw_body: str) -> dict[str, Any]:
    """登录即入网：携 relay 基址 + relay 内核 admin JWT → 直签 token → 凭据落盘 → 引擎重建。"""
    if _assembly is None:
        return _error("sync_agent not loaded (load pending)", 503)
    try:
        body = _decode_body(raw_body, strict_object=True)
    except ValueError:
        return _error("入网体必须是 JSON 对象", 400)
    relay_url = str(body.get("relay_url", "") or "").strip().rstrip("/")
    admin_jwt = str(body.get("admin_jwt", "") or "").strip()
    if not relay_url:
        return _error("relay_url 必填（relay 端点基址，如 http://<host>:<port>/ext/sync_relay）", 400)
    if not admin_jwt:
        return _error("admin_jwt 必填（relay 内核登录态——登录即入网的凭据锚）", 400)
    status, result = _post_enroll(f"{relay_url}/enroll", admin_jwt, _identity_payload())
    if status == 0:
        return _error("入网失败：relay 不可达", 502)
    if status >= 300:
        message = _relay_error_message(result)
        return _error(f"入网失败：{message or f'relay HTTP {status}'}", 502)
    device_token = str((result or {}).get("device_token", "") or "")
    if not device_token:
        return _error("签发响应缺 device_token（relay 协议违约）", 502)
    tenant = str((result or {}).get("tenant_id", "") or "")
    state = _assembly.state
    state.save_credential(_CRED_RELAY_URL, relay_url)
    state.save_credential(_CRED_DEVICE_TOKEN, device_token)
    state.save_credential(_CRED_TENANT, tenant)
    # 重入网清账：游标/资产/已应用按旧 relay 账本记账，换账本即作废（HLC 保留）。
    state.reset_pairing_state()
    await _restart_engine()
    logger.info("[sync_agent] 登录入网完成: device=%s relay=%s", _assembly.device_id, relay_url)
    return _ok(
        _json_response(
            {
                "paired": True,
                "device_id": _assembly.device_id,
                "tier": _assembly.device_tier,
                "relay_url": relay_url,
                "tenant": tenant,
            },
            200,
        )
    )


def _handle_status() -> dict[str, Any]:
    if _assembly is None:
        return _ok(_json_response({"loaded": False}, 200))
    if _engine is None:
        return _ok(
            _json_response(
                {
                    "loaded": True,
                    "paired": False,
                    "device_id": _assembly.device_id,
                    "tier": _assembly.device_tier,
                },
                200,
            )
        )
    return _ok(_json_response({"loaded": True, "paired": True, **_engine.status_dict()}, 200))


async def _relay_verify(relay_url: str, token: str) -> tuple[int, dict[str, Any] | None]:
    """relay /verify 出站（测试 monkeypatch 面）；返回 (status, body)。0=网络失败。
    stdlib urllib 实现（云端 venv 无 aiohttp 的 2026-10-22 实证后改道）。"""
    return _http_json("GET", f"{relay_url}/verify", token=token)


async def _post_borrow_auth(
    endpoint: str, device_id: str, token: str
) -> tuple[int, dict[str, Any] | None]:
    """目标侧 borrow_auth 出站（测试 monkeypatch 面）；返回 (status, body)。0=网络失败。
    stdlib urllib 实现（同 _relay_verify 改道口径），30s 长超时容忍目标慢启动。"""
    return _http_json(
        "POST", endpoint, token=token, payload={"device_id": device_id}, timeout=30.0
    )


def _bearer_token(headers: dict[str, str] | None) -> str:
    """取 Bearer 凭据裸值（sync_relay/server.py 同款；header key 已小写）。"""
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == "authorization" and isinstance(v, str):
            if v.lower().startswith("bearer "):
                return v[7:].strip()
    return ""


async def _mint_admin_session() -> dict[str, Any] | None:
    """env 管理口令 → 本机内核 login 换发 admin 会话（借权签发唯一出口）。

    sync_relay/server.py 同款孪生实现——播种件禁共享根跨包 import（装机炸
    铁律），改动两处必须同刀。口令缺失或登录失败返回 None（显式失败不降级）。
    """
    password = os.environ.get("AGENTOS_ADMIN_PASSWORD", "").strip()
    port = (os.environ.get("AGENTOS_KERNEL_PORT") or "").strip() or "9100"
    if not password:
        logger.error("[sync_agent] 借权被拒：AGENTOS_ADMIN_PASSWORD 未注入（部署契约缺失）")
        return None
    status, body = await _login_admin(password, port)
    if status < 200 or status >= 300 or not isinstance(body, dict):
        logger.error("[sync_agent] 借权换发登录失败: status=%s", status)
        return None
    return body


def _http_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict[str, Any] | None]:
    """stdlib HTTP JSON 单发（借权面出站统一出口，零三方依赖）。

    sync_relay/server.py 同款孪生实现——播种件禁共享根跨包 import（装机炸
    铁律），改动两处必须同刀。
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

    sync_relay/server.py 同款孪生实现——改动两处必须同刀。
    """
    from urllib.parse import urlsplit  # noqa: PLC0415

    host = urlsplit(url).hostname or ""
    return host in ("localhost", "::1") or host.startswith("127.")


async def _login_admin(password: str, port: str) -> tuple[int, dict[str, Any] | None]:
    """本机内核 login 出站（sync_relay/server.py 孪生；测试 monkeypatch 面）。"""
    return _http_json(
        "POST",
        f"http://127.0.0.1:{port}/api/v1/auth/login",
        payload={"username": "admin", "password": password},
    )


async def _handle_borrow_auth(headers: dict[str, str] | None) -> dict[str, Any]:
    """配对设备借权（member 目标；auth:none，bearer=请求方 per-device token）。

    核验序（A 档对等信任，ADR 2026-10-22-distributed-registry-peer-trust）：
    ①本机注册表副本比对（sha256(presented) == 副本条目 token_hash）——中枢
    退化为分发通道，已复制即可核验（中枢离线照样借权）；②副本未命中回退
    relay /verify（副本缺该请求方条目/存量无哈希行的收敛窗口）。信任边界与
    签发语义单点在 sync_relay/server.py _handle_borrow_auth（ADR 草案
    2026-10-22-borrowed-device-auth，用户裁定借权会话等同 admin）。本机未
    配对=借权面不存在语义，503。
    """
    if _assembly is None:
        return _error("sync_agent not loaded (load pending)", 503)
    relay_url, own_token = _resolve_pairing(_assembly.settings, _assembly.state)
    if not relay_url or not own_token:
        return _error("本机未配对，借权不可用", 503)
    bearer = _bearer_token(headers)
    if not bearer:
        return _error("缺少 per-device token（Bearer）", 401)
    verified_device = ""
    via = "relay"
    engine = _engine
    replica = engine.registry_replica if engine is not None else None
    if replica is not None:
        local = replica.verify(bearer)
        if local:
            verified_device, via = local, "replica"
    if not verified_device:
        status, verdict = await _relay_verify(relay_url, bearer)
        if status == 0:
            return _error("注册表验证不可达（本机副本亦无该请求方条目）", 502)
        if status >= 300:
            return _error("请求方 per-device token 未通过注册表验证", 403)
        if isinstance(verdict, dict):
            verified_device = str(verdict.get("device_id", "") or "")
    minted = await _mint_admin_session()
    if minted is None:
        return _error("借权换发失败（本机管理口令缺失或登录失败）", 503)
    logger.info("[sync_agent] 借权核验通过: device=%s via=%s", verified_device, via)
    return _ok(_json_response({**minted, "verified_device": verified_device}, 200))


async def _handle_borrow(raw_body: str) -> dict[str, Any]:
    """借权出站代理（本机前端 → 目标内核，auth=admin=仅已登录本机前端）。

    代持本机 per-device token 完成目标侧 borrow_auth 握手，令牌对原样回传
    前端（前端按 @base 分键落盘后整页切换）。目标为中枢走 sync_relay/
    borrow_auth，成员走 sync_agent/borrow_auth；本机未配对 503。
    """
    if _assembly is None:
        return _error("sync_agent not loaded (load pending)", 503)
    try:
        body = _decode_body(raw_body, strict_object=True)
    except ValueError:
        return _error("借权体必须是 JSON 对象", 400)
    target_base = str(body.get("target_base", "") or "").strip().rstrip("/")
    if not target_base.startswith(("http://", "https://")):
        return _error("target_base 必须是 http(s) 地址", 400)
    relay_url, own_token = _resolve_pairing(_assembly.settings, _assembly.state)
    if not relay_url or not own_token:
        return _error("本机未配对，借权不可用", 503)
    # target_is_hub 缺省=自动判别：先中枢端点，未命中（404/502）再试成员端点
    hub_flag = body.get("target_is_hub")
    candidates = (
        [True, False]
        if hub_flag is None
        else [hub_flag is True]
    )
    status = 0
    upstream: dict[str, Any] | None = None
    for is_hub in candidates:
        endpoint = (
            f"{target_base}/ext/sync_relay/borrow_auth"
            if is_hub
            else f"{target_base}/ext/sync_agent/borrow_auth"
        )
        status, upstream = await _post_borrow_auth(
            endpoint, str(_assembly.device_id), own_token
        )
        if status == 0:
            return _error(f"目标内核不可达（{target_base}）", 502)
        if status < 300:
            break
        if status not in (404, 502):
            break
    if status >= 300:
        message = ""
        if isinstance(upstream, dict):
            inner = upstream.get("error", "")
            message = inner.get("message", "") if isinstance(inner, dict) else str(inner)
        passthrough = status if status in (401, 403, 503) else 502
        return _error(message or f"目标内核拒绝借权（status={status}）", passthrough)
    if not isinstance(upstream, dict) or not upstream.get("access_token"):
        return _error("目标内核借权响应缺令牌（协议违约）", 502)
    return _ok(_json_response(upstream, 200))


def _handle_capture_step(state: Any) -> dict[str, Any]:
    """管道步骤入口（invoke_entry）：引擎投喂 working state → DB 通道捕获。

    未配对（引擎停转）零事件返回——管道调用零语义差（引擎容错先例）；
    capture_step 自身零抛出（离线第一同律）。
    """
    if _engine is None:
        return {}
    return _engine.capture_step(state)


def _handle_mirror_records(query: dict[str, str] | None) -> dict[str, Any]:
    """只读镜像呈现（T1）：单端点按表名过滤，无任何写路由（结构性只读）。"""
    if _assembly is None:
        return _error("sync_agent not loaded (load pending)", 503)
    table = str((query or {}).get("table", "") or "").strip() or None
    return _ok(
        _json_response({"records": _assembly.state.list_mirror_records(table)}, 200)
    )


def _relay_device_view(entry: dict[str, Any]) -> dict[str, Any]:
    """routing_view 条目 → 设备卡契约（前端 RelayDevice）：字段直传不编造。

    address/storage/is_hub 直连面透传（ADR 2026-10-21-device-card-direct-connect
    翻转「云端读面不透出」旧取舍：卡上直连/探活/中枢识别的数据源）；routing_view
    未携带（旧云端）时如实缺省，不猜不补。**他端回环自报不透出**（对端不可达，
    透出必生成死连接卡——云端中枢存量行 127.0.0.1:9100 生产实证；本机行保留，
    卡列表本就滤掉本机）：视同无地址，呈现回落心跳语义。
    """
    view = {
        "device_id": str(entry.get("device_id", "") or ""),
        "tier": str(entry.get("tier", "") or ""),
        "app_version": str(entry.get("app_version", "") or ""),
        "online": bool(entry.get("online", False)),
        "last_heartbeat_at": str(entry.get("last_heartbeat_at", "") or ""),
    }
    address = str(entry.get("address", "") or "")
    self_id = _assembly.device_id if _assembly is not None else ""
    if address and str(entry.get("device_id") or "") != self_id and _is_loopback_address(address):
        address = ""
    if address:
        view["address"] = address
    storage = entry.get("storage")
    if isinstance(storage, dict) and storage:
        view["storage"] = storage
    if entry.get("is_hub"):
        view["is_hub"] = True
    return view


async def _handle_relay_devices() -> dict[str, Any]:
    """GET /relay/devices：云端权威设备注册表透传（R30 数据面销项）。

    数据源 = 配对中枢的 routing_view（经引擎既有通道拉取，零新配置零第二
    通道）——桌面本机 sync_relay 实例注册表恒空（端侧非中枢），设备页数据
    面必须接生产权威。降级语义（禁假装成功）：未配对/云端不可达 → 先回落
    本机注册表副本（A 档复制面，source=replica + degraded + 副本截至时刻，
    分布式口径「每台设备都持有全网注册表」的呈现面）；副本亦空 → 空
    devices + degraded + error 的 200 信封；降级响应不缓存（恢复即见）；
    短 TTL 只缓存成功面。
    """
    if _assembly is None:
        return _error("sync_agent not loaded (load pending)", 503)
    global _registry_cache, _registry_cached_at
    now = _registry_mono()
    if _registry_cache is not None and now - _registry_cached_at < _REGISTRY_CACHE_TTL_SECS:
        return _ok(_json_response(_registry_cache, 200))
    engine = _engine
    if engine is None:
        return _ok(
            _json_response(
                {
                    "devices": [],
                    "source": "none",
                    "degraded": True,
                    "error": "本机未配对同步中枢（无云端注册表可透传）；在「接入设备」区输码配对",
                },
                200,
            )
        )
    try:
        view = await engine.pull_routing_view()
    except Exception as exc:  # noqa: BLE001 —— 网络/上游故障统一诚实降级
        message = f"{type(exc).__name__}: {exc}"[:300]
        replica = engine.registry_replica
        replica_entries = replica.devices() if replica is not None else []
        if replica_entries:
            assert replica is not None
            fetched = replica.fetched_at()
            logger.warning(
                "[sync_agent] 云端注册表不可达，呈现本机副本（截至 %s）: %s", fetched, message
            )
            return _ok(
                _json_response(
                    {
                        "devices": [
                            _relay_device_view(entry)
                            for entry in replica_entries
                        ],
                        "source": "replica",
                        "degraded": True,
                        "error": f"中枢不可达，呈现本机注册表副本（截至 {fetched or '未知'}）",
                        "fetched_at": fetched or None,
                    },
                    200,
                )
            )
        logger.warning("[sync_agent] 云端注册表透传失败（诚实降级）: %s", message)
        return _ok(
            _json_response(
                {
                    "devices": [],
                    "source": "cloud",
                    "degraded": True,
                    "error": f"云端注册表暂不可达: {message}",
                },
                200,
            )
        )
    entries = view.get("devices")
    body = {
        "devices": [
            _relay_device_view(entry) for entry in entries if isinstance(entry, dict)
        ]
        if isinstance(entries, list)
        else [],
        "source": "cloud",
        "degraded": False,
        "online_window_secs": view.get("online_window_secs"),
        "fetched_at": now_iso_utc(),
    }
    _registry_cache = body
    _registry_cached_at = now
    return _ok(_json_response(body, 200))


# ── 注册表地址实测探活（P1b，ADR 2026-10-21-device-card-direct-connect）────

#: 单目标探活超时与单请求目标数上限（防滥用；探活目标恒来自注册表）。
_PROBE_TIMEOUT_SECS = 3.0
_PROBE_MAX_TARGETS = 32


async def _probe_health(address: str) -> dict[str, Any]:
    """GET {address}/health 实测（内核直连地址该端点无鉴权，2xx 即通）。

    延迟导入 aiohttp 同 _aiohttp_* 出站原语先例（核心单测不触发网络栈）；
    网络/超时/非 2xx 统一翻译为值（ok=false + status/异常名），不抛。
    """
    started = time.monotonic()
    try:
        import aiohttp  # noqa: PLC0415

        timeout = aiohttp.ClientTimeout(total=_PROBE_TIMEOUT_SECS)
        async with aiohttp.ClientSession(timeout=timeout) as session, session.get(
            f"{address}/health"
        ) as resp:
            await resp.read()
            ok = resp.status < 300
            status = resp.status
    except Exception as exc:  # noqa: BLE001 —— 探活失败是值不是异常（翻译进结果）
        return {
            "ok": False,
            "status": 0,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": type(exc).__name__,
        }
    return {"ok": ok, "status": status, "latency_ms": int((time.monotonic() - started) * 1000)}


async def _registry_entries_for_probe() -> dict[str, dict[str, Any]]:
    """device_id → 含 address 的注册表条目（短 TTL 缓存优先，过期单拍重拉）。

    探活的地址唯一来源 = 云端权威注册表或本机副本（SSRF 硬界：不接受任意
    URL 入参）；未配对（引擎停转）原样上抛；云端不可达回落注册表副本（A 档
    复制面，中枢离线照样探活），副本亦空才上抛。
    回环地址只对自报者本人可达（他端拿到必假阳探到自己）——非本机设备的
    回环自报不进探活集（部署实证：云端自报 http://127.0.0.1:9100）。
    """
    now = _registry_mono()
    if _registry_cache is not None and now - _registry_cached_at < _REGISTRY_CACHE_TTL_SECS:
        raw = _registry_cache.get("devices")
        entries = raw if isinstance(raw, list) else []
    else:
        engine = _engine
        if engine is None:
            raise LookupError("本机未配对同步中枢（无注册表可探）")
        try:
            view = await engine.pull_routing_view()
            raw = view.get("devices")
            entries = raw if isinstance(raw, list) else []
        except Exception:
            replica = engine.registry_replica
            entries = replica.devices() if replica is not None else []
            if not entries:
                raise
    self_id = _assembly.device_id if _assembly is not None else ""
    result: dict[str, dict[str, Any]] = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        address = str(e.get("address") or "")
        device_id = str(e.get("device_id") or "")
        if not address:
            continue
        if _is_loopback_address(address) and device_id != self_id:
            continue
        result[device_id] = e
    return result


def _is_loopback_address(address: str) -> bool:
    """回环地址判定（http://127.*|localhost|::1 开头形态；宽松前缀够用）。"""
    from urllib.parse import urlparse

    try:
        host = (urlparse(address).hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "::1") or host.startswith("127.")


async def _handle_probe(raw_body: str) -> dict[str, Any]:
    """POST /probe：注册表设备实测探活（入参 device_ids，禁任意 URL）。

    返回逐设备 {device_id, address, ok, status, latency_ms}；注册表无地址的
    设备如实报 no_address（NAT 远端卡前端回落心跳语义，不假装可探）。
    """
    if _assembly is None:
        return _error("sync_agent not loaded (load pending)", 503)
    try:
        body = _decode_body(raw_body, strict_object=True)
    except ValueError:
        return _error("探活体必须是 JSON 对象", 400)
    raw_ids = body.get("device_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return _error("device_ids 必填且为非空字符串数组", 400)
    ids: list[str] = []
    for item in raw_ids:
        device_id = str(item or "").strip()
        if device_id and device_id not in ids:
            ids.append(device_id)
    if not ids:
        return _error("device_ids 无有效条目", 400)
    if len(ids) > _PROBE_MAX_TARGETS:
        return _error(f"device_ids 超上限 {_PROBE_MAX_TARGETS}", 400)
    try:
        targets = await _registry_entries_for_probe()
    except Exception as exc:  # noqa: BLE001 —— 未配对/云端不可达诚实降级
        message = f"{type(exc).__name__}: {exc}"[:300]
        return _error(f"云端注册表暂不可达: {message}", 503)
    entries = [targets.get(device_id) for device_id in ids]
    outcomes = await asyncio.gather(*(_probe_outcome(e) for e in entries))
    results = [
        {
            "device_id": device_id,
            "address": str(e.get("address") or "") if e is not None else None,
            **outcome,
        }
        for device_id, e, outcome in zip(ids, entries, outcomes, strict=True)
    ]
    return _ok(_json_response({"results": results}, 200))


async def _probe_outcome(entry: dict[str, Any] | None) -> dict[str, Any]:
    """单设备探活结果：注册表在案但无地址（NAT 远端/旧云端）如实报不可探。"""
    if entry is None:
        return {"ok": False, "status": 0, "latency_ms": 0, "error": "no_address"}
    return await _probe_health(str(entry.get("address") or ""))


# ── 手动备份（T6，拍板③a）────────────────────────────────────────


def _backup_of() -> BackupService | None:
    return _assembly.backup if _assembly is not None else None


_BACKUP_NOT_CONFIGURED = (
    "备份能力未配置（settings backup_repo_root 或 env AGENTOS_SYNC_BACKUP_ROOT）"
)


def _parse_workspace_specs(body: dict[str, Any]) -> list[tuple[str, str]] | None:
    """备份请求体 → 工作空间登记列表（畸形显式 400，禁静默丢弃）。"""
    raw_items = body.get("workspaces", [])
    if not isinstance(raw_items, list):
        return None
    specs: list[tuple[str, str]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            return None
        name = str(item.get("name", "") or "").strip()
        path = str(item.get("path", "") or "").strip()
        if not name or not path:
            return None
        specs.append((name, path))
    return specs


async def _handle_backup_run(raw_body: str, authorization: str) -> dict[str, Any]:
    """POST /backup/run：DB 逻辑记录 + 工作空间 → 备份仓库（两 commit）。"""
    service = _backup_of()
    if service is None:
        return _error(_BACKUP_NOT_CONFIGURED, 503)
    try:
        body = _decode_body(raw_body, strict_object=True)
    except ValueError:
        return _error("备份体必须是 JSON 对象", 400)
    specs = _parse_workspace_specs(body)
    if specs is None:
        return _error("workspaces 必须是 [{name, path}] 数组（name/path 均必填）", 400)
    try:
        result = await service.run_backup(specs, reads=_BackupKernelReads(authorization))
    except BackupError as exc:
        return _error(str(exc), exc.status)
    return _ok(_json_response(result, 200))


def _handle_backup_status() -> dict[str, Any]:
    """GET /backup/status：能力面（配置/git 可用/头/深度）。"""
    service = _backup_of()
    if service is None:
        return _ok(_json_response({"configured": False}, 200))
    return _ok(_json_response({"configured": True, **service.status()}, 200))


def _handle_backup_points() -> dict[str, Any]:
    """GET /backup/points：备份点清单（git log，新→旧）。"""
    service = _backup_of()
    if service is None:
        return _error(_BACKUP_NOT_CONFIGURED, 503)
    try:
        points = service.points()
    except BackupError as exc:
        return _error(str(exc), exc.status)
    return _ok(_json_response({"points": points}, 200))


async def _handle_backup_restore(raw_body: str) -> dict[str, Any]:
    """POST /backup/restore：从备份点检出（workspace=文件恢复；db=JSONL 物化）。"""
    service = _backup_of()
    if service is None:
        return _error(_BACKUP_NOT_CONFIGURED, 503)
    try:
        body = _decode_body(raw_body, strict_object=True)
    except ValueError:
        return _error("恢复体必须是 JSON 对象", 400)
    part = str(body.get("part", "") or "").strip()
    if part not in ("db", "workspace"):
        return _error("part 必填且为 db|workspace", 400)
    dest = str(body.get("dest", "") or "").strip()
    if not dest:
        return _error("dest 必填（检出目标绝对路径，须不存在或为空目录）", 400)
    commit = str(body.get("commit", "") or "").strip() or None
    name = str(body.get("name", "") or "").strip()
    try:
        result = await service.restore(commit=commit, part=part, dest=dest, name=name)
    except BackupError as exc:
        return _error(str(exc), exc.status)
    return _ok(_json_response(result, 200))


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
    description="HTTP endpoint handler for /ext/sync_agent/**（配对流与状态面）",
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
    """/ext/sync_agent/** 分发（内核 admin JWT 已验，端内不再自管鉴权）。"""
    del plugin_id, query_multi  # dispatcher 已路由到本插件
    parts = [unquote(p) for p in path.split("/") if p]
    rest = parts[2:]  # parts[0]="ext", parts[1]="sync_agent"
    if rest == ["enroll"] and method == "POST":
        return await _handle_enroll(raw_body)
    if rest == ["status"] and method == "GET":
        return _handle_status()
    if rest == ["relay", "devices"] and method == "GET":
        return await _handle_relay_devices()
    if rest == ["probe"] and method == "POST":
        return await _handle_probe(raw_body)
    if rest == ["borrow_auth"] and method == "POST":
        return await _handle_borrow_auth(headers or {})
    if rest == ["borrow"] and method == "POST":
        return await _handle_borrow(raw_body)
    if rest == ["mirror", "records"] and method == "GET":
        return _handle_mirror_records(query)
    if rest and rest[0] == "mirror":
        # 镜像面只读：/mirror/** 非本端点的任何方法一律 404（结构性不可篡改）。
        return _error(f"sync_agent: mirror is read-only (no route for {method} {path})", 404)
    if rest == ["backup", "run"] and method == "POST":
        return await _handle_backup_run(raw_body, _authorization(headers))
    if rest == ["backup", "status"] and method == "GET":
        return _handle_backup_status()
    if rest == ["backup", "points"] and method == "GET":
        return _handle_backup_points()
    if rest == ["backup", "restore"] and method == "POST":
        return await _handle_backup_restore(raw_body)
    return _error(f"sync_agent: no route for {method} {path}", 404)


@plugin.on_domain_event
async def _on_domain_event(params: dict[str, Any]) -> None:
    """域事件捕获（会话面）：session.created 登记 / session.deleted 墓碑。

    fire-and-forget：引擎未起（未配对）零动作；失败只告警不阻塞内核
    （trace_exporter 同律）。消息/任务面走管道步骤（capture_step），不在此。
    """
    if _engine is None:
        return
    event = str(params.get("event", "") or "")
    session_id = str(params.get("session_id", "") or "")
    if event == "session.created" and session_id:
        _engine.capture_session(session_id)
    elif event == "session.deleted" and session_id:
        _engine.capture_session(session_id, deleted=True)


@plugin.tool(
    name="sync_capture.execute",
    schema={
        "type": "object",
        "properties": {
            "state": {"type": "object", "description": "Pipeline working state（引擎投喂）"},
            "config": {"type": "object", "description": "步骤 config（未用，契约占位）"},
        },
    },
    description="DB 逻辑记录捕获步（autonomous.yaml post 组；消息 diff+任务漂移+会话骨架 → sync_event 上行）",
    output_schema={
        "type": "object",
        "properties": {},
    },
    render={"card": "generic"},
)
async def sync_capture_execute(
    state: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """管道步骤捕获（T1 DB 通道；异常零上抛，失败不阻塞管道）。"""
    del config
    return _handle_capture_step(state)


@plugin.tool(
    name="sync_agent.get_status",
    schema={"type": "object", "properties": {}},
    description="端侧同步引擎状态（配对/游标/待上行/计数面/末次错误）",
    output_schema={
        "type": "object",
        "properties": {
            "loaded": {"type": "boolean"},
            "paired": {"type": "boolean"},
            "device_id": {"type": "string"},
            "tier": {"type": "string"},
            "bootstrap_done": {"type": "boolean"},
            "pending": {"type": "integer"},
            "cursor": {"type": "integer"},
            "captured_total": {"type": "integer"},
            "applied_total": {"type": "integer"},
            "stale_total": {"type": "integer"},
            "flushed_total": {"type": "integer"},
            "failed_flushes": {"type": "integer"},
            "pulled_total": {"type": "integer"},
            "heartbeats_total": {"type": "integer"},
            "mirror_total": {"type": "integer"},
            "quarantined_total": {"type": "integer"},
            "last_error": {"type": "string"},
        },
    },
    render={"card": "generic"},
)
async def get_status() -> dict[str, Any]:
    """端侧同步运行状态（配对/断网续传/收敛进度的可观测面）。"""
    if _engine is None:
        if _assembly is None:
            return {"loaded": False}
        return {
            "loaded": True,
            "paired": False,
            "device_id": _assembly.device_id,
            "tier": _assembly.device_tier,
        }
    return {"loaded": True, "paired": True, **_engine.status_dict()}


if __name__ == "__main__":
    plugin.run()
