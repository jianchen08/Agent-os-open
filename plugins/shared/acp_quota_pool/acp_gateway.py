"""acp_quota_pool CPA 网关生命周期托管（独名模块，测试不 import server）。

职责（2026-10-16 R6）：网关健康检查 → 不在则静默拉起 start-gateway.bat
（bat 自带端口守卫，重复调用幂等）→ 拉起后复验一次，全程返回可观察结果。
on_load 与控制台页「拉起网关」按钮消费同一入口 ensure_gateway_running。

网关参数单源 = manifest config_files[id=gateway] 注入
（config/plugins/acp_quota_pool/gateway.yaml），环境变量可覆盖。
密钥纪律：管理密钥真值禁入仓（用户空间/环境变量承载）；本模块不读管理
密钥——数据面已裁撤，管理页登录由用户在嵌入页内自行完成。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

CONFIG_NAMESPACE = "gateway"
DEFAULT_BASE_URL = "http://127.0.0.1:8317"
MANAGEMENT_PAGE = "/management.html"
ENV_BASE_URL = "ACP_GATEWAY_BASE_URL"
ENV_START_COMMAND = "ACP_GATEWAY_START_COMMAND"

# 拉起为 fire-and-forget：bat 经 start /b 立即返回，Popen 句柄留模块引用防
# ResourceWarning，不 wait（等待语义由「拉起后复验」承担）。
_LAUNCHED: list[subprocess.Popen[bytes]] = []

Probe = Callable[[str, float], Awaitable[tuple[bool, str]]]
Launcher = Callable[[str], None]
Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class GatewayConfig:
    """网关托管参数（gateway 命名空间归一化结果）。"""

    base_url: str
    start_command: str
    health_timeout_secs: float
    startup_grace_secs: float


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def read_gateway_config(injected: dict[str, Any] | None) -> GatewayConfig:
    """归一化 gateway 命名空间配置（env 覆盖 > yaml > 默认）。

    Raises:
        ValueError: base_url 非 http(s) 形态（调用方翻译为失败详情，不静默回退）。
    """
    namespace = injected.get(CONFIG_NAMESPACE) if isinstance(injected, dict) else None
    namespace = namespace if isinstance(namespace, dict) else {}
    base_url = str(os.environ.get(ENV_BASE_URL) or namespace.get("base_url") or DEFAULT_BASE_URL)
    base_url = base_url.rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError(f"gateway.base_url 须为 http(s) URL，实得 {base_url!r}")
    start_command = str(
        os.environ.get(ENV_START_COMMAND) or namespace.get("start_command") or ""
    )
    return GatewayConfig(
        base_url=base_url,
        start_command=start_command,
        health_timeout_secs=_positive_float(namespace.get("health_timeout_secs"), 2.0),
        startup_grace_secs=_positive_float(namespace.get("startup_grace_secs"), 3.0),
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


def launch_gateway_process(command: str) -> None:
    """拉起网关命令（spawn 不等待；OSError 由调用方翻译为失败结果）。

    Windows 用 CREATE_NO_WINDOW：后台隐藏控制台承载 bat 与网关进程，cmd 退出
    后网关随控制台存活（不随插件 sidecar 重启而亡，端口守卫兜幂等）。
    """
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    _LAUNCHED.append(
        subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    )


async def ensure_gateway_running(
    config: GatewayConfig,
    *,
    probe: Probe = probe_gateway,
    launch: Launcher = launch_gateway_process,
    sleep: Sleeper = asyncio.sleep,
) -> dict[str, Any]:
    """健康检查 → 不在则拉起 → 复验一次。不抛；失败面显式降级为结果字段。

    result 形态：``{action, running, detail}``，action ∈
    none（本就运行）/ launched（已拉起，running=复验结论）/
    launch_failed（spawn 失败）/ launch_disabled（未配置拉起命令）。
    """
    running, detail = await probe(config.base_url, config.health_timeout_secs)
    if running:
        return {"action": "none", "running": True, "detail": detail}
    if not config.start_command:
        return {
            "action": "launch_disabled",
            "running": False,
            "detail": "未配置拉起命令（gateway.start_command / ACP_GATEWAY_START_COMMAND）",
        }
    try:
        launch(config.start_command)
    except OSError as exc:
        return {"action": "launch_failed", "running": False, "detail": f"拉起失败: {exc}"}
    await sleep(config.startup_grace_secs)
    running, detail = await probe(config.base_url, config.health_timeout_secs)
    return {"action": "launched", "running": running, "detail": detail}


__all__ = [
    "CONFIG_NAMESPACE",
    "DEFAULT_BASE_URL",
    "ENV_BASE_URL",
    "ENV_START_COMMAND",
    "GatewayConfig",
    "MANAGEMENT_PAGE",
    "ensure_gateway_running",
    "launch_gateway_process",
    "management_url",
    "probe_gateway",
    "read_gateway_config",
]
