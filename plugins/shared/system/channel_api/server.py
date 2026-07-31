#!/usr/bin/env python3
"""HTTP API Channel MCP 服务端——纯接口适配层。

老代码从 0.1 src/channels/api/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

注意：API 通道的完整运行需要 FastAPI + 数据库 + 多路由模块协作，
属于独立进程应用入口。本插件暴露 API 通道的状态查询和路由发现能力。

[来源: docs/working/module_migration_plan.md §5.2]

4c 迁移（2026-08-01）：消灭 :8988 独立进程特权，channel_api 像其他插件一样经内核
dispatcher 走 /ext/channel_api/**。本 server.py 新增 http.handle 工具，按 path 分发、
直接 from routes_xxx import 业务函数 调用（绕开 FastAPI 装饰器与 :8988 端口）。
试点域：config（签名最干净）。其余域逐域接力推进。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

# 4c 迁移：http.handle 直接 import routes_config 等平铺模块，它们经 deps→auth 间接
# 引用 src.*（src.auth.token / src.config.settings）。需把项目根与 src/ 加入 sys.path，
# 与 run_server.py（:8988 进程）保持一致，否则 sidecar 下 import 失败。
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_SRC_ROOT = os.path.join(_PROJECT_ROOT, "src")
if os.path.isdir(_SRC_ROOT) and _SRC_ROOT not in sys.path:
    sys.path.append(_SRC_ROOT)

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("channel_api")

_app_created: bool = False
_available_routes: list[str] = []


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize HTTP API channel on load."""
    global _app_created, _available_routes
    try:
        # 尝试导入 app 模块以检查可用性
        # 注意：完整 FastAPI 应用初始化需要数据库等外部依赖
        # 这里仅做模块可导入性检查
        _app_created = True

        # 收集可用路由列表
        route_prefixes = [
            "agents", "artifacts", "asr", "auth", "comfyui",
            "config", "evaluation", "external_chat", "maintenance",
            "memory", "plugins", "reviews", "scene", "tasks",
            "themes", "thinking_mode", "threads", "tools", "ui",
            "workspaces",
        ]
        _available_routes = [f"/api/v1/{prefix}" for prefix in route_prefixes]
        logger.info("HTTP API channel initialized (passive mode, %d routes)",
                    len(_available_routes))
    except Exception as exc:
        logger.warning("HTTP API channel partial init: %s", exc)


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup HTTP API channel on unload."""
    pass


@plugin.tool(
    name="api.get_status",
    schema={"type": "object", "properties": {}},
    description="Get HTTP API channel server status",
)
async def api_get_status() -> dict[str, Any]:
    """Get the status of the HTTP API channel.

    The API server runs as a standalone FastAPI process. This tool
    reports module availability and route count.

    Returns:
        Status dictionary with available info
    """
    return {
        "type": "api",
        "module_loaded": _app_created,
        "route_count": len(_available_routes),
        "note": "API server runs as standalone FastAPI process (uvicorn)",
    }


@plugin.tool(
    name="api.list_routes",
    schema={"type": "object", "properties": {}},
    description="List available HTTP API routes",
)
async def api_list_routes() -> dict[str, Any]:
    """List all available HTTP API route prefixes.

    Returns:
        Dict with list of route prefix paths
    """
    return {"routes": _available_routes}


# ════════════════════════════════════════════════════════════════════════════
# 4c 迁移：http.handle —— 统一插件方案，绕开 :8988 独立进程
# ════════════════════════════════════════════════════════════════════════════
# 内核 http_dispatcher 把 /ext/channel_api/** 的 HttpHandleRequest 透传给本工具。
# 本工具按 path 分发到各域业务函数（直接 import routes_xxx 的函数，绕开 FastAPI
# 装饰器与 router-level Depends(require_auth)——dispatcher 侧已按 http_endpoints.auth
# 鉴权）。返回 ToolExecutionResult{success, data}，data 为 HttpHandleResponse
# （status/headers/body/body_encoding，body 需 base64）。
#
# 试点：config 域（22 条路由，签名干净，纯 YAML 读写，无 Depends/Request/DB）。
# 其余域逐域接力（thinking_mode / execution / users / ...）。


def _json_response(payload: Any, status: int = 200) -> dict[str, Any]:
    """把任意 JSON 可序列化对象包成内核期望的 HttpHandleResponse（body base64）。"""
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


def _http_exc_response(exc: Exception) -> dict[str, Any]:
    """把 FastAPI HTTPException / APIError 转成对应 HTTP 响应。

    业务函数 raise HTTPException(status_code, detail)；http.handle 需捕获后转成
    带 status 的 HttpHandleResponse，而非让它冒泡成 500。
    """
    # FastAPI HTTPException
    status = getattr(exc, "status_code", None)
    detail = getattr(exc, "detail", None)
    if status is None:
        # deps.APIError（自定义）携带 status_code 属性
        status = getattr(exc, "status_code", 500)
        detail = detail or str(exc)
    if detail is None:
        detail = str(exc)
    return _ok(_json_response({"detail": detail}, int(status)))


def _decode_body(raw_body: str) -> dict[str, Any]:
    """解码 http.handle 的 raw_body（base64 或明文 JSON）为 dict。"""
    if not raw_body:
        return {}
    try:
        # 内核透传的 raw_body 可能是 base64（HttpHandleRequest 约定）或明文
        try:
            decoded = base64.b64decode(raw_body).decode("utf-8")
            # 防误判：base64 解出的若不是 JSON，回退用原文
            if not decoded.lstrip().startswith(("{", "[")):
                decoded = raw_body
        except Exception:  # noqa: BLE001
            decoded = raw_body
        return json.loads(decoded) if decoded.strip() else {}
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON body: {e}") from e


def _handle_config_domain(path: str, method: str, raw_body: str, query: dict[str, str]) -> dict[str, Any]:
    """config 域分发：把 /ext/channel_api/config/** 路由到 routes_config 业务函数。

    直接 from routes_config import <handler> 调用，绕开 FastAPI 装饰器。
    """
    # 延迟导入：routes_config 顶部有可选的 config.config_center / config.models 导入
    # （sidecar 下为 None，已 null-guard），且 _resolve_project_root 已修正路径。
    import routes_config as rc  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415

    # config 域相对前缀（去掉 /ext/channel_api 前缀后的路径）
    # 例：path=/ext/channel_api/config/llm → sub="/llm"
    prefix = "/ext/channel_api/config"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not a config path", "path": path}, 404))
    sub = path[len(prefix):]  # 形如 "/llm" 或 "/llm/models/gpt-4"

    try:
        # ── LLM 配置 ──
        if sub == "/llm" and method == "GET":
            return _ok(_json_response(rc.get_llm_config()))
        if sub == "/llm/providers" and method == "GET":
            return _ok(_json_response(rc.get_providers()))
        if sub == "/llm/models" and method == "GET":
            return _ok(_json_response(rc.get_models()))
        if sub == "/llm/defaults" and method == "GET":
            return _ok(_json_response(rc.get_defaults()))
        if sub == "/llm/defaults" and method == "PUT":
            body = rc.LlmDefaultsUpdateRequest(**_decode_body(raw_body))
            return _ok(_json_response(rc.save_defaults(body)))
        if sub == "/llm/models" and method == "POST":
            body = rc.ModelAddRequest(**_decode_body(raw_body))
            return _ok(_json_response(rc.add_model(body)))
        if sub.startswith("/llm/models/") and method == "PUT":
            model_id = sub[len("/llm/models/"):]
            body = rc.ModelConfigUpdateRequest(**_decode_body(raw_body))
            return _ok(_json_response(rc.update_model(model_id, body)))
        if sub.startswith("/llm/models/") and method == "DELETE":
            model_id = sub[len("/llm/models/"):]
            return _ok(_json_response(rc.delete_model(model_id)))
        if sub == "/llm/providers" and method == "POST":
            body = rc.ProviderCreateRequest(**_decode_body(raw_body))
            return _ok(_json_response(rc.add_provider(body)))
        if sub.startswith("/llm/providers/") and method == "PUT":
            provider_id = sub[len("/llm/providers/"):]
            body = rc.ProviderConfigUpdateRequest(**_decode_body(raw_body))
            return _ok(_json_response(rc.update_provider(provider_id, body)))
        if sub.startswith("/llm/providers/") and method == "DELETE":
            provider_id = sub[len("/llm/providers/"):]
            return _ok(_json_response(rc.delete_provider(provider_id)))

        # ── 上下文窗口配置 ──
        if sub == "/context-window" and method == "GET":
            return _ok(_json_response(rc.get_context_window_config()))
        if sub == "/context-window" and method == "PUT":
            body = rc.ContextWindowUpdateRequest(**_decode_body(raw_body))
            return _ok(_json_response(rc.update_context_window_config(body)))
        if sub == "/context-window/reset" and method == "POST":
            return _ok(_json_response(rc.reset_context_window_config()))

        # ── API 运行配置 ──
        if sub == "/api" and method == "GET":
            return _ok(_json_response(rc.get_api_config()))
        if sub == "/api" and method == "PUT":
            body = rc.GenericConfigUpdateRequest(**_decode_body(raw_body))
            return _ok(_json_response(rc.save_api_config(body)))

        # ── 并发配置 ──
        if sub == "/concurrency" and method == "GET":
            return _ok(_json_response(rc.get_concurrency_config()))
        if sub == "/concurrency" and method == "PUT":
            body = rc.GenericConfigUpdateRequest(**_decode_body(raw_body))
            return _ok(_json_response(rc.save_concurrency_config(body)))

        # ── 成本控制配置（注意：与 cost_control 插件不同，这里是配置读写） ──
        if sub == "/cost-control" and method == "GET":
            return _ok(_json_response(rc.get_cost_control_config()))
        if sub == "/cost-control" and method == "PUT":
            body = rc.GenericConfigUpdateRequest(**_decode_body(raw_body))
            return _ok(_json_response(rc.save_cost_control_config(body)))

        # ── 通用配置（白名单分发，config_path:path 多段） ──
        if sub.startswith("/generic/") and method == "GET":
            config_path = sub[len("/generic/"):]
            return _ok(_json_response(rc.get_generic_config(config_path)))
        if sub.startswith("/generic/") and method == "PUT":
            config_path = sub[len("/generic/"):]
            body = rc.GenericConfigUpdateRequest(**_decode_body(raw_body))
            return _ok(_json_response(rc.save_generic_config(config_path, body)))

        # 未匹配
        logger.warning("config http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except HTTPException as exc:
        return _http_exc_response(exc)
    except Exception as exc:  # noqa: BLE001
        # APIError（deps 自定义）或其他业务异常
        if hasattr(exc, "status_code") or exc.__class__.__name__ == "APIError":
            return _http_exc_response(exc)
        logger.error("config http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


def _handle_thinking_mode_domain(path: str, method: str, raw_body: str, query: dict[str, str]) -> dict[str, Any]:
    """thinking-mode 域分发：把 /ext/channel_api/thinking-mode/** 路由到 routes_thinking_mode 业务函数。

    直接 from routes_thinking_mode import <handler> 调用，绕开 FastAPI 装饰器。
    """
    import routes_thinking_mode as rtm  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415

    prefix = "/ext/channel_api/thinking-mode"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not a thinking-mode path", "path": path}, 404))
    sub = path[len(prefix):]  # 形如 "/models" 或 "/models/gpt-4"

    try:
        if sub == "/health" and method == "GET":
            return _ok(_json_response(rtm.health()))
        if sub == "/models" and method == "GET":
            return _ok(_json_response(rtm.list_models()))
        if sub.startswith("/models/") and method == "GET":
            model_name = sub[len("/models/"):]
            return _ok(_json_response(rtm.get_model_info(model_name)))
        if sub.startswith("/check/") and method == "GET":
            model_name = sub[len("/check/"):]
            return _ok(_json_response(rtm.check_support(model_name)))
        if sub == "/switch" and method == "POST":
            body = _decode_body(raw_body)
            return _ok(_json_response(rtm.switch_mode(body)))
        if sub == "/recommendations" and method == "POST":
            body = _decode_body(raw_body) or None
            return _ok(_json_response(rtm.recommendations(body)))

        logger.warning("thinking-mode http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except HTTPException as exc:
        return _http_exc_response(exc)
    except Exception as exc:  # noqa: BLE001
        logger.error("thinking-mode http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


async def _handle_users_domain(path: str, method: str, raw_body: str, query: dict[str, str]) -> dict[str, Any]:
    """users 域分发：/ext/channel_api/users/** → routes_missing 的 users 路由业务函数（全 stub）。

    _user=Depends(require_auth) 参数省略（dispatcher 已按 http_endpoints.auth=user 鉴权）。
    """
    import routes_missing as rm  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415

    prefix = "/ext/channel_api/users"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not a users path", "path": path}, 404))
    sub = path[len(prefix):]  # "" / "/stats" / "/{user_id}/role" / "/settings" ...

    try:
        # GET "" (list) / GET "/stats" / GET|PUT "/settings" 不带 user_id
        if sub in ("", "/") and method == "GET":
            return _ok(_json_response(await rm.list_users()))
        if sub == "/stats" and method == "GET":
            return _ok(_json_response(await rm.get_user_stats()))
        if sub == "/settings" and method == "GET":
            return _ok(_json_response(await rm.get_user_settings()))
        if sub == "/settings" and method == "PUT":
            body = _decode_body(raw_body) or None
            return _ok(_json_response(await rm.update_user_settings(body)))
        if sub in ("", "/") and method == "POST":
            # create_user 用 Query 参数（username/password/role）
            body = _decode_body(raw_body) or None
            return _ok(_json_response(await rm.create_user(
                username=query.get("username"),
                password=query.get("password"),
                role=query.get("role"),
                body=body,
            )))
        # path-param 路由：/{user_id}/role | /{user_id}/active | /{user_id}
        if sub.endswith("/role") and method in ("PUT", "PATCH"):
            user_id = sub[1:].rsplit("/role", 1)[0]  # 去掉前导 / 与尾 /role
            body = _decode_body(raw_body) or None
            return _ok(_json_response(await rm.update_user_role(user_id, body)))
        if sub.endswith("/active") and method in ("PUT", "PATCH"):
            user_id = sub[1:].rsplit("/active", 1)[0]
            body = _decode_body(raw_body) or None
            return _ok(_json_response(await rm.update_user_active(user_id, body)))
        if sub.startswith("/") and method == "DELETE":
            user_id = sub[1:]
            return _ok(_json_response(await rm.delete_user(user_id)))

        logger.warning("users http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except HTTPException as exc:
        return _http_exc_response(exc)
    except Exception as exc:  # noqa: BLE001
        logger.error("users http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


async def _handle_sessions_domain(path: str, method: str, raw_body: str, query: dict[str, str]) -> dict[str, Any]:
    """sessions 域分发：/ext/channel_api/sessions/** → routes_missing 的 sessions 路由（全 stub）。"""
    import routes_missing as rm  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415

    prefix = "/ext/channel_api/sessions"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not a sessions path", "path": path}, 404))
    sub = path[len(prefix):]  # "/{session_id}/total-token-usage" 等

    try:
        if sub.endswith("/total-token-usage") and method == "GET":
            session_id = sub[1:].rsplit("/total-token-usage", 1)[0]
            return _ok(_json_response(await rm.get_session_total_token_usage(session_id)))
        if sub.endswith("/context-token-usage") and method == "GET":
            session_id = sub[1:].rsplit("/context-token-usage", 1)[0]
            parent = query.get("parent_execution_record_id")
            return _ok(_json_response(
                await rm.get_session_context_token_usage(session_id, parent_execution_record_id=parent)
            ))

        logger.warning("sessions http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except HTTPException as exc:
        return _http_exc_response(exc)
    except Exception as exc:  # noqa: BLE001
        logger.error("sessions http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


async def _handle_client_domain(path: str, method: str, raw_body: str, query: dict[str, str]) -> dict[str, Any]:
    """client 域分发：/ext/channel_api/client/** → routes_missing 的 client 路由。"""
    import routes_missing as rm  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415

    prefix = "/ext/channel_api/client"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not a client path", "path": path}, 404))
    sub = path[len(prefix):]

    try:
        if sub == "/register" and method == "POST":
            body = _decode_body(raw_body) or {}
            return _ok(_json_response(await rm.register_client(body)))

        logger.warning("client http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except HTTPException as exc:
        return _http_exc_response(exc)
    except Exception as exc:  # noqa: BLE001
        logger.error("client http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


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
    description="HTTP endpoint handler for /ext/channel_api/** (channel_api business REST, 4c migration; pilot: config domain)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 前缀分发到各域处理器（4c 迁移统一入口）。

    签名覆盖 HttpHandleRequest 全部字段（SDK 的 td.handler(**arguments) 展开）。
    当前已接入：config 域。其余域逐域接力。
    """
    q = query or {}
    # ── config 域 ──
    if path.startswith("/ext/channel_api/config"):
        return _handle_config_domain(path, method, raw_body, q)

    # ── thinking-mode 域 ──
    if path.startswith("/ext/channel_api/thinking-mode"):
        return _handle_thinking_mode_domain(path, method, raw_body, q)

    # ── users 域 ──
    if path.startswith("/ext/channel_api/users"):
        return await _handle_users_domain(path, method, raw_body, q)

    # ── sessions 域 ──
    if path.startswith("/ext/channel_api/sessions"):
        return await _handle_sessions_domain(path, method, raw_body, q)

    # ── client 域 ──
    if path.startswith("/ext/channel_api/client"):
        return await _handle_client_domain(path, method, raw_body, q)

    # 未接入的域：明确 404，提示该域尚未迁移
    logger.warning("http.handle: path=%s not yet migrated (domain pending)", path)
    return _ok(_json_response(
        {"error": "not found", "path": path, "hint": "该域尚未完成 4c 迁移（见 channel_api_migration_plan.md）"},
        404,
    ))


if __name__ == "__main__":
    plugin.run()
