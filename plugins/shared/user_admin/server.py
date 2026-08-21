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
  本 http.handle 工具，按 path/method 组 capability 调用参数，经 SDK 反向
  调用通道（plugin.get_capability(...)）调内核 handler，把返回的信封组回
  HTTP 响应（status/headers/body base64）。

channel_api 退役批次 2（users 域 10 端点 → user_admin 插件）：
- 列表/统计/角色/激活/删除/设置 六组端点迁入本插件 http.handle（源
  routes_missing.py users_router）。数据经 db-admin capability（table_query /
  table_update_row / table_delete_row）查/写内核 users 表。
- **鉴权切凭证透传**：入站请求 Authorization 头原样放进 params 的
  `_authorization`，内核 db-admin handler 侧 resolve_request_user 做真实角色
  校验（table_query 需 admin/viewer、写操作为 admin）——替代 channel_api 侧
  _resolve_caller（token 载荷无 role 段、admin 端点默认拒绝的错误语义），
  恢复"管理员端点只有真 admin 可用"。
- create_user（POST /users）：**删除**——空存根无消费方（前端 users.ts 定义
  但零页面调用），用户创建属内核 register（自注册）与 user-admin capability
  扩展的职责面，插件侧 db-admin 裸 INSERT 无法承载 username 唯一性友好报错/
  一用户一租户（tenant_id=user_id）等业务约束（报告说明）。
- update_user_active（PUT/PATCH /users/{id}/active）：保留存根语义——users 表
  **无 is_active 列**（engine store.rs：user_id/username/password/email/role/
  tenant_id/created_at/last_login_at），前端管理面板的激活开关此前即无实际
  落点；schema 演进属内核侧课题，不在本插件刀口。
- PATCH /users/{id}/role 与 PATCH /users/{id}/tenant 维持 user-admin capability
  面（内核自保护：不能降自己角色/改自己租户）；PUT /users/{id}/role 走
  db-admin（源 users.ts 消费形态）。

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
_DB_CAPABILITY = "db-admin"

# users 表中绝不出口的敏感列（含口令散列）
_USER_SENSITIVE_COLUMNS = frozenset({"password", "password_hash"})


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


def _qint(query: dict[str, str] | None, key: str, default: int) -> int:
    """query 参数安全取整（非法值回退默认，对齐 channel_api _qint 语义）。"""
    q = query or {}
    try:
        return int(q[key]) if key in q else default
    except (TypeError, ValueError):
        return default


def _user_row_to_api(row: dict[str, Any]) -> dict[str, Any]:
    """users 表行 → 前端 User 形状（脱敏 + user_id → id 映射 + is_active 补齐）。"""
    safe = {k: v for k, v in row.items() if k not in _USER_SENSITIVE_COLUMNS}
    safe["id"] = safe.get("user_id") or safe.get("id") or ""
    safe.setdefault("is_active", True)
    return safe


def _route(path: str, method: str) -> tuple[str, dict[str, Any], tuple[str, ...]] | None:
    """按 path/method 决定 user-admin capability 方法（PATCH role/tenant 保留面）。

    channel_api users 域迁入后，GET /users 与 DELETE /users/{id} 改走 db-admin
    凭证透传（见 _handle_users_domain），此处只保留 user-admin capability 的
    PATCH 变更面。

    Returns:
        (method, 路径参数 dict, 需从 body 提取的字段名元组)；无匹配返回 None（404）。
    """
    parts = [unquote(p) for p in path.split("/") if p]
    # parts[0]="ext", parts[1]="user_admin"
    if len(parts) < 2 or parts[0] != "ext" or parts[1] != "user_admin":
        return None
    rest = parts[2:]
    if len(rest) == 3 and rest[0] == "users" and method == "PATCH":
        user_id = rest[1]
        if rest[2] == "role":
            return "update_role", {"user_id": user_id}, ("role",)
        if rest[2] == "tenant":
            return "update_tenant", {"user_id": user_id}, ("tenant_id",)
    return None


async def _handle_users_domain(
    path: str,
    method: str,
    raw_body: str,
    headers: dict[str, str] | None,
    query: dict[str, str] | None,
) -> tuple[bool, dict[str, Any]]:
    """users 域分发（channel_api 批次2 迁入，db-admin 凭证透传）。

    返回 (handled, response)：handled=False 表示不属本域（交回 _route 面）。
    """
    parts = [unquote(p) for p in path.split("/") if p]
    if len(parts) < 3 or parts[0] != "ext" or parts[1] != "user_admin" or parts[2] != "users":
        return False, {}

    rest = parts[3:]  # "" | "stats" | "{id}" | "{id}/active" | "{id}/role" | "settings" | ...
    auth = _authorization(headers)

    # ── GET /users（列表，db-admin.table_query 凭证透传）──
    if rest == [] and method == "GET":
        return True, await _users_list(auth, query)

    # ── GET /users/stats ──
    if rest == ["stats"] and method == "GET":
        return True, await _users_stats(auth)

    # ── GET|PUT /users/settings（无后端落点，保持存根语义）──
    if rest == ["settings"] and method == "GET":
        return True, _ok(_json_response({"settings": {}}))
    if rest == ["settings"] and method == "PUT":
        _decode_body(raw_body)  # 不消费字段；仅做 body 合法性校验
        return True, _ok(_json_response({"settings": {}, "message": "设置已更新"}))

    # ── PUT /users/{id}/role（db-admin.table_update_row；PATCH 走 user-admin 保留面）──
    if len(rest) == 2 and rest[1] == "role" and method == "PUT":
        return True, await _users_update_role(rest[0], raw_body, auth)

    # ── PUT|PATCH /users/{id}/active（users 表无 is_active 列，保持存根语义）──
    if len(rest) == 2 and rest[1] == "active" and method in ("PUT", "PATCH"):
        _decode_body(raw_body)
        return True, _ok(_json_response({"id": rest[0], "is_active": True}))

    # ── DELETE /users/{id}（db-admin.table_delete_row）──
    if len(rest) == 1 and method == "DELETE":
        return True, await _users_delete(rest[0], auth)

    return False, {}


