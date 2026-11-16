"""exec 通道端侧面 —— exec_channel 能力上报装配 + 属主端执行凭据验证（2c 续刀）。

契约（ADR 2026-10-17-unified-path-tree-and-capability-plane D8/D10a'/D13'）：
- **能力上报（D8 端侧采集面）**：本端经既有注册/心跳 capabilities 自由 JSON 上报
  ``exec_channel`` 子键 ``{mcp_url, allowed_roots, protocol_version}``——
  allowed_roots = 本端在统一路径树上的子树领地声明（前缀路由的路由表主体）；
  未配置 mcp_url = 不上报（空 dict，注册表零噪音）。
- **双钥执行凭据（D13'）**：发起端签发 → 属主端验证（per-device，短时效）。
  本模块承载**属主端验证面**：验证集拉取（传输口注入）/本地持久化（data/ 永
  不同步层）/constant-time 匹配 + 时效校验；verify 命中返回凭据元数据（授权
  回执面：谁签发、何时到期）。
- **协议握手（D10a'）**：``protocol_version`` 最低版本执法判定——声明低于最低
  版或畸形 = 诚实报错（禁静默降级）；未声明 = 不假拒（未知 ≠ 不兼容）。

接线注记：引擎注册/心跳携带 exec_channel 子键、验证集周期拉取 = 一行级钩子，
让位 T2 在飞面（agent_core.py 为其修改面），T2 落地后续刀接入（台账 R18）。
本模块零依赖插件内其他模块（standalone，消费方最小接线面）；时效判定恒按
注入时钟（测试零真实等待）。
"""

from __future__ import annotations

import hmac
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: 本端 exec 通道协议版本（最低执法线 = 同值；D10a' 预留子键）。
EXEC_CHANNEL_PROTOCOL_VERSION = "1"

#: 能力面 env 覆盖（settings.exec_channel 块之上的装配优先源）。
EXEC_MCP_URL_ENV = "AGENTOS_EXEC_MCP_URL"
EXEC_ALLOWED_ROOTS_ENV = "AGENTOS_EXEC_ALLOWED_ROOTS"


@dataclass(frozen=True)
class ExecChannelConfig:
    """本端执行通道装配参数（mcp_url 空 = 通道未启用）。"""

    mcp_url: str = ""
    allowed_roots: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.mcp_url)


def parse_roots(raw: Any) -> tuple[str, ...]:
    """allowed_roots 解析：list/tuple 或逗号/空白分隔串 → 去空去重保序元组。"""
    if isinstance(raw, str):
        items = raw.replace(",", " ").split()
    elif isinstance(raw, (list, tuple)):
        items = [str(item).strip() for item in raw]
    else:
        return ()
    seen: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return tuple(seen)


def exec_channel_from_settings(
    settings: dict[str, Any], env: dict[str, str] | None = None
) -> ExecChannelConfig:
    """装配 exec_channel 配置（env > settings.exec_channel 块；缺 mcp_url = 停用）。"""
    env_source = os.environ if env is None else env
    block_raw = settings.get("exec_channel")
    block = block_raw if isinstance(block_raw, dict) else {}
    mcp_url = (
        str(env_source.get(EXEC_MCP_URL_ENV, "") or "").strip()
        or str(block.get("mcp_url", "") or "").strip()
    )
    roots_raw = env_source.get(EXEC_ALLOWED_ROOTS_ENV) or block.get("allowed_roots") or ""
    return ExecChannelConfig(mcp_url=mcp_url, allowed_roots=parse_roots(roots_raw))


def build_capability(config: ExecChannelConfig) -> dict[str, Any]:
    """exec_channel 能力子键（注册/心跳 capabilities 携带；未启用 = 空 dict）。"""
    if not config.enabled:
        return {}
    return {
        "mcp_url": config.mcp_url,
        "allowed_roots": list(config.allowed_roots),
        "protocol_version": EXEC_CHANNEL_PROTOCOL_VERSION,
    }


