"""replay_link MCP 服务端——http.handle REST 面（/ext/replay_link/**）。

handler_capability 注册形态与 mode_personal 同构（skills/plugin-dev/SKILL.md
http_endpoints[] 七字段契约）：plugin.json 声明路由 → 本文件注册 http.handle
工具 → 按 path 分发到 ReplayStore。响应信封/入参解码统一走 plugins/shared
http_json 唯一样板（内核 HttpHandleResponse 契约：body base64）。
"""

from __future__ import annotations

import logging
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

from _root_api import decode_body, error, json_response, ok  # noqa: E402
from replay_store import ReplayStore, StoreFullError  # noqa: E402

logger = logging.getLogger("replay_link")

plugin = AgentOSPlugin("replay_link")

_store = ReplayStore()


def route(path: str, method: str, query: dict[str, str],
          body: dict[str, Any]) -> dict[str, Any]:
    """按 path/method 分发到 ReplayStore（返回 ToolExecutionResult 契约信封）。

    内部路由核（不下 LLM 工具面；错误以带 HTTP status 的 HttpHandleResponse 返回）。
    """
    if path == "/ext/replay_link/create" and method == "POST":
        try:
            record_id = _store.create(body.get("ciphertext_b64", ""),
                                      body.get("iv_b64", ""))
        except ValueError as exc:
            return ok(json_response({"error": str(exc)}, 422))
        except StoreFullError as exc:
            return ok(json_response({"error": str(exc)}, 507))
        return ok(json_response({"id": record_id}))

    if path == "/ext/replay_link/get" and method == "GET":
        record_id = query.get("id", "")
        rec = _store.get(record_id)
        if rec is None:
            return ok(json_response(
                {"error": "replay record not found or expired"}, 404))
        return ok(json_response(rec))

    return ok(json_response({"error": f"no route: {method} {path}"}, 404))


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
    description="HTTP endpoint handler for /ext/replay_link/** (回放链接密文存取 REST 面)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """http_endpoints 入口：create(POST)/get(GET) 两路由。"""
    del plugin_id, headers  # 无 per-caller 差异；auth 由 dispatcher 把关
    try:
        body = decode_body(raw_body)  # 内核 raw_body=base64 铁律；http_json 唯一解码实现
    except Exception as exc:  # noqa: BLE001 — 解码失败转 400 协议错误信封，不静默
        logger.warning("replay_link invalid body: %s", exc)
        return error(f"invalid body: {exc}", 400)
    try:
        return route(path, method, query or {}, body)
    except Exception as exc:  # noqa: BLE001 — 协议兜底：错误经 error 信封可见，不静默
        logger.exception("replay_link http.handle failed: %s", exc)
        return error(f"replay_link service error: {exc}", 500)
