#!/usr/bin/env python3
"""DB Admin 插件——db-admin capability 的 HTTP 面层（boot-plugin 第一刀）。

架构分工：
- SQL 能力层留内核：agentos-db-admin crate 的 DbAdminCapabilityHandler（注册进
  CapabilityHandlerRegistry，namespace=db-admin，7 method）。表/列白名单、参数绑定、
  租户隔离、BLOB 安全、SQL 执行器防线（check_dangerous/classify_sql）全部在内
  核 handler 内。
- 本插件只做 HTTP 面：内核 /ext/{*rest} 通配分发把 HttpHandleRequest 透传给本
  http.handle 工具，按 path/method 组 db-admin capability 调用参数，经 SDK 反向
  调用通道（plugin.get_capability("db-admin")）调内核 handler，把返回的信封组回
  HTTP 响应（status/headers/body base64）。

鉴权落点（重要）：内核 http_dispatcher 目前不执行 http_endpoints[].auth 字段
（dispatch_http 只查路由/并发/超时）。本插件不做鉴权决策——把入站请求的
Authorization 头原样放进 params["_authorization"]，角色/租户校验由内核 handler
侧 resolve_request_user 执行（插件无法伪造角色，信任锚点在内核）。

query 多值透传：内核 dispatcher 在单值 query 之外透传 ``query_multi``
（key → 全量 value 列表），重复 key（如 ``filter=a&filter=b`` 的多条件 AND）
全量到达、不塌缩成最后一个值——本插件 table_query 的 filter 从 query_multi
组全量数组；单值 ``query``（last-wins 塌缩形态）作兜底。

[来源: docs/working/重要设计/boot-plugin内核能力插件化立项.md §三]
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any
from urllib.parse import unquote

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("db_admin")
logger = logging.getLogger(__name__)

_PREFIX = "/ext/db_admin"
_CAPABILITY = "db-admin"


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


def _route(path: str, method: str) -> tuple[str, dict[str, Any]] | None:
    """按 path/method 决定 capability method 与路径参数。

    Returns:
        (method, 路径参数 dict)；无匹配路由返回 None（404）。

    路径段 unquote：内核 uri.path() 不做百分号解码（原 axum Path 提取器会解码），
    此处补齐以保持 pk_value 含特殊字符时的行为等价。
    """
    parts = [unquote(p) for p in path.split("/") if p]
    # parts[0]="ext", parts[1]="db_admin"
    if len(parts) < 2 or parts[0] != "ext" or parts[1] != "db_admin":
        return None
    rest = parts[2:]
    if rest == ["tables"] and method == "GET":
        return "list_tables", {}
    if rest == ["execute"] and method == "POST":
        return "execute", {}
    if len(rest) >= 2 and rest[0] == "table":
        table = rest[1]
        if len(rest) == 2:
            if method == "GET":
                return "table_query", {"table": table}
            if method == "POST":
                return "table_insert", {"table": table}
        if len(rest) == 3:
            pk = rest[2]
            routing = {
                "GET": "table_get_row",
                "PATCH": "table_update_row",
                "DELETE": "table_delete_row",
            }
            if method in routing:
                return routing[method], {"table": table, "pk_value": pk}
    return None


def _query_params(
    query: dict[str, str] | None,
    query_multi: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """把内核透传的 query 组为 table_query 的参数。

    filter 支持两种 key（filter / filter[]，后者是前端 axios 数组默认序列化形态）。
    filter 同时接受单值与多值形态，多值优先（query_multi 全量透传，重复 key
    全量到达、多条件 AND 不丢条件），单值 query（last-wins 塌缩形态）作兜底
    ——组单元素数组保持 capability 层 filter 恒为数组的契约。
    """
    q = query or {}
    qm = query_multi or {}
    params: dict[str, Any] = {}
    for key in ("limit", "offset"):
        if q.get(key):
            try:
                params[key] = int(q[key])
            except ValueError:
                pass  # 对齐原行为：非法数值忽略
    filters: list[str] = []
    for key in ("filter", "filter[]"):
        multi = qm.get(key)
        if multi:
            filters = [str(v) for v in multi if v]
            break
    if not filters:
        for key in ("filter", "filter[]"):
            if q.get(key):
                filters = [str(q[key])]
                break
    if filters:
        params["filter"] = filters
    if q.get("sort"):
        params["sort"] = q["sort"]
    return params


@plugin.tool(
    name="db_admin.status",
    schema={
        "type": "object",
        "properties": {},
    },
)
async def db_admin_status() -> dict[str, Any]:
    """插件状态（db-admin capability 句柄是否已注入）。"""
    try:
        plugin.get_capability(_CAPABILITY)
        injected = True
    except KeyError:
        injected = False
    return {"plugin": "db_admin", "capability": _CAPABILITY, "capability_injected": injected}


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
            "query_multi": {"type": "object"},
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
    query_multi: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """db-admin capability 的 HTTP 面转发层。

    签名覆盖 HttpHandleRequest 全部字段（method/path/plugin_id/raw_body/headers/
    query/query_multi）——SDK 的 ``td.handler(**arguments)`` 会把内核传入的整个
    request 对象展开为关键字参数（未声明 query_multi 的其他插件经 SDK 签名过滤
    不受影响）。
    """
    del plugin_id  # dispatcher 已路由到本插件，无需再判归属
    routed = _route(path, method)
    if routed is None:
        return _error(404, f"db_admin: no route for {method} {path}")
    cap_method, path_params = routed

    params: dict[str, Any] = dict(path_params)
    params["_authorization"] = _authorization(headers)

    if cap_method == "table_query":
        params.update(_query_params(query, query_multi))
    elif cap_method in ("table_insert", "table_update_row", "execute"):
        try:
            body = _decode_body(raw_body)
        except ValueError as exc:
            return _error(400, str(exc))
        if cap_method == "table_insert":
            params["row"] = body.get("row")
        elif cap_method == "table_update_row":
            params["updates"] = body.get("updates")
        else:
            params["sql"] = body.get("sql")
            params["confirm"] = bool(body.get("confirm", False))

    try:
        cap = plugin.get_capability(_CAPABILITY)
        envelope = await cap.call(cap_method, params)
    except KeyError:
        return _error(502, f"{_CAPABILITY} capability not injected (kernel handshake pending)")
    except Exception as exc:  # noqa: BLE001 —— capability 调用失败统一 502
        logger.warning("db_admin http.handle: capability %s failed: %s", cap_method, exc)
        return _error(502, f"db-admin capability call failed: {exc}")

    if not isinstance(envelope, dict):
        return _error(502, f"db-admin capability returned non-dict envelope: {type(envelope)}")
    status = int(envelope.get("status", 200))
    if 200 <= status < 300:
        payload = envelope.get("body", {})
    else:
        err = envelope.get("error") or {}
        payload = {
            "error": {
                "code": str(err.get("code", status)),
                "message": err.get("message", "db-admin error"),
            }
        }
    return _ok(_json_response(payload, status))


if __name__ == "__main__":
    plugin.run()
