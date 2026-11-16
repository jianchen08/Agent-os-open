"""执行凭据签发与验证集纯逻辑 —— 发起端签发 → 属主端验证（ADR D13'）。

双钥配对流语义：执行面凭据 ≠ 数据面凭据（sync token），per-device 绑定 +
短时效（缺省 1h、上限 24h，续用重签）；**发起端必须自带凭据体**（token 由
发起端生成——签发权在发起端，relay 只做登记与属主端分发，不代生成）。

边界：本模块不做网络、不持有注册表——目标设备存在性由调用方注入谓词
（device_exists），时效裁决恒按注入时钟（now），storage 面见 exec_token_store。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from exec_token_store import ExecTokenStore

#: 短时效三档：缺省 1h；下限 1min（防误配零时效）；上限 24h（短时效执法线）。
DEFAULT_TTL_SECS = 3600.0
MIN_TTL_SECS = 1.0
MAX_TTL_SECS = 86400.0


@dataclass
class ExecTokenError:
    """签发失败（映射 4xx 协议错误响应）。"""

    message: str
    status: int = 400


@dataclass
class IssueOutcome:
    """签发成功结果（不回显 token 本体——发起端自有凭据，属主端经验证集获得）。"""

    target_device: str
    issued_by: str
    expires_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "issued": True,
            "target_device": self.target_device,
            "issued_by": self.issued_by,
            "expires_at": self.expires_at,
        }


def issue_exec_token(
    store: ExecTokenStore,
    *,
    issued_by: str,
    target_device: str,
    token: str,
    ttl_secs: Any,
    now: datetime,
    device_exists: Any,
) -> IssueOutcome | ExecTokenError:
    """签发执法链：形态 → 目标存在性 → 时效夹取 → 登记；任何失败零登记。"""
    if not target_device:
        return ExecTokenError("target_device 必填")
    if device_exists is not None and not device_exists(target_device):
        return ExecTokenError(f"目标设备 {target_device} 不在注册表（未入网或已移除）", 404)
    if not token:
        return ExecTokenError("exec_token 必填（发起端自带凭据体，relay 不代生成）")
    ttl = _resolve_ttl(ttl_secs)
    if isinstance(ttl, ExecTokenError):
        return ttl
    expires_at = (now + timedelta(seconds=ttl)).astimezone(UTC).isoformat()
    store.insert(
        token=token,
        device_id=target_device,
        issued_by=issued_by,
        created_at=now.astimezone(UTC).isoformat(),
        expires_at=expires_at,
    )
    return IssueOutcome(
        target_device=target_device, issued_by=issued_by, expires_at=expires_at
    )


def verification_set(
    store: ExecTokenStore, *, device_id: str, now: datetime
) -> list[dict[str, str]]:
    """属主端当前有效凭据集（时效闭区间 now <= expires_at；过期行顺带物理清理）。"""
    store.prune(now)
    active: list[dict[str, str]] = []
    for binding in store.list_for(device_id):
        try:
            expires_at = datetime.fromisoformat(
                binding["expires_at"].replace("Z", "+00:00")
            )
        except ValueError:
            continue  # 畸形时效 fail-closed（不静默修复）
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if now <= expires_at:
            active.append(binding)
    return active


def _resolve_ttl(ttl_secs: Any) -> float | ExecTokenError:
    """时效解析：缺省 DEFAULT；非正数/非数字 = 明确报错；超上限夹取（短时效执法）。"""
    if ttl_secs is None:
        return DEFAULT_TTL_SECS
    try:
        value = float(ttl_secs)
    except (TypeError, ValueError):
        return ExecTokenError(f"ttl_secs 必须是数字（秒），得到 {ttl_secs!r}")
    if value < MIN_TTL_SECS:
        return ExecTokenError(f"ttl_secs 必须为正数（秒，下限 {MIN_TTL_SECS:g}），得到 {value!r}")
    return min(value, MAX_TTL_SECS)
