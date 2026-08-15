#!/usr/bin/env python3
"""User Admin 插件——user-admin capability 的 HTTP 面层（boot-plugin 第二刀）。

架构分工（§9.6 精确拆分，对齐第一刀 db_admin 的模式）：
- auth **执行门**永留内核：/api/v1/auth/login|logout|me|register|refresh 与
  WS 握手验签一行不动（被加载者不能把关加载门）。
- 用户管理**策略面**留内核 capability 层：agentos-user-admin crate 的
  UserAdminCapabilityHandler（注册进 CapabilityHandlerRegistry，
  namespace=user-admin，4 method）。admin 角色校验、self-service 防护
  （admin 不能删自己/降自己角色/改自己租户——防锁死系统）、password 剥离
  全部在内核 handler 内。
- 本插件只做 HTTP 面：内核 /ext/{*rest} 通配分发把 HttpHandleRequest 透传给
  本 http.handle 工具，按 path/method 组 user-admin capability 调用参数，经
  SDK 反向调用通道（plugin.get_capability("user-admin")）调内核 handler，把
  返回的信封组回 HTTP 响应（status/headers/body base64）。

鉴权落点（重要，与 db_admin 一致）：内核 http_dispatcher 目前不执行
http_endpoints[].auth 字段（dispatch_http 只查路由/并发/超时）。本插件不做
鉴权决策——把入站请求的 Authorization 头原样放进 params["_authorization"]，
角色校验由内核 handler 侧 resolve_request_user 执行（插件无法伪造角色，
信任锚点在内核）。

[来源: docs/working/重要设计/boot-plugin内核能力插件化立项.md §四/§五]
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any
from urllib.parse import unquote

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("user_admin")
logger = logging.getLogger(__name__)

_PREFIX = "/ext/user_admin"
_CAPABILITY = "user-admin"


def _json_response(payload: Any, status: int = 200) -> dict[str, Any]:
    """把 JSON 可序列化对象包成内核期望的 HttpHandleResponse（body base64）。"""
    body_str = json.dumps(payload, default=str, ensure_ascii=False)
    body_b64 = base64.b64encode(body_str.encode("utf-8")).decode("ascii")
    return {
        "status": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": body_b64,
        "body_encoding": "base64",
    }


def _ok(data: Any) -> dict[str, Any]:
    """成功响应：{success, data}（ToolExecutionResult 契约）。"""
    return {"success": True, "data": data}


def _error(status: int, message: str) -> dict[str, Any]:
    """错误响应（对齐 ApiError 的 {"error": {code, message}} 形状）。"""
    return _ok(_json_response({"error": {"code": str(status), "message": message}}, status))


def _decode_body(raw_body: str) -> dict[str, Any]:
    """解码 http.handle 的 raw_body（base64 → JSON dict；空 body 返回 {}）。"""
    if not raw_body:
        return {}
    decoded = raw_body
    try:
        candidate = base64.b64decode(raw_body).decode("utf-8")
        if candidate.lstrip().startswith(("{", "[")):
            decoded = candidate
    except Exception:  # noqa: BLE001 —— 非 base64 明文 body 直接按 JSON 解
        pass
    try:
        parsed = json.loads(decoded) if decoded.strip() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc
    return parsed if isinstance(parsed, dict) else {}


def _authorization(headers: dict[str, str] | None) -> str:
    """取入站请求的原始 Authorization 头值（内核 header key 已小写）。"""
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == "authorization" and v:
            return str(v)
    return ""


def _route(path: str, method: str) -> tuple[str, dict[str, Any], tuple[str, ...]] | None:
    """按 path/method 决定 capability method、路径参数与所需 body 字段。

    Returns:
        (method, 路径参数 dict, 需从 body 提取的字段名元组)；无匹配返回 None（404）。

    路径段 unquote：内核 uri.path() 不做百分号解码（原 axum Path 提取器会解码），
    此处补齐以保持 user_id 含特殊字符时的行为等价。
    """
    parts = [unquote(p) for p in path.split("/") if p]
    # parts[0]="ext", parts[1]="user_admin"
    if len(parts) < 2 or parts[0] != "ext" or parts[1] != "user_admin":
        return None
    rest = parts[2:]
    if rest == ["users"] and method == "GET":
        return "list_users", {}, ()
    if len(rest) == 2 and rest[0] == "users" and method == "DELETE":
        return "delete_user", {"user_id": rest[1]}, ()
    if len(rest) == 3 and rest[0] == "users" and method == "PATCH":
        user_id = rest[1]
        if rest[2] == "role":
            return "update_role", {"user_id": user_id}, ("role",)
        if rest[2] == "tenant":
            return "update_tenant", {"user_id": user_id}, ("tenant_id",)
    return None


@plugin.tool(
    name="user_admin.status",
    schema={
        "type": "object",
        "properties": {},
    },
)
async def user_admin_status() -> dict[str, Any]:
    """插件状态（user-admin capability 句柄是否已注入）。"""
    try:
        plugin.get_capability(_CAPABILITY)
        injected = True
    except KeyError:
        injected = False
    return {"plugin": "user_admin", "capability": _CAPABILITY, "capability_injected": injected}


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
    """user-admin capability 的 HTTP 面转发层。

    签名覆盖 HttpHandleRequest 全部字段（method/path/plugin_id/raw_body/headers/
    query）——SDK 的 ``td.handler(**arguments)`` 会把内核传入的整个 request 对象
    展开为关键字参数。
    """
    del plugin_id, query  # dispatcher 已路由到本插件；本面无 query 参数
    routed = _route(path, method)
    if routed is None:
        return _error(404, f"user_admin: no route for {method} {path}")
    cap_method, path_params, body_fields = routed

    params: dict[str, Any] = dict(path_params)
    params["_authorization"] = _authorization(headers)

    if body_fields:
        try:
            body = _decode_body(raw_body)
        except ValueError as exc:
            return _error(400, str(exc))
        for field in body_fields:
            params[field] = body.get(field)

    try:
        cap = plugin.get_capability(_CAPABILITY)
        envelope = await cap.call(cap_method, params)
    except KeyError:
        return _error(502, f"{_CAPABILITY} capability not injected (kernel handshake pending)")
    except Exception as exc:  # noqa: BLE001 —— capability 调用失败统一 502
        logger.warning("user_admin http.handle: capability %s failed: %s", cap_method, exc)
        return _error(502, f"user-admin capability call failed: {exc}")

    if not isinstance(envelope, dict):
        return _error(502, f"user-admin capability returned non-dict envelope: {type(envelope)}")
    status = int(envelope.get("status", 200))
    if 200 <= status < 300:
        payload = envelope.get("body", {})
    else:
        err = envelope.get("error") or {}
        payload = {
            "error": {
                "code": str(err.get("code", status)),
                "message": err.get("message", "user-admin error"),
            }
        }
    return _ok(_json_response(payload, status))


if __name__ == "__main__":
    plugin.run()
