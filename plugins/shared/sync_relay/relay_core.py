"""sync_relay 云端中继核心 —— token 鉴权、批处理执法、入网基线选择、登录即入网。

裁决/执法语义（ADR 2026-10-15-multi-device-sync-hlc-lww）：
- **per-device token（P6 铁律）**：``device:token`` 绑定表（env 注入 + 登录
  直签发两张来源，不落配置明文）；事件 device_id 必须等于 token 绑定设备
  （防顶名，trace_ingestion origin_host+token 同律）。
- **属主面只追加（D2）**：属主 scoped 通道（db：会话/消息/任务，通道注册表
  判定，ADR 2026-10-17-sync-two-channel-routing）首个事件设备即属主，他端
  写拒绝入账（死信留痕可查）。
- **无 HLC 不入账**：HLC 是 LWW 裁决依据，缺 HLC 事件不可全序（裸墙钟 LWW
  已被否）——死信拒收，不放行到账本。
- **入网基线 = 层级权威①（D2）**：每资产在 云>桌面>手机 序上取最高位阶设备
  的最新事件（同位阶按 HLC 后者）；改动权威不参与基线（新设备无本地状态，
  无"后改"可保护）。
- **在链存在性（D1）**：设备在线 = 心跳窗口内有心跳。
- **登录即入网（ADR 2026-11-02-login-as-enrollment）**：/enroll 端点由内核
  admin JWT 执法（auth:"admin"），admin 登录态直签 per-device token——唯一
  认证动作 = 连接 IP + 账号密码登录；入网码/一次性码机制已退役。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from _root_api import SyncEvent, is_owner_scoped, validate_event  # noqa: E402

#: 设备位阶（数值小 = 权威高）：层级权威收窄三用之①入网基线、②系统级默认、
#: ③2c 路由（D2/D7）。数据面裁决（HLC-LWW）不读本表。
TIER_RANK: dict[str, int] = {"cloud": 0, "desktop": 1, "phone": 2}
UNKNOWN_TIER_RANK = 3


def parse_tokens_env(raw: str) -> dict[str, str]:
    """``device:token`` 逗号对 → 绑定表（宽容解析：空白段/缺冒号段丢弃）。"""
    tokens: dict[str, str] = {}
    for part in raw.split(","):
        stripped = part.strip()
        if not stripped or ":" not in stripped:
            continue
        device, _, token = stripped.partition(":")
        device = device.strip()
        token = token.strip()
        if device and token:
            tokens[device] = token
    return tokens


class TokenAuth:
    """device↔token 绑定校验（constant-time 比较）。

    两张绑定来源：构造期静态表（env 预置）+ 可选动态源（入网码兑换签发面，
    每次校验现查——签发后即时生效，无需重载）。未配置任何来源 = 一律拒绝
    （fail-closed，装载不受阻、端点在但全拒，trace_ingestion 先例）。
    """

    def __init__(
        self, tokens: dict[str, str], issued_source: Callable[[], dict[str, str]] | None = None
    ) -> None:
        self._tokens = dict(tokens)
        self._issued_source = issued_source

    @property
    def configured_devices(self) -> list[str]:
        devices = set(self._tokens)
        if self._issued_source is not None:
            devices.update(self._issued_source())
        return sorted(devices)

    def device_for(self, bearer: str) -> str | None:
        """Bearer token → 绑定设备；未命中返回 None。"""
        if not bearer:
            return None
        for device, token in self._tokens.items():
            if hmac.compare_digest(token, bearer):
                return device
        if self._issued_source is not None:
            for device, token in self._issued_source().items():
                if hmac.compare_digest(token, bearer):
                    return device
        return None


@dataclass
class EnvelopeError:
    """信封级失败（映射 4xx 协议错误响应）。"""

    message: str
    status: int = 400


@dataclass
class EnrollOutcome:
    """登录直签发结果（正式 per-device token + 租户上下文）。

    不含 relay_url：调用方寻址即事实地址（调用 URL 即 relay 地址），无回显必要。
    """

    device_id: str
    device_token: str
    tenant_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "device_token": self.device_token,
            "tenant_id": self.tenant_id,
        }


def token_sha256(raw: str) -> str:
    """per-device token → sha256 十六进制摘要（对等信任哈希，随注册表复制分发）。

    仅摘要出域：哈希原像 = 高熵随机 token（token_urlsafe），离线预放不可行；
    端侧副本据此本地比对核验（A 档，ADR
    2026-10-22-distributed-registry-peer-trust）。
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def enroll_device(
    store: Any,
    *,
    device_id: str,
    tier: str,
    app_version: str,
    capabilities: dict[str, Any],
    tenant_id: str,
    now: datetime,
    token_gen: Callable[[], str] | None = None,
) -> EnrollOutcome | EnvelopeError:
    """登录即入网（ADR 2026-11-02-login-as-enrollment）：admin 登录态直签注册。

    admin JWT 已由内核 enforce_ext_auth 执法（端点 auth:"admin"），本函数只落
    注册面：签发 token → 登记设备 → 落对等信任哈希。重复 enroll（重装/换绑）
    幂等换发：upsert 覆盖旧 token 绑定，旧 token 即时失效。
    """
    if not device_id or not isinstance(device_id, str):
        return EnvelopeError("device_id 必须是非空字符串", 400)
    device_token = (token_gen or secrets.token_urlsafe)()
    store.upsert_device_token(
        device_id=device_id, token=device_token, tenant_id=tenant_id, issued_at=now.isoformat()
    )
    store.upsert_device(
        device_id=device_id,
        tier=tier,
        app_version=app_version,
        capabilities=capabilities,
        now_iso=now.isoformat(),
    )
    # 对等信任哈希随签发即落（注册表复制的分发面；端侧借权核验本地比对的数据源）。
    store.set_device_token_hash(device_id=device_id, token_hash=token_sha256(device_token))
    return EnrollOutcome(
        device_id=device_id,
        device_token=device_token,
        tenant_id=tenant_id,
    )


