#!/usr/bin/env python3
"""acp_quota_pool —— ACP 额度池插件壳（sidecar 入口，薄）。

载荷 = 技能包本体（SKILL.md + scripts/，路由与派发口径）+ CPA 网关生命周期
托管（on_load 健康检查→不在则静默拉起→复验，见 ``acp_gateway``）。播种与
网关逻辑单源在独名模块（``acp_seed`` / ``acp_gateway`` / ``acp_console_page``，
防 ``import server`` 跨插件碰撞——实证记录在案）；本入口只做接线：on_load
与 MCP 服务面（http.handle 承载 /ext/acp_quota_pool/** 三路由）。
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin

plugin = AgentOSPlugin("acp_quota_pool")

_paths = bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

from _root_api import (  # noqa: E402 — 必须在 bootstrap_plugin 之后（sys.path 注入）
    error as _error,
    json_response as _json_response,
    ok as _ok,
    user_root,
)
from acp_console_page import CONSOLE_HTML  # noqa: E402
from acp_gateway import (  # noqa: E402
    ensure_gateway_running,
    management_url,
    probe_gateway,
    read_gateway_config,
)
from acp_seed import seed_with_degradation  # noqa: E402

logger = logging.getLogger("acp_quota_pool")


def _html_response(html: str) -> dict[str, Any]:
    """包 HTML 成 HttpHandleResponse（body base64，Content-Type text/html）。"""
    return {
        "status": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": base64.b64encode(html.encode("utf-8")).decode("ascii"),
        "body_encoding": "base64",
    }


async def _gateway_ensure() -> dict[str, Any]:
    """网关生命周期入口（on_load 与页面「拉起网关」按钮共用一段逻辑）。

    配置非法/探活失败均显式降级为结果字段（加载与端点不因网关缺失而 5xx）。
    """
    try:
        config = read_gateway_config(plugin.get_config())
    except ValueError as exc:
        return {
            "action": "config_invalid",
            "running": False,
            "detail": str(exc),
            "base_url": None,
            "management_url": None,
        }
    result = await ensure_gateway_running(config)
    result["base_url"] = config.base_url
    result["management_url"] = management_url(config.base_url)
    return result


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """播种技能载荷（降级策略见 acp_seed）+ 网关生命周期托管（不在则拉起）。"""
    result = seed_with_degradation(user_root(), Path(__file__).resolve().parent)
    logger.info("[acp_quota_pool] 技能播种结果: %s", result)
    gateway = await _gateway_ensure()
    logger.info("[acp_quota_pool] 网关托管结果: %s", gateway)


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object"},
            "query": {"type": "object"},
        },
    },
    description="HTTP endpoint handler for /ext/acp_quota_pool/** (gateway lifecycle + console page)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发到网关生命周期端点与控制台页。"""
    del plugin_id, raw_body, headers, query  # 只读路由，无入参消费
    try:
        if path == "/ext/acp_quota_pool/gateway/status" and method == "GET":
            config = read_gateway_config(plugin.get_config())
            running, detail = await probe_gateway(config.base_url, config.health_timeout_secs)
            return _ok(
                _json_response(
                    {
                        "running": running,
                        "base_url": config.base_url,
                        "management_url": management_url(config.base_url),
                        "detail": detail,
                    }
                )
            )

        if path == "/ext/acp_quota_pool/gateway/start" and method == "POST":
            return _ok(_json_response(await _gateway_ensure()))

        if path == "/ext/acp_quota_pool/page/console" and method == "GET":
            return _ok(_html_response(CONSOLE_HTML))

        logger.warning("http.handle: no route for path=%s method=%s", path, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except ValueError as exc:
        # 配置校验失败（base_url 形态非法等）：422 语义，不 500
        return _ok(_json_response({"error": str(exc)}, 422))
    except Exception as exc:  # noqa: BLE001 — 端点边界翻译，响应体携带原因
        logger.exception("acp_quota_pool http.handle failed: %s", exc)
        return _error(f"acp_quota_pool service error: {exc}", 500)


if __name__ == "__main__":
    plugin.run()
