"""执行通道内部函数 —— 非文件类能力工具的 relay 读面与通道签发链。

现状（ADR 2026-10-18-unified-namespace-fs-mount-and-mcp-transfer D19）：文件类
操作一律走 OS 文件系统挂载，不经执行通道转发；本模块保留供**非文件类能力
工具**（手机摄像头/Shell、桌面 GPU 等能力不对称场景）的通道挂载，签发为
显式配置/管理动作（部署面）。编址记法对照单源在 tree_core（D16 规范记法，
本模块不做第二实现）。契约单源 = ADR
2026-10-17-unified-path-tree-and-capability-plane（D8 路由表读面 / D10' 通道
挂载 / D13' 双钥与发起端签发），实现层语义见 ADR 2026-10-18 D14-D19。

配置源（口令恒现读，settings 不承载 token——sync_agent 同律 P6）：
- relay_url   env AGENTOS_SYNC_RELAY_URL > settings.router.relay_url
- token       env AGENTOS_SYNC_TOKEN（本端 per-device sync token）
- self_device env AGENTOS_DEVICE_ID > platform.node()
- 挂载落点    用户插件根（user_space.user_plugins_dir）
"""

from __future__ import annotations

import logging
import os
import platform
import secrets
from dataclasses import dataclass
from typing import Any

from _root_api import user_plugins_dir, user_root
from mount import (
    build_mount_manifest,
    exec_token_env,
    protocol_handshake_error,
    provision_exec_token_env,
    write_mount,
)

logger = logging.getLogger(__name__)

_RELAY_URL_ENV = "AGENTOS_SYNC_RELAY_URL"
_TOKEN_ENV = "AGENTOS_SYNC_TOKEN"
_DEVICE_ID_ENV = "AGENTOS_DEVICE_ID"
_REQUEST_TIMEOUT_SECS = 10.0


@dataclass
class RouterSettings:
    """装配参数（env 优先；settings.router 块作 relay_url 回退）。"""

    relay_url: str = ""
    token: str = ""
    self_device_id: str = ""


def load_settings(config_settings: dict[str, Any] | None = None) -> RouterSettings:
    """装配解析（env 优先，进程内消费方传 None 即纯 env 形态）。"""
    settings = config_settings or {}
    return RouterSettings(
        relay_url=str(os.environ.get(_RELAY_URL_ENV, "") or settings.get("relay_url", "") or "").rstrip("/"),
        token=str(os.environ.get(_TOKEN_ENV, "") or ""),
        self_device_id=str(os.environ.get(_DEVICE_ID_ENV, "") or platform.node()),
    )


async def fetch_routing_view(settings: RouterSettings) -> dict[str, Any]:
    """relay routing_view 拉取（aiohttp 延迟导入：核心单测不触发网络栈）。"""
    import aiohttp  # noqa: PLC0415

    timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECS)
    async with aiohttp.ClientSession(timeout=timeout) as session, session.get(
        f"{settings.relay_url}/routing_view",
        headers={"Authorization": f"Bearer {settings.token}"},
    ) as resp:
        body = await resp.json(content_type=None)
        if resp.status >= 300:
            raise RuntimeError(f"routing_view {resp.status}: {str(body)[:200]}")
        return body


async def post_exec_token(settings: RouterSettings, payload: dict[str, Any]) -> dict[str, Any]:
    """执行凭据签发登记（POST /ext/sync_relay/exec_tokens，发起端 sync token 鉴权）。"""
    import aiohttp  # noqa: PLC0415

    timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECS)
    async with aiohttp.ClientSession(timeout=timeout) as session, session.post(
        f"{settings.relay_url}/exec_tokens",
        json=payload,
        headers={"Authorization": f"Bearer {settings.token}"},
    ) as resp:
        body = await resp.json(content_type=None)
        if resp.status >= 300:
            raise RuntimeError(f"exec_tokens {resp.status}: {str(body)[:200]}")
        return body if isinstance(body, dict) else {}


async def ensure_channel_mounted(
    device_id: str, settings: RouterSettings, view: dict[str, Any]
) -> dict[str, Any]:
    """首访通道建立链（幂等可重入）：握手 → 双钥 → manifest 落盘。

    凭据两源（D13'）：env 已预设沿用外部凭据（零网络面）；缺失自动签发——
    凭据体发起端自带 → relay 登记（属主端经验证集拉取）→ 用户空间 .env 供给
    （内核 ${ENV} overlay 解析，免重启生效）。任一环失败诚实报错，无半挂载态
    （manifest/.env 均不落）。回执带 mcp_url/exec_token_env（能力工具挂载出站
    消费面，D19；凭据体不回显，P6）。
    """
    if not device_id:
        return {"success": False, "error": "device_id 必填"}
    record = next((d for d in view.get("devices", []) if d.get("device_id") == device_id), None)
    if record is None:
        return {"success": False, "error": f"设备 {device_id} 不在路由表（未入网或已移除）"}
    channel = (record.get("capabilities") or {}).get("exec_channel") or {}
    handshake_error = protocol_handshake_error(channel.get("protocol_version"))
    if handshake_error is not None:
        return {"success": False, "error": handshake_error}
    env_name = exec_token_env(device_id)
    token_value: str | None = None
    issued: dict[str, Any] = {}
    if env_name not in os.environ:
        token_value = secrets.token_urlsafe(32)
        try:
            issued = await post_exec_token(
                settings,
                {"target_device": device_id, "exec_token": token_value},
            )
        except Exception as exc:  # noqa: BLE001 —— 签发失败诚实报错，无半挂载态
            return {"success": False, "error": f"执行凭据签发失败：{exc}"}
        env_path = user_root() / ".env" if user_root() is not None else None
        if env_path is None:
            return {"success": False, "error": "无法定位用户空间根（USER_ROOT 未配置），执行凭据无处供给"}
        provision_exec_token_env(env_path, env_name, token_value)
    try:
        manifest = build_mount_manifest(record, token_env=env_name, token_value=token_value)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    root = user_plugins_dir()
    if root is None:
        return {"success": False, "error": "无法定位用户插件根（USER_ROOT 未配置），挂载目录无处落盘"}
    target = write_mount(root, manifest)
    online = bool(record.get("online", False))
    logger.info("[distributed_router] 执行通道已挂载: %s -> %s (online=%s)", device_id, target, online)
    result: dict[str, Any] = {
        "mounted": True,
        "plugin_dir": str(target),
        "mcp_url": str(channel.get("mcp_url", "") or ""),
        "exec_token_env": env_name,
        "online": online,
    }
    if issued:
        result["issued"] = {
            "target_device": issued.get("target_device", device_id),
            "expires_at": issued.get("expires_at", ""),
        }
    if token_value is not None:
        result["note"] = (
            "执行凭据已自动签发并供给（短时效，续用重挂即重签）；子树可达"
            if online
            else "执行凭据已自动签发并供给；属主当前离线（掉盘）：通道已备妥，子树访问将诚实不可用，恢复后自动可用"
        )
    else:
        result["note"] = (
            "子树可达（凭据经外部 env 供给）"
            if online
            else "属主当前离线（掉盘）：通道已备妥，子树访问将诚实不可用，恢复后自动可用"
        )
    return result