async def _call_db(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """调用 db-admin capability；未注入返回 None（调用异常视为未注入）。"""
    try:
        cap = plugin.get_capability(_DB_CAPABILITY)
    except KeyError:
        logger.warning("db-admin capability not injected (kernel handshake pending)")
        return None
    try:
        envelope = await cap.call(method, params)
    except Exception as exc:  # noqa: BLE001 —— 能力调用失败统一按未注入处理
        logger.warning("db-admin capability %s failed: %s", method, exc)
        return None
    return envelope if isinstance(envelope, dict) else None


def _envelope_error(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """capability 信封非 2xx → 组错误响应；2xx → None。"""
    status = int(envelope.get("status", 500))
    if 200 <= status < 300:
        return None
    err = envelope.get("error") or {}
    return _ok(_json_response({
        "error": {
            "code": str(err.get("code", status)),
            "message": err.get("message", "db-admin error"),
        }
    }, status))


async def _users_list(auth: str, query: dict[str, str] | None) -> dict[str, Any]:
    """GET /users：db-admin.table_query 查 users 表（读角色校验内核侧执行）。"""
    skip = _qint(query, "skip", 0)
    limit = _qint(query, "limit", 100)
    envelope = await _call_db("table_query", {
        "table": "users", "limit": limit, "offset": skip, "_authorization": auth,
    })
    if envelope is None:
        # 能力不可用：降级 HTTP 200 空列表（前端契约不破坏，同 monitoring 读面）
        return _ok(_json_response([]))
    err = _envelope_error(envelope)
    if err is not None:
        return err
    rows = (envelope.get("body") or {}).get("rows", [])
    return _ok(_json_response([_user_row_to_api(r) for r in rows if isinstance(r, dict)]))


async def _users_stats(auth: str) -> dict[str, Any]:
    """GET /users/stats：db-admin.table_query（limit 500）聚合统计。"""
    envelope = await _call_db("table_query", {
        "table": "users", "limit": 500, "offset": 0, "_authorization": auth,
    })
    if envelope is None:
        return _ok(_json_response({"total_users": 0, "active_users": 0, "admin_count": 0}))
    err = _envelope_error(envelope)
    if err is not None:
        return err
    rows = [r for r in (envelope.get("body") or {}).get("rows", []) if isinstance(r, dict)]
    return _ok(_json_response({
        "total_users": len(rows),
        "active_users": sum(1 for r in rows if r.get("is_active", 1) in (1, True)),
        "admin_count": sum(1 for r in rows if r.get("role") == "admin"),
    }))


async def _users_update_role(user_id: str, raw_body: str, auth: str) -> dict[str, Any]:
    """PUT /users/{id}/role：db-admin.table_update_row 真实改角色（内核 admin 校验）。"""
    try:
        body = _decode_body(raw_body)
    except ValueError as exc:
        return _error(400, str(exc))
    role = body.get("role")
    if role not in ("admin", "user"):
        return _error(400, "role 必须为 admin 或 user")
    envelope = await _call_db("table_update_row", {
        "table": "users", "pk_value": user_id, "updates": {"role": role},
        "_authorization": auth,
    })
    if envelope is None:
        return _error(502, "db-admin capability not injected (kernel handshake pending)")
    err = _envelope_error(envelope)
    if err is not None:
        return err
    return _ok(_json_response({"id": user_id, "role": role}))


async def _users_delete(user_id: str, auth: str) -> dict[str, Any]:
    """DELETE /users/{id}：db-admin.table_delete_row 真实删除（内核 admin 校验）。

    注：与 user-admin capability delete_user（含"不能删自己"自保护）不同，
    db-admin 删除仅 admin 门；自保护语义见 user_admin PATCH 面/内核 capability。
    """
    envelope = await _call_db("table_delete_row", {
        "table": "users", "pk_value": user_id, "_authorization": auth,
    })
    if envelope is None:
        return _error(502, "db-admin capability not injected (kernel handshake pending)")
    err = _envelope_error(envelope)
    if err is not None:
        return err
    return _ok(_json_response({"message": "用户已删除", "id": user_id}))


@plugin.tool(
    name="user_admin.status",
    schema={
        "type": "object",
        "properties": {},
    },
)
async def user_admin_status() -> dict[str, Any]:
    """插件状态（user-admin / db-admin capability 句柄是否已注入）。"""
    injected = {}
    for name in (_CAPABILITY, _DB_CAPABILITY):
        try:
            plugin.get_capability(name)
            injected[name] = True
        except KeyError:
            injected[name] = False
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
    """user-admin 面 + users 域（db-admin 凭证透传）的 HTTP 分发层。

    签名覆盖 HttpHandleRequest 全部字段（method/path/plugin_id/raw_body/headers/
    query）——SDK 的 ``td.handler(**arguments)`` 会把内核传入的整个 request 对象
    展开为关键字参数。
    """
    del plugin_id

    # ── users 域（channel_api 批次2 迁入）──
    handled, response = await _handle_users_domain(path, method, raw_body, headers, query)
    if handled:
        return response

    # ── user-admin capability 面（PATCH role / PATCH tenant）──
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
    plugin.run()  # pragma: no cover —— 入口由 sidecar 启动器执行