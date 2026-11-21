"""acp_quota_pool CPA 网关探活（独名模块，测试不 import server）。

职责（2026-11-21 拉起面单点化收口，用户裁定）：网关健康探活 + 状态报告——
拉起职责已移交 llm_provider_gateway provider 插件（捆绑 bin/cli-proxy-api
自包含拉起，端口三态见该插件 gateway_manager）。本插件只读状态：探活命中
→ running（复用面不变）；未命中 → handoff（fail-visible 指向新拉起面），
不再本机拉起（``start_command_<os>`` 拉起面与个人路径硬编码退役）。
on_load 与控制台页消费同一入口 ensure_gateway_running。

网关参数单源 = manifest config_files[id=gateway] 注入
（config/plugins/acp_quota_pool/gateway.yaml），环境变量可覆盖。
密钥纪律：管理密钥真值禁入仓（用户空间/环境变量承载）；本模块不读管理
密钥——数据面已裁撤，管理页登录由用户在嵌入页内自行完成。
"""

from __future__ import annotations

import asyncio
import os
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

CONFIG_NAMESPACE = "gateway"
DEFAULT_BASE_URL = "http://127.0.0.1:8317"
MANAGEMENT_PAGE = "/management.html"
ENV_BASE_URL = "ACP_GATEWAY_BASE_URL"

Probe = Callable[[str, float], Awaitable[tuple[bool, str]]]


@dataclass(frozen=True)
class GatewayConfig:
    """网关探活参数（gateway 命名空间归一化结果）。"""

    base_url: str
    health_timeout_secs: float


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def read_gateway_config(injected: dict[str, Any] | None) -> GatewayConfig:
    """归一化 gateway 命名空间配置（env 覆盖 > 命名空间 > 默认）。

    Args:
        injected: 注入的配置视图（含 gateway 命名空间）。

    Raises:
        ValueError: base_url 非 http(s) 形态（调用方翻译为失败详情，不静默回退）。
    """
    namespace = injected.get(CONFIG_NAMESPACE) if isinstance(injected, dict) else None
    namespace = namespace if isinstance(namespace, dict) else {}
    base_url = str(os.environ.get(ENV_BASE_URL) or namespace.get("base_url") or DEFAULT_BASE_URL)
    base_url = base_url.rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError(f"gateway.base_url 须为 http(s) URL，实得 {base_url!r}")
    return GatewayConfig(
        base_url=base_url,
        health_timeout_secs=_positive_float(namespace.get("health_timeout_secs"), 2.0),
    )


def management_url(base_url: str) -> str:
    """网关管理页 URL（base_url 尾斜杠归一，拼接结果与入参形态无关）。"""
    return base_url.rstrip("/") + MANAGEMENT_PAGE


def _probe_sync(url: str, timeout_secs: float) -> tuple[bool, str]:
    """同步探活一次：任何 HTTP 响应（含 401/404）= 网关进程在；连接层失败 = 不在。"""
    request = urllib.request.Request(url, headers={"User-Agent": "agentos-acp-quota-pool"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_secs) as response:
            return True, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        # 管理页可能要求凭据（4xx/重定向）：有响应即存活，判活只看连接层
        return True, f"HTTP {exc.code}"
    except (OSError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def probe_gateway(base_url: str, timeout_secs: float = 2.0) -> tuple[bool, str]:
    """异步探活管理页（同步 socket 阻塞挪线程池，不堵事件循环）。"""
    return await asyncio.to_thread(_probe_sync, management_url(base_url), timeout_secs)


async def ensure_gateway_running(
    config: GatewayConfig,
    *,
    probe: Probe = probe_gateway,
) -> dict[str, Any]:
    """探活一次并报告（拉起语义移交后的幂等入口）。不抛；结果面显式降级。

    result 形态：``{action, running, detail}``，action ∈
    none（网关在，复用面不变）/ handoff（网关不在——拉起职责在
    llm_provider_gateway 插件，本插件不拉起，如实指向新拉起面）。
    """
    running, detail = await probe(config.base_url, config.health_timeout_secs)
    if running:
        return {"action": "none", "running": True, "detail": detail}
    return {
        "action": "handoff",
        "running": False,
        "detail": (
            f"网关不在（{detail}）——拉起职责已移交 llm_provider_gateway 插件"
            "（捆绑网关自持拉起，端口三态）；本插件仅探活与控制台页"
        ),
    }


__all__ = [
    "CONFIG_NAMESPACE",
    "DEFAULT_BASE_URL",
    "ENV_BASE_URL",
    "MANAGEMENT_PAGE",
    "GatewayConfig",
    "ensure_gateway_running",
    "management_url",
    "probe_gateway",
    "read_gateway_config",
]