def check_protocol_version(
    declared: Any, *, minimum: str = EXEC_CHANNEL_PROTOCOL_VERSION
) -> str | None:
    """协议版本握手判定：兼容返回 None；不兼容/畸形返回诚实错误文案。

    未声明（None/空白）= 不假拒（未知 ≠ 不兼容，旧版设备由调用面诚实提示）；
    主版本低于最低版 = 明确报错；声明但畸形 = 明确报错（禁静默降级）。
    """
    if declared is None or str(declared).strip() == "":
        return None
    declared_text = str(declared).strip()
    try:
        declared_major = int(declared_text.split(".")[0])
        minimum_major = int(str(minimum).split(".")[0])
    except (ValueError, AttributeError):
        return (
            f"exec_channel.protocol_version 畸形（declared={declared_text!r}，"
            f"minimum={minimum}），拒绝挂载"
        )
    if declared_major < minimum_major:
        return (
            f"exec_channel.protocol_version 不兼容（declared={declared_text} < 最低 {minimum}），"
            "明确报错不静默降级"
        )
    return None


class ExecTokenChannel:
    """属主端执行凭据验证集（拉取→替换→持久化；verify 恒按时效与恒时比较）。

    持久化落 data/（永不同步层，P6：token 明文不入同步面）；拉取失败向上传播
    且旧集保留——验证面不因单次拉取失败失明（错误要么处理要么传播）。
    """

    def __init__(self, persist_path: Path) -> None:
        self._path = Path(persist_path)
        self._tokens: dict[str, dict[str, str]] = {}
        self.synced_at = ""
        self._load()

    def refresh(self, fetch: Callable[[], list[dict[str, Any]]], *, now_iso: str) -> int:
        """拉取并整体替换验证集（换集即轮换）；返回有效凭据条数。"""
        fetched = fetch()
        tokens: dict[str, dict[str, str]] = {}
        for item in fetched:
            if not isinstance(item, dict):
                continue
            token = str(item.get("token", "") or "")
            if not token:
                continue
            tokens[token] = {
                "issued_by": str(item.get("issued_by", "") or ""),
                "expires_at": str(item.get("expires_at", "") or ""),
            }
        self._tokens = tokens
        self.synced_at = now_iso
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"tokens": self._tokens, "synced_at": now_iso}, ensure_ascii=False),
            encoding="utf-8",
        )
        return len(self._tokens)

    def verify(self, bearer: str, *, now: datetime) -> dict[str, str] | None:
        """Bearer → 授权回执 ``{issued_by, expires_at}``；未命中/过期/畸形时效 = None。

        恒时比较全表扫描（验证面表达一致）；时效闭区间（now <= expires_at 有效，
        与 relay 在线判定同律）；畸形时效 fail-closed。
        """
        if not bearer:
            return None
        for token, meta in self._tokens.items():
            if hmac.compare_digest(token, bearer):
                return self._active_receipt(meta, now)
        return None

    def status(self) -> dict[str, Any]:
        """验证面观测（持久化条数 + 最近拉取时刻；拉取成功才前移）。"""
        return {"tokens_persisted": len(self._tokens), "synced_at": self.synced_at}

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        tokens = raw.get("tokens")
        if isinstance(tokens, dict):
            self._tokens = {
                str(token): meta
                for token, meta in tokens.items()
                if isinstance(meta, dict)
            }
        self.synced_at = str(raw.get("synced_at", "") or "")

    @staticmethod
    def _active_receipt(meta: dict[str, str], now: datetime) -> dict[str, str] | None:
        try:
            expires_at = datetime.fromisoformat(
                str(meta.get("expires_at", "")).replace("Z", "+00:00")
            )
        except ValueError:
            return None
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if now > expires_at:
            return None
        return {"issued_by": meta.get("issued_by", ""), "expires_at": meta.get("expires_at", "")}
