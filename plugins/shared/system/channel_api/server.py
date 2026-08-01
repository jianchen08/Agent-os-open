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


async def _handle_execution_domain(path: str, method: str, raw_body: str, query: dict[str, str]) -> dict[str, Any]:
    """execution 域分发：/ext/channel_api/execution/** → routes_missing 的 execution 路由。

    storage 经 _get_exec_storage() 取（src/infrastructure.service_access，sidecar 下 src/ 已在
    sys.path；handler 内部已 None-safe：storage=None 时返回空结构）。
    仅迁前端实际消费的 /records* 子集（前端 executionRecords.ts）。
    """
    import routes_missing as rm  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415

    prefix = "/ext/channel_api/execution"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not an execution path", "path": path}, 404))
    sub = path[len(prefix):]  # "/records" / "/records/sessions" / "/records/{id}" / ...

    def _qint(key: str, default: int) -> int:
        try:
            return int(query[key]) if key in query else default
        except (TypeError, ValueError):
            return default

    try:
        # GET /records（list，query: session_id/parent_record_id/limit/offset）
        if sub == "/records" and method == "GET":
            return _ok(_json_response(await rm.list_execution_records(
                session_id=query.get("session_id"),
                parent_record_id=query.get("parent_record_id"),
                limit=_qint("limit", 50),
                offset=_qint("offset", 0),
            )))
        # GET /records/sessions
        if sub == "/records/sessions" and method == "GET":
            return _ok(_json_response(await rm.get_execution_record_sessions()))
        # GET /records/group-summary（query: session_id）
        if sub == "/records/group-summary" and method == "GET":
            return _ok(_json_response(await rm.get_record_group_summary(
                session_id=query.get("session_id"),
            )))
        # GET /records/tree/{session_id}（query: max_depth）
        if sub.startswith("/records/tree/") and method == "GET":
            session_id = sub[len("/records/tree/"):]
            return _ok(_json_response(await rm.get_execution_tree(
                session_id, max_depth=_qint("max_depth", 5),
            )))
        # GET /records/{record_id}/children
        if sub.endswith("/children") and sub.startswith("/records/") and method == "GET":
            record_id = sub[len("/records/"):-len("/children")]
            return _ok(_json_response(await rm.get_children_records(record_id)))
        # GET /records/{record_id}
        if sub.startswith("/records/") and method == "GET" and "/" not in sub[len("/records/"):]:
            record_id = sub[len("/records/"):]
            return _ok(_json_response(await rm.get_execution_record(record_id)))
        # DELETE /records/{record_id}
        if sub.startswith("/records/") and method == "DELETE" and "/" not in sub[len("/records/"):]:
            record_id = sub[len("/records/"):]
            return _ok(_json_response(await rm.delete_execution_record(record_id)))
        # DELETE /records/session/{session_id}
        if sub.startswith("/records/session/") and method == "DELETE":
            session_id = sub[len("/records/session/"):]
            return _ok(_json_response(await rm.delete_execution_records_by_session(session_id)))
        # POST /records/clear-all
        if sub == "/records/clear-all" and method == "POST":
            return _ok(_json_response(await rm.clear_all_records()))

        logger.warning("execution http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except HTTPException as exc:
        return _http_exc_response(exc)
    except Exception as exc:  # noqa: BLE001
        logger.error("execution http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


def _handle_modules_ui_domain(path: str, method: str, raw_body: str, query: dict[str, str]) -> dict[str, Any]:
    """modules/ui 域分发：/ext/channel_api/modules/ui/** → routes_ui 业务函数。

    仅迁 schema 查询（list/get）；data CRUD（get_module_data_router）不迁（前端 modules.ts
    仅消费 ui schema 列表）。_get_schema_parser 已修 config/modules 路径（_resolve_project_root）。
    """
    import routes_ui as rui  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415

    prefix = "/ext/channel_api/modules/ui"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not a modules/ui path", "path": path}, 404))
    sub = path[len(prefix):]  # "" / "/{module_id}"
    client_type = query.get("client_type")

    try:
        if sub in ("", "/") and method == "GET":
            return _ok(_json_response(rui.list_ui_schemas(client_type=client_type)))
        if sub.startswith("/") and method == "GET":
            module_id = sub[1:]
            return _ok(_json_response(rui.get_ui_schema(module_id, client_type=client_type)))

        logger.warning("modules/ui http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except HTTPException as exc:
        return _http_exc_response(exc)
    except Exception as exc:  # noqa: BLE001
        # APIError（deps 自定义，get_ui_schema 未找到时抛）→ 转 HTTP status
        if hasattr(exc, "status_code") or exc.__class__.__name__ == "APIError":
            return _http_exc_response(exc)
        logger.error("modules/ui http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


def _pydantic_to_dict(obj: Any) -> Any:
    """把 pydantic 模型转成可 JSON 化的 dict（routes_memory 部分返回 pydantic）。"""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(by_alias=True, exclude_none=True)
    return obj


def _handle_memory_domain(path: str, method: str, raw_body: str, query: dict[str, str]) -> dict[str, Any]:
    """memory 域分发：/ext/channel_api/memory/** → routes_memory 业务函数。

    store（channels.api.memory_store 单例）import 时即创建，sidecar 下读
    data/api_store/store.json（与 :8988 同路径，冷启动一致）。注意：这是进程态 store，
    与 :8988 进程的 store 是不同实例——活系统双进程期间写入不互通（4c stopgap，
    待 :8988 退役后归一）。部分返回 pydantic 模型，统一 model_dump。
    """
    import routes_memory as rmm  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415

    prefix = "/ext/channel_api/memory"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not a memory path", "path": path}, 404))
    sub = path[len(prefix):]  # "" / "/search" / "/episodes" / "/{memory_id}" ...

    def _qint(key: str, default: int) -> int:
        try:
            return int(query[key]) if key in query else default
        except (TypeError, ValueError):
            return default

    try:
        # GET ""（list，query: memory_type/limit/offset）
        if sub in ("", "/") and method == "GET":
            return _ok(_json_response(_pydantic_to_dict(rmm.list_memories(
                memory_type=query.get("memory_type"),
                limit=_qint("limit", 20),
                offset=_qint("offset", 0),
            ))))
        # GET /search（query: query/top_k/method）
        if sub == "/search" and method == "GET":
            return _ok(_json_response(_pydantic_to_dict(rmm.search_memories(
                query=query.get("query", ""),
                top_k=_qint("top_k", 5),
                method=query.get("method", "keyword"),
            ))))
        # POST /search（body: query/top_k）
        if sub == "/search" and method == "POST":
            body = _decode_body(raw_body) or None
            return _ok(_json_response(_pydantic_to_dict(rmm.search_memories_post(body))))
        # GET /episodes（query: page/page_size）
        if sub == "/episodes" and method == "GET":
            return _ok(_json_response(rmm.list_episodes(
                page=_qint("page", 1), page_size=_qint("page_size", 20),
            )))
        # GET /episodes/{episode_id}
        if sub.startswith("/episodes/") and method == "GET":
            episode_id = sub[len("/episodes/"):]
            return _ok(_json_response(rmm.get_episode(episode_id)))
        # GET /semantic
        if sub == "/semantic" and method == "GET":
            return _ok(_json_response(rmm.list_semantic()))
        # POST /consolidate
        if sub == "/consolidate" and method == "POST":
            return _ok(_json_response(rmm.consolidate_memory()))
        # GET /stats
        if sub == "/stats" and method == "GET":
            return _ok(_json_response(rmm.get_memory_stats()))
        # GET /{memory_id}（动态路径，放最后）
        if sub.startswith("/") and method == "GET" and "/" not in sub[1:]:
            memory_id = sub[1:]
            return _ok(_json_response(_pydantic_to_dict(rmm.get_memory(memory_id))))
        # DELETE /{memory_id}
        if sub.startswith("/") and method == "DELETE" and "/" not in sub[1:]:
            memory_id = sub[1:]
            return _ok(_json_response(rmm.delete_memory(memory_id)))

        logger.warning("memory http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except HTTPException as exc:
        return _http_exc_response(exc)
    except Exception as exc:  # noqa: BLE001
        if hasattr(exc, "status_code") or exc.__class__.__name__ == "APIError":
            return _http_exc_response(exc)
        logger.error("memory http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


def _handle_scenes_domain(path: str, method: str, raw_body: str, query: dict[str, str]) -> dict[str, Any]:
    """scenes 域分发：/ext/channel_api/scenes/** → routes_scene 业务函数（批次3 首迁）。

    复用 4c 模式（同 config/thinking-mode）。7 路由全覆盖，含 path-param {scene_id}。
    _user=Depends(require_auth) 省略（dispatcher 已鉴权，handler 内不读 _user）。
    create/update 用 pydantic 模型（SceneCreateRequest/SceneUpdateRequest）。
    """
    import routes_scene as rsc  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415

    prefix = "/ext/channel_api/scenes"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not a scenes path", "path": path}, 404))
    sub = path[len(prefix):]  # "" / "/templates" / "/{scene_id}" / "/{scene_id}/switch"

    try:
        # GET "" (list)
        if sub in ("", "/") and method == "GET":
            return _ok(_json_response(rsc.list_scenes()))
        # POST "" (create, pydantic body)
        if sub in ("", "/") and method == "POST":
            body = _decode_body(raw_body)
            request = rsc.SceneCreateRequest(**body)
            return _ok(_json_response(rsc.create_scene(request)))
        # GET /templates
        if sub == "/templates" and method == "GET":
            return _ok(_json_response(rsc.get_templates()))
        # /{scene_id} 系列
        if sub.startswith("/") and "/" not in sub[1:]:
            scene_id = sub[1:]
            if method == "GET":
                return _ok(_json_response(rsc.get_scene(scene_id)))
            if method == "PUT":
                body = _decode_body(raw_body)
                request = rsc.SceneUpdateRequest(**body)
                return _ok(_json_response(rsc.update_scene(scene_id, request)))
            if method == "DELETE":
                return _ok(_json_response(rsc.delete_scene(scene_id)))
        # /{scene_id}/switch
        if sub.endswith("/switch") and method == "POST":
            scene_id = sub[1:].rsplit("/switch", 1)[0]
            return _ok(_json_response(rsc.switch_scene(scene_id)))

        logger.warning("scenes http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except HTTPException as exc:
        return _http_exc_response(exc)
    except Exception as exc:  # noqa: BLE001
        if hasattr(exc, "status_code") or exc.__class__.__name__ == "APIError":
            return _http_exc_response(exc)
        logger.error("scenes http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


async def _handle_asr_domain(
    path: str, method: str, raw_body: str, headers: dict[str, str]
) -> dict[str, Any]:
    """asr 域分发：/ext/channel_api/audio/transcriptions → routes_asr 业务。

    单 multipart 路由（POST /transcriptions）。内核透传原始字节，sidecar 解 multipart
    取 file + language，直接调 multimodal.get_asr_service().transcribe（对齐 routes_asr）。
    ASR 未配置时 503（对齐原 handler）。
    """
    import base64 as _b64  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415
    from multimodal import get_asr_service  # noqa: PLC0415

    if path != "/ext/channel_api/audio/transcriptions" or method != "POST":
        return _ok(_json_response({"error": "not found", "path": path}, 404))

    try:
        body_bytes = _b64.b64decode(raw_body) if raw_body else b""
    except Exception as exc:  # noqa: BLE001
        return _ok(_json_response({"error": f"invalid upload body: {exc}"}, 400))

    content_type = headers.get("content-type", "") or headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        return _ok(_json_response(
            {"error": "asr requires multipart/form-data", "content_type": content_type}, 400,
        ))

    try:
        fields = _parse_multipart(content_type, body_bytes)
    except Exception as exc:  # noqa: BLE001
        return _ok(_json_response({"error": f"multipart parse failed: {exc}"}, 400))

    file_field = fields.get("file")
    if not isinstance(file_field, dict) or not file_field.get("data"):
        return _ok(_json_response({"error": "missing or empty 'file' field"}, 400))

    audio_bytes: bytes = file_field["data"]
    mime_type = file_field.get("content_type") or "audio/webm"
    language = fields.get("language") or None
    if isinstance(language, str) and language.strip() == "":
        language = None

    try:
        asr = get_asr_service()
        if not asr.is_available():
            return _ok(_json_response(
                {"code": "asr_not_configured", "message": "语音转文字服务未配置"}, 503,
            ))
        text = await asr.transcribe(audio_bytes, mime_type, language)
        return _ok(_json_response({"text": text}))
    except HTTPException as exc:
        return _http_exc_response(exc)
    except RuntimeError as exc:
        # 对齐原 handler：转写失败 502
        return _ok(_json_response(
            {"code": "asr_failed", "message": str(exc)}, 502,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.error("asr http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


async def _handle_workspaces_domain(
    path: str, method: str, raw_body: str, query: dict[str, str]
) -> dict[str, Any]:
    """workspaces 域分发：/ext/channel_api/workspaces/** → routes_workspaces 业务函数。

    11 路由（FS/IDE 操作）。业务函数 async，全 dict body（非 pydantic）。
    _user=Depends 省略（dispatcher 鉴权，handler 不读 _user）。
    path-param {container_task_id}；file-content/delete-entry 用 Query(path=)。
    """
    import routes_workspaces as rws  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415

    prefix = "/ext/channel_api/workspaces"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not a workspaces path", "path": path}, 404))
    sub = path[len(prefix):]  # "" / "/open-file" / "/{id}" / "/{id}/file-tree" ...

    try:
        # POST /open-file（body: file_path/line/column）
        if sub == "/open-file" and method == "POST":
            body = _decode_body(raw_body) or {}
            return _ok(_json_response(await rws.open_file_in_ide(body)))

        # /{container_task_id} 系列路由
        if sub.startswith("/") and len(sub) > 1:
            rest = sub[1:]  # "{id}" 或 "{id}/file-tree" 等
            # 单级：/{id}（GET workspace / POST open）
            if "/" not in rest:
                cid = rest
                if method == "GET":
                    return _ok(_json_response(await rws.get_workspace(cid)))
                if method == "POST":  # /{id}/open 实际是二级，这里只处理纯 /{id} 的 fallback
                    pass
            else:
                cid, action = rest.split("/", 1)
                if action == "artifacts" and method == "GET":
                    return _ok(_json_response(await rws.get_workspace_artifacts(cid)))
                if action == "file-tree" and method == "GET":
                    return _ok(_json_response(await rws.get_file_tree(cid)))
                if action == "file-content" and method == "GET":
                    return _ok(_json_response(await rws.get_file_content(
                        cid, path=query.get("path", ""),
                    )))
                if action == "file-content" and method == "PUT":
                    body = _decode_body(raw_body)
                    return _ok(_json_response(await rws.save_file_content(
                        cid, path=query.get("path", ""), body=body,
                    )))
                if action == "create-entry" and method == "POST":
                    body = _decode_body(raw_body) or {}
                    return _ok(_json_response(await rws.create_entry(cid, body)))
                if action == "entries" and method == "DELETE":
                    return _ok(_json_response(await rws.delete_entry(
                        cid, path=query.get("path", ""),
                    )))
                if action == "rename-entry" and method == "POST":
                    body = _decode_body(raw_body) or {}
                    return _ok(_json_response(await rws.rename_entry(cid, body)))
                if action == "move-entry" and method == "POST":
                    body = _decode_body(raw_body) or {}
                    return _ok(_json_response(await rws.move_entry(cid, body)))
                if action == "open" and method == "POST":
                    return _ok(_json_response(await rws.open_workspace_in_ide(cid)))

        logger.warning("workspaces http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except HTTPException as exc:
        return _http_exc_response(exc)
    except Exception as exc:  # noqa: BLE001
        if hasattr(exc, "status_code") or exc.__class__.__name__ == "APIError":
            return _http_exc_response(exc)
        logger.error("workspaces http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


async def _handle_reviews_domain(
    path: str, method: str, raw_body: str, query: dict[str, str], headers: dict[str, str]
) -> dict[str, Any]:
    """reviews 域分发：/ext/channel_api/reviews/** → routes_reviews 业务函数。

    9 路由：create/get/list/feedback/viewed/cancel（JSON body 或 path-param）+
    media-review（multipart 上传 → media_review_service）+ media-metadata +
    attachments（JSON body，文件路径非上传）。_user 不读，省略。
    """
    import routes_reviews as rrv  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415

    prefix = "/ext/channel_api/reviews"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not a reviews path", "path": path}, 404))
    sub = path[len(prefix):]  # "" / "/media-review" / "/{id}" / "/{id}/feedback" ...

    def _qint(key: str, default: int) -> int:
        try:
            return int(query[key]) if key in query else default
        except (TypeError, ValueError):
            return default

    try:
        # POST "" (create)
        if sub in ("", "/") and method == "POST":
            body = _decode_body(raw_body) or {}
            return _ok(_json_response(await rrv.create_review(body)))
        # GET "" (list, query: task_id/limit)
        if sub in ("", "/") and method == "GET":
            return _ok(_json_response(await rrv.list_reviews(
                task_id=query.get("task_id", ""),
                limit=_qint("limit", 50),
            )))
        # POST /media-review（multipart：file + media_type）
        if sub == "/media-review" and method == "POST":
            return await _handle_review_media_upload(raw_body, headers)

        # /{review_id} 系列
        if sub.startswith("/") and len(sub) > 1:
            rest = sub[1:]
            if "/" not in rest:
                rid = rest
                if method == "GET":
                    return _ok(_json_response(await rrv.get_review(rid)))
            else:
                rid, action = rest.split("/", 1)
                if action == "feedback" and method == "POST":
                    body = _decode_body(raw_body) or {}
                    return _ok(_json_response(await rrv.submit_feedback(rid, body)))
                if action == "viewed" and method == "POST":
                    return _ok(_json_response(await rrv.mark_as_viewed(rid)))
                if action == "cancel" and method == "POST":
                    return _ok(_json_response(await rrv.cancel_review(rid)))
                if action == "media-metadata" and method == "GET":
                    return _ok(_json_response(await rrv.get_media_metadata(rid)))
                if action == "attachments" and method == "POST":
                    body = _decode_body(raw_body) or {}
                    return _ok(_json_response(await rrv.add_attachments(rid, body)))

        logger.warning("reviews http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except HTTPException as exc:
        return _http_exc_response(exc)
    except Exception as exc:  # noqa: BLE001
        if hasattr(exc, "status_code") or exc.__class__.__name__ == "APIError":
            return _http_exc_response(exc)
        logger.error("reviews http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


async def _handle_tasks_domain(
    path: str, method: str, raw_body: str, query: dict[str, str]
) -> dict[str, Any]:
    """tasks 域分发（批次3 最大域）：/ext/channel_api/{tasks,projects}/**。

    覆盖 routes_tasks.py（13 路由，/tasks）+ routes_missing.py projects_router（6，/projects）
    + routes_missing.py task_phase_router（9，/tasks/{id}/phase|ac）。前端 4 块（TASKS/
    PROJECTS/TASK_PHASES/TASK_EVALUATION）全切到此。pydantic 返回值统一 model_dump。
    """
    import routes_tasks as rt  # noqa: PLC0415
    import routes_missing as rm  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415

    def _qint(key: str, default: int) -> int | None:
        if key not in query:
            return default
        try:
            return int(query[key])
        except (TypeError, ValueError):
            return default

    def _qopt(key: str) -> str | None:
        return query.get(key)

    try:
        # ── projects 域（/ext/channel_api/projects...）──
        if path.startswith("/ext/channel_api/projects"):
            sub = path[len("/ext/channel_api/projects"):]
            if sub in ("", "/") and method == "GET":
                return _ok(_json_response(await rm.list_projects(
                    limit=_qint("limit", 20), offset=_qint("offset", 0),
                )))
            if sub in ("", "/") and method == "POST":
                body = _decode_body(raw_body)
                return _ok(_json_response(await rm.create_project(body)))
            if sub.startswith("/") and "/" not in sub[1:]:
                pid = sub[1:]
                if method == "GET":
                    return _ok(_json_response(await rm.get_project(pid)))
                if method == "DELETE":
                    return _ok(_json_response(await rm.delete_project(pid)))
            elif sub.startswith("/") and "/" in sub[1:]:
                pid, action = sub[1:].split("/", 1)
                if action == "auto-execute" and method == "POST":
                    return _ok(_json_response(await rm.toggle_auto_execute(pid)))
                if action == "pause" and method == "POST":
                    return _ok(_json_response(await rm.pause_project(pid)))
                if action == "resume" and method == "POST":
                    return _ok(_json_response(await rm.resume_project(pid)))

        # ── tasks 域（/ext/channel_api/tasks...）──
        if path.startswith("/ext/channel_api/tasks"):
            sub = path[len("/ext/channel_api/tasks"):]
            # 顶层路由
            if sub in ("", "/") and method == "GET":
                skip = _qint("skip", None) if "skip" in query else None
                return _ok(_json_response(_pydantic_to_dict(await rt.list_tasks(
                    status=_qopt("status"), priority=_qint("priority", None),
                    session_id=_qopt("session_id"), limit=_qint("limit", 20),
                    offset=_qint("offset", 0), skip=skip,
                ))))
            if sub in ("", "/") and method == "POST":
                body = rt.TaskCreate(**_decode_body(raw_body))
                return _ok(_json_response(_pydantic_to_dict(await rt.create_task(body))))
            if sub == "/debug/all" and method == "GET":
                return _ok(_json_response(await rt.get_tasks_debug()))
            if sub == "/root" and method == "POST":
                body = _decode_body(raw_body)
                return _ok(_json_response(await rt.create_root_task(body)))
            if sub == "/containers" and method == "GET":
                return _ok(_json_response(await rt.list_container_tasks(
                    session_id=_qopt("session_id"),
                )))
            # /{task_id} 系列
            if sub.startswith("/") and len(sub) > 1:
                rest = sub[1:]
                parts = rest.split("/")
                tid = parts[0]
                # 单级 /{task_id}
                if len(parts) == 1:
                    if method == "GET":
                        return _ok(_json_response(_pydantic_to_dict(rt.get_task(tid))))
                    if method == "PATCH":  # 前端 UPDATE 用 PATCH
                        body = rt.TaskUpdate(**_decode_body(raw_body))
                        return _ok(_json_response(_pydantic_to_dict(rt.update_task(tid, body))))
                    if method == "DELETE":
                        return _ok(_json_response(await rt.delete_task(tid)))
                # /{task_id}/submit|evaluate|pause|resume|cancel
                if len(parts) == 2:
                    action = parts[1]
                    if action == "submit" and method == "POST":
                        return _ok(_json_response(_pydantic_to_dict(await rt.submit_task(tid))))
                    if action == "evaluate" and method == "POST":
                        return _ok(_json_response(await rt.evaluate_task(tid)))
                    if action == "pause" and method == "POST":
                        return _ok(_json_response(await rt.pause_task(tid)))
                    if action == "resume" and method == "POST":
                        return _ok(_json_response(await rt.resume_task(tid)))
                    if action == "cancel" and method == "POST":
                        return _ok(_json_response(await rt.cancel_task(tid)))
                # task_phase：/{task_id}/phase... 与 /{task_id}/ac...
                if len(parts) >= 2 and parts[1] == "phase":
                    return _ok(_json_response(await _dispatch_task_phase(rm, tid, parts[2:], method, raw_body)))
                if len(parts) >= 2 and parts[1] == "ac":
                    return _ok(_json_response(await _dispatch_task_ac(rm, tid, parts[2:], method)))

        logger.warning("tasks http.handle: no route for path=%s method=%s", path, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except HTTPException as exc:
        return _http_exc_response(exc)
    except Exception as exc:  # noqa: BLE001
        if hasattr(exc, "status_code") or exc.__class__.__name__ == "APIError":
            return _http_exc_response(exc)
        logger.error("tasks http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


async def _dispatch_task_phase(rm, task_id: str, parts: list[str], method: str, raw_body: str) -> Any:
    """分发 /tasks/{id}/phase/** 子路由（routes_missing.task_phase_router）。"""
    from fastapi import HTTPException  # noqa: PLC0415
    # GET /phase
    if not parts and method == "GET":
        return await rm.get_task_phase(task_id)
    # POST /phase/prepare/complete
    if len(parts) == 2 and parts[0] == "prepare" and parts[1] == "complete" and method == "POST":
        return await rm.complete_prepare_phase(task_id)
    # POST /phase/execute/complete
    if len(parts) == 2 and parts[0] == "execute" and parts[1] == "complete" and method == "POST":
        return await rm.complete_execute_phase(task_id)
    # GET /phase/{phase}/output
    if len(parts) == 2 and parts[1] == "output" and method == "GET":
        return await rm.get_phase_output(task_id, parts[0])
    raise HTTPException(status_code=404, detail=f"task phase route not found: /{'/'.join(parts)}")


async def _dispatch_task_ac(rm, task_id: str, parts: list[str], method: str) -> Any:
    """分发 /tasks/{id}/ac/** 子路由（routes_missing.task_phase_router AC 部分）。"""
    from fastapi import HTTPException  # noqa: PLC0415
    # GET /ac
    if not parts and method == "GET":
        return await rm.get_task_ac(task_id)
    # POST /ac/evaluate-all
    if len(parts) == 1 and parts[0] == "evaluate-all" and method == "POST":
        return await rm.evaluate_all_ac(task_id)
    # POST /ac/{ac_id}/evaluate
    if len(parts) == 2 and parts[1] == "evaluate" and method == "POST":
        return await rm.evaluate_ac(task_id, parts[0])
    # GET /ac/{ac_id}/result
    if len(parts) == 2 and parts[1] == "result" and method == "GET":
        return await rm.get_ac_result(task_id, parts[0])
    raise HTTPException(status_code=404, detail=f"task ac route not found: /{'/'.join(parts)}")


async def _handle_triggers_domain(
    path: str, method: str, raw_body: str, query: dict[str, str]
) -> dict[str, Any]:
    """triggers 域分发：/ext/channel_api/triggers/** → routes_missing.triggers_router 业务。

    9 路由 stub（list/stats/get/create/update/delete/enable/disable/trigger）。
    复用 4c 模式。批次5 重新分类：原"待启用决策"前提对 triggers 不成立（前端有消费）。
    """
    import routes_missing as rm  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415

    prefix = "/ext/channel_api/triggers"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not a triggers path", "path": path}, 404))
    sub = path[len(prefix):]  # "" / "/stats" / "/{id}" / "/{id}/enable" ...

    try:
        if sub in ("", "/") and method == "GET":
            return _ok(_json_response(await rm.list_triggers()))
        if sub in ("", "/") and method == "POST":
            body = _decode_body(raw_body)
            return _ok(_json_response(await rm.create_trigger(body)))
        if sub == "/stats" and method == "GET":
            return _ok(_json_response(await rm.get_trigger_stats()))
        # /{trigger_id} 系列
        if sub.startswith("/") and len(sub) > 1:
            rest = sub[1:]
            if "/" not in rest:
                tid = rest
                if method == "GET":
                    return _ok(_json_response(await rm.get_trigger(tid)))
                if method == "PUT":
                    body = _decode_body(raw_body)
                    return _ok(_json_response(await rm.update_trigger(tid, body)))
                if method == "DELETE":
                    return _ok(_json_response(await rm.delete_trigger(tid)))
            else:
                tid, action = rest.split("/", 1)
                if action == "enable" and method == "POST":
                    return _ok(_json_response(await rm.enable_trigger(tid)))
                if action == "disable" and method == "POST":
                    return _ok(_json_response(await rm.disable_trigger(tid)))
                if action == "trigger" and method == "POST":
                    return _ok(_json_response(await rm.manual_trigger(tid)))

        logger.warning("triggers http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except HTTPException as exc:
        return _http_exc_response(exc)
    except Exception as exc:  # noqa: BLE001
        if hasattr(exc, "status_code") or exc.__class__.__name__ == "APIError":
            return _http_exc_response(exc)
        logger.error("triggers http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


async def _handle_interaction_domain(
    path: str, method: str, raw_body: str, query: dict[str, str]
) -> dict[str, Any]:
    """interaction 域分发：/ext/channel_api/interaction/** → routes_missing.interaction_router。

    7 路由：response(POST)/pending(GET)/{id}(GET)/{id}/approve|deny|cancel|viewed(POST)。
    经 get_human_interaction_service。前端 INTERACTION 块 7 端点完全对应。
    """
    import routes_missing as rm  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415

    prefix = "/ext/channel_api/interaction"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not an interaction path", "path": path}, 404))
    sub = path[len(prefix):]  # "/response" / "/pending" / "/{id}" / "/{id}/approve" ...

    try:
        if sub == "/response" and method == "POST":
            body = _decode_body(raw_body)
            return _ok(_json_response(await rm.submit_interaction_response(body)))
        if sub == "/pending" and method == "GET":
            return _ok(_json_response(await rm.get_pending_interactions()))
        # /{request_id} 系列
        if sub.startswith("/") and len(sub) > 1:
            rest = sub[1:]
            if "/" not in rest:
                rid = rest
                if method == "GET":
                    return _ok(_json_response(await rm.get_interaction(rid)))
            else:
                rid, action = rest.split("/", 1)
                if action == "approve" and method == "POST":
                    body = _decode_body(raw_body)
                    return _ok(_json_response(await rm.approve_interaction(rid, body)))
                if action == "deny" and method == "POST":
                    body = _decode_body(raw_body)
                    return _ok(_json_response(await rm.deny_interaction(rid, body)))
                if action == "cancel" and method == "POST":
                    body = _decode_body(raw_body)
                    return _ok(_json_response(await rm.cancel_interaction(rid, body)))
                if action == "viewed" and method == "POST":
                    return _ok(_json_response(await rm.mark_viewed(rid)))

        logger.warning("interaction http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except HTTPException as exc:
        return _http_exc_response(exc)
    except Exception as exc:  # noqa: BLE001
        if hasattr(exc, "status_code") or exc.__class__.__name__ == "APIError":
            return _http_exc_response(exc)
        logger.error("interaction http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


async def _handle_floating_chat_domain(
    path: str, method: str, raw_body: str, query: dict[str, str]
) -> dict[str, Any]:
    """floating-chat 域分发：/ext/channel_api/floating-chat/** → routes_missing.floating_chat_router。

    2 路由 stub：status(GET)/launch(POST)。前端 FLOATING_CHAT 块 2 端点完全对应。
    """
    import routes_missing as rm  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415

    if path == "/ext/channel_api/floating-chat/status" and method == "GET":
        try:
            return _ok(_json_response(await rm.get_floating_chat_status()))
        except HTTPException as exc:
            return _http_exc_response(exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("floating-chat http.handle 未预期错误: %s", exc, exc_info=True)
            return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))
    if path == "/ext/channel_api/floating-chat/launch" and method == "POST":
        try:
            return _ok(_json_response(await rm.launch_floating_chat()))
        except HTTPException as exc:
            return _http_exc_response(exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("floating-chat http.handle 未预期错误: %s", exc, exc_info=True)
            return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))

    return _ok(_json_response({"error": "not found", "path": path}, 404))


async def _handle_review_media_upload(raw_body: str, headers: dict[str, str]) -> dict[str, Any]:
    """处理 reviews /media-review（multipart：file + media_type）。

    复用 routes_reviews.media_review 的逻辑：解 multipart 取 file + media_type →
    保存临时文件 → get_media_review_service().review_media → 清理临时文件。
    """
    import base64 as _b64  # noqa: PLC0415
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import routes_reviews as rrv  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415

    try:
        body_bytes = _b64.b64decode(raw_body) if raw_body else b""
    except Exception as exc:  # noqa: BLE001
        return _ok(_json_response({"error": f"invalid upload body: {exc}"}, 400))

    content_type = headers.get("content-type", "") or headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        return _ok(_json_response(
            {"error": "media-review requires multipart/form-data", "content_type": content_type}, 400,
        ))
    try:
        fields = _parse_multipart(content_type, body_bytes)
    except Exception as exc:  # noqa: BLE001
        return _ok(_json_response({"error": f"multipart parse failed: {exc}"}, 400))

    file_field = fields.get("file")
    if not isinstance(file_field, dict) or not file_field.get("data"):
        return _ok(_json_response({"error": "missing or empty 'file' field"}, 400))
    media_type = fields.get("media_type") or ""
    if isinstance(media_type, str):
        media_type = media_type.strip()

    filename = file_field.get("filename") or "upload"
    content: bytes = file_field["data"]
    suffix = os.path.splitext(filename)[1]
    tmp_dir = tempfile.mkdtemp(prefix="media_review_")
    tmp_path = os.path.join(tmp_dir, filename if filename else f"upload{suffix}")
    try:
        with open(tmp_path, "wb") as f:
            f.write(content)

        # 复用 routes_reviews 的 media_review 逻辑（推断 media_type + review_media）
        effective = media_type
        if not effective:
            from review.media_review_service import _infer_media_type  # noqa: PLC0415

            try:
                effective = _infer_media_type(tmp_path)
            except ValueError:
                return _ok(_json_response({
                    "error": {"code": "INVALID", "message": f"无法推断媒体类型，请显式指定 media_type（文件: {filename}）"}
                }, 400))

        media_svc = rrv.get_media_review_service()
        result = await media_svc.review_media(tmp_path, effective)
        result_dict = result.to_dict()
        result_dict["media_type"] = effective
        result_dict["filename"] = filename
        return _ok(_json_response(result_dict))
    except HTTPException as exc:
        return _http_exc_response(exc)
    except Exception as exc:  # noqa: BLE001
        logger.error("review media-upload 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


def _parse_multipart(content_type: str, body_bytes: bytes) -> dict[str, Any]:
    """解析 multipart/form-data（内核透传的 raw_body base64 解码后的字节）。

    返回 {字段名: 值}；文件字段值为 {filename, content_type, data(bytes)}，
    普通字段为 str。用 email.parser 解析（标准库，无需外部依赖）。
    """
    import email  # noqa: PLC0415
    from email.policy import default as default_policy  # noqa: PLC0415

    # 构造一个完整 multipart 消息让 email 解析
    header = f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
    msg = email.message_from_bytes(header + body_bytes, policy=default_policy)
    fields: dict[str, Any] = {}
    if not msg.is_multipart():
        return fields
    for part in msg.get_payload():
        name = part.get_param("name", header="content-disposition")
        if name is None:
            continue
        filename = part.get_filename()
        if filename is not None:
            # 文件字段
            data = part.get_payload(decode=True) or b""
            fields[name] = {
                "filename": filename,
                "content_type": part.get_content_type(),
                "data": data,
            }
        else:
            # 普通字段
            payload = part.get_payload(decode=True)
            fields[name] = payload.decode("utf-8", errors="replace") if payload else ""
    return fields


async def _handle_artifacts_domain(
    path: str, method: str, raw_body: str, query: dict[str, str], headers: dict[str, str]
) -> dict[str, Any]:
    """artifacts + annotations 域分发：/ext/channel_api/{artifacts,annotations}/** → routes_artifacts。

    非 upload 路由直接调业务函数；upload 路由从 raw_body(base64 字节)解析 multipart
    （内核 dispatcher 已透传原始字节，不反序列化），手动落盘 + 存元数据
    （对齐 routes_artifacts.upload_file 的落盘逻辑；_push_upload_event 在 0.2 已是 no-op）。
    """
    import routes_artifacts as ra  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415

    def _qint(key: str, default: int) -> int:
        try:
            return int(query[key]) if key in query else default
        except (TypeError, ValueError):
            return default

    try:
        # ── annotations 独立路由（/ext/channel_api/annotations/{id}...）──
        if path.startswith("/ext/channel_api/annotations/"):
            ann_id = path[len("/ext/channel_api/annotations/"):]
            if "/" in ann_id:
                # /annotations/{id}/resolve
                aid, rest = ann_id.split("/", 1)
                if rest == "resolve" and method == "POST":
                    return _ok(_json_response(await ra.resolve_annotation(aid)))
            else:
                if method == "PUT":
                    body = _decode_body(raw_body)
                    return _ok(_json_response(await ra.update_annotation(ann_id, body)))
                if method == "DELETE":
                    return _ok(_json_response(await ra.delete_annotation(ann_id)))

        # ── artifacts 路由（/ext/channel_api/artifacts...）──
        if path == "/ext/channel_api/artifacts/upload" and method == "POST":
            return await _handle_artifact_upload(raw_body, headers)

        # GET "" (list, query: task_id/limit/offset)
        if path == "/ext/channel_api/artifacts" and method == "GET":
            return _ok(_json_response(await ra.list_artifacts(
                task_id=query.get("task_id", ""),
                limit=_qint("limit", 50),
                offset=_qint("offset", 0),
            )))
        # POST "" (create)
        if path == "/ext/channel_api/artifacts" and method == "POST":
            body = _decode_body(raw_body)
            return _ok(_json_response(await ra.create_artifact(body)))

        # 子路径 /artifacts/{artifact_id}[/versions|/diff|/annotations...]
        if path.startswith("/ext/channel_api/artifacts/"):
            rest = path[len("/ext/channel_api/artifacts/"):]
            # 含二级路径
            if "/" in rest:
                art_id, sub_path = rest.split("/", 1)
                if sub_path == "versions" and method == "GET":
                    return _ok(_json_response(await ra.get_version_history(art_id)))
                if sub_path == "diff" and method == "GET":
                    return _ok(_json_response(await ra.get_version_diff(
                        art_id, _qint("from", 1), _qint("to", 2),
                    )))
                if sub_path == "annotations" and method == "GET":
                    return _ok(_json_response(await ra.list_annotations(
                        art_id, status=query.get("status"), limit=_qint("limit", 100),
                    )))
                if sub_path == "annotations" and method == "POST":
                    body = _decode_body(raw_body)
                    return _ok(_json_response(await ra.create_annotation(art_id, body)))
            else:
                # /artifacts/{artifact_id} 单级
                if method == "GET":
                    return _ok(_json_response(await ra.get_artifact(rest)))
                if method == "PUT":
                    body = _decode_body(raw_body)
                    return _ok(_json_response(await ra.update_artifact(rest, body)))
                if method == "DELETE":
                    return _ok(_json_response(await ra.delete_artifact(rest)))

        logger.warning("artifacts http.handle: no route for path=%s method=%s", path, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except HTTPException as exc:
        return _http_exc_response(exc)
    except Exception as exc:  # noqa: BLE001
        if hasattr(exc, "status_code") or exc.__class__.__name__ == "APIError":
            return _http_exc_response(exc)
        logger.error("artifacts http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


async def _handle_artifact_upload(raw_body: str, headers: dict[str, str]) -> dict[str, Any]:
    """处理 /artifacts/upload（multipart/form-data）。

    内核 dispatcher 透传原始字节（base64 编码在 raw_body）。解 multipart 取 file + thread_id，
    复用 routes_artifacts 的落盘逻辑（_get_uploads_dir + DiskFileStorage）。
    对齐 upload_file 返回结构 {file_id, filename, mime_type, media_type, size, url}。
    """
    import base64 as _b64  # noqa: PLC0415
    import os  # noqa: PLC0415
    import uuid  # noqa: PLC0415
    import routes_artifacts as ra  # noqa: PLC0415
    from fastapi import HTTPException  # noqa: PLC0415

    try:
        body_bytes = _b64.b64decode(raw_body) if raw_body else b""
    except Exception as exc:  # noqa: BLE001
        return _ok(_json_response({"error": f"invalid upload body: {exc}"}, 400))

    content_type = headers.get("content-type", "") or headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        return _ok(_json_response(
            {"error": "upload requires multipart/form-data", "content_type": content_type}, 400,
        ))

    try:
        fields = _parse_multipart(content_type, body_bytes)
    except Exception as exc:  # noqa: BLE001
        return _ok(_json_response({"error": f"multipart parse failed: {exc}"}, 400))

    file_field = fields.get("file")
    if not isinstance(file_field, dict):
        return _ok(_json_response({"error": "missing 'file' field in multipart"}, 400))

    thread_id = fields.get("thread_id", "") or ""
    content = file_field["data"]
    filename = file_field.get("filename") or "upload"
    mime_type = file_field.get("content_type") or "application/octet-stream"

    # 复用 routes_artifacts 的落盘 + 元数据逻辑
    file_id = uuid.uuid4().hex[:12]
    media_type = ra._infer_media_type(mime_type)
    uploads_dir = ra._get_uploads_dir()
    os.makedirs(uploads_dir, exist_ok=True)
    ext = os.path.splitext(filename)[1]
    saved_filename = f"{file_id}{ext}"
    file_path = os.path.join(uploads_dir, saved_filename)
    with open(file_path, "wb") as f:
        f.write(content)
    url = f"/uploads/{saved_filename}"

    # 存元数据到 DiskFileStorage
    from multimodal import AttachmentInfo, MediaType  # noqa: PLC0415
    attachment = AttachmentInfo(
        file_id=file_id,
        filename=filename,
        mime_type=mime_type,
        size=len(content),
        media_type=MediaType(media_type),
        url=url,
    )
    storage = ra.get_file_storage()
    await storage.save(file_id, attachment)

    logger.info("[upload] multipart 上传成功 file_id=%s filename=%s size=%d", file_id, filename, len(content))
    return _ok(_json_response({
        "file_id": file_id,
        "filename": filename,
        "mime_type": mime_type,
        "media_type": media_type,
        "size": len(content),
        "url": url,
    }))


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

    # ── execution 域 ──
    if path.startswith("/ext/channel_api/execution"):
        return await _handle_execution_domain(path, method, raw_body, q)

    # ── modules/ui 域 ──
    if path.startswith("/ext/channel_api/modules/ui"):
        return _handle_modules_ui_domain(path, method, raw_body, q)

    # ── memory 域 ──
    if path.startswith("/ext/channel_api/memory"):
        return _handle_memory_domain(path, method, raw_body, q)

    # ── scenes 域（批次3 首迁，复用 4c 模式）──
    if path.startswith("/ext/channel_api/scenes"):
        return _handle_scenes_domain(path, method, raw_body, q)

    # ── asr 域（批次3，单 multipart 路由，复用 multipart 解析）──
    if path.startswith("/ext/channel_api/audio"):
        return await _handle_asr_domain(path, method, raw_body, headers or {})

    # ── workspaces 域（批次3，11 路由，FS/IDE 操作）──
    if path.startswith("/ext/channel_api/workspaces"):
        return await _handle_workspaces_domain(path, method, raw_body, q)

    # ── reviews 域（批次3，9 路由，含 media-review multipart）──
    if path.startswith("/ext/channel_api/reviews"):
        return await _handle_reviews_domain(path, method, raw_body, q, headers or {})

    # ── tasks 域（批次3，最大域：tasks + task_phase + projects）──
    if path.startswith("/ext/channel_api/tasks") or path.startswith("/ext/channel_api/projects"):
        return await _handle_tasks_domain(path, method, raw_body, q)

    # ── triggers 域（批次5，有前端消费，9 路由 stub）──
    if path.startswith("/ext/channel_api/triggers"):
        return await _handle_triggers_domain(path, method, raw_body, q)

    # ── interaction 域（批次5，有前端消费，7 路由）──
    if path.startswith("/ext/channel_api/interaction"):
        return await _handle_interaction_domain(path, method, raw_body, q)

    # ── floating-chat 域（批次5，有前端消费，2 路由 stub）──
    if path.startswith("/ext/channel_api/floating-chat"):
        return await _handle_floating_chat_domain(path, method, raw_body, q)

    # ── artifacts 域（含上传）+ annotations 域 ──
    if path.startswith("/ext/channel_api/artifacts") or path.startswith("/ext/channel_api/annotations"):
        return await _handle_artifacts_domain(path, method, raw_body, q, headers or {})

    # 未接入的域：明确 404，提示该域尚未迁移
    logger.warning("http.handle: path=%s not yet migrated (domain pending)", path)
    return _ok(_json_response(
        {"error": "not found", "path": path, "hint": "该域尚未完成 4c 迁移（见 channel_api_migration_plan.md）"},
        404,
    ))


if __name__ == "__main__":
    plugin.run()