@dataclass
class BatchOutcome:
    """一批事件的处理结果（接受/重放/死信明细）。"""

    accepted: int = 0
    duplicates: int = 0
    dead_lettered: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "dead_lettered": len(self.dead_lettered),
            "details": self.dead_lettered,
        }


def process_events(
    store: Any,
    events_raw: Any,
    *,
    auth: TokenAuth,
    bearer: str,
    received_at: str,
) -> BatchOutcome | EnvelopeError:
    """批处理：鉴权 → 逐事件校验/执法 → ULID 幂等入账。

    单事件违约不阻塞批次（死信留痕，trace_ingestion 先例）；信封形态违约
    （非列表/空批）映射信封级错误。
    """
    bound_device = auth.device_for(bearer)
    if bound_device is None:
        return EnvelopeError("unrecognized per-device token", 401)
    if not isinstance(events_raw, list) or not events_raw:
        return EnvelopeError("envelope.events 必须是非空数组", 400)

    outcome = BatchOutcome()
    for raw in events_raw:
        rejection = _evaluate(store, raw, bound_device=bound_device, received_at=received_at)
        if rejection is not None:
            outcome.dead_lettered.append(rejection)
            continue
        inserted = store.events.upsert(raw, received_at=received_at)
        if inserted:
            outcome.accepted += 1
        else:
            outcome.duplicates += 1
    return outcome


