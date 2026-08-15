#!/usr/bin/env python3
"""Metrics Admin 插件——metrics-admin capability 的 HTTP 面层（boot-plugin 第三刀）。

架构分工（对齐 db_admin 第一刀模式）：
- 写面留内核：metrics.record（插件上报指标的热路径反向调用）是
  KernelCapabilityRouter 内置 match，不经本插件。
- 读面在内核 handler：agentos-api metrics/capability.rs 的
  MetricsAdminCapabilityHandler（namespace=metrics-admin，3 method：
  query/list/prometheus），查询/枚举/导出逻辑与鉴权全部在内核。
- 本插件只做 HTTP 面：内核 /ext/{*rest} 通配分发把 HttpHandleRequest 透传给
  本 http.handle 工具，按 path 组 metrics-admin capability 调用参数（query 的
  过滤参数来自 query string），经 SDK 反向调用通道（plugin.get_capability
  ("metrics-admin")）调内核 handler，把返回的信封组回 HTTP 响应。

鉴权落点（重要）：内核 http_dispatcher 不执行 http_endpoints[].auth 字段。
本插件不做鉴权决策——把入站请求的 Authorization 头原样放进
params["_authorization"]，角色校验（admin/viewer）由内核 handler 侧
resolve_request_user 执行（插件无法伪造角色，信任锚点在内核）。

[来源: docs/working/重要设计/boot-plugin内核能力插件化立项.md §五]
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any
from urllib.parse import unquote

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("metrics_admin")
logger = logging.getLogger(__name__)

_PREFIX = "/ext/metrics_admin"
_CAPABILITY = "metrics-admin"

# query 支持的过滤参数（原 /api/v1/metrics 查询串，单值透传）。
_QUERY_KEYS = ("plugin", "metric", "window", "labels")


def _response(payload_b64: str, status: int, content_type: str) -> dict[str, Any]:
    """把已编码 body 包成内核期望的 HttpHandleResponse。"""
    return {
        "status": status,
        "headers": {"Content-Type": content_type},
        "body": payload_b64,
        "body_encoding": "base64",
    }


def _json_response(payload: Any, status: int = 200) -> dict[str, Any]:
    body = json.dumps(payload, default=str, ensure_ascii=False)
    return _response(
        base64.b64encode(body.encode("utf-8")).decode("ascii"),
        status,
        "application/json; charset=utf-8",
    )


def _text_response(text: str, status: int = 200) -> dict[str, Any]:
    return _response(
        base64.b64encode(text.encode("utf-8")).decode("ascii"),
        status,
        "text/plain; charset=utf-8; version=0.0.4",
    )


def _ok(data: Any) -> dict[str, Any]:
    """成功响应：{success, data}（ToolExecutionResult 契约）。"""
    return {"success": True, "data": data}


def _error(status: int, message: str) -> dict[str, Any]:
    """错误响应（对齐 ApiError 的 {"error": {code, message}} 形状）。"""
    return _ok(_json_response({"error": {"code": str(status), "message": message}}, status))


def _authorization(headers: dict[str, str] | None) -> str:
    """取入站请求的原始 Authorization 头值（内核 header key 已小写）。"""
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == "authorization" and v:
            return str(v)
    return ""


def _route(path: str) -> str | None:
    """按 path 决定 capability method（无匹配返回 None → 404）。"""
    parts = [unquote(p) for p in path.split("/") if p]
    # parts[0]="ext", parts[1]="metrics_admin"
    if len(parts) < 2 or parts[0] != "ext" or parts[1] != "metrics_admin":
        return None
    rest = parts[2:]
    if rest == ["query"]:
        return "query"
    if rest == ["series"]:
        return "list"
    if rest == ["prometheus"]:
        return "prometheus"
    return None


def _query_params(query: dict[str, str] | None) -> dict[str, Any]:
    """把内核透传的 query 单值 map 组为 query 过滤参数。"""
    q = query or {}
    return {k: q[k] for k in _QUERY_KEYS if q.get(k)}


@plugin.tool(
    name="metrics_admin.status",
    schema={
        "type": "object",
        "properties": {},
    },
)
async def metrics_admin_status() -> dict[str, Any]:
    """插件状态（metrics-admin capability 句柄是否已注入）。"""
    try:
        plugin.get_capability(_CAPABILITY)
        injected = True
    except KeyError:
        injected = False
    return {"plugin": "metrics_admin", "capability": _CAPABILITY, "capability_injected": injected}


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
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """metrics-admin capability 的 HTTP 面转发层（全 GET 只读面）。

    签名覆盖 HttpHandleRequest 全部字段——SDK 的 ``td.handler(**arguments)``
    会把内核传入的整个 request 对象展开为关键字参数。
    """
    del plugin_id, raw_body  # 只读面：无 body 消费；dispatcher 已路由到本插件
    cap_method = _route(path)
    if cap_method is None:
        return _error(404, f"metrics_admin: no route for {method} {path}")

    params: dict[str, Any] = _query_params(query) if cap_method == "query" else {}
    params["_authorization"] = _authorization(headers)

    try:
        cap = plugin.get_capability(_CAPABILITY)
        envelope = await cap.call(cap_method, params)
    except KeyError:
        return _error(502, f"{_CAPABILITY} capability not injected (kernel handshake pending)")
    except Exception as exc:  # noqa: BLE001 —— capability 调用失败统一 502
        logger.warning("metrics_admin http.handle: capability %s failed: %s", cap_method, exc)
        return _error(502, f"metrics-admin capability call failed: {exc}")

    if not isinstance(envelope, dict):
        return _error(502, f"metrics-admin capability returned non-dict envelope: {type(envelope)}")
    status = int(envelope.get("status", 200))
    if not 200 <= status < 300:
        err = envelope.get("error") or {}
        return _ok(
            _json_response(
                {
                    "error": {
                        "code": str(err.get("code", status)),
                        "message": err.get("message", "metrics-admin error"),
                    }
                },
                status,
            )
        )
    # prometheus 的 body 是 exposition 文本（text/plain）；query/list 是 JSON。
    if cap_method == "prometheus":
        return _ok(_text_response(str(envelope.get("body", "")), status))
    return _ok(_json_response(envelope.get("body", {}), status))


if __name__ == "__main__":
    plugin.run()