def _evaluate(
    store: Any, raw: Any, *, bound_device: str, received_at: str
) -> dict[str, str] | None:
    """单事件执法链：违约落死信并返回拒绝明细；合法返回 None（放行入账）。

    顺序即执法层级：形态/(schema) → 防顶名 → HLC 必备 → 属主面只追加。
    """
    ulid = str(raw.get("ulid", "")) if isinstance(raw, dict) else ""
    device = str(raw.get("device_id", "")) if isinstance(raw, dict) else ""
    event: SyncEvent | None = None
    reason = ""
    if not isinstance(raw, dict):
        reason = "not_an_object"
    else:
        try:
            event = validate_event(raw)
        except ValueError as exc:
            reason = f"schema: {exc}"
        else:
            if event.device_id != bound_device:
                reason = "device_mismatch（事件 device_id 与 token 绑定不符，防顶名）"
            elif event.hlc is None:
                reason = "missing_hlc（无 HLC 不可全序，裸墙钟 LWW 已被 ADR 否）"
            else:
                reason = enforce_owner(store, event, received_at=received_at) or ""
    if not reason:
        return None
    store.dead_letter(
        reason="rejected", event={"reason": reason, **_as_dict(raw)}, received_at=received_at
    )
    return {"ulid": ulid, "device_id": device, "reason": reason}


def _as_dict(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {"raw": repr(raw)}


def enforce_owner(
    store: Any, event: SyncEvent, *, received_at: str
) -> str | None:
    """属主面执法（属主 scoped 通道）：返回拒绝理由；共享面/属主本人返回 None。

    属主 scoped 判定 = 通道注册表（``is_owner_scoped``，共享协议单点——db
    通道属主面只追加：首个事件设备登记为属主，他端写拒绝；file 通道共享面
    不设属主）。通道语义挂通道定义，不随资产类型增殖（ADR 2026-10-17
    sync-two-channel-routing D2）。
    """
    if not is_owner_scoped(event.asset_type):
        return None
    owner = store.get_owner(event.asset_key)
    if owner is None:
        store.claim_owner(asset_key=event.asset_key, device_id=event.device_id, now_iso=received_at)
        return None
    if owner != event.device_id:
        return f"owner_violation（{event.asset_key} 属主 {owner}，{event.device_id} 无写权）"
    return None


def select_baseline(events: list[SyncEvent], device_tiers: dict[str, str]) -> list[SyncEvent]:
    """入网基线选择（层级权威①，D2）：每资产键取位阶最高设备的最新事件。

    先按 (资产, 设备) 取各设备自己的最新事件（HLC 最大），再跨设备按
    (tier_rank, hlc) 取最小 rank、同 rank 取 HLC 最大者。无 tier 记录的设备
    按最低位阶处理。无 HLC 事件不参与（不可全序）。
    """
    per_device_latest: dict[tuple[str, str, str], SyncEvent] = {}
    for event in events:
        if event.hlc is None:
            continue
        key = (event.asset_type, event.asset_id, event.device_id)
        current = per_device_latest.get(key)
        if current is None or _hlc_key(event) > _hlc_key(current):
            per_device_latest[key] = event

    winners: dict[tuple[str, str], SyncEvent] = {}
    for event in per_device_latest.values():
        asset = (event.asset_type, event.asset_id)
        current = winners.get(asset)
        if current is None:
            winners[asset] = event
            continue
        if _rank(event, device_tiers) != _rank(current, device_tiers):
            if _rank(event, device_tiers) < _rank(current, device_tiers):
                winners[asset] = event
        elif _hlc_key(event) > _hlc_key(current):
            winners[asset] = event
    return sorted(winners.values(), key=lambda e: (e.asset_type, e.asset_id, e.ulid))


def _rank(event: SyncEvent, device_tiers: dict[str, str]) -> int:
    return TIER_RANK.get(device_tiers.get(event.device_id, ""), UNKNOWN_TIER_RANK)


def _hlc_key(event: SyncEvent) -> tuple[int, int, str]:
    assert event.hlc is not None  # 入选 per_device_latest 前已过滤
    return event.hlc.sort_key


def is_online(last_heartbeat_at: str, now: datetime, window_secs: float) -> bool:
    """在链判定：心跳在窗口内即在线（D1）。心跳时刻解析失败 = 不在线。"""
    try:
        last = datetime.fromisoformat(last_heartbeat_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (now - last) <= timedelta(seconds=window_secs)
